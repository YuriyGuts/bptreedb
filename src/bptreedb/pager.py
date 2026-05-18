"""Implements the pager operations."""

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
    """A statistics object that tracks pager operations."""

    page_reads: int = 0
    page_writes: int = 0
    pages_allocated: int = 0
    pages_freed: int = 0
    pages_reused_from_freelist: int = 0
    meta_flushes: int = 0
    fsyncs: int = 0

    def reset(self) -> None:
        """Reset all stats to initial values."""
        self.page_reads = 0
        self.page_writes = 0
        self.pages_allocated = 0
        self.pages_freed = 0
        self.pages_reused_from_freelist = 0
        self.meta_flushes = 0
        self.fsyncs = 0


class Pager:
    """Manages the persistence of pages in the page file on disk."""

    def __init__(self, path: Path, *, page_size_bytes: int) -> None:
        """
        Create a new pager instance.

        Parameters
        ----------
        path
            The path to the page file.
        page_size_bytes
            The size of a single page in bytes.
        """
        if page_size_bytes < MIN_PAGE_SIZE:
            raise ValueError(f"Page size must be at least {MIN_PAGE_SIZE} bytes")
        self.path = path
        self.page_size_bytes = page_size_bytes
        self.stats = PagerStats()
        self._file: IO[bytes] | None = None
        self._meta_page: MetaPage | None = None
        self._is_meta_dirty = False
        self._freelist_cache: dict[int, FreelistPage] = {}
        self._dirty_freelist_page_ids: set[int] = set()

    def page_count(self) -> int:
        """Count the number of pages managed by the pager."""
        assert self._meta_page is not None
        return self._meta_page.next_page_id

    def open(self) -> None:
        """Open the pager file, bootstrapping an empty DB if it does not exist yet."""
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
        """Set up a blank database containing only the meta page and one root leaf page."""
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
        # Write the leaf before the meta so a crash here leaves an uncommitted meta rather
        # than one that points at a zero-filled page.
        self.write_page(leaf_page_id, encode_page(initial_leaf_page, self.page_size_bytes))
        self.flush_meta()
        self.fsync()
        fsync_directory(self.path.parent)

    def close(self) -> None:
        """Flush the changes to disk and close the pager."""
        if self._file is None:
            return
        if self._dirty_freelist_page_ids:
            self.flush_dirty_freelist_pages()
        if self._is_meta_dirty:
            self.flush_meta()
        self.fsync()
        self._file.close()
        self._file = None
        self._freelist_cache.clear()
        self._dirty_freelist_page_ids.clear()

    def __enter__(self) -> Self:
        """Enter the context manager that automatically opens and closes the pager."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Exit the context manager that automatically opens and closes the pager."""
        self.close()
        # Do not suppress exceptions.
        return False

    def _extend_file_by_one_page(self) -> None:
        """Grow the page file by one page."""
        assert self._file is not None
        self._file.seek(0, io.SEEK_END)
        self._file.write(bytes(self.page_size_bytes))

    def _get_freelist_page(self, page_id: int) -> FreelistPage:
        """Read a freelist page, preferring dirty in-memory state over disk."""
        if page_id in self._freelist_cache:
            return self._freelist_cache[page_id]

        page = decode_page(self.read_page(page_id))
        assert isinstance(page, FreelistPage)
        self._freelist_cache[page_id] = page
        return page

    def _read_freelist_head_page(self) -> FreelistPage:
        """Read the freelist head page that the current meta page points to."""
        assert self._meta_page is not None
        return self._get_freelist_page(self._meta_page.freelist_head_page_id)

    def _mark_freelist_page_dirty(self, page_id: int, page: FreelistPage) -> None:
        """Stage a freelist page update for the next checkpoint."""
        self._freelist_cache[page_id] = page
        self._dirty_freelist_page_ids.add(page_id)

    def read_page(self, page_id: int) -> bytes:
        """
        Read a page from disk.

        Parameters
        ----------
        page_id
            The ID of the page to read.

        Returns
        -------
        Page contents encoded in the wire format.
        """
        assert self._file is not None
        self._file.seek(page_id * self.page_size_bytes)
        data = self._file.read(self.page_size_bytes)
        if page_id != META_PAGE_ID and len(data) != self.page_size_bytes:
            raise DBCorruptedError(f"Unexpected end of file while reading page {page_id}")
        self.stats.page_reads += 1
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        """
        Write a page to disk.

        Parameters
        ----------
        page_id
            The page ID to record the page under.
        data
            Page contents encoded in the wire format.
        """
        assert self._file is not None
        self._file.seek(page_id * self.page_size_bytes)
        self._file.write(data)
        self.stats.page_writes += 1

    def fsync(self) -> None:
        """Flush the changes to disk."""
        assert self._file is not None
        fsync_file(self._file)
        self.stats.fsyncs += 1

    def get_meta(self) -> MetaPage:
        """Retrieve the meta page."""
        assert self._meta_page is not None
        return self._meta_page.copy()

    def update_meta(self, **fields: Any) -> None:  # noqa: ANN401
        """
        Update the attributes of the meta page.

        Parameters
        ----------
        fields
            The keyword arguments to update the meta page with.
        """
        assert self._meta_page is not None
        for key, value in fields.items():
            setattr(self._meta_page, key, value)
        self._is_meta_dirty = True

    def flush_meta(self) -> None:
        """Flush the changes to the meta page to disk."""
        assert self._meta_page is not None
        self.write_page(META_PAGE_ID, encode_meta_page(self._meta_page))
        self._is_meta_dirty = False
        self.stats.meta_flushes += 1

    def flush_dirty_freelist_pages(self) -> None:
        """Flush dirty freelist pages to disk and mark them clean."""
        for page_id in sorted(self._dirty_freelist_page_ids):
            page = self._freelist_cache.get(page_id)
            if page is None:
                continue
            self.write_page(page_id, encode_page(page, self.page_size_bytes))
        self._dirty_freelist_page_ids.clear()

    def _bump_allocate_one_page(self) -> int:
        """Allocate a new page by explicitly growing the data file."""
        assert self._meta_page is not None
        page_id = self._meta_page.next_page_id
        self._extend_file_by_one_page()
        self.update_meta(next_page_id=page_id + 1)
        self.stats.pages_allocated += 1
        return page_id

    def allocate_page(self) -> int:
        """Allocate a new page, either by reusing a freelist page or by growing the data file."""
        assert self._meta_page is not None

        # Is there a page in the freelist we can reuse?
        if self._meta_page.freelist_head_page_id != 0:
            freelist_page = self._read_freelist_head_page()
            assert isinstance(freelist_page, FreelistPage)

            # Freelist has a page available.
            if freelist_page.freed_page_ids:
                page_id = freelist_page.freed_page_ids.pop()
                self._mark_freelist_page_dirty(self._meta_page.freelist_head_page_id, freelist_page)
                self._freelist_cache.pop(page_id, None)
                self._dirty_freelist_page_ids.discard(page_id)
                self.stats.pages_reused_from_freelist += 1
                return page_id

            # Freelist head page is exhausted: recycle it for allocation and update the freelist
            # pointer to a successor page if available.
            page_id = self._meta_page.freelist_head_page_id
            self.update_meta(freelist_head_page_id=freelist_page.next_freelist_page_id)
            self._freelist_cache.pop(page_id, None)
            self._dirty_freelist_page_ids.discard(page_id)
            self.stats.pages_reused_from_freelist += 1
            return page_id

        # Freelist is unavailable or exhausted: bump-allocate a new page.
        return self._bump_allocate_one_page()

    def free_page(self, page_id: int) -> None:
        """
        Free the specified page.

        Parameters
        ----------
        page_id
            The ID of the page to free.
        """
        assert self._meta_page is not None
        self.stats.pages_freed += 1

        # If we already have a freelist head page which has room for one more entry, use it.
        if self._meta_page.freelist_head_page_id != 0:
            freelist_page = self._read_freelist_head_page()
            max_entries = get_max_freed_ids_per_freelist_page(self.page_size_bytes)
            if len(freelist_page.freed_page_ids) < max_entries:
                freelist_page.freed_page_ids.append(page_id)
                self._mark_freelist_page_dirty(
                    self._meta_page.freelist_head_page_id,
                    freelist_page,
                )
                return

        # Otherwise, bump-allocate a new freelist head page.
        new_head_page_id = self._bump_allocate_one_page()
        freelist_page = FreelistPage(
            last_modified_lsn=0,
            next_freelist_page_id=self._meta_page.freelist_head_page_id,
            freed_page_ids=[page_id],
        )
        self._mark_freelist_page_dirty(new_head_page_id, freelist_page)
        self.update_meta(freelist_head_page_id=new_head_page_id)
