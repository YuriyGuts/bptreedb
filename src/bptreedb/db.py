from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Self

from sortedcontainers import SortedDict

from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.entities import WALRecord
from bptreedb.exceptions import DBClosedError
from bptreedb.exceptions import DBConcurrentPageModificationError
from bptreedb.fs import fsync_directory
from bptreedb.wal import WAL

WAL_FILENAME = "bptreedb.wal"


class DB:
    def __init__(
        self,
        data_dir: str | Path,
        page_size_bytes: int = 4096,
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
        self.wal = WAL(self.data_dir / WAL_FILENAME)
        self.data = SortedDict()
        self.is_opened = False
        self._version_counter = 0

    def open(self) -> None:
        dir_already_existed = self.data_dir.exists()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not dir_already_existed:
            fsync_directory(self.data_dir.parent)

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
            self.data.clear()
        finally:
            self.is_opened = False

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
                self.data[record.key] = record.value
            case WALDeleteRecord():
                self.data.pop(record.key, None)

    def _ensure_bytes_type(self, value: bytes, param_name: str) -> None:
        if not isinstance(value, bytes):
            raise TypeError(f"{param_name} must have the bytes type")

    def put(self, key: bytes, value: bytes) -> None:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        self._ensure_bytes_type(value, "value")
        self.wal.append_put(key, value)
        self.data[key] = value
        self._version_counter += 1

    def get(self, key: bytes) -> bytes | None:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        return self.data.get(key, None)

    def delete(self, key: bytes) -> bool:
        self._check_if_opened()
        self._ensure_bytes_type(key, "key")
        if key in self.data:
            self.wal.append_delete(key)
            del self.data[key]
            self._version_counter += 1
            return True
        return False

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

        return self._scan(start_key_inclusive, end_key_exclusive)

    def _scan(
        self,
        start_key_inclusive: bytes | None,
        end_key_exclusive: bytes | None,
    ) -> Iterator[tuple[bytes, bytes]]:
        version_snapshot = self._version_counter
        key_iter = self.data.irange(
            start_key_inclusive,
            end_key_exclusive,
            inclusive=(True, False),
        )
        for key in key_iter:
            if self._version_counter != version_snapshot:
                raise DBConcurrentPageModificationError()
            yield key, self.data[key]
