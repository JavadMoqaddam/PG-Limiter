"""Short, read-only health cards built from public runtime receipts."""

import importlib
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from telegram_bot.handlers.admin import check_admin_privilege
from telegram_bot.keyboards import create_status_reports_keyboard
from utils.logs import get_logger

report_logger = get_logger("telegram.status_reports")
CARD_BUDGET = 2000
ADMISSION_REASONS = {
    "Topics are disabled": "Topics disabled",
    "Topics disabled": "Topics disabled",
    "No forum group is configured": "No forum group configured",
}
SKIP_REASONS = {
    **ADMISSION_REASONS,
    "Topics monitoring changed": "Topics monitoring changed",
    "Topics monitoring changed during the scan": "Topics monitoring changed during the scan",
    "Database configuration is unavailable": "Configuration unavailable",
    "API configuration unavailable": "Configuration unavailable",
    "configuration changed": "Configuration changed",
    "configuration or collection changed": "Configuration or collection changed",
    "API sample unavailable or source changed": "API sample unavailable or source changed",
    "Scan did not complete: source evidence or policy unavailable": "Source evidence or policy unavailable",
    "panel unavailable": "Panel unavailable",
    "node availability unknown": "Node availability unknown",
    "candidate query failed": "Candidate query unavailable",
    "no successful lookups": "No successful API lookups",
    "all user evidence unknown": "Sample evidence unavailable",
}


@dataclass(frozen=True)
class ReportFacts:
    """Current settings and separately owned historical runtime evidence."""

    config: dict
    admission: Any = None
    completed: Any = None
    attempt: Any = None
    collection: dict | None = None
    config_revision: int | None = None
    runtime_available: bool = True


def _runtime_module():
    try:
        return importlib.import_module("utils.monitoring_gate")
    except ImportError:
        return None


def _config_revision() -> int | None:
    module = importlib.import_module("utils.read_config")
    reader = getattr(module, "get_config_revision", None)
    return reader() if reader else None


async def _read_current_config() -> tuple[dict, int | None]:
    """Refresh committed settings; an older unsupported reader is unavailable."""
    module = importlib.import_module("utils.read_config")
    reader = getattr(module, "read_config_snapshot", None)
    if reader:
        snapshot = await reader()
        return snapshot.config, snapshot.local_revision
    config = await module.read_config(refresh=True)
    return config, config.get("_revision")


async def collect_report_facts() -> ReportFacts:
    """Retry changed observations without querying panel or Telegram services."""
    admission = completed = attempt = None
    runtime = None
    for _ in range(3):
        try:
            runtime = _runtime_module()
            admission = runtime.monitoring_snapshot() if runtime else None
            completed = runtime.get_last_completed_scan() if runtime else None
            attempt = runtime.get_last_cycle_outcome() if runtime else None
            config, revision = await _read_current_config()
            collection = None
            try:
                collector = importlib.import_module("utils.ip_source_api")
                collection = collector.get_last_cycle_stats()
            except Exception:
                report_logger.exception("Could not read API collection diagnostics")
            if revision != _config_revision() or (runtime and admission != runtime.monitoring_snapshot()):
                continue
            return ReportFacts(config, admission, completed, attempt, collection, revision, runtime is not None)
        except Exception:
            report_logger.exception("Could not read status/report facts")
            break
    return ReportFacts({"config_degraded": True}, admission, completed, attempt, runtime_available=runtime is not None)


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _source(value: Any) -> str:
    return {"logs": "Logs", "api": "API"}.get(value, "unavailable")


def _timestamp(value: Any, now: float) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return "time unavailable"
    try:
        timestamp = datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        return "time unavailable"
    if value > now:
        return f"{timestamp} (clock ahead)"
    return f"{timestamp} ({int(now - value)}s ago)"


def _count(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12:
        return str(value)
    return "unavailable"


def _base_lines(facts: ReportFacts, now: float) -> list[str]:
    config = facts.config
    if config.get("config_degraded"):
        admission = "paused — Configuration unavailable"
    elif facts.admission is None:
        admission = "unavailable"
    elif facts.admission.allowed:
        admission = "enabled"
    else:
        admission = f"paused — {ADMISSION_REASONS.get(facts.admission.reason, 'Reason unavailable')}"
    source = "unavailable" if config.get("config_degraded") else _source(config.get("ip_source"))
    mode = (
        "unavailable"
        if config.get("config_degraded")
        else {"device": "Per device (by inbound)", "ip": "Per IP", "inbound": "Per Inbound", "node": "Per Node"}.get(
            config.get("device_count_mode"), "unavailable"
        )
    )
    lines = [f"Monitoring admission: {admission}", f"IP source: {source}", f"Counting mode: {mode}"]
    scan = facts.completed
    if scan is None:
        lines.append(
            "No completed scan recorded in this process."
            if facts.runtime_available
            else "Completed scan telemetry: unavailable"
        )
    else:
        lines.extend(
            [
                "<b>Last completed scan (historical receipt)</b>",
                f"{_timestamp(scan.completed_at, now)} · {_source(scan.source)}",
                f"Users counted: {_count(scan.users_counted)}",
            ]
        )
        required = getattr(scan, "required_node_ids", None)
        eligible = getattr(scan, "eligible_node_ids", None)
        if scan.source == "logs" and required is not None and eligible is not None:
            complete = bool(getattr(scan, "coverage_complete", False))
            lines.append(f"Node coverage: {'complete' if complete else 'partial'} ({len(eligible)}/{len(required)})")
            missing = sorted(node_id for node_id in required - eligible if type(node_id) is int)
            if missing:
                display = ", ".join(str(node_id) for node_id in missing[:12])
                lines.append(f"Missing node IDs: {display}" + (" …" if len(missing) > 12 else ""))
        if facts.config_revision != getattr(scan, "config_revision", None):
            lines.append("Settings changed since this scan.")
    return lines


def _card(title: str, lines: list[str]) -> str:
    """Keep whole balanced lines within the conservative HTML source budget."""
    text = f"<b>{title}</b>\n\n"
    for line in lines:
        candidate = text + line + "\n"
        if _units(candidate) <= CARD_BUDGET:
            text = candidate
    return text.rstrip()


def render_status(facts: ReportFacts, *, now: float | None = None) -> str:
    """Admission is configuration permission, never a process liveness verdict."""
    return _card("Limiter status", _base_lines(facts, time.time() if now is None else now))


def render_health(facts: ReportFacts, *, now: float | None = None) -> str:
    """Present historical completion, finished attempts and collection separately."""
    now = time.time() if now is None else now
    lines = _base_lines(facts, now)
    attempt = facts.attempt
    if attempt is None:
        lines.append("Latest finished attempt: unavailable")
    else:
        outcome = attempt.outcome if attempt.outcome in {"completed", "skipped", "failed"} else "unavailable"
        lines.extend(
            [
                f"<b>Latest finished attempt: {outcome}</b>",
                f"{_timestamp(attempt.finished_at, now)} · {_source(attempt.source)}",
            ]
        )
        # Owner reasons may contain raw exceptions or individual activity. Only
        # known aggregate skip labels can cross this read-only report boundary.
        if outcome == "failed":
            lines.append("Reason: Scan failed; see application logs.")
        elif outcome == "skipped":
            lines.append(f"Reason: {SKIP_REASONS.get(attempt.reason, 'Reason unavailable')}")
        if facts.config_revision != getattr(attempt, "config_revision", None):
            lines.append("Attempt used earlier settings.")
    scan = facts.completed
    if scan is not None:
        for label, key in [
            ("Protected", "protected_users_count"),
            ("Metadata unknown", "metadata_unknown_users_count"),
            ("User failures", "failed_users_count"),
        ]:
            if hasattr(scan, key):
                lines.append(f"{label}: {_count(getattr(scan, key))} (completed scan)")
    stats = facts.collection
    started = stats.get("ran_at") if stats else None
    if not isinstance(started, (int, float)) or not math.isfinite(started) or started <= 0:
        lines.append("Collection diagnostics: unavailable")
    else:
        historical = "Historical " if facts.config.get("ip_source") != "api" else ""
        lines.extend([f"<b>{historical}API collection diagnostics</b>", f"started {_timestamp(started, now)}"])
        if "fetched_failed" in stats:
            lines.append(f"Lookup failures: {_count(stats['fetched_failed'])}")
        for label, key in [("User coverage", "coverage"), ("Node coverage", "node_coverage")]:
            value = stats.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 <= value <= 1
            ):
                lines.append(f"{label}: {value:.0%}")
        lines.append("Collection diagnostics may still be updating.")
    lines.append("Inspect individual activity in Topics.")
    return _card("Aggregate health report", lines)


async def deliver_card(update: Update, text: str, keyboard) -> None:
    """Allow unchanged refreshes; propagate a failed final delivery to the bot."""
    kwargs = {"text": text, "reply_markup": keyboard, "parse_mode": "HTML"}
    query = getattr(update, "callback_query", None)
    try:
        if query:
            try:
                await query.answer()
            except Exception:
                report_logger.debug("Could not acknowledge status/report callback", exc_info=True)
            try:
                await query.edit_message_text(**kwargs)
                return
            except BadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
            except Exception:
                pass
            await query.message.reply_text(**kwargs)
        else:
            await update.message.reply_text(**kwargs)
    except Exception:
        report_logger.exception("Status/report delivery failed")
        raise


async def _show_card(update: Update, context: ContextTypes.DEFAULT_TYPE, *, health: bool):
    check = await check_admin_privilege(update)
    if check:
        return check
    # The shared router cancels pending input too; direct legacy entries do so here.
    try:
        from telegram_bot.input_owner import end_input
    except ImportError:
        pass
    else:
        end_input(context)
    facts = await collect_report_facts()
    text = render_health(facts) if health else render_status(facts)
    await deliver_card(update, text, create_status_reports_keyboard(health=health))
    return ConversationHandler.END


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open or refresh the factual status card."""
    return await _show_card(update, context, health=False)


async def health_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open or refresh the aggregate health card."""
    return await _show_card(update, context, health=True)


async def legacy_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Older detailed report shortcuts now show bounded aggregate evidence."""
    return await health_report_command(update, context)
