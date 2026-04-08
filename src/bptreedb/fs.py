import os
from typing import IO


def fsync_file(fd: IO) -> None:
    fd.flush()
    os.fsync(fd)


def fsync_directory(path: str | os.PathLike) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except (PermissionError, OSError):
        return  # Windows / unsupported filesystem
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
