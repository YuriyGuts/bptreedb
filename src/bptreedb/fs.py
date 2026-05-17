"""Filesystem helpers for durably persisting data to disk."""

import os
from typing import IO


def fsync_file(fd: IO) -> None:
    """
    Ensure the modifications to the file are durably persisted on disk.

    Parameters
    ----------
    fd
        The file descriptor to flush and sync.
    """
    fd.flush()
    os.fsync(fd)


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
        return  # Windows / unsupported filesystem
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
