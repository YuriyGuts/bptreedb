import io
from pathlib import Path
from types import TracebackType
from typing import IO
from typing import Any
from typing import Self

from bptreedb.codec import MIN_PAGE_SIZE
from bptreedb.codec import decode_meta_page
from bptreedb.codec import encode_meta_page
from bptreedb.codec import encode_page
from bptreedb.entities import LeafPage
from bptreedb.entities import MetaPage
from bptreedb.exceptions import DBCorruptedError
from bptreedb.fs import fsync_directory
from bptreedb.fs import fsync_file

META_PAGE_ID = 0
DEFAULT_ROOT_PAGE_ID = 1


class Pager:
    def __init__(self, path: Path, *, page_size_bytes: int) -> None:
        if page_size_bytes < MIN_PAGE_SIZE:
            raise ValueError(f"Page size must be at least {MIN_PAGE_SIZE} bytes")
        self.path = path
        self.page_size_bytes = page_size_bytes
        self._file: IO[bytes] | None = None
        self._meta_page: MetaPage | None = None
        self._is_meta_dirty = False

    def page_count(self) -> int:
        assert self._meta_page is not None
        return self._meta_page.next_page_id

    def open(self) -> None:
        if self._file is not None:
            return
        file_already_existed = self.path.exists()
        if not file_already_existed:
            self.path.touch()
        self._file = open(self.path, "r+b")  # noqa: SIM115
        if not file_already_existed:
            self._bootstrap_initial_db()
        else:
            self._meta_page = decode_meta_page(self.read_page(META_PAGE_ID))
            self.page_size_bytes = self._meta_page.page_size_bytes

    def _bootstrap_initial_db(self) -> None:
        self._meta_page = MetaPage(
            page_size_bytes=self.page_size_bytes,
            root_page_id=DEFAULT_ROOT_PAGE_ID,
            next_page_id=DEFAULT_ROOT_PAGE_ID,
        )
        leaf_page_id = self.allocate_page()
        initial_leaf_page = LeafPage(
            right_sibling_page_id=0,
            slots=[],
        )
        self.flush_meta()
        self.write_page(leaf_page_id, encode_page(initial_leaf_page, self.page_size_bytes))
        self.fsync()
        fsync_directory(self.path.parent)

    def close(self) -> None:
        if self._file is None:
            return
        if self._is_meta_dirty:
            self.flush_meta()
        self.fsync()
        self._file.close()
        self._file = None

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

    def _extend_file_by_one_page(self) -> None:
        assert self._file is not None
        self._file.seek(0, io.SEEK_END)
        self._file.write(bytes(self.page_size_bytes))

    def read_page(self, page_id: int) -> bytes:
        assert self._file is not None
        self._file.seek(page_id * self.page_size_bytes)
        data = self._file.read(self.page_size_bytes)
        if page_id != META_PAGE_ID and len(data) != self.page_size_bytes:
            raise DBCorruptedError(f"Unexpected end of file while reading page {page_id}")
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        assert self._file is not None
        self._file.seek(page_id * self.page_size_bytes)
        self._file.write(data)

    def fsync(self) -> None:
        assert self._file is not None
        fsync_file(self._file)

    def get_meta(self) -> MetaPage:
        assert self._meta_page is not None
        return self._meta_page.copy()

    def update_meta(self, **fields: Any) -> None:  # noqa: ANN401
        assert self._meta_page is not None
        for key, value in fields.items():
            setattr(self._meta_page, key, value)
        self._is_meta_dirty = True

    def flush_meta(self) -> None:
        assert self._meta_page is not None
        self.write_page(META_PAGE_ID, encode_meta_page(self._meta_page))
        self._is_meta_dirty = False

    def allocate_page(self) -> int:
        assert self._meta_page is not None
        page_id = self._meta_page.next_page_id
        self._extend_file_by_one_page()
        self.update_meta(next_page_id=page_id + 1)
        return page_id
