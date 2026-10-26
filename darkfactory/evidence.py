"""Bounded browser evidence reads; never follow agent-created symbolic links."""

import mimetypes
import os
import stat
from pathlib import Path

MAX_FILE = 25 * 1024 * 1024
MAX_ENTRIES = 200
MAX_SCANNED = 2000


def _directory(root: Path, parts=()):
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def read(root: Path, name: str) -> tuple[bytes, str]:
    parts = name.split("/")
    if (
        len(name) > 500
        or len(parts) > 12
        or any(p in {"", ".", ".."} or "\\" in p or "\0" in p for p in parts)
    ):
        raise ValueError("Invalid evidence path")
    directory = _directory(root, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ValueError("Evidence file too large or not a regular file")
            data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise ValueError("Evidence file too large")
        return data, mimetypes.guess_type(name)[0] or "application/octet-stream"
    finally:
        os.close(directory)


def read_lines(root: Path, name: str, offset: int, limit: int) -> tuple[bytes, int, bool]:
    """Whole lines from byte `offset`, at most `limit` bytes: the lines, the offset after them, and
    whether the window was full (more may follow).

    A line still being written (no trailing newline yet) is left for the next read.
    """
    parts = name.split("/")
    if len(name) > 500 or any(p in {"", ".", ".."} or "\\" in p or "\0" in p for p in parts):
        raise ValueError("Invalid evidence path")
    directory = _directory(root, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Evidence file is not a regular file")
            stream.seek(offset)
            data = stream.read(limit)
    finally:
        os.close(directory)
    end = data.rfind(b"\n") + 1
    if not end and len(data) == limit:
        # One line longer than the window: skip it rather than stall the cursor.
        return b"", offset + len(data), True
    return data[:end], offset + end, len(data) == limit


def listing(root: Path) -> dict:
    items, scanned, pending = [], 0, [()]
    while pending and scanned < MAX_SCANNED and len(items) < MAX_ENTRIES:
        parts = pending.pop()
        try:
            directory = _directory(root, parts)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_SCANNED or len(items) >= MAX_ENTRIES:
                            break
                        name = "/".join((*parts, entry.name))
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False) and len(parts) < 11:
                            pending.append((*parts, entry.name))
                        elif entry.is_file(follow_symlinks=False):
                            info = entry.stat(follow_symlinks=False)
                            if info.st_size <= MAX_FILE:
                                items.append({"name": name, "bytes": info.st_size})
            finally:
                os.close(directory)
        except OSError:
            continue
    return {
        "files": sorted(items, key=lambda item: item["name"]),
        "truncated": bool(pending) or scanned >= MAX_SCANNED or len(items) >= MAX_ENTRIES,
    }
