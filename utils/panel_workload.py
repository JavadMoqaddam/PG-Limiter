"""Serialize full user synchronization and API IP observations in the daemon."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from utils.logs import get_logger

_logger = get_logger("panel_workload")
_workload_lock = asyncio.Lock()
_active_workload = ""


@asynccontextmanager
async def serialized_panel_workload(workload: str) -> AsyncIterator[None]:
    """Wait fairly for panel work without consuming an API request timeout.

    Acquire before taking observation timestamps or opening DB transactions.
    Callers must revalidate admission/configuration after this cancellable wait.
    This is not reentrant and must not wrap individual fan-out requests: the
    owning collector already holds the slot until all its child requests end.
    """
    global _active_workload

    waiting_since = None
    if _workload_lock.locked():
        waiting_since = time.monotonic()
        _logger.info("Panel workload waiting: %s queued behind %s", workload, _active_workload)
    async with _workload_lock:
        _active_workload = workload
        try:
            if waiting_since is not None:
                _logger.info(
                    "Panel workload resumed: %s after %.1fs waiting", workload, time.monotonic() - waiting_since
                )
            yield
        finally:
            _active_workload = ""
