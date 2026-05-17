"""Append-only write-ahead log: durability for writes and the source of truth for recovery."""

from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO
from typing import Self

from bptreedb.codec import decode_next_wal_record_from_file
from bptreedb.codec import encode_wal_record
from bptreedb.entities import WALCheckpointRecord
from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.entities import WALRecord
from bptreedb.exceptions import DBChecksumError
from bptreedb.exceptions import DBCorruptedError
from bptreedb.fs import fsync_directory
from bptreedb.fs import fsync_file


@dataclass
class WALStats:
    """A statistics object that tracks WAL activity."""

    records_appended: int = 0
    bytes_appended: int = 0
    fsyncs: int = 0
    records_replayed: int = 0
    truncations: int = 0

    def reset(self) -> None:
        """Reset all stats to initial values."""
        self.records_appended = 0
        self.bytes_appended = 0
        self.fsyncs = 0
        self.records_replayed = 0
        self.truncations = 0


class WAL:
    """Append-only log of mutations, fsynced on every append for crash safety."""

    def __init__(self, path: Path) -> None:
        """
        Create a new WAL instance backed by the file at `path`.

        Parameters
        ----------
        path
            The path to the WAL file. The file does not need to exist yet.
        """
        self.path = path
        self.checkpoint_temp_path = path.with_name(path.name + ".new")
        self.current_lsn = 0
        self.stats = WALStats()
        self._fd: IO[bytes] | None = None

    def open(self) -> None:
        """Open the WAL file in append mode, cleaning up any leftover rotation temp file."""
        if self._fd is not None:
            return

        # A previous `truncate_before` call may have crashed mid-rotation.
        self.checkpoint_temp_path.unlink(missing_ok=True)

        self.current_lsn = 0
        wal_already_existed = self.path.exists()
        self._fd = open(self.path, "a+b")  # noqa: SIM115
        if not wal_already_existed:
            fsync_directory(self.path.parent)

    def close(self) -> None:
        """Fsync any buffered writes and close the file handle."""
        if self._fd is not None:
            fsync_file(self._fd)
            self.stats.fsyncs += 1
            self._fd.close()
            self._fd = None

    def __enter__(self) -> Self:
        """Enter the context manager that automatically opens and closes the WAL."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Exit the context manager that automatically opens and closes the WAL."""
        self.close()
        # Do not suppress exceptions.
        return False

    @property
    def size_bytes(self) -> int:
        """Current size of the WAL file on disk, in bytes."""
        return self.path.stat().st_size

    def append_put(self, key: bytes, value: bytes) -> int:
        """
        Append a PUT record.

        Parameters
        ----------
        key
            The key being written.
        value
            The associated value.

        Returns
        -------
        The LSN assigned to the new record.
        """
        record = WALPutRecord(
            lsn=self.current_lsn + 1,
            key=key,
            value=value,
        )
        return self._append(record)

    def append_delete(self, key: bytes) -> int:
        """
        Append a DELETE record.

        Parameters
        ----------
        key
            The key being deleted.

        Returns
        -------
        The LSN assigned to the new record.
        """
        record = WALDeleteRecord(
            lsn=self.current_lsn + 1,
            key=key,
        )
        return self._append(record)

    def append_checkpoint(self, root_page_id: int, freelist_head: int, next_page_id: int) -> int:
        """
        Append a CHECKPOINT record snapshotting the given meta fields.

        Parameters
        ----------
        root_page_id
            ID of the root page at the time of the checkpoint.
        freelist_head
            ID of the freelist head page (or zero if there is none).
        next_page_id
            Next page ID the pager would hand out via bump allocation.

        Returns
        -------
        The LSN assigned to the new record.
        """
        record = WALCheckpointRecord(
            lsn=self.current_lsn + 1,
            root_page_id=root_page_id,
            freelist_head=freelist_head,
            next_page_id=next_page_id,
        )
        return self._append(record)

    def _append(self, record: WALRecord) -> int:
        """
        Encode a record, write it, fsync, and update internal state.

        Parameters
        ----------
        record
            The fully populated record to append. Its `lsn` becomes the new `current_lsn`.

        Returns
        -------
        The LSN of the appended record.
        """
        assert self._fd is not None
        encoded = encode_wal_record(record)
        self.current_lsn = record.lsn
        self._fd.write(encoded)
        fsync_file(self._fd)
        self.stats.records_appended += 1
        self.stats.bytes_appended += len(encoded)
        self.stats.fsyncs += 1
        return record.lsn

    def _iter_records(self) -> Iterator[WALRecord]:
        """
        Yield records from the start of the WAL, tolerating a corrupt record at the tail.

        Raises
        ------
        DBCorruptedError
            If a corrupt record is followed by a valid one, or if LSNs are non-sequential.
        """
        assert self._fd is not None
        self._fd.seek(0)
        last_lsn = 0
        already_encountered_broken_record = False

        while True:
            try:
                record = decode_next_wal_record_from_file(self._fd)
                if already_encountered_broken_record:
                    msg = "WAL contains a broken record followed by a valid record"
                    raise DBCorruptedError(msg)
                if last_lsn and record.lsn != last_lsn + 1:
                    msg = f"WAL contains non-sequential LSNs: {last_lsn} followed by {record.lsn}"
                    raise DBCorruptedError(msg)

                last_lsn = record.lsn
                yield record
            except DBChecksumError:
                already_encountered_broken_record = True
            except EOFError:
                break

    def replay(self, callback: Callable[[WALRecord], None]) -> None:
        """
        Replay every record through `callback`, then truncate any partial trailing record.

        Truncation runs unconditionally so the next `_append` writes to a clean tail.

        Parameters
        ----------
        callback
            Function invoked once per record, in LSN order.
        """
        assert self._fd is not None
        self.current_lsn = 0
        last_good_file_pos = 0

        for record in self._iter_records():
            last_good_file_pos = self._fd.tell()
            self.current_lsn = record.lsn
            self.stats.records_replayed += 1
            callback(record)

        # Truncate any partial record at the tail.
        self._fd.seek(last_good_file_pos)
        self._fd.truncate()
        fsync_file(self._fd)
        self.stats.fsyncs += 1

    def truncate_before(self, lsn: int) -> None:
        """
        Atomically rewrite the WAL, dropping every record with LSN strictly less than `lsn`.

        The rewrite goes through a temp file that is then `rename()`d over the original, so a
        crash mid-rotation leaves either the old or the new WAL intact.

        Parameters
        ----------
        lsn
            Records with `record.lsn >= lsn` are kept; everything older is discarded.
        """
        assert self._fd is not None

        # Generate a new WAL file, transfer newer records there, and replace the old WAL with it.
        with open(self.checkpoint_temp_path, "wb") as new_wal_file:
            for record in self._iter_records():
                if record.lsn >= lsn:
                    new_wal_file.write(encode_wal_record(record))
            fsync_file(new_wal_file)

        self.checkpoint_temp_path.replace(self.path)
        fsync_directory(self.path.parent)

        self._fd.close()
        self._fd = open(self.path, "a+b")  # noqa: SIM115
        self.stats.truncations += 1
