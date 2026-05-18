"""Filesystem helpers for durably persisting data to disk."""

import contextlib
import errno
import os
import sys
from typing import IO

# `fcntl` is not available on Windows.
with contextlib.suppress(ImportError):
    import fcntl


def _fsync_fd(fd: IO | int) -> None:
    """Flush the specified file descriptor to durable storage."""
    if sys.platform == "darwin":
        # On macOS, plain `fsync` only reaches the drive's volatile cache.
        fileno = fd if isinstance(fd, int) else fd.fileno()
        try:
            fcntl.fcntl(fileno, fcntl.F_FULLFSYNC)
        except OSError as exc:
            # Tolerate `ENOTSUP` on some external/network volumes.
            if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
        else:
            return

    os.fsync(fd)


def fsync_file(fd: IO) -> None:
    """
    Ensure the modifications to the file are durably persisted on disk.

    Parameters
    ----------
    fd
        The file descriptor to flush and sync.
    """
    fd.flush()
    _fsync_fd(fd)


def fsync_directory(path: str | os.PathLike) -> None:
    """
    Ensure the modifications to the directory are durably persisted on disk.

    Most commonly, this is used to ensure that file/subdirectory creation is committed to disk.
    In this case, the argument to this function is the PARENT path of the newly created item.

    Parameters
    ----------
    path
        The directory to sync.
    """
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except (PermissionError, OSError):
        # Windows / unsupported filesystem.
        return
    try:
        _fsync_fd(dir_fd)
    finally:
        os.close(dir_fd)
