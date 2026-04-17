from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Self

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
    def __init__(
        self,
        data_dir: str | Path,
        *,
        page_size_bytes: int = DEFAULT_PAGE_SIZE,
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
        """
        self.data_dir = Path(data_dir)
        self.page_size_bytes = page_size_bytes
        self.pager = Pager(self.data_dir / PAGER_FILENAME, page_size_bytes=page_size_bytes)
        self.tree = BPlusTree(self.pager)
        self.wal = WAL(self.data_dir / WAL_FILENAME)
        self.is_opened = False
        self._version_counter = 0

    def open(self) -> None:
        dir_already_existed = self.data_dir.exists()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not dir_already_existed:
            fsync_directory(self.data_dir.parent)

        # Reset the data file to a clean state.
        # In the current implementation, we replay all writes from the WAL.
        self.pager.path.unlink(missing_ok=True)

        self.pager.open()
        self.wal.open()
        try:
            self.wal.replay(self._apply_wal_record)
        except Exception:
            self.wal.close()
            raise

        self.is_opened = True

    def close(self) -> None:
        try:
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
        match record:
            case WALPutRecord():
                self.tree.insert(record.key, record.value)
            case WALDeleteRecord():
                self.tree.delete(record.key)

    def _ensure_bytes_type(self, value: bytes, param_name: str) -> None:
        if not isinstance(value, bytes):
            raise TypeError(f"{param_name} must have the bytes type")

    def put(self, key: bytes, value: bytes) -> None:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        self._ensure_bytes_type(value, "value")
        self.wal.append_put(key, value)
        self.tree.insert(key, value)
        self._version_counter += 1

    def get(self, key: bytes) -> bytes | None:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        return self.tree.search(key)

    def delete(self, key: bytes) -> bool:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        if self.tree.search(key) is None:
            return False

        self.wal.append_delete(key)
        self.tree.delete(key)
        self._version_counter += 1
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
