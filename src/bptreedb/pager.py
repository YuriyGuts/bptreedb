import io
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO
from typing import Any
from typing import Self

from bptreedb.codec import MIN_PAGE_SIZE
from bptreedb.codec import decode_meta_page
from bptreedb.codec import decode_page
from bptreedb.codec import encode_meta_page
from bptreedb.codec import encode_page
from bptreedb.codec import get_max_freed_ids_per_freelist_page
from bptreedb.entities import FreelistPage
from bptreedb.entities import LeafPage
from bptreedb.entities import MetaPage
from bptreedb.exceptions import DBCorruptedError
from bptreedb.fs import fsync_directory
from bptreedb.fs import fsync_file

META_PAGE_ID = 0
DEFAULT_ROOT_PAGE_ID = 1


@dataclass
class PagerStats:
    page_reads: int = 0
    page_writes: int = 0
    pages_allocated: int = 0
    pages_freed: int = 0
    pages_reused_from_freelist: int = 0
    meta_flushes: int = 0
    fsyncs: int = 0

    def reset(self) -> None:
        self.page_reads = 0
        self.page_writes = 0
        self.pages_allocated = 0
        self.pages_freed = 0
        self.pages_reused_from_freelist = 0
        self.meta_flushes = 0
        self.fsyncs = 0


class Pager:
    def __init__(self, path: Path, *, page_size_bytes: int) -> None:
        if page_size_bytes < MIN_PAGE_SIZE:
            raise ValueError(f"Page size must be at least {MIN_PAGE_SIZE} bytes")
        self.path = path
        self.page_size_bytes = page_size_bytes
        self.stats = PagerStats()
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
            freelist_head_page_id=0,
            last_checkpoint_lsn=0,
        )
        leaf_page_id = self.allocate_page()
        initial_leaf_page = LeafPage(
            last_modified_lsn=0,
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

    def _read_freelist_head_page(self) -> FreelistPage:
        assert self._meta_page is not None
        page = decode_page(self.read_page(self._meta_page.freelist_head_page_id))
        assert isinstance(page, FreelistPage)
        return page

    def read_page(self, page_id: int) -> bytes:
        assert self._file is not None
        self._file.seek(page_id * self.page_size_bytes)
        data = self._file.read(self.page_size_bytes)
        if page_id != META_PAGE_ID and len(data) != self.page_size_bytes:
            raise DBCorruptedError(f"Unexpected end of file while reading page {page_id}")
        self.stats.page_reads += 1
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        assert self._file is not None
        self._file.seek(page_id * self.page_size_bytes)
        self._file.write(data)
        self.stats.page_writes += 1

    def fsync(self) -> None:
        assert self._file is not None
        fsync_file(self._file)
        self.stats.fsyncs += 1

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
        self.stats.meta_flushes += 1

    def _bump_allocate_one_page(self) -> int:
        assert self._meta_page is not None
        page_id = self._meta_page.next_page_id
        self._extend_file_by_one_page()
        self.update_meta(next_page_id=page_id + 1)
        self.stats.pages_allocated += 1
        return page_id

    def allocate_page(self) -> int:
        assert self._meta_page is not None

        # Is there a page in the freelist we can reuse?
        if self._meta_page.freelist_head_page_id != 0:
            freelist_page = self._read_freelist_head_page()
            assert isinstance(freelist_page, FreelistPage)

            # Freelist has a page available.
            if freelist_page.freed_page_ids:
                page_id = freelist_page.freed_page_ids.pop()
                self.write_page(
                    self._meta_page.freelist_head_page_id,
                    encode_page(freelist_page, self.page_size_bytes),
                )
                self.stats.pages_reused_from_freelist += 1
                return page_id

            # Freelist head page is exhausted: recycle it for allocation and update the freelist
            # pointer to a successor page if available.
            page_id = self._meta_page.freelist_head_page_id
            self.update_meta(freelist_head_page_id=freelist_page.next_freelist_page_id)
            self.stats.pages_reused_from_freelist += 1
            return page_id

        # Freelist is unavailable or exhausted: bump-allocate a new page.
        return self._bump_allocate_one_page()

    def free_page(self, page_id: int) -> None:
        assert self._meta_page is not None
        self.stats.pages_freed += 1

        # If we already have a freelist head page which has room for one more entry, use it.
        if self._meta_page.freelist_head_page_id != 0:
            freelist_page = self._read_freelist_head_page()
            max_entries = get_max_freed_ids_per_freelist_page(self.page_size_bytes)
            if len(freelist_page.freed_page_ids) < max_entries:
                freelist_page.freed_page_ids.append(page_id)
                self.write_page(
                    self._meta_page.freelist_head_page_id,
                    encode_page(freelist_page, self.page_size_bytes),
                )
                return

        # Otherwise, bump-allocate a new freelist head page.
        new_head_page_id = self._bump_allocate_one_page()
        freelist_page = FreelistPage(
            last_modified_lsn=0,
            next_freelist_page_id=self._meta_page.freelist_head_page_id,
            freed_page_ids=[page_id],
        )
        self.write_page(new_head_page_id, encode_page(freelist_page, self.page_size_bytes))
        self.update_meta(freelist_head_page_id=new_head_page_id)
