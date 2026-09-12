"""Atomic, cross-process-safe JSON persistence utilities."""

import copy
import fcntl
import json
import os
import tempfile
from typing import Any, Callable

_MISSING = object()


class JsonSnapshot(dict):
    """A dict carrying the baseline needed to merge a later stale write."""

    def __init__(self, data: dict):
        super().__init__(copy.deepcopy(data))
        self._baseline = copy.deepcopy(data)

    def commit(self, latest: dict) -> None:
        """Apply only this snapshot's changes to the latest on-disk value."""
        _merge_changes(latest, self._baseline, self)


def _merge_changes(latest: dict, baseline: dict, edited: dict) -> None:
    for key in baseline.keys() - edited.keys():
        latest.pop(key, None)
    for key, value in edited.items():
        if key not in baseline:
            latest[key] = copy.deepcopy(value)
            continue
        old_value = baseline[key]
        if isinstance(old_value, dict) and isinstance(value, dict):
            current = latest.get(key)
            if not isinstance(current, dict):
                if value != old_value:
                    latest[key] = copy.deepcopy(value)
                continue
            _merge_changes(current, old_value, value)
        elif isinstance(old_value, list) and value != old_value:
            raise ValueError(
                "List mutations require an explicit atomic_update_json mutator"
            )
        elif value != old_value:
            latest[key] = copy.deepcopy(value)


def _lock_path(filepath: str | os.PathLike[str]) -> str:
    return f"{os.path.abspath(filepath)}.lock"


def _load_unlocked(filepath: str, default: Any = _MISSING) -> Any:
    try:
        with open(filepath, "r", encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return copy.deepcopy(default)


def _write_unlocked(
    filepath: str,
    data: Any,
    *,
    indent: int,
    ensure_ascii: bool,
) -> None:
    directory = os.path.dirname(filepath)
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(data, target, indent=indent, ensure_ascii=ensure_ascii)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, filepath)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json_snapshot(
    filepath: str | os.PathLike[str],
    *,
    default: Any = _MISSING,
) -> JsonSnapshot:
    """Read a dict under the same stable lock used by updates."""
    absolute = os.path.abspath(filepath)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(_lock_path(absolute), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            data = _load_unlocked(absolute, default)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if not isinstance(data, dict):
        raise TypeError(f"Expected a JSON object in {filepath}")
    return JsonSnapshot(data)


def atomic_update_json(
    filepath: str | os.PathLike[str],
    mutator: Callable[[dict], dict | None],
    *,
    default: Any = _MISSING,
    indent: int = 2,
    ensure_ascii: bool = True,
) -> dict:
    """Lock, read, mutate, fsync, and replace one JSON object transactionally."""
    absolute = os.path.abspath(filepath)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(_lock_path(absolute), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = _load_unlocked(absolute, default)
            if not isinstance(current, dict):
                raise TypeError(f"Expected a JSON object in {filepath}")
            working = copy.deepcopy(current)
            replacement = mutator(working)
            if replacement is not None:
                working = replacement
            if not isinstance(working, dict):
                raise TypeError("JSON mutator must produce a dict")
            snapshot = copy.deepcopy(dict(working))
            _write_unlocked(
                absolute,
                snapshot,
                indent=indent,
                ensure_ascii=ensure_ascii,
            )
            return copy.deepcopy(snapshot)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write_json(
    filepath: str | os.PathLike[str],
    data: Any,
    indent: int = 2,
    ensure_ascii: bool = True,
):
    """Replace one JSON object while holding its stable sidecar lock."""
    replacement = copy.deepcopy(data)

    def replace(_current: dict) -> dict:
        return replacement

    return atomic_update_json(
        filepath,
        replace,
        default={},
        indent=indent,
        ensure_ascii=ensure_ascii,
    )
