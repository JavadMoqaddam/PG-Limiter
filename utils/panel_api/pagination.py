"""Serialize user-page HTTP attempts and leave a pause after each completion."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

PAGE_COMPLETION_PAUSE = 1.0


class _PageGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.ready_at = 0.0


_gates: WeakKeyDictionary = WeakKeyDictionary()


@asynccontextmanager
async def page_request_slot():
    """Share pacing between sync, candidate pages, membership checks and retries."""
    loop = asyncio.get_running_loop()
    gate = _gates.setdefault(loop, _PageGate())
    async with gate.lock:
        delay = gate.ready_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            yield
        finally:
            gate.ready_at = loop.time() + PAGE_COMPLETION_PAUSE
