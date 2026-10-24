"""
Enhanced Warning System for Limiter
This module provides a warning system with monitoring periods for users who exceed limits
"""

import asyncio
import html
import json
import os
import time
import ipaddress
from collections.abc import Awaitable, Mapping
from typing import Callable, Dict, Optional, Set
from datetime import datetime

from utils.logs import logger, log_monitoring_event, get_logger
from utils.types import PanelType, UserType
from utils.warning_system.user_warning import UserLimitWarning
from utils.warning_system.helpers import (
    safe_send_logs,
    safe_send_warning_log,
    safe_send_disable_notification,
    safe_disable_user_with_punishment,
    get_disable_ip_activity,
    capture_monitoring_guard,
)
from utils.atomic_io import atomic_write_json
from utils.user_policy import UserPolicy, evaluate_user_policy
from utils.enforcement_guard import enforcement_guard

# Module logger
warning_logger = get_logger("warning_system")


class EnhancedWarningSystem:
    # Class-level constants kept for the test suite. Note that no code path returns
    # "instant_disabled", so INSTANT_DISABLE_THRESHOLD is not consulted anywhere:
    # every ban goes through the max_warnings consecutive-scan rule.
    INSTANT_DISABLE_THRESHOLD = -60
    MIN_DEVICE_DURATION = 120  # 2 minutes

    def __init__(
        self,
        filename: str = "/var/lib/pg-limiter/user_warnings.json",
        history_filename: str = "/var/lib/pg-limiter/warning_history.json",
        check_interval: int = 60,
        max_warnings: int = 3,
    ):
        self.filename = filename
        self.history_filename = history_filename
        self.warnings: Dict[str, UserLimitWarning] = {}
        self.warning_history: Dict[str, list] = {}
        self.check_interval = max(5, int(check_interval))
        self.max_warnings = max(1, int(max_warnings))
        self.monitoring_period = self.check_interval * self.max_warnings
        self._write_lock = asyncio.Lock()
        self.load_warnings()
        self.load_warning_history()
        warning_logger.debug(
            f"⚠️ EnhancedWarningSystem initialized (monitoring_period={self.monitoring_period}s, max_warnings={self.max_warnings})"
        )

    def update_settings(self, check_interval: int, max_warnings: int = 3) -> None:
        """Dynamically update monitoring settings based on runtime configuration."""
        self.check_interval = max(5, int(check_interval))
        self.max_warnings = max(1, int(max_warnings))
        self.monitoring_period = self.check_interval * self.max_warnings

    def effective_max_warnings(self, warning: UserLimitWarning) -> int:
        """
        How many consecutive violating scans this specific record needs before a ban.

        check_usage calls update_settings() on every cycle, so self.max_warnings is a
        live value. Comparing a counter that has been accumulating for two scans
        against a threshold the operator just lowered would ban on the old count under
        a new, stricter rule - a user mid-streak would be disabled without ever
        completing the number of scans they were being monitored for.

        Taking the maximum of the record's own threshold and the current one means a
        settings change can only ever make a ban harder, never easier:

          lowered  (3 -> 2): max(3, 2) = 3, the record keeps the rule it started under
          raised   (3 -> 5): max(3, 5) = 5, the stricter-for-the-limiter value wins
          new records:       stored == current, so the new setting applies in full

        A record with no stored threshold predates this field and falls back to the
        live value; those records expire within one monitoring window.
        """
        stored = getattr(warning, "max_warnings_at_creation", 0) or 0
        try:
            stored = int(stored)
        except (TypeError, ValueError):
            stored = 0
        if stored < 1:
            return self.max_warnings
        return max(stored, self.max_warnings)

    def load_warning_history(self):
        """Load warning history from file"""
        try:
            if os.path.exists(self.history_filename) and os.path.getsize(self.history_filename) > 0:
                with open(self.history_filename, "r", encoding="utf-8") as file:
                    self.warning_history = json.load(file)
                    self.cleanup_old_warning_history()
                    warning_logger.debug(f"⚠️ Loaded warning history for {len(self.warning_history)} users")
        except Exception as e:
            warning_logger.error(f"Error loading warning history: {e}")
            self.warning_history = {}

    def _sync_save_warning_history(self):
        """Synchronous file write for warning history."""
        atomic_write_json(self.history_filename, self.warning_history)

    async def save_warning_history(self):
        """Save warning history to file using asyncio.to_thread to avoid blocking."""
        try:
            async with self._write_lock:
                await asyncio.to_thread(self._sync_save_warning_history)
            warning_logger.debug(f"⚠️ Saved warning history ({len(self.warning_history)} users)")
        except Exception as e:
            warning_logger.error(f"Error saving warning history: {e}")

    def cleanup_old_warning_history(self):
        """Remove warnings older than 24 hours from history"""
        current_time = time.time()
        twenty_four_hours_ago = current_time - (24 * 60 * 60)

        for username in list(self.warning_history.keys()):
            self.warning_history[username] = [ts for ts in self.warning_history[username] if ts > twenty_four_hours_ago]
            if not self.warning_history[username]:
                del self.warning_history[username]

    def count_recent_warnings(self, username: str, hours: int = 12) -> int:
        """Count disables for user in last X hours"""
        current_time = time.time()
        cutoff_time = current_time - (hours * 60 * 60)
        warnings = self.warning_history.get(username, [])
        return len([ts for ts in warnings if ts > cutoff_time])

    async def add_to_warning_history(self, username: str):
        """Add current warning to history"""
        current_time = time.time()
        if username not in self.warning_history:
            self.warning_history[username] = []
        self.warning_history[username].append(current_time)
        await self.save_warning_history()

    async def record_disable_outcome(self, username: str, action: str) -> bool:
        """Record disable history only when the panel actually disabled or revoked.

        A warning, error, or skipped outcome must not add a disable-history entry: the
        history feeds trust scoring, so a failed punishment must leave the record intact
        rather than logging a disable that never happened.
        """
        if action in ("disabled", "revoked", "applied_unrecorded"):
            await self.add_to_warning_history(username)
            return True
        return False

    async def clear_all_trust_data(self) -> tuple[int, int]:
        """
        Clear all trust-related data for all users.
        This resets warning history and current warnings.

        Returns:
            tuple[int, int]: (warnings_cleared, history_entries_cleared)
        """
        warnings_count = len(self.warnings)
        history_count = len(self.warning_history)

        # Clear active warnings
        self.warnings.clear()
        await self.save_warnings()

        # Clear warning history
        self.warning_history.clear()
        await self.save_warning_history()

        warning_logger.info(f"🗑️ Cleared all trust data: {warnings_count} warnings, {history_count} history entries")
        return warnings_count, history_count

    async def clear_user_warning(self, username: str, *, expected_warning: UserLimitWarning | None = None) -> bool:
        """Consume a monitoring streak without erasing recent disable history."""
        if expected_warning is not None and self.warnings.get(username) is not expected_warning:
            return False
        if username not in self.warnings:
            return False
        self.warnings.pop(username)
        await self.save_warnings()
        return True

    async def clear_user_trust_data(self, username: str, *, expected_warning: UserLimitWarning | None = None) -> bool:
        """
        Clear trust data for a specific user.

        Args:
            username: The username to clear data for
            expected_warning: Automatic clears consume only this warning owner.

        Returns:
            bool: True if user had data to clear
        """
        if expected_warning is not None and self.warnings.get(username) is not expected_warning:
            return False
        # Claim both removals without an await. Persistence may yield to a new
        # warning/history owner, whose state must survive the old clear operation.
        had_warning = username in self.warnings
        had_history = username in self.warning_history
        self.warnings.pop(username, None)
        self.warning_history.pop(username, None)
        had_data = had_warning or had_history

        if had_warning:
            await self.save_warnings()
        if had_history:
            await self.save_warning_history()

        if had_data:
            warning_logger.info(f"🗑️ Cleared trust data for user: {username}")

        return had_data

    async def analyze_user_activity_patterns(self, username: str) -> dict:
        """Summarize a monitored user's IP activity for the analytics view.

        Derived from the record already on hand (monitoring_history plus the
        first/last-seen maps). An unknown user returns the same keys with empty
        values, so the caller never has to guard for a missing contract.
        """
        empty = {
            "consistently_active_ips": set(),
            "total_snapshots": 0,
            "ip_change_frequency": 0.0,
            "peak_ip_count": 0,
            "average_ip_count": 0.0,
        }
        warning = self.warnings.get(username)
        if warning is None:
            return empty

        history = list(warning.monitoring_history or [])
        counts = [int(snapshot.get("ip_count", 0)) for snapshot in history]
        total = len(history)
        peak = max(counts) if counts else int(warning.ip_count)
        average = (sum(counts) / total) if total else float(warning.ip_count)

        # Mean size of the symmetric difference between consecutive IP sets.
        changes = 0
        for previous, current in zip(history, history[1:]):
            changes += len(set(previous.get("ips", set())) ^ set(current.get("ips", set())))
        ip_change_frequency = (changes / (total - 1)) if total > 1 else 0.0

        # An IP is "consistently active" once it has been seen across >= 4 minutes.
        first_seen = warning.ip_first_seen or {}
        last_seen = warning.ip_last_seen or {}
        consistently_active_ips = {
            ip for ip in (warning.ips or set()) if last_seen.get(ip, 0.0) - first_seen.get(ip, 0.0) >= 240.0
        }

        return {
            "consistently_active_ips": consistently_active_ips,
            "total_snapshots": total,
            "ip_change_frequency": ip_change_frequency,
            "peak_ip_count": peak,
            "average_ip_count": average,
        }

    @staticmethod
    def _deserialize_warning(warning_data: dict) -> UserLimitWarning:
        """Rebuild one UserLimitWarning from its persisted dict. Raises on bad input."""
        monitoring_history = []
        for snapshot in warning_data.get("monitoring_history") or []:
            monitoring_history.append(
                {
                    "timestamp": snapshot["timestamp"],
                    "ips": set(snapshot["ips"]),
                    "ip_count": snapshot["ip_count"],
                }
            )

        ip_to_inbounds = {}
        for ip, inbounds in (warning_data.get("ip_to_inbounds") or {}).items():
            ip_to_inbounds[ip] = set(inbounds)

        warning = UserLimitWarning(
            username=warning_data["username"],
            ip_count=warning_data["ip_count"],
            ips=set(warning_data["ips"]),
            warning_time=warning_data["warning_time"],
            monitoring_end_time=warning_data["monitoring_end_time"],
            warned=warning_data.get("warned", False),
            ip_first_seen=warning_data.get("ip_first_seen", {}),
            ip_last_seen=warning_data.get("ip_last_seen", {}),
            ip_seen_count=warning_data.get("ip_seen_count", {}),
            trust_score=warning_data.get("trust_score", 0.0),
            inbound_protocols=set(warning_data.get("inbound_protocols", [])),
            ip_subnets=set(warning_data.get("ip_subnets", [])),
            previous_warnings_12h=warning_data.get("previous_warnings_12h", 0),
            previous_warnings_24h=warning_data.get("previous_warnings_24h", 0),
            ip_to_inbounds=ip_to_inbounds,
            same_ip_multiple_inbounds=warning_data.get("same_ip_multiple_inbounds", False),
            connection_details=warning_data.get("connection_details", []),
            user_limit=warning_data.get("user_limit", 1),
            consecutive_violations=warning_data.get("consecutive_violations", 1),
            last_scan_time=warning_data.get("last_scan_time", 0.0),
            max_warnings_at_creation=warning_data.get("max_warnings_at_creation", 0),
        )
        warning.monitoring_history = monitoring_history
        if "isp_names" in warning_data or "isp_change_pattern" in warning_data:
            warning.trust_score = warning.calculate_trust_score()
        return warning

    def load_warnings(self):
        """Load warnings from file"""
        try:
            if os.path.exists(self.filename) and os.path.getsize(self.filename) > 0:
                with open(self.filename, "r", encoding="utf-8") as file:
                    data = json.load(file)
                    now = time.time()
                    expired = 0
                    damaged = 0
                    for username, warning_data in data.items():
                        # Per-record, not per-file: one record with a missing key used
                        # to abort the loop, silently discarding every record after it
                        # and leaving the system half-loaded.
                        try:
                            warning = self._deserialize_warning(warning_data)
                        except Exception as record_error:  # pylint: disable=broad-except
                            damaged += 1
                            warning_logger.exception(
                                f"Skipping unreadable warning record for {username}: {record_error}"
                            )
                            continue
                        if warning.monitoring_end_time <= now:
                            # The monitoring window closed while the process was down.
                            # Restoring the counter would treat scans from before the
                            # downtime as consecutive with the next one, so a user could
                            # be banned on a single fresh observation. Dropping the
                            # record costs a delayed ban, which is the safe direction.
                            expired += 1
                            continue
                        self.warnings[username] = warning
                    if expired or damaged:
                        warning_logger.warning(
                            f"⚠️ Dropped {expired} expired and {damaged} unreadable warning "
                            f"record(s) while loading; counters restart for those users"
                        )
                    warning_logger.debug(f"⚠️ Loaded {len(self.warnings)} active warnings from file")
        except Exception as e:
            warning_logger.error(f"Error loading warnings: {e}")

    def _serialize_warnings(self) -> dict:
        """Build a plain, detached snapshot of the active warnings.

        Runs on the event loop so a concurrent clear cannot mutate ``self.warnings``
        while the writer thread iterates it; the thread only ever sees this snapshot.
        """
        data = {}
        for username, warning in self.warnings.items():
            monitoring_history_serializable = []
            for snapshot in warning.monitoring_history:
                monitoring_history_serializable.append(
                    {"timestamp": snapshot["timestamp"], "ips": list(snapshot["ips"]), "ip_count": snapshot["ip_count"]}
                )

            ip_to_inbounds_serializable = {}
            if warning.ip_to_inbounds:
                for ip, inbounds in warning.ip_to_inbounds.items():
                    ip_to_inbounds_serializable[ip] = list(inbounds)

            data[username] = {
                "username": warning.username,
                "ip_count": warning.ip_count,
                "ips": list(warning.ips),
                "warning_time": warning.warning_time,
                "monitoring_end_time": warning.monitoring_end_time,
                "last_scan_time": warning.last_scan_time,
                "warned": warning.warned,
                "monitoring_history": monitoring_history_serializable,
                "ip_first_seen": warning.ip_first_seen,
                "ip_last_seen": warning.ip_last_seen,
                "ip_seen_count": warning.ip_seen_count,
                "trust_score": warning.trust_score,
                "inbound_protocols": list(warning.inbound_protocols),
                "ip_subnets": list(warning.ip_subnets),
                "previous_warnings_12h": warning.previous_warnings_12h,
                "previous_warnings_24h": warning.previous_warnings_24h,
                "ip_to_inbounds": ip_to_inbounds_serializable,
                "same_ip_multiple_inbounds": warning.same_ip_multiple_inbounds,
                "connection_details": warning.connection_details,
                "user_limit": getattr(warning, "user_limit", 1),
                "consecutive_violations": getattr(warning, "consecutive_violations", 1),
                "max_warnings_at_creation": getattr(warning, "max_warnings_at_creation", 0),
            }

        return data

    async def save_warnings(self):
        """Save warnings to file using asyncio.to_thread to avoid blocking."""
        try:
            async with self._write_lock:
                # Snapshot on the loop under the lock, then hand the detached dict to
                # the writer thread - so a concurrent clear cannot race the write.
                data = self._serialize_warnings()
                count = len(data)
                await asyncio.to_thread(atomic_write_json, self.filename, data)
            warning_logger.debug(f"⚠️ Saved {count} warnings to file")

        except Exception as e:
            warning_logger.error(f"Error saving warnings: {e}")

    async def add_warning(
        self,
        username: str,
        ip_count: int,
        ips: Set[str],
        user_limit: int = None,
        user_data: "UserType" = None,
        panel_data: "PanelType" = None,
        send_telegram_notification: bool = True,
    ) -> str:
        """
        Add a warning for a user, updating the consecutive-scan counter.

        Returns:
            str: "new" for a first warning, "updated" while the user is under
            monitoring, "violation_limit_reached" once max_warnings consecutive
            violating scans have been counted.
        """
        current_time = time.time()
        warning_logger.info(f"⚠️ Processing warning for user: {username} (ip_count={ip_count}, limit={user_limit})")

        if username in self.warnings:
            warning = self.warnings[username]

            # consecutive_violations counts *scans*, and a scan only means something
            # once per check_interval. Nothing here used to compare the clock, so any
            # fast loop advanced the counter - above all a process that crashes and is
            # restarted by the supervisor, since every fresh cycle calls this again and
            # the counters are reloaded from disk. Three such calls a few seconds apart
            # reached max_warnings and banned a user who should have had
            # check_interval * max_warnings to correct themselves.
            #
            # The previous scan time is read from the record itself. It used to be
            # derived as ``monitoring_end_time - monitoring_period``, which only holds
            # while the period never changes - but update_settings() rewrites
            # monitoring_period from live config on every cycle, so raising
            # check_interval put the derived time in the past (every gap accepted, the
            # guard disabled) and lowering it put the derived time in the future (every
            # gap negative, no violation ever counted).
            previous_scan_time = warning.last_scan_time
            min_scan_gap = max(1.0, self.check_interval * 0.5)
            max_scan_gap = float(self.monitoring_period)
            counted = False
            not_counted_reason = ""

            if previous_scan_time <= 0:
                # Record written before last_scan_time existed. Refusing to count is
                # the fail-safe reading: a missed increment only delays a ban, while a
                # guessed one bans a user who never had max_warnings scans.
                warning.last_scan_time = current_time
                not_counted_reason = "the previous scan time is unknown (record predates this field)"
            elif current_time < previous_scan_time:
                # Clock stepped backwards (NTP correction). The streak cannot be
                # verified, so it restarts instead of stalling forever on a negative gap.
                warning.consecutive_violations = 1
                warning.last_scan_time = current_time
                not_counted_reason = "the clock moved backwards, so the streak restarted at 1"
            else:
                elapsed_since_scan = current_time - previous_scan_time
                if elapsed_since_scan > max_scan_gap:
                    # Too long ago to be the previous scan of the same streak: downtime,
                    # or a record resurrected from disk after a restart. Scans that far
                    # apart are not consecutive, whatever the counter says.
                    warning.consecutive_violations = 1
                    warning.last_scan_time = current_time
                    not_counted_reason = (
                        f"the previous scan was {elapsed_since_scan:.0f}s ago "
                        f"(more than {max_scan_gap:.0f}s), so the streak restarted at 1"
                    )
                elif elapsed_since_scan >= min_scan_gap:
                    warning.consecutive_violations = getattr(warning, "consecutive_violations", 1) + 1
                    warning.last_scan_time = current_time
                    counted = True
                else:
                    not_counted_reason = (
                        f"it came only {elapsed_since_scan:.0f}s after the previous scan (minimum {min_scan_gap:.0f}s)"
                    )

            required_violations = self.effective_max_warnings(warning)

            if not counted:
                warning_logger.warning(
                    f"⏱️ User {username} violated again but the violation was not counted: "
                    f"{not_counted_reason} - the record is refreshed, so no fast loop and no "
                    f"restart can shortcut the {required_violations}-scan rule"
                )

            warning.ip_count = ip_count
            warning.ips = ips
            if user_limit:
                warning.user_limit = user_limit
            warning.update_ip_activity(ips, current_time)

            # Rolling monitoring window: every confirmed violation scan extends the
            # deadline. Without this the window (check_interval * max_warnings) can
            # expire before the required consecutive scans complete whenever a cycle
            # runs slower than check_interval, and the record is dropped at scan
            # max_warnings-1 - so the user is never disabled.
            warning.monitoring_end_time = current_time + self.monitoring_period

            if user_data and user_data.device_info:
                warning.inbound_protocols = user_data.device_info.inbound_protocols
                warning.ip_to_inbounds = self._extract_ip_to_inbounds(user_data)
            warning.ip_subnets = self._extract_subnets(ips)

            warning.trust_score = warning.calculate_trust_score()

            await self.save_warnings()
            warning_logger.info(
                f"⚠️ User {username} consecutive violation scan #{warning.consecutive_violations} (trust={warning.trust_score:.0f})"
            )
            log_monitoring_event(
                "warning_updated",
                username,
                {
                    "ip_count": ip_count,
                    "trust_score": warning.trust_score,
                    "consecutive": warning.consecutive_violations,
                },
            )

            if warning.consecutive_violations >= required_violations:
                return "violation_limit_reached"
            return "updated"

        previous_warnings_12h = self.count_recent_warnings(username, hours=12)
        previous_warnings_24h = self.count_recent_warnings(username, hours=24)
        warning_logger.debug(
            f"⚠️ User {username} history: {previous_warnings_12h} in 12h, {previous_warnings_24h} in 24h"
        )

        inbound_protocols = set()
        ip_subnets = self._extract_subnets(ips)
        ip_to_inbounds = {}
        connection_details = []

        if user_data and user_data.device_info:
            inbound_protocols = user_data.device_info.inbound_protocols
            ip_to_inbounds = self._extract_ip_to_inbounds(user_data)
            for conn in user_data.device_info.connections:
                connection_details.append(
                    {
                        "ip": conn.ip,
                        "node_id": conn.node_id,
                        "node_name": conn.node_name,
                        "inbound_protocol": conn.inbound_protocol,
                        "last_seen": conn.last_seen,
                    }
                )

        same_ip_multiple_inbounds = any(len(inbounds) > 1 for inbounds in ip_to_inbounds.values())

        warning = UserLimitWarning(
            username=username,
            ip_count=ip_count,
            ips=ips,
            warning_time=current_time,
            monitoring_end_time=current_time + self.monitoring_period,
            warned=True,
            inbound_protocols=inbound_protocols,
            ip_subnets=ip_subnets,
            previous_warnings_12h=previous_warnings_12h,
            previous_warnings_24h=previous_warnings_24h,
            ip_to_inbounds=ip_to_inbounds,
            same_ip_multiple_inbounds=same_ip_multiple_inbounds,
            connection_details=connection_details,
            user_limit=user_limit if user_limit and user_limit >= 1 else 1,
            consecutive_violations=1,
            last_scan_time=current_time,
            max_warnings_at_creation=self.max_warnings,
        )

        warning.trust_score = warning.calculate_trust_score()
        trust_level = warning.get_trust_level()
        behavior_summary = warning.get_behavior_summary()

        # Track initial activity and record active warning
        warning.update_ip_activity(ips, current_time)

        self.warnings[username] = warning
        await self.save_warnings()

        trust_details = []
        if same_ip_multiple_inbounds:
            trust_details.append(f"📱 Same IP uses multiple inbounds (likely 1 device)")
        elif len(inbound_protocols) > 1:
            trust_details.append(f"🔴 {len(inbound_protocols)} different inbounds with different IPs")

        if previous_warnings_12h > 0:
            trust_details.append(f"⚠️ {previous_warnings_12h} disables in last 12h")
        elif previous_warnings_24h > 0:
            trust_details.append(f"⚠️ {previous_warnings_24h} disables in last 24h")

        trust_info = (
            "\n".join([f"  • {detail}" for detail in trust_details]) if trust_details else "  • No suspicious patterns"
        )

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        limit_text = f"User limit: <code>{user_limit}</code>\n" if user_limit else ""
        period_min = max(1, round(self.monitoring_period / 60))

        if send_telegram_notification:
            await safe_send_warning_log(
                f"⚠️ <b>WARNING</b> - {time_str}\n\n"
                f"User: <code>{html.escape(username)}</code>\n"
                f"Active IPs: <code>{ip_count}</code>\n"
                f"{limit_text}"
                f"Trust Level: {trust_level} (<code>{warning.trust_score:.0f}</code>)\n"
                f"Behavior: <code>{behavior_summary}</code>\n"
                f"Trust Factors:\n{trust_info}\n\n"
                f"📡 Monitoring Window: <code>{self.max_warnings} cycles (~{period_min} min)</code>\n"
                f"If violations persist across {self.max_warnings} consecutive scans, user will be disabled."
            )

        logger.warning(
            f"Warning issued for user {username} with {ip_count} active IPs (limit: {user_limit}, trust: {warning.trust_score:.0f})"
        )
        return "new"

    def _extract_ip_to_inbounds(self, user_data: "UserType") -> Dict[str, Set[str]]:
        """Extract IP to inbound protocol mapping from user data"""
        ip_to_inbounds = {}
        if user_data and user_data.device_info and user_data.device_info.connections:
            for conn in user_data.device_info.connections:
                ip = conn.ip
                if ip not in ip_to_inbounds:
                    ip_to_inbounds[ip] = set()
                ip_to_inbounds[ip].add(conn.inbound_protocol)
        return ip_to_inbounds

    def _extract_subnets(self, ips: Set[str]) -> Set[str]:
        """Extract /24 subnets from IP addresses"""
        subnets = set()
        for ip in ips:
            try:
                ip_obj = ipaddress.ip_address(ip)
                if ip_obj.version == 4:
                    network = ipaddress.ip_network(f"{ip}/24", strict=False)
                    subnet_base = str(network.network_address).rsplit(".", 1)[0]
                    subnets.add(f"{subnet_base}.x")
                else:
                    subnets.add(ip)
            except ValueError:
                subnets.add(ip)
        return subnets

    async def check_persistent_violations(
        self,
        panel_data: PanelType,
        all_users_actual_ips: Dict[str, Set[str]],
        config_data: dict,
        batched_group_limits: dict = None,
        device_counts: Dict[str, int] = None,
        sample_is_trustworthy: bool = True,
        except_users=None,
        known_users: Optional[Set[str]] = None,
        protected_users: Optional[Set[str]] = None,
        refresh_user_evidence: Optional[Callable[[str], bool]] = None,
        *,
        user_policies: Mapping[str, UserPolicy] | None = None,
        refresh_user_policy: Callable[[str], Awaitable[UserPolicy | None]] | None = None,
        enforcement_is_current: Callable[[], bool] | None = None,
    ) -> tuple[Set[str], Set[str]]:
        """
        Check for users who still violate limits after the monitoring window ended.

        Args:
            device_counts: Optional cycle-wide {username: device_count} map. When
                given, the ban decision uses exactly the same number the warning
                path used, instead of the raw unique-IP count.
            sample_is_trustworthy: False when the caller judged this cycle's sample
                incomplete. Nothing is banned and no record is deleted, because a
                partial view cannot tell "this user disconnected" from "the node
                carrying this user went silent".
            batched_group_limits, except_users, known_users: Retained for caller
                compatibility. Current canonical policy supplies eligibility/limits.
            protected_users: Users whose evidence or policy cannot be judged;
                preserve their records.
            refresh_user_evidence: Optional local evaluation hook after awaits. It
                refreshes IP/device maps and returns False if a user cannot be judged.
            user_policies: Initial coherent policies for active and retained users.
            refresh_user_policy: Awaited current policy check immediately before
                judgment. Default calls read canonical settings and factual fields.
            enforcement_is_current: Pure original cycle/revision check at the panel
                effect boundary. Default captures the current configuration revision.

        Returns:
            Tuple[Set[str], Set[str]]: (disabled_users, warned_users) - sets of users who were disabled or warned
        """
        disabled_users = set()
        warned_users = set()  # Track users who received warnings to prevent double processing
        users_to_remove = {}
        normalized_users = set()
        monitoring_is_current = capture_monitoring_guard()
        if enforcement_is_current is None:
            from utils.read_config import get_config_revision

            original_revision = get_config_revision()

            def enforcement_is_current() -> bool:
                return get_config_revision() == original_revision

        if not monitoring_is_current():
            return disabled_users, warned_users

        if not sample_is_trustworthy:
            # Deliberately before any other work. This function used to run before the
            # caller had even computed the trustworthiness of the sample, and its
            # "user not found in current logs" branch deleted the record - so a few
            # cycles with one silent node wiped the counters of every offender whose
            # window had closed, and the guard that was supposed to prevent exactly
            # that ran 200 lines later.
            warning_logger.error(
                f"⛔ Sample is not trustworthy this cycle - {len(self.warnings)} monitored "
                f"record(s) are left exactly as they are: nothing banned, nothing deleted"
            )
            return disabled_users, warned_users

        if protected_users is None:
            protected_users = set()
        if refresh_user_policy is None:
            from utils.read_config import get_config_revision, read_config_snapshot
            from utils.settings_service import read_user_policy

            try:
                snapshot = await read_config_snapshot(usernames=self.warnings)
            except Exception as error:
                warning_logger.error(f"Persistent enforcement waiting: {error}")
                return disabled_users, warned_users
            if snapshot.config.get("config_degraded"):
                return disabled_users, warned_users
            if user_policies is None:
                user_policies = {
                    name: evaluate_user_policy(
                        username=name, config=snapshot.config, facts=snapshot.user_facts.get(name)
                    )
                    for name in self.warnings
                }

            async def refresh_user_policy(username: str) -> UserPolicy | None:
                if get_config_revision() != snapshot.local_revision:
                    return None
                if refresh_user_evidence is not None and not refresh_user_evidence(username):
                    return None
                policy = await read_user_policy(username, expected_db_revision=snapshot.db_revision)
                if get_config_revision() != snapshot.local_revision:
                    return None
                if refresh_user_evidence is not None and not refresh_user_evidence(username):
                    return None
                return policy

        async def current_policy(username: str) -> UserPolicy | None:
            if not monitoring_is_current():
                protected_users.add(username)
                return None
            try:
                policy = await refresh_user_policy(username)
            except Exception as error:
                warning_logger.error(f"Current policy unavailable for {username}: {error}")
                policy = None
            if not monitoring_is_current() or policy is None or not policy.known or not policy.eligible:
                protected_users.add(username)
                if policy is not None and not policy.known:
                    from utils.user_sync import queue_unknown_user_fetch

                    await queue_unknown_user_fetch(username)
                return None
            return policy

        warning_logger.debug(f"⚠️ Checking {len(self.warnings)} active warnings for persistent violations")

        # Snapshot the items: the body below awaits (limit resolution, panel disable,
        # Telegram sends) and self.warnings can be mutated at any of those points - by
        # the operator's clear-trust-data command or by the next cycle - which used to
        # raise "dictionary changed size during iteration" and abandon every remaining
        # offender for that cycle.
        for username, warning in list(self.warnings.items()):
            if not monitoring_is_current():
                break
            if username in protected_users:
                continue
            if refresh_user_evidence is not None and not refresh_user_evidence(username):
                continue
            if not warning.is_monitoring_active():
                warning_logger.debug(f"⚠️ Monitoring ended for {username}")
                policy = (user_policies or {}).get(username)
                if policy is not None and not policy.eligible:
                    protected_users.add(username)
                    if not policy.known:
                        from utils.user_sync import queue_unknown_user_fetch

                        await queue_unknown_user_fetch(username)
                    continue
                policy = await current_policy(username)
                if policy is None or self.warnings.get(username) is not warning:
                    continue
                if refresh_user_evidence is not None and not refresh_user_evidence(username):
                    continue
                user_limit_number = policy.limit
                if warning.active_monitoring_task and not warning.active_monitoring_task.done():
                    warning.active_monitoring_task.cancel()
                trust_score = warning.trust_score
                trust_level = warning.get_trust_level()

                if username in all_users_actual_ips:
                    current_ips = all_users_actual_ips[username]
                    # Prefer the unified device count of this cycle; fall back to
                    # the raw unique-IP count when it was not supplied.
                    if device_counts is not None and username in device_counts:
                        device_count = device_counts[username]
                    else:
                        device_count = len(current_ips)
                    activity_summary = warning.get_ip_activity_summary()
                    required_violations = self.effective_max_warnings(warning)

                    warning_logger.info(
                        f"⚠️ User {username}: {device_count} devices / {len(current_ips)} IPs (limit: {user_limit_number}, violations: {warning.consecutive_violations}/{required_violations}), trust={trust_score:.0f}"
                    )

                    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    if device_count > user_limit_number and warning.consecutive_violations >= required_violations:
                        try:

                            def punishment_is_current():
                                return (
                                    monitoring_is_current()
                                    and enforcement_is_current()
                                    and self.warnings.get(username) is warning
                                    and not warning.is_monitoring_active()
                                    and warning.consecutive_violations >= self.effective_max_warnings(warning)
                                    and (refresh_user_evidence is None or refresh_user_evidence(username))
                                )

                            async def revalidate_punishment():
                                policy = await current_policy(username)
                                count = (device_counts or {}).get(username, len(all_users_actual_ips.get(username, ())))
                                return policy is not None and punishment_is_current() and count > policy.limit

                            with enforcement_guard(revalidate_punishment, punishment_is_current):
                                punishment_result = await safe_disable_user_with_punishment(
                                    panel_data, UserType(name=username, ip=[])
                                )
                            if punishment_result.get("protected"):
                                protected_users.add(username)
                                continue

                            # Remove the applied incident before optional lookup or
                            # notification I/O can fail or be cancelled.
                            await self.clear_user_warning(username, expected_warning=warning)

                            if punishment_result.get("action") in ("reconciled", "deduplicated") or (
                                punishment_result.get("action") == "applied_unrecorded"
                                and not punishment_result.get("newly_applied", False)
                            ):
                                # A ledger retry or duplicate claim applies nothing
                                # new; do not report or count another disable, or
                                # let the main scan warn the same user again this cycle.
                                disabled_users.add(username)
                                warning_logger.info(
                                    "Punishment result for %s: %s; no new panel action was applied",
                                    username,
                                    punishment_result.get("action"),
                                )
                                continue

                            # Only a real disable/revoke belongs in disable history; a
                            # warning or a failed punishment must not log a disable.
                            await self.record_disable_outcome(username, punishment_result.get("action", ""))

                            notice_action = punishment_result.get("action")
                            record_note = ""
                            if notice_action == "applied_unrecorded":
                                # Only the history write failed; the panel disable was applied.
                                notice_action = "revoked" if "revoke_success" in punishment_result else "disabled"
                                record_note = f"⚠️ {html.escape(str(punishment_result.get('message') or 'Violation not yet recorded'))}\n"

                            if notice_action in ("disabled", "revoked"):
                                warning.update_ip_activity(current_ips, time.time())
                                activity_summary = await get_disable_ip_activity(warning, config_data)

                            if punishment_result["action"] == "warning":
                                await safe_send_warning_log(
                                    f"⚠️ <b>WARNING</b> - {time_str}\n\n"
                                    f"User: <code>{html.escape(username)}</code>\n"
                                    f"Active Devices: <code>{device_count}</code> ({len(current_ips)} IPs)\n"
                                    f"User limit: <code>{user_limit_number}</code>\n"
                                    f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                                    f"📊 Violation #{punishment_result['violation_count']} in time window\n"
                                    f"⚡ Next violation will result in disable."
                                )
                                warning_logger.warning(
                                    f"⚠️ WARNING: User {username} - {device_count} IPs (limit: {user_limit_number}) - violation #{punishment_result['violation_count']}"
                                )
                                log_monitoring_event(
                                    "persistent_warning",
                                    username,
                                    {
                                        "devices": device_count,
                                        "limit": user_limit_number,
                                        "violation": punishment_result["violation_count"],
                                    },
                                )
                                warned_users.add(username)  # Track warned users to prevent double processing
                            elif notice_action == "disabled":
                                disabled_users.add(username)

                                duration_text = ""
                                if punishment_result["duration_minutes"] > 0:
                                    duration_text = (
                                        f"Duration: <code>{punishment_result['duration_minutes']} minutes</code>\n"
                                    )
                                else:
                                    duration_text = "Duration: <code>Until manual enable</code>\n"

                                await safe_send_disable_notification(
                                    f"🚫 <b>USER DISABLED</b> - {time_str}\n\n"
                                    f"User: <code>{html.escape(username)}</code>\n"
                                    f"Active Devices: <code>{device_count}</code> ({len(current_ips)} IPs)\n"
                                    f"User limit: <code>{user_limit_number}</code>\n"
                                    f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                                    f"📊 Violation #{punishment_result['violation_count']} (Step {punishment_result['step_index'] + 1})\n"
                                    f"{duration_text}"
                                    f"{record_note}"
                                    f"📊 IP Activity:\n<code>{html.escape(activity_summary)}</code>",
                                    username,
                                )

                                warning_logger.warning(
                                    f"🚫 Disabled user {username}: {device_count} IPs (limit: {user_limit_number}) - step {punishment_result['step_index'] + 1}"
                                )
                                log_monitoring_event(
                                    "user_disabled",
                                    username,
                                    {
                                        "devices": device_count,
                                        "limit": user_limit_number,
                                        "duration_min": punishment_result.get("duration_minutes", 0),
                                    },
                                )
                            elif notice_action == "revoked":
                                disabled_users.add(username)

                                revoke_note = (
                                    "✅ Sub revoked"
                                    if punishment_result.get("revoke_success", False)
                                    else "⚠️ Revoke failed"
                                )
                                uuid_note = (
                                    "✅ UUID reset"
                                    if punishment_result.get("uuid_reset_success", False)
                                    else "⚠️ UUID failed"
                                )

                                await safe_send_disable_notification(
                                    f"🔄 <b>SUBSCRIPTION REVOKED + DISABLED</b> - {time_str}\n\n"
                                    f"User: <code>{html.escape(username)}</code>\n"
                                    f"Active Devices: <code>{device_count}</code> ({len(current_ips)} IPs)\n"
                                    f"User limit: <code>{user_limit_number}</code>\n"
                                    f"Trust Level: {trust_level} (<code>{trust_score:.0f}</code>)\n\n"
                                    f"📊 Violation #{punishment_result['violation_count']} (Step {punishment_result['step_index'] + 1})\n"
                                    f"{revoke_note}, {uuid_note}\n"
                                    f"Duration: <code>Until manual enable</code>\n"
                                    f"{record_note}"
                                    f"📊 IP Activity:\n<code>{html.escape(activity_summary)}</code>",
                                    username,
                                )

                                warning_logger.warning(
                                    f"🔄 Revoked + disabled user {username}: {device_count} IPs (limit: {user_limit_number}) - step {punishment_result['step_index'] + 1}"
                                )
                                log_monitoring_event(
                                    "user_revoked",
                                    username,
                                    {
                                        "devices": device_count,
                                        "limit": user_limit_number,
                                        "revoke_success": punishment_result.get("revoke_success", False),
                                    },
                                )
                            else:
                                warning_logger.error(
                                    f"Punishment action error for {username}: {punishment_result['message']}"
                                )

                        except Exception as e:
                            warning_logger.error(f"Failed to disable user {username}: {e}")
                            await safe_send_logs(f"❌ <b>Error:</b> Failed to disable user {username}: {e}")

                    elif device_count <= user_limit_number:
                        normalized_users.add(username)
                        warning_logger.info(
                            f"✅ User {username} is now within limits ({device_count} IPs, limit: {user_limit_number})"
                        )
                        log_monitoring_event(
                            "monitoring_ended", username, {"devices": device_count, "limit": user_limit_number}
                        )
                else:
                    normalized_users.add(username)
                    warning_logger.info(f"ℹ️ User {username} not found in current logs - monitoring ended")
                    log_monitoring_event("monitoring_ended", username, {"reason": "user_inactive"})

                users_to_remove[username] = warning

        for username, warning in users_to_remove.items():
            if self.warnings.get(username) is not warning:
                continue
            # Earlier absence/normalization decisions may have waited behind a
            # later user's preparation. Preserve them if the sample was retired.
            # Already-applied punishments still consume their warning record.
            if username not in disabled_users | warned_users:
                policy = await current_policy(username)
                if policy is None or self.warnings.get(username) is not warning:
                    continue
                if refresh_user_evidence is not None and not refresh_user_evidence(username):
                    continue
                if username in normalized_users:
                    count = (device_counts or {}).get(username, len(all_users_actual_ips.get(username, ())))
                    if count > policy.limit:
                        continue
            # pop, not del: clear_user_trust_data may already have removed the key
            # while this function was awaiting a panel call or a Telegram send.
            self.warnings.pop(username, None)

        if users_to_remove:
            await self.save_warnings()

        return disabled_users, warned_users

    async def send_monitoring_status(self):
        """Send status of currently monitored users"""
        if not self.warnings:
            return

        active_warnings = []
        for username, warning in self.warnings.items():
            if warning.is_monitoring_active():
                remaining = warning.time_remaining()
                minutes = remaining // 60
                seconds = remaining % 60
                active_warnings.append(f"• <code>{username}</code> - {minutes}m {seconds}s remaining")

        if active_warnings:
            warning_logger.debug(f"Currently monitoring {len(active_warnings)} users")

    def get_monitoring_users(self) -> Set[str]:
        """Get set of users currently being monitored"""
        return {username for username, warning in self.warnings.items() if warning.is_monitoring_active()}

    def is_user_being_monitored(self, username: str) -> bool:
        """Check if a user is currently being monitored"""
        return username in self.warnings and self.warnings[username].is_monitoring_active()

    async def cleanup_expired_warnings(
        self,
        sample_is_trustworthy: bool = True,
        protected_users: Optional[Set[str]] = None,
        *,
        expected_warnings: Mapping[str, UserLimitWarning] | None = None,
        authorize_clear: Callable[[str], Awaitable[bool]] | None = None,
    ):
        """
        Delete monitoring records whose window has closed.

        Takes the trustworthiness of the cycle's sample for the same reason
        ``check_persistent_violations`` does: an expired record is only safe to drop
        once it has actually been judged, and that function returns without judging
        anything when the sample is partial. Running unconditionally afterwards deleted
        precisely the records it had just preserved, so a partial sample still wiped
        real offenders' counters - the failure those guards exist to prevent.

        The caller can capture ``expected_warnings`` before reporting and provide
        ``authorize_clear`` to refresh current policy, evidence, and device count.
        Direct callers refresh canonical eligibility before each expired removal.
        """
        monitoring_is_current = capture_monitoring_guard()
        if not sample_is_trustworthy or not monitoring_is_current():
            if self.warnings:
                warning_logger.warning(
                    f"⏳ Keeping {len(self.warnings)} monitoring record(s): the sample "
                    f"was not trustworthy this cycle, so a closed window has not been "
                    f"judged yet"
                )
            return

        candidates = dict(self.warnings if expected_warnings is None else expected_warnings)
        if not candidates:
            return
        if authorize_clear is None:
            from utils.read_config import get_config_revision, read_config_snapshot
            from utils.settings_service import read_user_policy

            try:
                snapshot = await read_config_snapshot(usernames=candidates)
            except Exception as error:
                warning_logger.error(f"Expired cleanup waiting: {error}")
                return
            if snapshot.config.get("config_degraded"):
                return

            async def authorize_clear(username: str) -> bool:
                if get_config_revision() != snapshot.local_revision:
                    return False
                policy = await read_user_policy(username, expected_db_revision=snapshot.db_revision)
                return bool(
                    get_config_revision() == snapshot.local_revision
                    and policy is not None
                    and policy.known
                    and policy.eligible
                )

        expired_users = []
        for username, warning in candidates.items():
            if not monitoring_is_current():
                break
            if (
                username in (protected_users or ())
                or self.warnings.get(username) is not warning
                or warning.is_monitoring_active()
            ):
                continue
            try:
                authorized = await authorize_clear(username)
            except Exception as error:
                warning_logger.error(f"Expired cleanup unavailable for {username}: {error}")
                continue
            if (
                not authorized
                or not monitoring_is_current()
                or self.warnings.get(username) is not warning
                or warning.is_monitoring_active()
            ):
                continue
            self.warnings.pop(username, None)
            expired_users.append(username)

        if expired_users:
            await self.save_warnings()
            warning_logger.info(f"🧹 Cleaned up {len(expired_users)} expired warnings")

    async def start_monitoring_task(self, username: str, panel_data: PanelType):
        """
        Start a background monitoring task for a user.
        Currently disabled to prevent circular import issues.
        """
        warning_logger.debug(f"📍 Monitoring for {username} handled through periodic checks")
        return


# Global singleton instance and accessor function
_warning_system: Optional[EnhancedWarningSystem] = None


def get_warning_system() -> EnhancedWarningSystem:
    """Get or create the global EnhancedWarningSystem singleton instance."""
    global _warning_system
    if _warning_system is None:
        _warning_system = EnhancedWarningSystem()
    return _warning_system


# Global instance alias to be imported by other modules (for backward compatibility)
warning_system = get_warning_system()
