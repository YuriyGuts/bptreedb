from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import IO
from typing import Self

from bptreedb.codec import decode_next_wal_record_from_file
from bptreedb.codec import encode_wal_record
from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.entities import WALRecord
from bptreedb.exceptions import DBChecksumError
from bptreedb.exceptions import DBCorruptedError
from bptreedb.fs import fsync_directory
from bptreedb.fs import fsync_file


class WAL:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.current_lsn = 0
        self._fd: IO[bytes] | None = None

    def open(self) -> None:
        if self._fd is not None:
            return

        self.current_lsn = 0
        wal_already_existed = self.path.exists()
        self._fd = open(self.path, "a+b")  # noqa: SIM115
        if not wal_already_existed:
            fsync_directory(self.path.parent)

    def close(self) -> None:
        if self._fd is not None:
            fsync_file(self._fd)
            self._fd.close()
            self._fd = None

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

    def append_put(self, key: bytes, value: bytes) -> int:
        record = WALPutRecord(
            lsn=self.current_lsn + 1,
            key=key,
            value=value,
        )
        return self._append(record)

    def append_delete(self, key: bytes) -> int:
        record = WALDeleteRecord(
            lsn=self.current_lsn + 1,
            key=key,
        )
        return self._append(record)

    def _append(self, record: WALRecord) -> int:
        assert self._fd is not None
        self.current_lsn = record.lsn
        self._fd.write(encode_wal_record(record))
        fsync_file(self._fd)
        return record.lsn

    def replay(self, callback: Callable[[WALRecord], None]) -> None:
        assert self._fd is not None
        self._fd.seek(0)
        self.current_lsn = 0
        last_good_file_pos = 0
        already_encountered_broken_record = False

        while True:
            try:
                record = decode_next_wal_record_from_file(self._fd)
                if already_encountered_broken_record:
                    msg = "WAL contains a broken record followed by a valid record"
                    raise DBCorruptedError(msg)
                if self.current_lsn and record.lsn != self.current_lsn + 1:
                    msg = (
                        "WAL contains non-sequential LSNs: "
                        f"{self.current_lsn} followed by {record.lsn}"
                    )
                    raise DBCorruptedError(msg)

                last_good_file_pos = self._fd.tell()
                self.current_lsn = record.lsn
                callback(record)
            except DBChecksumError:
                already_encountered_broken_record = True
            except EOFError:
                break

        # Truncate the file after the last known good record.
        self._fd.seek(last_good_file_pos)
        self._fd.truncate()
        fsync_file(self._fd)
