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
from bptreedb.exceptions import DBBufferPoolOverflowError
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
        dir_already_existed = self.data_dir.exists()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not dir_already_existed:
            fsync_directory(self.data_dir.parent)

        self.pager.open()
        self.wal.open()
        try:
            self.buffer_pool.enable_eviction = False
            self._recovery_last_checkpoint_lsn = self.pager.get_meta().last_checkpoint_lsn
            self.wal.replay(self._apply_wal_record)
            self.buffer_pool.enable_eviction = True
            self.checkpoint()
        except Exception:
            self.wal.close()
            self.pager.close()
            raise

        self.is_opened = True

    def close(self) -> None:
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
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        # Do not suppress exceptions.
        return False

    def _check_if_opened(self) -> None:
        if not self.is_opened:
            raise DBClosedError()

    def _apply_wal_record(self, record: WALRecord) -> None:
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
        if not isinstance(value, bytes):
            raise TypeError(f"{param_name} must have the bytes type")

    def _maybe_checkpoint_after_write(self) -> None:
        should_checkpoint = (
            self.wal.size_bytes > self.checkpoint_wal_size_bytes
            or self.buffer_pool.dirty_ratio > self.checkpoint_dirty_page_ratio
        )
        if should_checkpoint:
            self.checkpoint()

    def put(self, key: bytes, value: bytes) -> None:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        self._ensure_bytes_type(value, "value")

        lsn = self.wal.append_put(key, value)
        try:
            self.tree.insert(key, value, lsn)
        except DBBufferPoolOverflowError:
            self.checkpoint()
            self.tree.insert(key, value, lsn)

        self._version_counter += 1
        self._maybe_checkpoint_after_write()

    def get(self, key: bytes) -> bytes | None:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        return self.tree.search(key)

    def delete(self, key: bytes) -> bool:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        if self.tree.search(key) is None:
            return False

        lsn = self.wal.append_delete(key)
        try:
            self.tree.delete(key, lsn)
        except DBBufferPoolOverflowError:
            self.checkpoint()
            self.tree.delete(key, lsn)

        self._version_counter += 1
        self._maybe_checkpoint_after_write()
        return True

    def scan(
        self,
        start_key_inclusive: bytes | None,
        end_key_exclusive: bytes | None,
    ) -> Iterator[tuple[bytes, bytes]]:
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
        checkpoint_lsn = self.wal.current_lsn + 1
        self.buffer_pool.flush_all()
        self.pager.fsync()
        meta = self.pager.get_meta()
        self.wal.append_checkpoint(
            root_page_id=meta.root_page_id,
            freelist_head=0,
            next_page_id=meta.next_page_id,
        )
        self.pager.update_meta(last_checkpoint_lsn=checkpoint_lsn)
        self.pager.flush_meta()
        self.pager.fsync()
        self.wal.truncate_before(checkpoint_lsn)
