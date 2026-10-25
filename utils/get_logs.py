"""
This module contains functions to get logs from the nodes using SSE (Server-Sent Events).
"""

import asyncio
import html
import os
import time
from asyncio import Task
from datetime import datetime
from contextlib import asynccontextmanager

from utils.device_count import COUNT_MODE_INBOUND
from utils.parse_logs import INVALID_IPS
from utils.log_observations import LOG_OBSERVATIONS
from utils import node_health_notifications
from utils.monitoring_gate import (
    MonitoringAdmission,
    monitoring_is_current,
    monitoring_snapshot,
    synchronize_monitoring,
    wait_for_monitoring_change,
)

try:
    import httpx
except ImportError as exc:
    raise ImportError("Module 'httpx' is not installed. Use: 'pip install httpx'") from exc
from telegram_bot.send_message import send_logs, edit_message
from utils.logs import logger  # pylint: disable=ungrouped-imports
from utils.panel_api import get_nodes, get_token
from utils.parse_logs import clear_node_active_connections, parse_logs, set_current_node_info
from utils.shared_state import (
    clear_node_events,
    forget_node_event,
    get_node_event_ages,
    node_silence_window,
    note_node_event,
)
from utils.types import NodeType, PanelType

TASKS = []

task_node_mapping = {}
task_monitoring_admissions = {}

# Track the status message for node connections
_node_status_message_id = None
_node_connection_status = {}  # node_id -> {"name": str, "status": str}


async def _build_node_status_message() -> str:
    """Build a formatted message showing all node connection statuses."""
    global _node_connection_status

    if not _node_connection_status:
        return "🔄 <b>SSE Node Connections</b>\n\nNo nodes to connect."

    time_str = datetime.now().strftime("%H:%M:%S")
    # When every node reports the API-mode marker the streams are intentionally
    # closed, so the header says so instead of implying a broken SSE.
    api_mode = all("🛰️" in info["status"] for info in _node_connection_status.values())
    title = "🛰️ <b>IP Source: Panel API</b>" if api_mode else "🔄 <b>SSE Node Connections</b>"
    lines = [f"{title} - {time_str}\n"]

    for node_id in sorted(_node_connection_status.keys()):
        info = _node_connection_status[node_id]
        lines.append(f"  {info['status']} Node {node_id}: <code>{html.escape(info['name'])}</code>")

    if api_mode:
        lines.append(
            f"\n📊 Nodes: {len(_node_connection_status)} | IPs are collected from the panel API each check cycle"
        )
        return "\n".join(lines)

    # Count statuses
    connected = sum(1 for info in _node_connection_status.values() if "✅" in info["status"])
    connecting = sum(1 for info in _node_connection_status.values() if "⏳" in info["status"])
    failed = sum(1 for info in _node_connection_status.values() if "❌" in info["status"])

    lines.append(f"\n📊 Connected: {connected} | Connecting: {connecting} | Failed: {failed}")

    evidence = LOG_OBSERVATIONS.evidence()
    reason = evidence.unavailable_reason
    lines.append("\nLogs evidence: " + (html.escape(reason) if reason else "ready"))
    if evidence.required_node_ids is not None and evidence.eligible_node_ids is not None:
        coverage = "complete" if evidence.coverage_complete else "partial"
        lines.append(
            f"Ready nodes: {len(evidence.eligible_node_ids)}/{len(evidence.required_node_ids)} | Coverage: {coverage}"
        )
    return "\n".join(lines)


_last_status_edit_time = 0.0
_last_status_text = ""
_pending_status_update_task: asyncio.Task | None = None

# How often a live SSE stream re-checks whether the IP source was switched
# to API mode from the Telegram bot.
_IP_SOURCE_RECHECK_SECONDS = 15.0


async def _ip_source_is_api() -> bool:
    """
    Report whether the limiter is currently configured to pull IPs from the API.

    Uses the scalar reader rather than ``read_config()``: this is polled from inside
    every node's streaming loop, and a full ``read_config()`` would deep-copy the
    whole configuration ~588 times per cycle on a 49-node fleet to look at one string.
    """
    from utils.read_config import read_config_scalar

    try:
        source = await read_config_scalar("ip_source", "logs")
    except Exception:  # pylint: disable=broad-except
        return False
    return str(source or "logs") == "api"


async def _get_status_throttle_interval() -> float:
    """Get node status edit throttle interval (synced with check_interval, default 60s)."""
    try:
        from utils.read_config import read_config_scalar

        # read_config always sets this at the root and derives monitoring.check_interval
        # from it, so the old nested fallback could never fire.
        return float(await read_config_scalar("check_interval", 60) or 60)
    except Exception:
        return 60.0


async def _get_node_poll_interval() -> float:
    """
    How long to wait between two passes over the panel's node list.

    Three check intervals, the same window `_sample_is_trustworthy` uses to call a
    stream silent, so the poller that reconnects a node and the gate that distrusts
    its absence cannot disagree about how long silence is allowed to last.
    """
    return node_silence_window(await _get_status_throttle_interval())


async def _delayed_status_update(delay: float, admission: MonitoringAdmission) -> None:
    """Flush the latest node connection status to Telegram after the throttle window ends."""
    global _last_status_edit_time, _last_status_text, _node_status_message_id
    try:
        await asyncio.sleep(delay)
        if not monitoring_is_current(admission):
            return
        message = await _build_node_status_message()
        if monitoring_is_current(admission) and message != _last_status_text and _node_status_message_id:
            _last_status_edit_time = time.time()
            _last_status_text = message
            await edit_message(_node_status_message_id, message)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"Delayed node status update note: {e}")


_status_lock = asyncio.Lock()
_status_initializing: bool = False


async def _report_node_health(admission, *, nodes=None, observed_node_ids=None) -> None:
    """Keep diagnostic admission failures outside transport error handling."""
    try:
        await node_health_notifications.report_node_health(admission, nodes=nodes, observed_node_ids=observed_node_ids)
    except Exception:
        logger.warning("Could not evaluate node health for General; transport monitoring continues")


async def _report_polled_node_health(nodes, admission) -> None:
    """Do not label a node's first pending connection as a transport incident."""
    observed = {
        node.node_id
        for node in nodes or ()
        if node.status != "connected"
        or (
            node.node_id in _node_connection_status
            and _node_connection_status[node.node_id]["status"] != "⏳ Connecting..."
        )
    }
    await _report_node_health(admission, nodes=nodes, observed_node_ids=observed)


async def _update_node_status(
    node_id: int, node_name: str, status: str, admission: MonitoringAdmission | None = None
) -> None:
    """Update the status of a node in memory and refresh the message with rate throttling and trailing flush."""
    global \
        _node_connection_status, \
        _node_status_message_id, \
        _last_status_edit_time, \
        _last_status_text, \
        _pending_status_update_task, \
        _status_initializing

    if admission is None:
        admission = monitoring_snapshot()
    if not monitoring_is_current(admission):
        return
    _node_connection_status[node_id] = {"name": node_name, "status": status}
    if "❌" in status or "⚫" in status:
        await _report_node_health(
            admission,
            nodes=[NodeType(node_id, node_name, "", "connected")],
            observed_node_ids={node_id},
        )

    # Throttle edits: sync with check_interval (default 60s)
    now = time.time()
    throttle_interval = await _get_status_throttle_interval()
    if not monitoring_is_current(admission):
        return
    elapsed = now - _last_status_edit_time

    if _status_initializing:
        # Initial status message is in flight; trailing update will flush latest states
        return

    if _node_status_message_id and elapsed < throttle_interval:
        # Schedule trailing update so latest states are flushed at the end of the throttle window
        if _pending_status_update_task is None or _pending_status_update_task.done():
            _pending_status_update_task = asyncio.create_task(
                _delayed_status_update(throttle_interval - elapsed, admission)
            )
        return

    async with _status_lock:
        message = await _build_node_status_message()
        if not monitoring_is_current(admission) or message == _last_status_text:
            return

        _last_status_edit_time = now
        _last_status_text = message

        if _node_status_message_id:
            # Try to edit the existing message
            result = await edit_message(_node_status_message_id, message)
            if not result and monitoring_is_current(admission):
                # If edit fails, send a new message
                _node_status_message_id = await send_logs(message, return_message_id=True)
        else:
            # Send initial message (guarded against concurrent duplicate sends)
            _status_initializing = True
            try:
                _node_status_message_id = await send_logs(message, return_message_id=True)
            finally:
                _status_initializing = False


async def _refresh_log_config() -> None:
    """Apply committed scope changes before registering or opening node streams."""
    from utils.read_config import read_config, get_config_revision
    from utils.shared_state import ACTIVE_USERS, ACTIVE_USERS_LOCK

    config = await read_config()
    async with ACTIVE_USERS_LOCK:
        if config.get("_revision", get_config_revision()) == get_config_revision():
            if LOG_OBSERVATIONS.configure(config):
                ACTIVE_USERS.clear()


async def init_node_status_message(nodes: list, admission: MonitoringAdmission | None = None) -> None:
    """Initialize the status message with monitored nodes showing as connecting."""
    global \
        _node_connection_status, \
        _node_status_message_id, \
        _status_initializing, \
        _last_status_edit_time, \
        _last_status_text

    if admission is None:
        admission = monitoring_snapshot()
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return
    await _refresh_log_config()
    if not monitoring_is_current(admission):
        return
    LOG_OBSERVATIONS.register_fleet(nodes)
    await _report_node_health(
        admission,
        nodes=nodes,
        observed_node_ids={node.node_id for node in nodes if node.status != "connected"},
    )
    if not monitoring_is_current(admission):
        return
    _node_connection_status = {}
    _node_status_message_id = None

    for node in nodes:
        if node.status == "connected" and LOG_OBSERVATIONS.monitors(node.node_id):
            _node_connection_status[node.node_id] = {"name": node.node_name, "status": "⏳ Connecting..."}

    if _node_connection_status:
        message = await _build_node_status_message()
        if not monitoring_is_current(admission):
            return
        _last_status_edit_time = time.time()
        _last_status_text = message
        _status_initializing = True
        try:
            _node_status_message_id = await send_logs(message, return_message_id=True)
        finally:
            _status_initializing = False


@asynccontextmanager
async def _track_log_transport(node_id: int, admission: MonitoringAdmission | None = None):
    """A new stream and its closure delimit complete transport observation."""
    if admission is None:
        admission = monitoring_snapshot()
    forget_node_event(node_id)
    try:
        yield
    finally:
        # Runs on EOF, parser/HTTP failures and task cancellation before retries
        # or periodic rebuilding can make the old heartbeat look healthy.
        forget_node_event(node_id)
        await _report_node_health(admission, observed_node_ids={node_id})


async def get_nodes_logs(panel_data: PanelType, node: NodeType, admission: MonitoringAdmission | None = None) -> None:
    """
    This function establishes an SSE connection to a specific node and retrieves logs.
    Automatically waits for panel to be available during restarts.

    Args:
        panel_data (PanelType): The credentials for the panel.
        node (NodeType): The specific node to connect to.
        admission: Original owner's Topics admission, when scheduled by a node poll.

    Raises:
        ValueError: If there is an issue with getting the panel token.
    """
    from utils.panel_api.request_helper import wait_for_panel, is_panel_available
    from utils.read_config import get_config_revision

    global _node_connection_status

    if admission is None:
        admission = monitoring_snapshot()
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return

    # Set current node information for log parsing
    await set_current_node_info(node.node_id, node.node_name)

    consecutive_failures = 0
    max_failures_before_wait = 3  # Wait for panel after 3 consecutive connection failures

    while True:
        await synchronize_monitoring()
        if not monitoring_is_current(admission):
            return
        await _refresh_log_config()
        config_revision = get_config_revision()
        if (
            not monitoring_is_current(admission)
            or node.status != "connected"
            or not LOG_OBSERVATIONS.monitors(node.node_id)
        ):
            return
        # API mode owns the IP collection, so the SSE stream must stay closed;
        # otherwise both writers would feed ACTIVE_USERS and double-count
        # devices. The task idles instead of exiting so that switching back to
        # log mode from Telegram resumes streaming without a restart.
        api_mode = await _ip_source_is_api()
        if not monitoring_is_current(admission):
            return
        if api_mode:
            await _update_node_status(node.node_id, node.node_name, "🛰️ API mode", admission)
            await asyncio.sleep(10)
            continue
        if not monitoring_is_current(admission):
            return

        # If we've had multiple consecutive failures, panel might be restarting
        if consecutive_failures >= max_failures_before_wait:
            await _update_node_status(node.node_id, node.node_name, "⏳ Waiting for panel...", admission)
            logger.warning(
                f"Node {node.node_id} had {consecutive_failures} failures, waiting for panel to be available..."
            )

            if await wait_for_panel(panel_data):
                logger.info(f"Panel is back, resuming SSE connection for node {node.node_id}")
                consecutive_failures = 0
            else:
                logger.error(f"Panel still unavailable, will retry node {node.node_id} connection")
                consecutive_failures = 0  # Reset and try again
                await asyncio.sleep(30)
                continue
        if not monitoring_is_current(admission):
            return

        # get_token raises on failure. This call sat outside the try below, so a panel
        # that refused to authenticate killed this node's task - and with it, being a
        # TaskGroup child, the whole process. Retry in place instead: the loop already
        # handles panel unavailability a few lines up.
        try:
            get_panel_token = await get_token(panel_data)
        except Exception as token_error:  # pylint: disable=broad-except
            logger.error(f"Could not obtain a panel token for node {node.node_id}: {token_error}. Retrying in 30s.")
            await _update_node_status(node.node_id, node.node_name, "❌ Authentication Error", admission)
            await asyncio.sleep(30)
            continue
        token = get_panel_token.panel_token
        if not monitoring_is_current(admission):
            return

        # Determine the scheme based on the domain
        scheme = "http" if panel_data.panel_domain.startswith("http://") else "https"
        base_url = panel_data.panel_domain.replace("https://", "").replace("http://", "")

        try:
            url = f"{scheme}://{base_url}/api/node/{node.node_id}/logs"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            }

            # timeout=None capped nothing at all: a panel that accepted the TCP
            # connection but never completed the TLS handshake held this node's task
            # forever. connect/write/pool are bounded here because none of them can
            # touch an established stream.
            #
            # read is deliberately left unbounded. In httpx one read timeout covers
            # every chunk of the response body, so any value shorter than a node's
            # quietest period would tear down healthy streams - and picking that number
            # needs real traffic, not a guess. A stream that goes silent while still
            # looking connected is caught by the per-node heartbeat gate in
            # check_usage, which blocks counter changes rather than trusting the sample.
            sse_timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
            # Match the API client instead of hardcoding False, so PANEL_VERIFY_SSL
            # covers the log streams too. Same host as the API calls, so a setting that
            # works there works here; the default stays off.
            verify_ssl = os.getenv("PANEL_VERIFY_SSL", "false").lower() in ("true", "1", "yes")
            async with httpx.AsyncClient(verify=verify_ssl, timeout=sse_timeout) as client:
                logger.info(f"Establishing SSE connection for node {node.node_id}: {node.node_name}")

                async with (
                    client.stream("GET", url, headers=headers) as response,
                    _track_log_transport(node.node_id, admission),
                ):
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code}", request=response.request, response=response
                        )

                    # Connection successful, reset failure counter
                    consecutive_failures = 0
                    if not monitoring_is_current(admission):
                        return

                    # Update status to connected
                    await _update_node_status(node.node_id, node.node_name, "✅ Connected", admission)
                    if not monitoring_is_current(admission):
                        return

                    # Seed transport liveness, while starting a complete-interval
                    # warmup. HTTP 200 cannot certify the missing preceding logs.
                    # Every line below refreshes liveness independently of parsing.
                    note_node_event(node.node_id)
                    await _report_node_health(admission, nodes=[node], observed_node_ids={node.node_id})

                    # Keep unconsumed observations across reconnects. Replayed
                    # records are rejected and could not replace a cleared sample.

                    next_mode_check = time.time() + _IP_SOURCE_RECHECK_SECONDS
                    async for line in response.aiter_lines():
                        if not monitoring_is_current(admission):
                            return
                        # Honour a runtime switch to API mode: the stream is
                        # abandoned so the outer loop can park the task.
                        now = time.time()
                        # Any line at all counts, including the keep-alives that
                        # carry no "data:" payload: this answers "is the stream
                        # alive", not "did a user connect". Counting only parsed
                        # events would read a busy node whose lines are all
                        # filtered out as dead.
                        if now >= next_mode_check:
                            next_mode_check = now + _IP_SOURCE_RECHECK_SECONDS
                            # Refresh mutable settings only after a committed change;
                            # ordinary stream pings keep the cheap scalar source read.
                            if config_revision != get_config_revision():
                                await _refresh_log_config()
                                config_revision = get_config_revision()
                            if not LOG_OBSERVATIONS.monitors(node.node_id):
                                break
                            if await _ip_source_is_api():
                                logger.info(
                                    f"🛰️ IP source switched to API, closing SSE stream "
                                    f"for node {node.node_id} ({node.node_name})"
                                )
                                from utils.read_config import read_config_scalar

                                # API cannot supply inbound tags. Preserve this
                                # unevaluated sample while that combination pauses
                                # enforcement instead of discarding it on shutdown.
                                count_mode = (
                                    str(await read_config_scalar("device_count_mode", "device")).strip().lower()
                                )
                                if count_mode != COUNT_MODE_INBOUND:
                                    await clear_node_active_connections(node.node_id)
                                forget_node_event(node.node_id)
                                break
                            if not monitoring_is_current(admission):
                                return

                        note_node_event(node.node_id, now)

                        if line.startswith("data: "):
                            log_data = line[6:]  # Remove "data: " prefix
                            if log_data.strip():  # Only process non-empty log data
                                await parse_logs(log_data, node.node_id, node.node_name)

        except httpx.HTTPStatusError as error:
            consecutive_failures += 1
            await _update_node_status(node.node_id, node.node_name, "❌ HTTP Error", admission)
            logger.error(f"HTTP error connecting to node {node.node_id}: {error}")
            retry_delay = min(60, 5 * (2 ** min(consecutive_failures - 1, 4)))
            await asyncio.sleep(retry_delay)
            await _update_node_status(node.node_id, node.node_name, "⏳ Reconnecting...", admission)
            continue

        except httpx.ConnectError as error:
            consecutive_failures += 1
            await _update_node_status(node.node_id, node.node_name, "❌ Connection Error", admission)
            logger.error(f"Connection error for node {node.node_id}: {error}")
            # Shorter sleep if panel might be restarting
            retry_delay = (
                min(30, 3 * (2 ** min(consecutive_failures - 1, 3)))
                if consecutive_failures < max_failures_before_wait
                else 2
            )
            await asyncio.sleep(retry_delay)
            await _update_node_status(node.node_id, node.node_name, "⏳ Reconnecting...", admission)
            continue

        except Exception as error:  # pylint: disable=broad-except
            consecutive_failures += 1
            await _update_node_status(node.node_id, node.node_name, "❌ Failed", admission)
            logger.error(f"Failed to connect to node {node.node_id}: {error}")
            retry_delay = min(60, 5 * (2 ** min(consecutive_failures - 1, 4)))
            await asyncio.sleep(retry_delay)
            await _update_node_status(node.node_id, node.node_name, "⏳ Reconnecting...", admission)
            continue


async def retire_log_streams(admission=None) -> None:
    """Close all owned SSE transports and abandon their pending status report."""
    global _pending_status_update_task, _node_status_message_id, _last_status_text, _last_status_edit_time
    tasks = [
        task
        for task in list(TASKS)
        if admission is None or task_monitoring_admissions.get(task) != admission or not admission.allowed
    ]
    if _pending_status_update_task is not None:
        tasks.append(_pending_status_update_task)
        _pending_status_update_task = None
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        if task in TASKS:
            TASKS.remove(task)
        task_node_mapping.pop(task, None)
        task_monitoring_admissions.pop(task, None)
    surviving_ids = {node.node_id for task, node in task_node_mapping.items() if not task.done()}
    for node_id in set(_node_connection_status) - surviving_ids:
        _node_connection_status.pop(node_id, None)
    _node_status_message_id = None
    _last_status_text = ""
    _last_status_edit_time = 0.0


async def handle_cancel(panel_data: PanelType, tasks: list[Task]) -> None:
    """
    An asynchronous coroutine that cancels tasks for disconnected nodes.

    Args:
        panel_data (PanelType): The credentials for the panel.
        tasks (list[Task]): The list of tasks to be cancelled.
    """
    global _node_connection_status

    while True:
        admission = await synchronize_monitoring()
        if not monitoring_is_current(admission):
            await wait_for_monitoring_change(admission, 60)
            continue
        poll_interval = await _get_node_poll_interval()
        if not monitoring_is_current(admission):
            continue
        # get_nodes RAISES ValueError when the panel is unreachable or the circuit
        # breaker is open - it never returns one, despite the isinstance() guards
        # scattered around the codebase. This coroutine is a TaskGroup child, so an
        # unguarded raise here cancelled every SSE stream and took the whole process
        # down on a transient panel hiccup. One failed poll must only cost one poll.
        #
        # force_refresh matters: get_nodes caches for an hour, so without it this poll
        # was reading node status up to an hour old and a node that dropped stayed
        # "connected" in our view for the rest of that hour. enabled_only=False matters
        # for the same reason in the other direction - with the default filter a node
        # disabled in the panel vanishes from the list entirely instead of arriving with
        # status "disabled", and the loop below only looks at nodes it can see, so that
        # node's stream was never cancelled at all.
        try:
            nodes_list = await get_nodes(panel_data, force_refresh=True, enabled_only=False)
            if not monitoring_is_current(admission):
                continue
            await _refresh_log_config()
            if not monitoring_is_current(admission):
                continue
            LOG_OBSERVATIONS.register_fleet(nodes_list)
            await _report_polled_node_health(nodes_list, admission)
        except Exception as error:  # pylint: disable=broad-except
            LOG_OBSERVATIONS.register_fleet(None)
            logger.warning(
                f"Could not fetch the node list to look for disconnected nodes: {error}. "
                f"Retrying on the next pass; existing streams are left alone."
            )
            await asyncio.sleep(poll_interval)
            continue
        silence_window = poll_interval
        deactivate_nodes = {}  # Current poll only; excluded nodes may have no task yet.
        event_ages = get_node_event_ages()
        for node in nodes_list or []:
            task_name = f"Task-{node.node_id}-{node.node_name}"
            if node.status != "connected" or not LOG_OBSERVATIONS.monitors(node.node_id):
                deactivate_nodes[task_name] = node.node_id
                continue
            # The panel says this node is fine, so panel-to-node is healthy - but the
            # panel-to-us hop is a different connection, and that is the one that goes
            # half-open. Every line counts as a heartbeat, keep-alives included, so a
            # node with no users still reports in: silence this long means our stream
            # is dead, not that the node is quiet. Cancel it and let
            # check_and_add_new_nodes rebuild it, which is cheaper and more certain
            # than guessing a read timeout that would also tear down healthy streams.
            age = event_ages.get(node.node_id)
            if age is not None and age > silence_window:
                logger.error(
                    f"🔌 Node {node.node_id} ({node.node_name}) is 'connected' on the panel "
                    f"but its log stream has produced nothing for {int(age)}s "
                    f"(limit {int(silence_window)}s) - reconnecting it"
                )
                deactivate_nodes[task_name] = node.node_id

        for task in list(tasks):
            task_name = task.get_name()
            task_node = task_node_mapping.get(task)
            if task_node is not None and not LOG_OBSERVATIONS.monitors(task_node.node_id):
                deactivate_nodes[task_name] = task_node.node_id
            if task_name in deactivate_nodes:
                node_id = deactivate_nodes[task_name]
                logger.info(f"Cancelling disconnected node task: {task_name}")

                # Update status to show disconnected
                if node_id in _node_connection_status:
                    await _update_node_status(
                        node_id, _node_connection_status[node_id]["name"], "⚫ Disconnected", admission
                    )
                if not monitoring_is_current(admission):
                    break

                # The panel says this node is gone, so drop its heartbeat rather
                # than leaving a stale timestamp behind. A stale entry is the
                # signal for a node that should be streaming and is not; a node
                # that is deliberately down must not drag that ratio down.
                forget_node_event(node_id)

                del deactivate_nodes[task_name]
                task.cancel()
                tasks.remove(task)
                if task in task_node_mapping:
                    task_node_mapping.pop(task)
                task_monitoring_admissions.pop(task, None)
        await wait_for_monitoring_change(admission, poll_interval)


async def handle_cancel_one(tasks: list[Task]) -> None:
    """
    *This is used for tests*
    An asynchronous coroutine that cancels just one task in the given list.

    Args:
        tasks (list[Task]): The list of tasks to be cancelled.
    """
    # Since panel no longer provides logs, we cancel the first available task
    if tasks:
        task = tasks[0]
        print(f"Cancelling {task.get_name()}...")
        task.cancel()
        tasks.remove(task)


async def handle_cancel_all(tasks: list[Task], panel_data: PanelType, tg: asyncio.TaskGroup) -> None:
    """
    An asynchronous coroutine that periodically restarts all SSE connections every 2 hours.
    This ensures fresh connections and re-fetches the node list.

    Args:
        tasks (list[Task]): The list of tasks to be cancelled and restarted.
        panel_data (PanelType): The credentials for the panel.
        tg (asyncio.TaskGroup): The TaskGroup to create new tasks in.
    """
    global _node_status_message_id, _node_connection_status

    while True:
        # Wait for 2 hours before restarting all SSE connections
        await asyncio.sleep(2 * 60 * 60)  # 2 hours

        # Nothing to refresh in API mode — the node tasks are parked and no
        # stream is open, so a restart would only produce a misleading
        # "SSE Refresh" notification.
        admission = await synchronize_monitoring()
        if not monitoring_is_current(admission) or await _ip_source_is_api():
            logger.debug("🛰️ Skipping SSE refresh: IP source is API mode")
            continue
        if not monitoring_is_current(admission):
            continue

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[{time_str}] Restarting all SSE connections (2-hour refresh)")
        await send_logs(f"🔄 <b>SSE Refresh</b> - {time_str}\n\nRestarting all node connections...")
        if not monitoring_is_current(admission):
            continue

        # Planned maintenance already has its own General message. Its deliberate
        # stream closures and ordinary fresh-interval warmup are not incidents.
        with node_health_notifications.NODE_HEALTH_NOTIFICATIONS.planned_refresh(LOG_OBSERVATIONS.required_nodes or ()):
            # Cancel all existing node tasks
            for task in list(tasks):
                if task.get_name().startswith("Task-"):
                    task.cancel()
                    tasks.remove(task)
                    if task in task_node_mapping:
                        task_node_mapping.pop(task)
                    task_monitoring_admissions.pop(task, None)

            # Reset status tracking
            _node_status_message_id = None
            _node_connection_status = {}

            # The old heartbeats belong to transports that are being closed.
            clear_node_events()
            LOG_OBSERVATIONS.register_fleet(None)

            # Small delay to let tasks clean up
            await asyncio.sleep(2)
            if not monitoring_is_current(admission):
                continue

            # A failed fetch must not take the TaskGroup down during maintenance.
            try:
                nodes_list = await get_nodes(panel_data)
            except Exception as error:  # pylint: disable=broad-except
                logger.error(
                    f"Could not fetch the node list while rebuilding SSE streams: {error}. "
                    f"No streams were recreated on this pass."
                )
                nodes_list = None
            if nodes_list and monitoring_is_current(admission):
                await init_node_status_message(nodes_list, admission)

                for node in nodes_list:
                    if not monitoring_is_current(admission):
                        break
                    if node.status == "connected":
                        await create_node_task(panel_data, tg, node, admission)
                        await asyncio.sleep(1)

        logger.info(f"SSE connections restarted. Active tasks: {len(tasks)}")


async def check_and_add_new_nodes(panel_data: PanelType, tg: asyncio.TaskGroup) -> None:
    """
    An asynchronous coroutine that checks for new nodes and creates tasks for them.

    Args:
        panel_data (PanelType): The credentials for the panel.
        tg (asyncio.TaskGroup): The TaskGroup to which the new task will be added.
    """
    global _node_connection_status

    while True:
        admission = await synchronize_monitoring()
        if not monitoring_is_current(admission):
            await wait_for_monitoring_change(admission, 60)
            continue
        try:
            # Clean up completed or cancelled tasks from mapping
            dead_tasks = [t for t in list(TASKS) if t.done()]
            for t in dead_tasks:
                TASKS.remove(t)
                task_node_mapping.pop(t, None)
                task_monitoring_admissions.pop(t, None)

            active_node_ids = {n.node_id for t, n in task_node_mapping.items() if not t.done()}

            # force_refresh for the same reason as in handle_cancel: get_nodes caches
            # for an hour, so without it a node that came back stayed invisible here
            # until the cache expired. enabled_only=False keeps the status field
            # authoritative for every node instead of silently omitting disabled ones -
            # the "connected" test below is what decides whether to reconnect.
            all_nodes = await get_nodes(panel_data, force_refresh=True, enabled_only=False)
            if not monitoring_is_current(admission):
                continue
            await _refresh_log_config()
            if not monitoring_is_current(admission):
                continue
            LOG_OBSERVATIONS.register_fleet(all_nodes)
            await _report_polled_node_health(all_nodes, admission)
            if all_nodes and not isinstance(all_nodes, ValueError):
                for node in all_nodes:
                    if not monitoring_is_current(admission):
                        break
                    if (
                        node.node_id not in active_node_ids
                        and node.status == "connected"
                        and LOG_OBSERVATIONS.monitors(node.node_id)
                    ):
                        _node_connection_status[node.node_id] = {"name": node.node_name, "status": "⏳ Connecting..."}

                        logger.info(f"Add a new node. id: {node.node_id} name: {node.node_name}")
                        try:
                            await _update_node_status(node.node_id, node.node_name, "⏳ Connecting...", admission)
                        except Exception as update_err:
                            logger.warning(f"Failed updating node status: {update_err}")
                        await create_node_task(panel_data, tg, node, admission)
                        active_node_ids.add(node.node_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            LOG_OBSERVATIONS.register_fleet(None)
            logger.error(f"Error in check_and_add_new_nodes: {e}")

        await wait_for_monitoring_change(admission, await _get_node_poll_interval())


async def create_node_task(
    panel_data: PanelType, tg: asyncio.TaskGroup, node: NodeType, admission: MonitoringAdmission | None = None
) -> None:
    """
    An asynchronous coroutine that creates a new task for a node and adds it to the TASKS list.

    Args:
        panel_data (PanelType): The credentials for the panel.
        tg (asyncio.TaskGroup): The TaskGroup to which the new task will be added.
        node (NodeType): The node for which the new task will be created.
        admission: Original owner's Topics admission, when the node list was fetched.
    """
    if admission is None:
        admission = monitoring_snapshot()
    await synchronize_monitoring()
    if not monitoring_is_current(admission):
        return
    await _refresh_log_config()
    if (
        not monitoring_is_current(admission)
        or node.status != "connected"
        or not LOG_OBSERVATIONS.monitors(node.node_id)
    ):
        return
    INVALID_IPS.add(node.node_ip)
    task = tg.create_task(get_nodes_logs(panel_data, node, admission), name=f"Task-{node.node_id}-{node.node_name}")
    TASKS.append(task)
    task_node_mapping[task] = node
    task_monitoring_admissions[task] = admission
