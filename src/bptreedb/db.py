"""Public database facade that ties the pager, buffer pool, B+ tree, and WAL together."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Self

from bptreedb.cache import BufferPool
from bptreedb.entities import WALCheckpointRecord
from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.entities import WALRecord
from bptreedb.exceptions import DBClosedError
from bptreedb.exceptions import DBConcurrentPageModificationError
from bptreedb.fs import fsync_directory
from bptreedb.pager import Pager
from bptreedb.tree import BPlusTree
from bptreedb.wal import WAL

PAGER_FILENAME = "bptreedb.dat"
WAL_FILENAME = "bptreedb.wal"
DEFAULT_PAGE_SIZE = 4096


class DB:
    """A single-process embedded key/value database."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        page_size_bytes: int = DEFAULT_PAGE_SIZE,
        cache_capacity_pages: int = 256,
        checkpoint_wal_size_bytes: int = 4 * 1024 * 1024,
        checkpoint_dirty_page_ratio: float = 0.5,
    ) -> None:
        """
        Create a new DB instance.

        Parameters
        ----------
        data_dir
            The directory where the data will be stored.
            Automatically created on `.open()` if it does not exist.
        page_size_bytes
            The size of the page in bytes.
            Applies only to newly created databases; existing ones will use the page size
            stored in the metadata.
        checkpoint_wal_size_bytes
            When the WAL grows past this size, the next write operation triggers a checkpoint.
        checkpoint_dirty_page_ratio
            When the fraction of dirty pages in the buffer pool exceeds this ratio, the next write
            operation triggers a checkpoint.
        """
        self.data_dir = Path(data_dir)
        self.page_size_bytes = page_size_bytes
        self.checkpoint_wal_size_bytes = checkpoint_wal_size_bytes
        self.checkpoint_dirty_page_ratio = checkpoint_dirty_page_ratio
        self.pager = Pager(self.data_dir / PAGER_FILENAME, page_size_bytes=page_size_bytes)
        self.buffer_pool = BufferPool(self.pager, cache_capacity_pages)
        self.tree = BPlusTree(self.pager, self.buffer_pool)
        self.wal = WAL(self.data_dir / WAL_FILENAME)
        self.is_opened = False
        self._version_counter = 0
        self._recovery_last_checkpoint_lsn = 0

    def open(self) -> None:
        """Open the data directory, recover from the WAL, and bring the database online."""
        dir_already_existed = self.data_dir.exists()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not dir_already_existed:
            fsync_directory(self.data_dir.parent)

        self.pager.open()
        self.wal.open()
        try:
            self._recover()
        except Exception:
            self.wal.close()
            self.pager.close()
            raise

        self.is_opened = True

    def _recover(self) -> None:
        """Recover the database state from the WAL."""
        with self.buffer_pool.eviction_disabled():
            self._repair_pager_meta_from_wal_checkpoint()
            self._recovery_last_checkpoint_lsn = self.pager.get_meta().last_checkpoint_lsn
            self.wal.replay(self._apply_wal_record)
        self.checkpoint()

    def _repair_pager_meta_from_wal_checkpoint(self) -> None:
        """Advance the in-memory pager meta if the WAL has a newer CHECKPOINT than the disk meta."""
        on_disk_last_checkpoint_lsn = self.pager.get_meta().last_checkpoint_lsn
        latest_checkpoint: WALCheckpointRecord | None = None
        for record in self.wal.peek_records():
            if isinstance(record, WALCheckpointRecord) and (
                latest_checkpoint is None or record.lsn > latest_checkpoint.lsn
            ):
                latest_checkpoint = record

        if latest_checkpoint is None or latest_checkpoint.lsn <= on_disk_last_checkpoint_lsn:
            return

        self.pager.update_meta(
            root_page_id=latest_checkpoint.root_page_id,
            freelist_head_page_id=latest_checkpoint.freelist_head,
            next_page_id=latest_checkpoint.next_page_id,
            last_checkpoint_lsn=latest_checkpoint.lsn,
        )

    def close(self) -> None:
        """Take a final checkpoint and release all underlying file handles."""
        if not self.is_opened:
            return

        try:
            self.checkpoint()
            self.wal.close()
            self.pager.close()
        finally:
            self.is_opened = False
            self._version_counter = 0

    def __enter__(self) -> Self:
        """Enter the context manager that automatically opens and closes the database."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Exit the context manager that automatically opens and closes the database."""
        self.close()
        # Do not suppress exceptions.
        return False

    def _check_if_opened(self) -> None:
        """Raise `DBClosedError` if the database has not been opened."""
        if not self.is_opened:
            raise DBClosedError()

    def _apply_wal_record(self, record: WALRecord) -> None:
        """
        Re-apply a single WAL record during recovery, skipping anything pre-checkpoint.

        Parameters
        ----------
        record
            The WAL record being replayed.
        """
        if record.lsn <= self._recovery_last_checkpoint_lsn:
            return

        match record:
            case WALPutRecord():
                self.tree.insert(record.key, record.value, record.lsn)
            case WALDeleteRecord():
                self.tree.delete(record.key, record.lsn)
            case WALCheckpointRecord():
                pass

    def _ensure_bytes_type(self, value: bytes, param_name: str) -> None:
        """
        Reject any argument that is not a `bytes` instance.

        Parameters
        ----------
        value
            The value to type-check.
        param_name
            Name of the parameter being checked; used to build the error message.
        """
        if not isinstance(value, bytes):
            raise TypeError(f"{param_name} must have the bytes type")

    def _maybe_checkpoint(self) -> None:
        """Trigger a checkpoint if the WAL or the dirty page ratio crossed the configured limit."""
        should_checkpoint = (
            self.wal.size_bytes > self.checkpoint_wal_size_bytes
            or self.buffer_pool.dirty_ratio > self.checkpoint_dirty_page_ratio
        )
        if should_checkpoint:
            self.checkpoint()

    def put(self, key: bytes, value: bytes) -> None:
        """
        Insert or overwrite the value associated with `key`.

        Parameters
        ----------
        key
            The key to insert or update.
        value
            The value to associate with the key.
        """
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        self._ensure_bytes_type(value, "value")

        # Make sure the buffer pool has room for the pages that may be touched by tree rebalancing.
        # `tree.insert` is not transactional, so a pool overflow during rebalance would leave the
        # tree in a half-mutated state we cannot recover from.
        self._maybe_checkpoint()

        lsn = self.wal.append_put(key, value)
        self.tree.insert(key, value, lsn)

        self._version_counter += 1
        self._maybe_checkpoint()

    def get(self, key: bytes) -> bytes | None:
        """
        Look up the value associated with `key`.

        Parameters
        ----------
        key
            The key to look up.

        Returns
        -------
        The stored value, or `None` if no such key exists.
        """
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        return self.tree.search(key)

    def delete(self, key: bytes) -> bool:
        """
        Delete `key` from the database.

        Parameters
        ----------
        key
            The key to remove.

        Returns
        -------
        `True` if the key existed and was removed, `False` if it was not present.
        """
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        if self.tree.search(key) is None:
            return False

        # Make sure the buffer pool has room for the pages that may be touched by tree rebalancing.
        self._maybe_checkpoint()

        lsn = self.wal.append_delete(key)
        was_deleted = self.tree.delete(key, lsn)
        assert was_deleted, "tree.delete returned False right after a positive pre-search"

        self._version_counter += 1
        self._maybe_checkpoint()
        return True

    def scan(
        self,
        start_key_inclusive: bytes | None,
        end_key_exclusive: bytes | None,
    ) -> Iterator[tuple[bytes, bytes]]:
        """
        Iterate over key/value pairs within the half-open range `[start, end)`.

        Parameters
        ----------
        start_key_inclusive
            Lower bound on the key; pass `None` to start from the very first key.
        end_key_exclusive
            Upper bound on the key (exclusive); pass `None` to scan to the end.

        Returns
        -------
        An iterator over `(key, value)` tuples in key order.

        Raises
        ------
        DBConcurrentPageModificationError
            If the database is mutated by another `put`/`delete` during iteration.
        """
        # Validate eagerly so callers see exceptions at the call site, not on the first `next()`.
        # A bare `yield` in this function body would turn it into a generator and defer every check.
        self._check_if_opened()

        if start_key_inclusive is not None:
            self._ensure_bytes_type(start_key_inclusive, "start_key_inclusive")
        if end_key_exclusive is not None:
            self._ensure_bytes_type(end_key_exclusive, "end_key_exclusive")

        # Capture the current version number to detect modifications during iteration.
        version_snapshot = self._version_counter

        def check_version() -> None:
            if version_snapshot != self._version_counter:
                raise DBConcurrentPageModificationError()

        return self.tree.scan(start_key_inclusive, end_key_exclusive, check_version)

    def checkpoint(self) -> None:
        """
        Flush dirty pages, record a checkpoint in the WAL, then truncate the WAL.

        After a successful checkpoint, recovery is bounded: the next `open()` only needs to
        replay records past `last_checkpoint_lsn`.
        """
        checkpoint_lsn = self.wal.current_lsn + 1
        self.buffer_pool.flush_all()
        self.pager.fsync()
        meta = self.pager.get_meta()
        self.wal.append_checkpoint(
            root_page_id=meta.root_page_id,
            freelist_head=meta.freelist_head_page_id,
            next_page_id=meta.next_page_id,
        )
        self.pager.update_meta(last_checkpoint_lsn=checkpoint_lsn)
        self.pager.flush_meta()
        self.pager.fsync()
        self.wal.truncate_before(checkpoint_lsn)
