"""
Atomic file I/O utilities for safe JSON persistence.

Provides atomic write operations to prevent file corruption from:
- Process crashes mid-write
- Concurrent write attempts

Uses a temporary file + os.replace() pattern which is atomic on POSIX systems.
"""

import json
import os
import tempfile


def atomic_write_json(filepath: str, data, indent: int = 2, ensure_ascii: bool = True):
    """
    Write JSON data to a file atomically.

    Uses a temporary file in the same directory and os.replace() to ensure
    the target file is never left in a partially-written state, even if the
    process crashes mid-write.

    Args:
        filepath: Path to the target JSON file.
        data: Data to serialize as JSON.
        indent: JSON indentation level.
        ensure_ascii: Whether to escape non-ASCII characters.
    """
    abs_filepath = os.path.abspath(filepath)
    dir_name = os.path.dirname(abs_filepath)
    os.makedirs(dir_name, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_filepath)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
