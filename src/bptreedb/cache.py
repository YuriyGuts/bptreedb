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
    page: LeafPage | InternalPage
    is_dirty: bool


@dataclass
class BufferPoolStats:
    cache_hits: int = 0
    cache_misses: int = 0
    evictions: int = 0
    flushes: int = 0
    dirty_pages_flushed: int = 0

    def reset(self) -> None:
        self.cache_hits = 0
        self.cache_misses = 0
        self.evictions = 0
        self.flushes = 0
        self.dirty_pages_flushed = 0


class BufferPool:
    def __init__(self, pager: Pager, capacity_pages: int) -> None:
        self.pager = pager
        self.capacity_pages = capacity_pages
        self.enable_eviction = True
        self.stats = BufferPoolStats()
        self._cache: OrderedDict[int, CachedPage] = OrderedDict()

    @property
    def dirty_count(self) -> int:
        return sum(1 for cached_page in self._cache.values() if cached_page.is_dirty)

    @property
    def dirty_ratio(self) -> float:
        return self.dirty_count / self.capacity_pages

    def get(self, page_id: int) -> LeafPage | InternalPage:
        # Cache hit.
        if page_id in self._cache:
            self._cache.move_to_end(page_id)
            self.stats.cache_hits += 1
            return self._cache[page_id].page

        # Cache miss, capacity full.
        self._evict_oldest_clean_page_if_full()

        # Cache miss, below capacity.
        page = decode_page(self.pager.read_page(page_id))
        self._cache[page_id] = CachedPage(page=page, is_dirty=False)
        self.stats.cache_misses += 1
        return page

    def insert(self, page_id: int, page: LeafPage | InternalPage, lsn: int) -> None:
        assert page_id not in self._cache
        self._evict_oldest_clean_page_if_full()
        page.last_modified_lsn = lsn
        self._cache[page_id] = CachedPage(page=page, is_dirty=True)

    def mark_dirty(self, page_id: int, lsn: int) -> None:
        cached_page = self._cache[page_id]
        cached_page.is_dirty = True
        cached_page.page.last_modified_lsn = max(cached_page.page.last_modified_lsn, lsn)

    def flush_all(self) -> None:
        self.stats.flushes += 1
        for page_id, cached_page in self._cache.items():
            if cached_page.is_dirty:
                self.pager.write_page(
                    page_id, encode_page(cached_page.page, self.pager.page_size_bytes)
                )
                cached_page.is_dirty = False
                self.stats.dirty_pages_flushed += 1

    def get_dirty_page_ids(self) -> list[int]:
        result = [page_id for page_id, cached_page in self._cache.items() if cached_page.is_dirty]
        return result

    def _evict_oldest_clean_page_if_full(self) -> None:
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
