"""Carry an enforcement decision through asynchronous punishment preparation."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class EnforcementProtected(Exception):
    """The original decision no longer authorizes a new panel action."""


@dataclass
class EnforcementGuard:
    revalidate: Callable[[], Awaitable[bool]]
    is_current: Callable[[], bool]
    started: bool = False


_guard: ContextVar[EnforcementGuard | None] = ContextVar("enforcement_guard", default=None)


@contextmanager
def enforcement_guard(revalidate: Callable[[], Awaitable[bool]], is_current: Callable[[], bool]) -> Iterator[None]:
    """Keep public punishment callback signatures unchanged and isolate callers."""
    token = _guard.set(EnforcementGuard(revalidate, is_current))
    try:
        yield
    finally:
        _guard.reset(token)


async def validate_enforcement() -> None:
    """Refresh canonical facts, then check original admission without another await.

    Direct management has no guard. Once a panel effect starts, the incident
    continues to own its remaining effects and reconciliation.
    """
    guard = _guard.get()
    if guard is None or guard.started:
        return
    try:
        allowed = await guard.revalidate()
    except Exception as error:
        raise EnforcementProtected("Current enforcement policy is unavailable") from error
    if not allowed or not guard.is_current():
        raise EnforcementProtected("Original enforcement decision is no longer current")


def start_enforcement_effect() -> None:
    """Acquire incident ownership synchronously at the first panel mutation."""
    guard = _guard.get()
    if guard is not None and not guard.started:
        if not guard.is_current():
            raise EnforcementProtected("Original enforcement decision is no longer current")
        guard.started = True


def own_returned_enforcement_effect(succeeded: bool) -> None:
    """Retain ownership from a confirmed successful adapter effect."""
    guard = _guard.get()
    if guard is not None and succeeded:
        guard.started = True
