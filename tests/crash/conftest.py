"""
Crash-test infrastructure.

Contains a fault-injecting file wrapper plus a pytest fixture that monkeypatches
the WAL's `open` and the global `os.fsync` so that DB writes can be "rolled back"
mid-test to simulate a power failure.

The simulation model:
1. Every write goes through to the real file immediately (so same-process reads see
   the data, just like the OS page cache would).
2. On `fsync`, the wrapper takes a snapshot of the file's on-disk contents.
3. On `crash`, the file is rewritten to that snapshot, discarding anything written
   since the last fsync, exactly as a crash would discard data still sitting in
   the OS buffer.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import IO
from typing import Self

import pytest


class FaultyFile:
    """A binary file wrapper with crash-rollback semantics for testing."""

    def __init__(self, path: str | Path, mode: str) -> None:
        self._path = Path(path)
        self._file: IO[bytes] = open(path, mode)  # noqa: SIM115
        self._fsynced_snapshot = self._read_disk_contents()

    def _read_disk_contents(self) -> bytes:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return b""

    # File-like protocol — simple delegation to the wrapped file.

    def write(self, data: bytes) -> int:
        return self._file.write(data)

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def truncate(self, size: int | None = None) -> int:
        return self._file.truncate() if size is None else self._file.truncate(size)

    def flush(self) -> None:
        if not self._file.closed:
            self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    @property
    def closed(self) -> bool:
        return self._file.closed

    def close(self) -> None:
        if not self._file.closed:
            # The file may already be in a bad state after `crash()`; closing
            # should still be safe to call.
            with contextlib.suppress(OSError, ValueError):
                self._file.flush()
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # Crash-test API.

    def record_fsync(self) -> None:
        """Snapshot the file's current on-disk contents as the durable state.

        Called by the patched `os.fsync` whenever the WAL syncs this file.
        """
        self._fsynced_snapshot = self._read_disk_contents()

    def crash(self) -> None:
        """Roll the file's on-disk contents back to the last fsynced snapshot.

        Models a power failure where unfsynced writes living in the OS buffer
        are lost. Any DB instance still holding this file should be considered
        dead — the test must construct a fresh DB to inspect the rolled-back
        state.
        """
        with open(self._path, "wb") as rollback_handle:
            rollback_handle.write(self._fsynced_snapshot)
            rollback_handle.flush()
            os.fsync(rollback_handle.fileno())


class FaultyFileFixture:
    """Tracks every `FaultyFile` opened during a test so they can be crashed
    together via `crash_all()`."""

    def __init__(self) -> None:
        self.instances: list[FaultyFile] = []

    def register(self, faulty_file: FaultyFile) -> None:
        self.instances.append(faulty_file)

    def crash_all(self) -> None:
        for faulty_file in self.instances:
            faulty_file.crash()


@pytest.fixture
def faulty_files(monkeypatch: pytest.MonkeyPatch) -> FaultyFileFixture:
    """Patch the WAL's `open` and the global `os.fsync` so every WAL file is a
    `FaultyFile` and every fsync snapshots the file's durable state.

    Returns the fixture object, whose `crash_all()` method simulates a power
    failure by rolling every tracked file back to its last fsynced contents.
    """
    fixture = FaultyFileFixture()
    real_fsync = os.fsync

    def faulty_open(path: str | Path, mode: str, *args: object, **kwargs: object):  # noqa: ANN202
        # Read-only opens don't need crash semantics: reads can't be lost on a
        # crash because they don't write anything. Wrapping them would also be
        # actively harmful: a read-only `FaultyFile` snapshots whatever happens
        # to be on disk at construction time, and `crash_all()` would then
        # roll the file back to that stale snapshot, clobbering writes made
        # through the writable handle. Pass read-only opens straight through.
        if "w" not in mode and "a" not in mode and "+" not in mode:
            return open(path, mode, *args, **kwargs)  # type: ignore[arg-type]  # noqa: SIM115
        faulty_file = FaultyFile(path, mode)
        fixture.register(faulty_file)
        return faulty_file

    def patched_fsync(fd: int | FaultyFile | IO[bytes]) -> None:
        if isinstance(fd, FaultyFile):
            fd.record_fsync()
            real_fsync(fd.fileno())
        else:
            real_fsync(fd)

    # `bptreedb.wal.open` resolves to `builtins.open` because the wal module
    # never rebinds it. Setting an attribute on the module makes the lookup
    # find our shim first; `raising=False` allows the attribute to be created.
    monkeypatch.setattr("bptreedb.wal.open", faulty_open, raising=False)

    # Patch `os.fsync` globally for the duration of the test. The WAL is the
    # only thing that calls `os.fsync` in the codebase, so this is effectively
    # surgical even though the patch is module-wide.
    monkeypatch.setattr(os, "fsync", patched_fsync)

    return fixture
