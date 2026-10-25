"""Transition-only monitoring health messages through the General dispatcher.

Only transport, fleet and cycle boundaries call this module. Access records never
queue health messages, and outbound admission does not wait for Telegram delivery.
"""

import asyncio
import html
import math
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from telegram_bot.dispatcher import Priority
from telegram_bot.send_message import send_logs
from telegram_bot.topics import TopicType
from utils.logs import get_logger
from utils.monitoring_gate import MonitoringAdmission, monitoring_is_current
from utils.read_config import get_config_revision

health_logger = get_logger("limiter.node_health")

_NODE_REASONS = frozenset(
    {
        "source timezone missing or invalid",
        "panel connection unavailable",
        "waiting for log transport",
        "timestamp or replay uncertainty in the current interval",
        "collecting a complete check interval",
    }
)
_CYCLE_REASONS = {
    "waiting for the node fleet": "Node fleet unavailable",
    "Scan did not complete: source evidence or policy unavailable": "Source evidence or policy unavailable",
    "Database configuration is unavailable": "Configuration unavailable",
    "API configuration unavailable": "Configuration unavailable",
    "panel unavailable": "Panel unavailable",
    "candidate query failed": "Candidate query unavailable",
    "node availability unknown": "Node fleet unavailable",
    "no successful lookups": "No successful API lookups",
    "all user evidence unknown": "Sample evidence unavailable",
    "API sample unavailable or source changed": "API sample unavailable or source changed",
    "configuration changed": "Configuration changed",
    "configuration or collection changed": "Configuration changed",
    "Per Inbound requires node logs": "Per Inbound requires node logs",
    "collecting a complete check interval": "Collecting a complete check interval",
    "timestamp or replay uncertainty in the current interval": "Timestamp or replay uncertainty",
}
_MODES = {"node": "Per Node", "inbound": "Per Inbound", "device": "Device", "ip": "IP"}


class NodeHealthState(str, Enum):
    """Transport recovery and usable evidence are separate operator milestones."""

    UNAVAILABLE = "unavailable"
    WARMING = "warming"
    READY = "ready"


@dataclass(frozen=True)
class NodeHealth:
    state: NodeHealthState
    reason: str = ""


def _admission_namespace(admission) -> tuple:
    return admission.manager_id, admission.revision, admission.group_id


def _safe_node_reason(reason: str) -> str:
    return reason if reason in _NODE_REASONS else "node evidence unavailable"


def _safe_cycle_reason(reason: str) -> str:
    if reason in _CYCLE_REASONS:
        return _CYCLE_REASONS[reason]
    # Older strict evidence includes a numeric node prefix; preserve no free text.
    if isinstance(reason, str) and reason.startswith("node "):
        prefix, separator, detail = reason.partition(": ")
        if separator and prefix[5:].isdecimal() and detail in _NODE_REASONS:
            return f"Node {prefix[5:]}: {detail}"
    return "Monitoring evidence unavailable"


def _ids_summary(node_ids: frozenset[int]) -> str:
    selected = sorted(node_ids)
    summary = ", ".join(str(node_id) for node_id in selected[:10])
    return summary + (f" (+{len(selected) - 10} more)" if len(selected) > 10 else "")


class NodeHealthNotifications:
    """Own process-local transition memory, bounded by the authoritative scope."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._node_namespace = None
        self._node_states: dict[int, NodeHealth] = {}
        self._incident_nodes: set[int] = set()
        self._node_names: dict[int, str] = {}
        self._cycle_namespace = None
        self._cycle_state = None
        self._planned_nodes: set[int] = set()
        self._planned_depth = 0
        self._refresh_warming: set[int] = set()
        self._refresh_config_revision: int | None = None
        self._refresh_admission_namespace: tuple | None = None
        self._event_prefix = uuid.uuid4().hex
        self._event_sequence = 0

    @contextmanager
    def planned_refresh(self, node_ids):
        """Hide deliberate stream closure and its normal evidence warmup."""
        previous = self._planned_nodes.copy()
        if not self._planned_depth:
            self._refresh_config_revision = get_config_revision()
            namespace = self._node_namespace or self._cycle_namespace
            self._refresh_admission_namespace = namespace[:3] if namespace else None
        self._planned_depth += 1
        self._planned_nodes.update(node_ids)
        self._refresh_warming.update(node_ids)
        try:
            yield
        finally:
            self._planned_nodes = previous
            self._planned_depth -= 1

    def _clear_refresh_warmup(self) -> None:
        self._refresh_warming.clear()
        self._refresh_config_revision = None
        self._refresh_admission_namespace = None

    def _refresh_applies(self, admission, source: str) -> bool:
        """A scheduled rebuild cannot hide a later configuration or scope change."""
        if (
            source != "logs"
            or self._refresh_config_revision != get_config_revision()
            or (
                self._refresh_admission_namespace is not None
                and self._refresh_admission_namespace != _admission_namespace(admission)
            )
        ):
            self._clear_refresh_warmup()
            return False
        return True

    async def _send(self, admission, text: str) -> bool:
        if not monitoring_is_current(admission):
            return False
        self._event_sequence += 1
        try:
            await send_logs(
                text,
                topic_type=TopicType.GENERAL,
                priority=Priority.SYSTEM,
                message_key=f"node-health:{self._event_prefix}:{self._event_sequence}",
                ttl=600.0,
            )
            return monitoring_is_current(admission)
        except Exception:
            # Diagnostics cannot kill a collector or expose remote error bodies.
            health_logger.warning("Could not queue General monitoring health notice; retrying at the next boundary")
            return False

    async def report_nodes(self, observations, admission, *, nodes=None, evidence=None, observed_node_ids=None) -> None:
        """Describe an authoritative captured fleet at an ordinary poll boundary."""
        async with self._lock:
            if not monitoring_is_current(admission) or observations.source != "logs":
                self._node_namespace = None
                self._node_states.clear()
                self._incident_nodes.clear()
                self._node_names.clear()
                self._clear_refresh_warmup()
                return
            self._refresh_applies(admission, observations.source)
            evidence = observations.evidence() if evidence is None else evidence
            if evidence.generation != observations.generation or evidence.config_revision != get_config_revision():
                return
            required = evidence.required_node_ids
            eligible = evidence.eligible_node_ids
            if required is None or eligible is None or evidence.unavailable_reason == "waiting for the node fleet":
                return
            namespace = (*_admission_namespace(admission), observations.source, evidence.generation)
            if namespace != self._node_namespace:
                self._node_namespace = namespace
                self._node_states.clear()
                self._incident_nodes.clear()
            self._node_states = {node: state for node, state in self._node_states.items() if node in required}
            self._incident_nodes.intersection_update(required)
            self._node_names = {node: name for node, name in self._node_names.items() if node in required}
            self._refresh_warming.intersection_update(required)
            for node in nodes or ():
                if node.node_id in required:
                    self._node_names[node.node_id] = str(node.node_name)[:128]
            observed = required if observed_node_ids is None else required.intersection(observed_node_ids)
            for node_id in sorted(observed):
                if (
                    not monitoring_is_current(admission)
                    or observations.source != "logs"
                    or evidence.generation != observations.generation
                    or evidence.config_revision != get_config_revision()
                ):
                    return
                reason = observations.node_unavailable_reason(node_id, end=evidence.interval_end)
                if not reason and node_id in eligible:
                    state = NodeHealth(NodeHealthState.READY)
                elif not reason or reason == "collecting a complete check interval":
                    state = NodeHealth(NodeHealthState.WARMING, "collecting a complete check interval")
                else:
                    state = NodeHealth(NodeHealthState.UNAVAILABLE, _safe_node_reason(reason))
                previous = self._node_states.get(node_id)
                if observed_node_ids is None and previous is None and reason == "waiting for log transport":
                    # The first ordinary poll can run before transports have
                    # opened. Explicit error/closure hooks bypass this grace.
                    floor = observations.node_interval_floor(node_id)
                    if floor is not None and evidence.interval_start < floor:
                        continue
                if node_id in self._planned_nodes:
                    # Do not remember an unreported fault as delivered: a failed
                    # rebuild must become visible after the maintenance scope.
                    if state.state != NodeHealthState.UNAVAILABLE:
                        self._node_states[node_id] = state
                    continue
                if node_id in self._refresh_warming:
                    if state.state != NodeHealthState.UNAVAILABLE:
                        self._node_states[node_id] = state
                        if state.state == NodeHealthState.READY:
                            self._refresh_warming.discard(node_id)
                            self._incident_nodes.discard(node_id)
                        continue
                    self._refresh_warming.discard(node_id)
                if previous == state:
                    continue
                # Startup connecting/healthy nodes already appear in the SSE summary.
                if node_id not in self._incident_nodes and state.state != NodeHealthState.UNAVAILABLE:
                    self._node_states[node_id] = state
                    continue
                name = self._node_names.get(node_id)
                identity = f"Node {node_id}" + (f": <code>{html.escape(name)}</code>" if name else "")
                if state.state == NodeHealthState.UNAVAILABLE:
                    text = f"⚠️ <b>Included node unavailable</b>\n{identity}\nReason: {state.reason}"
                elif state.state == NodeHealthState.WARMING:
                    text = f"🔄 <b>Node transport restored</b>\n{identity}\nCollecting a fresh interval before evidence is ready."
                else:
                    text = f"✅ <b>Node evidence ready</b>\n{identity}\nA complete fresh interval is available."
                if await self._send(admission, text):
                    self._node_states[node_id] = state
                    if state.state == NodeHealthState.UNAVAILABLE:
                        self._incident_nodes.add(node_id)
                    elif state.state == NodeHealthState.READY:
                        self._incident_nodes.discard(node_id)

    async def report_cycle(
        self,
        admission: MonitoringAdmission,
        source: str,
        outcome: str,
        *,
        device_count_mode: str = "device",
        coverage_complete: bool = True,
        required_node_ids: frozenset[int] | None = None,
        eligible_node_ids: frozenset[int] | None = None,
        users_counted: int = 0,
        protected_users_count: int = 0,
        duration_seconds: float = 0.0,
        reason: str = "",
        config_revision: int | None = None,
    ) -> None:
        """Publish first completion and changed degradation from immutable telemetry."""
        async with self._lock:
            if not monitoring_is_current(admission):
                self._cycle_namespace = None
                self._cycle_state = None
                self._clear_refresh_warmup()
                return
            if config_revision is not None and config_revision != get_config_revision():
                return
            if source not in ("logs", "api") or outcome not in ("completed", "skipped", "failed"):
                return
            refresh_applies = self._refresh_applies(admission, source)
            if refresh_applies and (
                self._planned_depth
                or (self._refresh_warming and outcome == "skipped" and reason == "collecting a complete check interval")
            ):
                return
            namespace = (*_admission_namespace(admission), source, device_count_mode)
            if namespace != self._cycle_namespace:
                self._cycle_namespace, self._cycle_state = namespace, None
            safe_reason = _safe_cycle_reason(reason) if outcome != "completed" else ""
            missing = (
                required_node_ids - eligible_node_ids
                if required_node_ids is not None and eligible_node_ids is not None
                else None
            )
            state = outcome, bool(coverage_complete) if outcome == "completed" else None, missing, safe_reason
            if state == self._cycle_state:
                return
            mode = _MODES.get(device_count_mode, "Device")
            heading = "Monitoring cycle " + outcome
            if outcome == "completed":
                heading += " — complete coverage" if coverage_complete else " — partial coverage"
            lines = [
                f"{'✅' if outcome == 'completed' else '⚠️'} <b>{heading}</b>",
                f"Source: {source.upper()} | {mode}",
            ]
            if outcome == "completed":
                if required_node_ids is not None and eligible_node_ids is not None:
                    lines.append(f"Ready nodes: {len(eligible_node_ids)}/{len(required_node_ids)}")
                if missing:
                    lines.append("Unavailable nodes: " + _ids_summary(missing))
                lines.append(f"Users counted: {users_counted} | Protected users: {protected_users_count}")
                if not coverage_complete:
                    lines.append("Warnings for absent or below-limit users are preserved.")
            else:
                lines.append("Reason: " + safe_reason)
            if math.isfinite(duration_seconds) and duration_seconds >= 0:
                lines.append(f"Duration: {duration_seconds:.1f}s")
            if await self._send(admission, "\n".join(lines)):
                self._cycle_state = state


NODE_HEALTH_NOTIFICATIONS = NodeHealthNotifications()


async def report_node_health(
    admission: MonitoringAdmission, *, nodes=None, evidence=None, observed_node_ids=None
) -> None:
    """Evaluate node health outside the access-record ingestion path."""
    from utils.log_observations import LOG_OBSERVATIONS

    await NODE_HEALTH_NOTIFICATIONS.report_nodes(
        LOG_OBSERVATIONS, admission, nodes=nodes, evidence=evidence, observed_node_ids=observed_node_ids
    )


async def report_cycle(admission: MonitoringAdmission, source: str, outcome: str, **kwargs) -> None:
    """Existing cycle coordinator supplies the captured final scan metadata."""
    await NODE_HEALTH_NOTIFICATIONS.report_cycle(admission, source, outcome, **kwargs)
