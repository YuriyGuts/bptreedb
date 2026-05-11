from unittest.mock import MagicMock

import pytest

from bptreedb.cache import BufferPool
from bptreedb.codec import encode_page
from bptreedb.entities import LeafPage
from bptreedb.exceptions import DBBufferPoolOverflowError
from bptreedb.pager import Pager


@pytest.fixture
def mock_pager():
    pages = [LeafPage(last_modified_lsn=i, right_sibling_page_id=0, slots=[]) for i in range(1, 5)]
    pager = MagicMock(spec=Pager)
    pager.page_size_bytes = 256
    pager.read_page.side_effect = lambda page_id: encode_page(
        pages[page_id - 1], pager.page_size_bytes
    )
    return pager


def test_get_cache_hit(mock_pager):
    # GIVEN a buffer pool with a pager serving fake pages
    pool = BufferPool(pager=mock_pager, capacity_pages=16)

    # WHEN retrieving the same page ID multiple times
    page_1 = pool.get(1)
    page_2 = pool.get(1)
    page_3 = pool.get(1)

    # THEN it should return the same page data every time, but use the pager only the first time
    assert page_1 == page_2 == page_3
    assert page_1.last_modified_lsn == 1
    assert mock_pager.read_page.call_count == 1


def test_get_lru_eviction(mock_pager):
    # GIVEN a buffer pool with a pager serving fake pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)

    # WHEN accessing more pages than the cache's capacity
    page_1 = pool.get(1)
    page_2 = pool.get(2)
    page_3 = pool.get(3)
    page_4 = pool.get(4)

    # THEN it should read all pages correctly, but evict the least recently accessed page
    assert page_1.last_modified_lsn == 1
    assert page_2.last_modified_lsn == 2
    assert page_3.last_modified_lsn == 3
    assert page_4.last_modified_lsn == 4
    assert list(pool._cache) == [2, 3, 4]
    assert mock_pager.write_page.call_count == 0


def test_get_promotes_to_mru(mock_pager):
    # GIVEN a buffer pool filled with clean pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)
    pool.get(2)
    pool.get(3)

    # WHEN doing one cache hit and one cache miss
    pool.get(1)
    pool.get(4)

    # THEN the recently accessed pages (1, 4) should be moved to MRU and 2 should get evicted
    assert list(pool._cache) == [3, 1, 4]

    # THEN the pager should only be used for reading pages on cache misses and never for writing
    assert mock_pager.read_page.call_count == 4
    assert mock_pager.write_page.call_count == 0


def test_get_lru_eviction_all_dirty(mock_pager):
    # GIVEN a buffer pool filled with dirty pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)
    pool.get(2)
    pool.get(3)
    pool.mark_dirty(1, 2)
    pool.mark_dirty(2, 2)
    pool.mark_dirty(3, 2)

    # WHEN trying to retrieve another page
    # THEN it should raise an overflow exception
    with pytest.raises(DBBufferPoolOverflowError):
        pool.get(4)


def test_mark_dirty_reflected_in_other_methods(mock_pager):
    # GIVEN a buffer pool with a clean page
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)

    # WHEN checking the dirty pages
    # THEN it should report none
    assert pool.dirty_count == 0
    assert pool.get_dirty_page_ids() == []

    # WHEN marking the page as dirty
    pool.mark_dirty(1, 2)
    # THEN it should report the page
    assert pool.dirty_count == 1
    assert pool.get_dirty_page_ids() == [1]


def test_insert_all_clean(mock_pager):
    # GIVEN a buffer pool full of clean pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)
    pool.get(2)
    pool.get(3)
    assert pool.get_dirty_page_ids() == []
    assert mock_pager.read_page.call_count == 3

    # WHEN inserting a new page into the pool
    pool.insert(5, LeafPage(last_modified_lsn=5, right_sibling_page_id=0, slots=[]), 6)

    # THEN it should evict the oldest clean page, insert the new page as MRU, and mark it dirty
    assert list(pool._cache) == [2, 3, 5]
    assert pool.get_dirty_page_ids() == [5]
    assert pool.get(5).last_modified_lsn == 6
    assert mock_pager.read_page.call_count == 3
    assert mock_pager.write_page.call_count == 0


def test_insert_all_dirty(mock_pager):
    # GIVEN a buffer pool full of dirty pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)
    pool.get(2)
    pool.get(3)
    pool.mark_dirty(1, 2)
    pool.mark_dirty(2, 2)
    pool.mark_dirty(3, 2)
    assert pool.get_dirty_page_ids() == [1, 2, 3]

    # WHEN inserting a new page into the pool
    # THEN it should raise an overflow exception
    with pytest.raises(DBBufferPoolOverflowError):
        pool.insert(5, LeafPage(last_modified_lsn=5, right_sibling_page_id=0, slots=[]), 2)


def test_mark_dirty_updates_to_highest_lsn(mock_pager):
    # GIVEN a buffer pool with a clean page
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    page = pool.get(4)
    assert page.last_modified_lsn == 4

    # WHEN marking it dirty with a specific LSN
    # THEN it should update its last modified LSN to the higher value
    pool.mark_dirty(4, 3)
    assert pool.get(4).last_modified_lsn == 4
    pool.mark_dirty(4, 5)
    assert pool.get(4).last_modified_lsn == 5


def test_flush_all_clean(mock_pager):
    # GIVEN a buffer pool full of clean pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)
    pool.get(2)
    pool.get(3)
    assert list(pool._cache) == [1, 2, 3]
    assert mock_pager.read_page.call_count == 3

    # WHEN flushing all pages
    pool.flush_all()

    # THEN it should not write any pages and remain as is
    assert list(pool._cache) == [1, 2, 3]
    assert mock_pager.read_page.call_count == 3
    assert mock_pager.write_page.call_count == 0


def test_flush_all_dirty(mock_pager):
    # GIVEN a buffer pool containing dirty pages
    pool = BufferPool(pager=mock_pager, capacity_pages=3)
    pool.get(1)
    pool.get(2)
    pool.get(3)
    assert list(pool._cache) == [1, 2, 3]

    pool.mark_dirty(1, 2)
    pool.mark_dirty(3, 2)
    assert pool.dirty_count == 2
    assert pool.get_dirty_page_ids() == [1, 3]
    assert mock_pager.read_page.call_count == 3
    assert mock_pager.write_page.call_count == 0

    # WHEN flushing all pages
    pool.flush_all()

    # THEN it should write the dirty pages, mark them clean, and maintain the current LRU order
    assert list(pool._cache) == [1, 2, 3]
    assert mock_pager.read_page.call_count == 3
    assert mock_pager.write_page.call_count == 2
    assert pool.dirty_count == 0
    assert pool.get_dirty_page_ids() == []
