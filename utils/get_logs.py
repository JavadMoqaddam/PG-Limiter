"""
This module contains functions to get logs from the nodes using SSE (Server-Sent Events).
"""

import asyncio
import os
import time
from asyncio import Task
from datetime import datetime

from utils.parse_logs import INVALID_IPS

try:
    import httpx
except ImportError as exc:
    raise ImportError("Module 'httpx' is not installed. Use: 'pip install httpx'") from exc
from telegram_bot.send_message import send_logs, edit_message
from utils.logs import logger  # pylint: disable=ungrouped-imports
from utils.panel_api import get_nodes, get_token
from utils.parse_logs import parse_logs, set_current_node_info
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
    api_mode = all(
        "🛰️" in info["status"] for info in _node_connection_status.values()
    )
    title = (
        "🛰️ <b>IP Source: Panel API</b>"
        if api_mode
        else "🔄 <b>SSE Node Connections</b>"
    )
    lines = [f"{title} - {time_str}\n"]
    
    for node_id in sorted(_node_connection_status.keys()):
        info = _node_connection_status[node_id]
        lines.append(f"  {info['status']} Node {node_id}: <code>{info['name']}</code>")
    
    if api_mode:
        lines.append(
            f"\n📊 Nodes: {len(_node_connection_status)} | "
            "IPs are collected from the panel API each check cycle"
        )
        return "\n".join(lines)

    # Count statuses
    connected = sum(1 for info in _node_connection_status.values() if "✅" in info['status'])
    connecting = sum(1 for info in _node_connection_status.values() if "⏳" in info['status'])
    failed = sum(1 for info in _node_connection_status.values() if "❌" in info['status'])
    
    lines.append(f"\n📊 Connected: {connected} | Connecting: {connecting} | Failed: {failed}")
    
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


async def _delayed_status_update(delay: float) -> None:
    """Flush the latest node connection status to Telegram after the throttle window ends."""
    global _last_status_edit_time, _last_status_text, _node_status_message_id
    try:
        await asyncio.sleep(delay)
        message = await _build_node_status_message()
        if message != _last_status_text and _node_status_message_id:
            _last_status_edit_time = time.time()
            _last_status_text = message
            await edit_message(_node_status_message_id, message)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"Delayed node status update note: {e}")


_status_lock = asyncio.Lock()
_status_initializing: bool = False


async def _update_node_status(node_id: int, node_name: str, status: str) -> None:
    """Update the status of a node in memory and refresh the message with rate throttling and trailing flush."""
    global _node_connection_status, _node_status_message_id, _last_status_edit_time, _last_status_text, _pending_status_update_task, _status_initializing
    
    _node_connection_status[node_id] = {"name": node_name, "status": status}
    
    # Throttle edits: sync with check_interval (default 60s)
    now = time.time()
    throttle_interval = await _get_status_throttle_interval()
    elapsed = now - _last_status_edit_time
    
    if _status_initializing:
        # Initial status message is in flight; trailing update will flush latest states
        return
        
    if _node_status_message_id and elapsed < throttle_interval:
        # Schedule trailing update so latest states are flushed at the end of the throttle window
        if _pending_status_update_task is None or _pending_status_update_task.done():
            _pending_status_update_task = asyncio.create_task(_delayed_status_update(throttle_interval - elapsed))
        return
    
    async with _status_lock:
        message = await _build_node_status_message()
        if message == _last_status_text:
            return
        
        _last_status_edit_time = now
        _last_status_text = message
        
        if _node_status_message_id:
            # Try to edit the existing message
            result = await edit_message(_node_status_message_id, message)
            if not result:
                # If edit fails, send a new message
                _node_status_message_id = await send_logs(message, return_message_id=True)
        else:
            # Send initial message (guarded against concurrent duplicate sends)
            _status_initializing = True
            try:
                _node_status_message_id = await send_logs(message, return_message_id=True)
            finally:
                _status_initializing = False


async def init_node_status_message(nodes: list) -> None:
    """Initialize the status message with all nodes showing as connecting."""
    global _node_connection_status, _node_status_message_id, _status_initializing, _last_status_edit_time, _last_status_text
    
    _node_connection_status = {}
    _node_status_message_id = None
    
    for node in nodes:
        if node.status == "connected":
            _node_connection_status[node.node_id] = {
                "name": node.node_name,
                "status": "⏳ Connecting..."
            }
    
    if _node_connection_status:
        message = await _build_node_status_message()
        _last_status_edit_time = time.time()
        _last_status_text = message
        _status_initializing = True
        try:
            _node_status_message_id = await send_logs(message, return_message_id=True)
        finally:
            _status_initializing = False


async def get_nodes_logs(panel_data: PanelType, node: NodeType) -> None:
    """
    This function establishes an SSE connection to a specific node and retrieves logs.
    Automatically waits for panel to be available during restarts.

    Args:
        panel_data (PanelType): The credentials for the panel.
        node (NodeType): The specific node to connect to.

    Raises:
        ValueError: If there is an issue with getting the panel token.
    """
    from utils.panel_api.request_helper import wait_for_panel, is_panel_available
    
    global _node_connection_status
    
    # Set current node information for log parsing
    await set_current_node_info(node.node_id, node.node_name)
    
    consecutive_failures = 0
    max_failures_before_wait = 3  # Wait for panel after 3 consecutive connection failures
    
    while True:
        # API mode owns the IP collection, so the SSE stream must stay closed;
        # otherwise both writers would feed ACTIVE_USERS and double-count
        # devices. The task idles instead of exiting so that switching back to
        # log mode from Telegram resumes streaming without a restart.
        if await _ip_source_is_api():
            await _update_node_status(node.node_id, node.node_name, "🛰️ API mode")
            await asyncio.sleep(10)
            continue

        # If we've had multiple consecutive failures, panel might be restarting
        if consecutive_failures >= max_failures_before_wait:
            await _update_node_status(node.node_id, node.node_name, "⏳ Waiting for panel...")
            logger.warning(f"Node {node.node_id} had {consecutive_failures} failures, waiting for panel to be available...")
            
            if await wait_for_panel(panel_data):
                logger.info(f"Panel is back, resuming SSE connection for node {node.node_id}")
                consecutive_failures = 0
            else:
                logger.error(f"Panel still unavailable, will retry node {node.node_id} connection")
                consecutive_failures = 0  # Reset and try again
                await asyncio.sleep(30)
                continue
        
        # get_token raises on failure. This call sat outside the try below, so a panel
        # that refused to authenticate killed this node's task - and with it, being a
        # TaskGroup child, the whole process. Retry in place instead: the loop already
        # handles panel unavailability a few lines up.
        try:
            get_panel_token = await get_token(panel_data)
        except Exception as token_error:  # pylint: disable=broad-except
            logger.error(
                f"Could not obtain a panel token for node {node.node_id}: {token_error}. "
                f"Retrying in 30s."
            )
            await asyncio.sleep(30)
            continue
        token = get_panel_token.panel_token
        
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
                
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code}", 
                            request=response.request, 
                            response=response
                        )
                    
                    # Connection successful, reset failure counter
                    consecutive_failures = 0
                    
                    # Update status to connected
                    await _update_node_status(node.node_id, node.node_name, "✅ Connected")

                    # Seed the heartbeat so a node that has just connected is not
                    # immediately judged stale by the enforcement cycle. Every line
                    # below refreshes it; nothing else in this module proves the
                    # stream is still delivering, because timeout=None means a
                    # half-open connection never raises.
                    note_node_event(node.node_id)

                    # Clear stale/ghost connections for this specific node BEFORE reading fresh stream
                    from utils.parse_logs import clear_node_active_connections
                    cleared_count = await clear_node_active_connections(node.node_id)
                    if cleared_count > 0:
                        logger.info(f"🧹 Cleared {cleared_count} stale connections for node {node.node_id} ({node.node_name}) before reading SSE stream")

                    next_mode_check = time.time() + _IP_SOURCE_RECHECK_SECONDS
                    async for line in response.aiter_lines():
                        # Honour a runtime switch to API mode: the stream is
                        # abandoned so the outer loop can park the task.
                        now = time.time()
                        # Any line at all counts, including the keep-alives that
                        # carry no "data:" payload: this answers "is the stream
                        # alive", not "did a user connect". Counting only parsed
                        # events would read a busy node whose lines are all
                        # filtered out as dead.
                        note_node_event(node.node_id, now)
                        if now >= next_mode_check:
                            next_mode_check = now + _IP_SOURCE_RECHECK_SECONDS
                            if await _ip_source_is_api():
                                logger.info(
                                    f"🛰️ IP source switched to API, closing SSE stream "
                                    f"for node {node.node_id} ({node.node_name})"
                                )
                                await clear_node_active_connections(node.node_id)
                                forget_node_event(node.node_id)
                                break

                        if line.startswith("data: "):
                            log_data = line[6:]  # Remove "data: " prefix
                            if log_data.strip():  # Only process non-empty log data
                                await parse_logs(log_data, node.node_id, node.node_name)
                                
        except httpx.HTTPStatusError as error:
            consecutive_failures += 1
            await _update_node_status(node.node_id, node.node_name, "❌ HTTP Error")
            logger.error(f"HTTP error connecting to node {node.node_id}: {error}")
            retry_delay = min(60, 5 * (2 ** min(consecutive_failures - 1, 4)))
            await asyncio.sleep(retry_delay)
            await _update_node_status(node.node_id, node.node_name, "⏳ Reconnecting...")
            continue
        
        except httpx.ConnectError as error:
            consecutive_failures += 1
            await _update_node_status(node.node_id, node.node_name, "❌ Connection Error")
            logger.error(f"Connection error for node {node.node_id}: {error}")
            # Shorter sleep if panel might be restarting
            retry_delay = min(30, 3 * (2 ** min(consecutive_failures - 1, 3))) if consecutive_failures < max_failures_before_wait else 2
            await asyncio.sleep(retry_delay)
            await _update_node_status(node.node_id, node.node_name, "⏳ Reconnecting...")
            continue
            
        except Exception as error:  # pylint: disable=broad-except
            consecutive_failures += 1
            await _update_node_status(node.node_id, node.node_name, "❌ Failed")
            logger.error(f"Failed to connect to node {node.node_id}: {error}")
            retry_delay = min(60, 5 * (2 ** min(consecutive_failures - 1, 4)))
            await asyncio.sleep(retry_delay)
            await _update_node_status(node.node_id, node.node_name, "⏳ Reconnecting...")
            continue


async def handle_cancel(panel_data: PanelType, tasks: list[Task]) -> None:
    """
    An asynchronous coroutine that cancels tasks for disconnected nodes.

    Args:
        panel_data (PanelType): The credentials for the panel.
        tasks (list[Task]): The list of tasks to be cancelled.
    """
    global _node_connection_status

    deactivate_nodes = {}  # task_name -> node_id
    while True:
        poll_interval = await _get_node_poll_interval()
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
        except Exception as error:  # pylint: disable=broad-except
            logger.warning(
                f"Could not fetch the node list to look for disconnected nodes: {error}. "
                f"Retrying on the next pass; existing streams are left alone."
            )
            await asyncio.sleep(poll_interval)
            continue
        silence_window = poll_interval
        event_ages = get_node_event_ages()
        for node in nodes_list or []:
            task_name = f"Task-{node.node_id}-{node.node_name}"
            if node.status != "connected":
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
            if task_name in deactivate_nodes:
                node_id = deactivate_nodes[task_name]
                logger.info(f"Cancelling disconnected node task: {task_name}")
                
                # Update status to show disconnected
                if node_id in _node_connection_status:
                    await _update_node_status(node_id, _node_connection_status[node_id]["name"], "⚫ Disconnected")
                
                # Clear active connections for this deactivated node
                from utils.parse_logs import clear_node_active_connections
                await clear_node_active_connections(node_id)

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
        await asyncio.sleep(poll_interval)


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
        if await _ip_source_is_api():
            logger.debug("🛰️ Skipping SSE refresh: IP source is API mode")
            continue

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[{time_str}] Restarting all SSE connections (2-hour refresh)")
        await send_logs(f"🔄 <b>SSE Refresh</b> - {time_str}\n\nRestarting all node connections...")
        
        # Cancel all existing node tasks
        for task in list(tasks):
            if task.get_name().startswith("Task-"):
                task.cancel()
                tasks.remove(task)
                if task in task_node_mapping:
                    task_node_mapping.pop(task)
        
        # Reset status tracking
        _node_status_message_id = None
        _node_connection_status = {}

        # Every stream is about to be rebuilt, so the old heartbeats describe
        # tasks that no longer exist. Leaving them would make the next cycle read
        # the whole fleet as stale during the rebuild and skip enforcement.
        clear_node_events()
        
        # Small delay to let tasks clean up
        await asyncio.sleep(2)
        
        # Fetch fresh node list. get_nodes raises on failure rather than returning a
        # ValueError, so an unguarded call here could take the whole TaskGroup - and
        # therefore the process - down while rebuilding the streams.
        try:
            nodes_list = await get_nodes(panel_data)
        except Exception as error:  # pylint: disable=broad-except
            logger.error(
                f"Could not fetch the node list while rebuilding SSE streams: {error}. "
                f"No streams were recreated on this pass."
            )
            nodes_list = None
        if nodes_list:
            # Initialize status message for all nodes
            await init_node_status_message(nodes_list)
            
            # Create new tasks for all connected nodes
            for node in nodes_list:
                if node.status == "connected":
                    await create_node_task(panel_data, tg, node)
                    await asyncio.sleep(1)  # Small delay between node connections
        
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
        try:
            # Clean up completed or cancelled tasks from mapping
            dead_tasks = [t for t in list(TASKS) if t.done()]
            for t in dead_tasks:
                TASKS.remove(t)
                task_node_mapping.pop(t, None)

            active_node_ids = {n.node_id for t, n in task_node_mapping.items() if not t.done()}

            # force_refresh for the same reason as in handle_cancel: get_nodes caches
            # for an hour, so without it a node that came back stayed invisible here
            # until the cache expired. enabled_only=False keeps the status field
            # authoritative for every node instead of silently omitting disabled ones -
            # the "connected" test below is what decides whether to reconnect.
            all_nodes = await get_nodes(panel_data, force_refresh=True, enabled_only=False)
            if all_nodes and not isinstance(all_nodes, ValueError):
                for node in all_nodes:
                    if (
                        node.node_id not in active_node_ids
                        and node.status == "connected"
                    ):
                        _node_connection_status[node.node_id] = {
                            "name": node.node_name,
                            "status": "⏳ Connecting..."
                        }
                        
                        logger.info(f"Add a new node. id: {node.node_id} name: {node.node_name}")
                        try:
                            await _update_node_status(node.node_id, node.node_name, "⏳ Connecting...")
                        except Exception as update_err:
                            logger.warning(f"Failed updating node status: {update_err}")
                        await create_node_task(panel_data, tg, node)
                        active_node_ids.add(node.node_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in check_and_add_new_nodes: {e}")

        await asyncio.sleep(await _get_node_poll_interval())


async def create_node_task(
    panel_data: PanelType, tg: asyncio.TaskGroup, node: NodeType
) -> None:
    """
    An asynchronous coroutine that creates a new task for a node and adds it to the TASKS list.

    Args:
        panel_data (PanelType): The credentials for the panel.
        tg (asyncio.TaskGroup): The TaskGroup to which the new task will be added.
        node (NodeType): The node for which the new task will be created.
    """
    INVALID_IPS.add(node.node_ip)
    task = tg.create_task(
        get_nodes_logs(panel_data, node), name=f"Task-{node.node_id}-{node.node_name}"
    )
    TASKS.append(task)
    task_node_mapping[task] = node
