from __future__ import annotations

from collections.abc import Iterator
from types import TracebackType
from typing import Self

from sortedcontainers import SortedDict

from bptreedb.exceptions import DBClosedError


class DB:
    def __init__(self) -> None:
        self.data = SortedDict()
        self.is_opened = False

    def open(self) -> None:
        self.is_opened = True

    def close(self) -> None:
        self.data.clear()
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

    def _ensure_opened(self) -> None:
        if not self.is_opened:
            raise DBClosedError()

    def put(self, key: bytes, value: bytes) -> None:
        self._ensure_opened()
        self.data[key] = value

    def get(self, key: bytes) -> bytes | None:
        self._ensure_opened()
        return self.data.get(key, None)

    def delete(self, key: bytes) -> bool:
        self._ensure_opened()
        return self.data.pop(key, None) is not None

    def scan(
        self,
        start_key_inclusive: bytes | None,
        end_key_exclusive: bytes | None,
    ) -> Iterator[tuple[bytes, bytes]]:
        self._ensure_opened()
        key_iter = self.data.irange(
            start_key_inclusive,
            end_key_exclusive,
            inclusive=(True, False),
        )
        for key in key_iter:
            yield key, self.data[key]
