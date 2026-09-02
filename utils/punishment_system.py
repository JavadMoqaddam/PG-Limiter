"""
Smart Punishment System for Limiter
This module provides an escalating punishment system based on violation history.

The system tracks violations within a configurable time window and applies
progressively harsher punishments based on the violation count.

Example configuration:
{
    "punishment": {
        "enabled": true,
        "window_hours": 72,  // 3 days
        "steps": [
            {"type": "warning", "duration": 0},
            {"type": "disable", "duration": 15},  // 15 minutes
            {"type": "disable", "duration": 60},  // 1 hour
            {"type": "disable", "duration": 240}, // 4 hours
            {"type": "disable", "duration": 0}    // unlimited (0 = permanent until manual)
        ]
    }
}
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from utils.logs import get_logger
from utils.atomic_io import atomic_write_json

punishment_logger = get_logger("punishment")


# Used when a configured disable step has a duration that cannot be read. It is
# deliberately not 0: on a disable step 0 means "unlimited, manual enable only", so a
# malformed or missing value must never be able to produce the harshest punishment in
# the list. 10 minutes matches the shortest step in DEFAULT_STEPS.
FALLBACK_DISABLE_MINUTES = 10

_VALID_STEP_TYPES = ("warning", "disable", "revoke")


def _coerce_duration_minutes(raw, step_type: str) -> int:
    """
    Turn a configured duration into a non-negative whole number of minutes.

    The steps list arrives as untyped JSON (read_config json.loads() the
    ``punishment_steps`` row), so this value can be a string, a float, None, or
    absent. Nothing downstream defends itself: get_duration_seconds() multiplies it by
    60, is_unlimited_disable() compares it to 0 with ``==``, and get_display_text()
    compares it to 60 with ``<``. A string therefore reached the panel as
    ``"30" * 60`` - a 120-character string - and only failed later, after the user had
    already been disabled but before anything recorded it, leaving a ban nothing would
    ever lift. None raised before the disable instead, so the violation went
    unpunished.

    0 is a meaningful value, so anything unusable must not collapse to it. On a disable
    step an unusable value becomes FALLBACK_DISABLE_MINUTES; on a warning or revoke
    step it becomes 0, where the duration is ignored anyway.
    """
    unusable = FALLBACK_DISABLE_MINUTES if step_type == "disable" else 0

    if isinstance(raw, bool):
        # bool is a subclass of int; True would silently become 1 minute.
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = int(raw) if raw.is_integer() else None
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            value = None
    else:
        value = None

    if value is None or value < 0:
        punishment_logger.error(
            f"⚠️ Punishment step duration {raw!r} is not a whole number of minutes >= 0 "
            f"(step type {step_type!r}) - using {unusable} instead. On a disable step "
            f'"duration": 0 is the only way to ask for an unlimited ban.'
        )
        return unusable
    return value


def _coerce_window_hours(raw, default: int) -> int:
    """
    Turn a configured violation window into a positive whole number of hours.

    cleanup_old_violations() computes ``window_hours * 60 * 60`` and subtracts it from
    a timestamp, so a string here multiplied into a huge string and then raised
    TypeError from inside the violation count, on the ban path.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        punishment_logger.error(
            f"⚠️ Punishment window {raw!r} is not a positive number of hours - "
            f"using the default of {default}h"
        )
        return default
    return value


@dataclass
class PunishmentStep:
    """Represents a single punishment step"""
    step_type: str  # "warning", "disable", or "revoke"
    duration_minutes: int  # 0 = unlimited/permanent for disable, ignored for warning/revoke

    def __post_init__(self):
        """
        Normalise both fields so no later reader has to defend itself.

        This is the invariant the rest of the class relies on: after construction,
        ``duration_minutes`` is always a non-negative int, whatever was passed in.
        """
        self.step_type = str(self.step_type or "").strip().lower()
        self.duration_minutes = _coerce_duration_minutes(self.duration_minutes, self.step_type)
    
    def is_warning(self) -> bool:
        """Check if this step is a warning only"""
        return self.step_type == "warning"
    
    def is_unlimited_disable(self) -> bool:
        """Check if this step is an unlimited/permanent disable"""
        return self.step_type == "disable" and self.duration_minutes == 0
    
    def is_revoke(self) -> bool:
        """Check if this step revokes the subscription (changes UUID)"""
        return self.step_type == "revoke"
    
    def get_duration_seconds(self) -> int:
        """Get duration in seconds"""
        return self.duration_minutes * 60
    
    def get_display_text(self) -> str:
        """Get human-readable text for this step"""
        if self.is_warning():
            return "⚠️ Warning only"
        if self.is_revoke():
            return "🔄 Revoke subscription + Disable"
        if self.is_unlimited_disable():
            return "🚫 Unlimited disable"
        # Format duration nicely
        minutes = self.duration_minutes
        if minutes < 60:
            return f"🔒 {minutes} minute{'s' if minutes != 1 else ''} disable"
        hours = minutes // 60
        remaining_mins = minutes % 60
        if remaining_mins == 0:
            return f"🔒 {hours} hour{'s' if hours != 1 else ''} disable"
        return f"🔒 {hours}h {remaining_mins}m disable"


def _parse_step(position: int, step) -> Optional[PunishmentStep]:
    """
    Build one PunishmentStep from its configured dict, or None if it is unusable.

    ``position`` is 1-based and only used in the log lines, so an operator can tell
    which entry of their steps list is wrong.
    """
    if not isinstance(step, dict):
        punishment_logger.error(
            f"⚠️ Punishment step {position} is {type(step).__name__}, not an object - "
            f"skipping it"
        )
        return None

    step_type = str(step.get("type") or "disable").strip().lower()
    if step_type not in _VALID_STEP_TYPES:
        punishment_logger.error(
            f"⚠️ Punishment step {position} has unknown type {step.get('type')!r} - "
            f"treating it as a warning, so a step nobody can read cannot disable anyone"
        )
        step_type = "warning"

    if "duration" in step:
        return PunishmentStep(step_type, _coerce_duration_minutes(step["duration"], step_type))

    if step_type == "disable":
        # The old code read this with .get("duration", 0), and 0 on a disable step
        # means unlimited - so a step that simply forgot the key was a permanent,
        # manual-enable-only ban, logged nowhere.
        punishment_logger.error(
            f"⚠️ Punishment step {position} is a disable with no duration key - using "
            f"{FALLBACK_DISABLE_MINUTES} minutes. An unlimited ban has to say so "
            f'explicitly with "duration": 0.'
        )
        return PunishmentStep(step_type, FALLBACK_DISABLE_MINUTES)

    return PunishmentStep(step_type, 0)


@dataclass
class ViolationRecord:
    """Represents a single violation record"""
    username: str
    timestamp: float
    step_applied: int  # Which step was applied (0-indexed)
    disable_duration: int  # Duration in minutes (0 = unlimited or warning)
    enabled_at: Optional[float] = None  # When user was re-enabled (for timed disables)


class PunishmentSystem:
    """
    Smart punishment system with escalating penalties.
    
    Tracks user violations within a configurable time window and applies
    progressively harsher punishments based on violation count.
    """
    
    DEFAULT_STEPS = [
        PunishmentStep("warning", 0),
        PunishmentStep("disable", 10),
        PunishmentStep("disable", 30),
        PunishmentStep("disable", 60),
        PunishmentStep("disable", 0),  # Unlimited
    ]
    
    DEFAULT_WINDOW_HOURS = 168  # 7 days
    
    def __init__(self, filename: str = "/var/lib/pg-limiter/violation_history.json"):
        self.filename = filename
        self.violations: dict[str, list[ViolationRecord]] = {}  # username -> list of violations
        self.steps: list[PunishmentStep] = self.DEFAULT_STEPS.copy()
        self.window_hours: int = self.DEFAULT_WINDOW_HOURS
        self.enabled: bool = True
        self._write_lock = asyncio.Lock()
        self._last_cleanup: float = 0.0
        # Rate limit for the "reading the JSON copy instead of SQLite" warning: the
        # enforcement loop asks about thousands of users per cycle, so warning per
        # user would bury the log it is meant to make legible.
        self._last_fallback_warning: float = 0.0
        self.load_violations()

    def _warn_json_fallback(self, reason: str) -> None:
        """
        Say out loud that a punishment decision is about to use the JSON copy.

        This used to be a ``debug`` line, which at normal log level meant no line
        at all: escalation could silently restart at step 1 while SQLite held the
        real history. Whether that is lenient or harsh depends on how stale the
        file is, and either way the operator needs to know it happened.
        """
        now = time.time()
        if now - self._last_fallback_warning < 60:
            return
        self._last_fallback_warning = now
        punishment_logger.warning(
            f"⚠️ Punishment history is coming from {self.filename} instead of SQLite: "
            f"{reason}. That copy can be stale, so the escalation step may be wrong. "
            f"Further occurrences are suppressed for 60s."
        )
    
    def load_violations(self):
        """Load violation history from file"""
        try:
            if os.path.exists(self.filename) and os.path.getsize(self.filename) > 0:
                punishment_logger.debug(f"📂 Loading violation history from {self.filename}")
                with open(self.filename, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    
                    for username, records in data.get("violations", {}).items():
                        self.violations[username] = []
                        for record in records:
                            self.violations[username].append(ViolationRecord(
                                username=record["username"],
                                timestamp=record["timestamp"],
                                step_applied=record["step_applied"],
                                disable_duration=record["disable_duration"],
                                enabled_at=record.get("enabled_at")
                            ))
                    
                    # Clean up old violations
                    self.cleanup_old_violations()
                    
                    punishment_logger.info(f"✅ Loaded violation history for {len(self.violations)} users")
        except Exception as e:
            punishment_logger.error(f"❌ Error loading violation history: {e}")
            self.violations = {}
    
    def _sync_save_violations(self):
        """Synchronous file write for violation history data."""
        data = {"violations": {}}
        for username, records in self.violations.items():
            data["violations"][username] = []
            for record in records:
                data["violations"][username].append({
                    "username": record.username,
                    "timestamp": record.timestamp,
                    "step_applied": record.step_applied,
                    "disable_duration": record.disable_duration,
                    "enabled_at": record.enabled_at
                })
        atomic_write_json(self.filename, data)

    async def save_violations(self):
        """Save violation history to file using asyncio.Lock and atomic write for safety."""
        try:
            async with self._write_lock:
                await asyncio.to_thread(self._sync_save_violations)
            punishment_logger.debug(f"💾 Saved violation history for {len(self.violations)} users")
        except Exception as e:
            punishment_logger.error(f"❌ Error saving violation history: {e}")
    
    def load_config(self, config_data: dict):
        """
        Load punishment configuration from config data.
        
        Args:
            config_data: Configuration dictionary with optional 'punishment' key
        """
        punishment_config = config_data.get("punishment", {})
        
        self.enabled = bool(punishment_config.get("enabled", True))
        self.window_hours = _coerce_window_hours(
            punishment_config.get("window_hours", self.DEFAULT_WINDOW_HOURS),
            self.DEFAULT_WINDOW_HOURS,
        )

        # Load steps from config
        steps_config = punishment_config.get("steps", None)
        if steps_config and isinstance(steps_config, list) and len(steps_config) > 0:
            self.steps = [
                parsed
                for position, step in enumerate(steps_config, start=1)
                if (parsed := _parse_step(position, step)) is not None
            ]

            if not self.steps:
                self.steps = self.DEFAULT_STEPS.copy()
                punishment_logger.error(
                    "⚠️ No usable punishment step in the configuration - falling back "
                    "to the built-in steps"
                )
            else:
                punishment_logger.info(f"📋 Loaded {len(self.steps)} punishment steps from config (window: {self.window_hours}h)")
        else:
            self.steps = self.DEFAULT_STEPS.copy()
            punishment_logger.debug("📋 Using default punishment steps")
    
    def cleanup_old_violations(self):
        """Remove violations older than the time window"""
        current_time = time.time()
        self._last_cleanup = current_time
        window_seconds = self.window_hours * 60 * 60
        cutoff_time = current_time - window_seconds
        
        for username in list(self.violations.keys()):
            # Filter out old violations
            self.violations[username] = [
                v for v in self.violations[username]
                if v.timestamp > cutoff_time
            ]
            # Remove user if no recent violations
            if not self.violations[username]:
                del self.violations[username]

    def _ensure_cleanup(self, force: bool = False):
        """
        Ensure old violations are cleaned up periodically (at most once every 5 minutes).
        Avoids O(N) iteration over all users on every single user check.
        """
        current_time = time.time()
        if force or (current_time - self._last_cleanup > 300):
            self.cleanup_old_violations()

    async def get_violation_count_async(self, username: str) -> int:
        """
        Violation count inside the window, read from SQLite.

        SQLite is the store the escalation decision is actually made on, so this
        is the number that matters. The JSON copy is a fallback of last resort and
        is now announced when it is used - see ``_warn_json_fallback``.
        """
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud.violations import ViolationHistoryCRUD
            if DB_AVAILABLE:
                async with get_db() as db:
                    return await ViolationHistoryCRUD.get_violation_count(db, username, window_hours=self.window_hours)
            self._warn_json_fallback("db.database reports DB_AVAILABLE is False")
        except Exception as e:
            self._warn_json_fallback(f"the query failed ({e})")

        self._ensure_cleanup()
        return len(self.violations.get(username, []))
    
    def get_violation_count(self, username: str) -> int:
        """
        Get the number of violations for a user within the time window.
        
        Args:
            username: The username to check
            
        Returns:
            Number of violations in the time window
        """
        self._ensure_cleanup()
        return len(self.violations.get(username, []))
    
    def get_next_step_index(self, username: str) -> int:
        """
        Get the index of the next punishment step for a user.
        
        Args:
            username: The username to check
            
        Returns:
            Step index (0-indexed), capped at max step
        """
        violation_count = self.get_violation_count(username)
        # Cap at the last step
        return min(violation_count, len(self.steps) - 1)
    
    def get_next_punishment(self, username: str) -> PunishmentStep:
        """
        Get the next punishment step for a user.
        
        Args:
            username: The username to check
            
        Returns:
            The PunishmentStep to apply
        """
        step_index = self.get_next_step_index(username)
        return self.steps[step_index]
    
    async def record_violation(self, username: str, step_applied: int, duration_minutes: int):
        """
        Record a new violation for a user in SQLite and in-memory cache.
        
        Args:
            username: The username
            step_applied: Which step was applied (0-indexed)
            duration_minutes: Duration of disable in minutes (0 for warning or unlimited)
        """
        now = time.time()
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud.violations import ViolationHistoryCRUD
            if DB_AVAILABLE:
                async with get_db() as db:
                    await ViolationHistoryCRUD.add(
                        db=db,
                        username=username,
                        step_applied=step_applied,
                        disable_duration=duration_minutes
                    )
                    await db.commit()
        except Exception as e:
            punishment_logger.error(f"Error saving violation to DB for {username}: {e}")

        if username not in self.violations:
            self.violations[username] = []
        
        record = ViolationRecord(
            username=username,
            timestamp=now,
            step_applied=step_applied,
            disable_duration=duration_minutes
        )
        
        self.violations[username].append(record)
        await self.save_violations()
        
        punishment_logger.info(f"📝 Recorded violation #{len(self.violations[username])} for {username} (step {step_applied}, duration: {duration_minutes}min)")
    
    async def clear_user_history(self, username: str):
        """Clear all violation history for a user"""
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud.violations import ViolationHistoryCRUD
            if DB_AVAILABLE:
                async with get_db() as db:
                    await ViolationHistoryCRUD.clear_user(db, username)
                    await db.commit()
        except Exception as e:
            punishment_logger.debug(f"DB clear_user_history error for {username}: {e}")

        if username in self.violations:
            del self.violations[username]
            await self.save_violations()
            punishment_logger.info(f"🗑️ Cleared violation history for {username}")
    
    async def clear_all_history(self):
        """Clear all violation history"""
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud.violations import ViolationHistoryCRUD
            if DB_AVAILABLE:
                async with get_db() as db:
                    await ViolationHistoryCRUD.clear_all(db)
                    await db.commit()
        except Exception as e:
            punishment_logger.debug(f"DB clear_all_history error: {e}")

        self.violations = {}
        await self.save_violations()
        punishment_logger.info("🗑️ Cleared all violation history")
    
    def _build_status(self, username: str, entries: list[tuple[int, int, float]]) -> dict:
        """
        Shape the status dict from (step_applied, disable_duration, timestamp) rows.

        ``time_ago`` is part of the contract: telegram_bot/handlers/punishment.py
        reads ``v['time_ago']`` for every row, and it was never in the dict, so the
        /user_violations command raised KeyError and the admin saw
        "❌ Error: 'time_ago'" for any user who had a violation. ``_format_time_ago``
        existed the whole time and was called from nowhere.
        """
        violation_count = len(entries)
        next_step_idx = min(violation_count, len(self.steps) - 1)
        next_step = self.steps[next_step_idx]

        return {
            "username": username,
            "violation_count": violation_count,
            "window_hours": self.window_hours,
            "next_step_index": next_step_idx,
            "next_punishment": next_step.get_display_text(),
            "is_warning_next": next_step.is_warning(),
            "is_unlimited_next": next_step.is_unlimited_disable(),
            "recent_violations": [
                {
                    "step": step_applied,
                    "duration": disable_duration,
                    "timestamp": timestamp,
                    "time_ago": self._format_time_ago(timestamp),
                }
                for step_applied, disable_duration, timestamp in entries
            ],
        }

    async def get_user_status_async(self, username: str) -> dict:
        """
        Status for a user, read from SQLite - the same store the punishment uses.

        The sync ``get_user_status`` reads the JSON copy, so the "Next Punishment"
        it reported could differ from the one actually applied a moment later by
        ``get_punishment_for_user``, which goes to SQLite. Anything shown to an
        admin should come through here.
        """
        try:
            from db.database import get_db, DB_AVAILABLE
            from db.crud.violations import ViolationHistoryCRUD
            if DB_AVAILABLE:
                async with get_db() as db:
                    rows = await ViolationHistoryCRUD.get_user_violations(
                        db, username, window_hours=self.window_hours
                    )
                return self._build_status(
                    username,
                    [(row.step_applied, row.disable_duration, row.timestamp) for row in rows],
                )
            self._warn_json_fallback("db.database reports DB_AVAILABLE is False")
        except Exception as e:
            self._warn_json_fallback(f"the query failed ({e})")

        return self.get_user_status(username)

    def get_user_status(self, username: str) -> dict:
        """
        Status for a user from the JSON copy.

        Kept for callers that cannot await, and it is the fallback for
        ``get_user_status_async``. Prefer the async one: this number can disagree
        with the punishment that will really be applied.
        """
        self.cleanup_old_violations()

        violations = self.violations.get(username, [])
        return self._build_status(
            username,
            [(v.step_applied, v.disable_duration, v.timestamp) for v in violations],
        )
    
    def _format_time_ago(self, timestamp: float) -> str:
        """Format timestamp as 'X ago' string"""
        diff = time.time() - timestamp
        if diff < 60:
            return f"{int(diff)}s ago"
        if diff < 3600:
            return f"{int(diff / 60)}m ago"
        if diff < 86400:
            return f"{int(diff / 3600)}h ago"
        return f"{int(diff / 86400)}d ago"
    
    def get_steps_summary(self) -> str:
        """Get a formatted summary of all punishment steps"""
        lines = [f"📋 <b>Punishment Steps</b> (window: {self.window_hours}h):\n"]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  {i}. {step.get_display_text()}")
        return "\n".join(lines)


# Global instance
_punishment_system: Optional[PunishmentSystem] = None


def get_punishment_system() -> PunishmentSystem:
    """Get or create the global punishment system instance"""
    global _punishment_system
    if _punishment_system is None:
        _punishment_system = PunishmentSystem()
    return _punishment_system


# Global instance alias to be imported by other modules (uniform with warning_system)
punishment_system = get_punishment_system()


async def get_punishment_for_user(username: str, config_data: dict) -> tuple[PunishmentStep, int, int]:
    """
    Get the punishment to apply for a user.
    
    Args:
        username: The username
        config_data: Configuration data with punishment settings
        
    Returns:
        Tuple of (PunishmentStep, step_index, violation_count)
    """
    system = get_punishment_system()
    system.load_config(config_data)
    
    if not system.enabled:
        # Punishment system disabled - use unlimited disable as default
        return PunishmentStep("disable", 0), 0, 0
    
    violation_count = await system.get_violation_count_async(username)
    step_index = min(violation_count, len(system.steps) - 1)
    punishment = system.steps[step_index]
    
    return punishment, step_index, violation_count


async def record_user_violation(username: str, step_index: int, duration_minutes: int):
    """Record a violation for a user"""
    system = get_punishment_system()
    await system.record_violation(username, step_index, duration_minutes)
