"""LRU buffer pool that sits between the B+ tree and the pager."""

from collections import OrderedDict
from dataclasses import dataclass

from bptreedb.codec import decode_page
from bptreedb.codec import encode_page
from bptreedb.entities import InternalPage
from bptreedb.entities import LeafPage
from bptreedb.exceptions import DBBufferPoolOverflowError
from bptreedb.pager import Pager


@dataclass
class CachedPage:
    """A page held in the buffer pool, paired with its dirty bit."""

    page: LeafPage | InternalPage
    is_dirty: bool


@dataclass
class BufferPoolStats:
    """A statistics object that tracks buffer pool activity."""

    cache_hits: int = 0
    cache_misses: int = 0
    evictions: int = 0
    flushes: int = 0
    dirty_pages_flushed: int = 0

    def reset(self) -> None:
        """Reset all stats to initial values."""
        self.cache_hits = 0
        self.cache_misses = 0
        self.evictions = 0
        self.flushes = 0
        self.dirty_pages_flushed = 0


class BufferPool:
    """Caches decoded pages in memory and defers writes to the pager until eviction or flush."""

    def __init__(self, pager: Pager, capacity_pages: int) -> None:
        """
        Create a new buffer pool backed by the given pager.

        Parameters
        ----------
        pager
            The pager to read uncached pages from and to flush dirty pages to.
        capacity_pages
            The maximum number of pages that can be cached at any given time.
        """
        self.pager = pager
        self.capacity_pages = capacity_pages
        self.enable_eviction = True
        self.stats = BufferPoolStats()
        self._cache: OrderedDict[int, CachedPage] = OrderedDict()

    @property
    def dirty_count(self) -> int:
        """Number of dirty pages currently held in the pool."""
        return sum(1 for cached_page in self._cache.values() if cached_page.is_dirty)

    @property
    def dirty_ratio(self) -> float:
        """Fraction of the pool's capacity that is currently dirty."""
        return self.dirty_count / self.capacity_pages

    def get(self, page_id: int) -> LeafPage | InternalPage:
        """
        Fetch the page with the given ID, loading it from disk on a cache miss.

        Touching a page promotes it to the most-recently-used position.

        Parameters
        ----------
        page_id
            The ID of the page to fetch.

        Returns
        -------
        The cached page object (mutable; callers may modify it before marking it dirty).
        """
        # Cache hit.
        if page_id in self._cache:
            self._cache.move_to_end(page_id)
            self.stats.cache_hits += 1
            return self._cache[page_id].page

        # Cache miss, capacity full.
        self._evict_oldest_clean_page_if_full()

        # Cache miss, below capacity.
        page = decode_page(self.pager.read_page(page_id))
        assert isinstance(page, (LeafPage, InternalPage))
        self._cache[page_id] = CachedPage(page=page, is_dirty=False)
        self.stats.cache_misses += 1
        return page

    def insert(self, page_id: int, page: LeafPage | InternalPage, lsn: int) -> None:
        """
        Register a newly created page in the pool as dirty.

        Parameters
        ----------
        page_id
            The ID assigned to the new page.
        page
            The page object to cache.
        lsn
            LSN to stamp onto the page's `last_modified_lsn`.
        """
        assert page_id not in self._cache
        self._evict_oldest_clean_page_if_full()
        page.last_modified_lsn = lsn
        self._cache[page_id] = CachedPage(page=page, is_dirty=True)

    def mark_dirty(self, page_id: int, lsn: int) -> None:
        """
        Mark a cached page as dirty and bump its `last_modified_lsn` if the LSN is newer.

        Parameters
        ----------
        page_id
            The ID of the cached page.
        lsn
            The LSN of the operation that mutated the page.
        """
        cached_page = self._cache[page_id]
        cached_page.is_dirty = True
        cached_page.page.last_modified_lsn = max(cached_page.page.last_modified_lsn, lsn)

    def delete(self, page_id: int) -> None:
        """
        Drop a page from the pool, e.g. after the underlying page was freed by the pager.

        Parameters
        ----------
        page_id
            The ID of the page to drop. Missing IDs are silently ignored.
        """
        self._cache.pop(page_id, None)

    def flush_all(self) -> None:
        """Write every dirty page back to the pager and mark it clean."""
        self.stats.flushes += 1
        for page_id, cached_page in self._cache.items():
            if cached_page.is_dirty:
                self.pager.write_page(
                    page_id, encode_page(cached_page.page, self.pager.page_size_bytes)
                )
                cached_page.is_dirty = False
                self.stats.dirty_pages_flushed += 1

    def get_dirty_page_ids(self) -> list[int]:
        """
        List the dirty pages currently held in the pool.

        Returns
        -------
        The IDs of all dirty pages, in insertion order.
        """
        result = [page_id for page_id, cached_page in self._cache.items() if cached_page.is_dirty]
        return result

    def _evict_oldest_clean_page_if_full(self) -> None:
        """
        Evict the oldest clean page when the pool is at capacity.

        Raises
        ------
        DBBufferPoolOverflowError
            If the pool is at capacity and every cached page is dirty.
        """
        if not self.enable_eviction:
            return

        if len(self._cache) >= self.capacity_pages:
            try:
                page_id_to_evict = next(
                    page_id
                    for page_id, cached_page in self._cache.items()
                    if not cached_page.is_dirty
                )
            except StopIteration:
                raise DBBufferPoolOverflowError() from None

            del self._cache[page_id_to_evict]
            self.stats.evictions += 1
