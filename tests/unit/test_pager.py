import zlib

import pytest

from bptreedb.codec import decode_meta_page
from bptreedb.codec import decode_page
from bptreedb.codec import encode_meta_page
from bptreedb.entities import FreelistPage
from bptreedb.entities import LeafPage
from bptreedb.entities import MetaPage
from bptreedb.exceptions import DBChecksumError
from bptreedb.exceptions import DBCorruptedError
from bptreedb.pager import Pager


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data.db"


def _meta(**overrides):
    defaults = {
        "page_size_bytes": 256,
        "root_page_id": 1,
        "next_page_id": 2,
        "freelist_head_page_id": 0,
        "last_checkpoint_lsn": 0,
    }
    return MetaPage(**(defaults | overrides))


def test_fresh_directory_bootstrap(path):
    # GIVEN a path with no existing database file
    # WHEN opening a pager there
    with Pager(path, page_size_bytes=256) as pager:
        pass

    # THEN the file contains exactly a meta page and an empty bootstrap leaf
    assert path.stat().st_size == pager.page_size_bytes * 2
    with open(path, "rb") as f:
        meta_page = decode_meta_page(f.read(pager.page_size_bytes))
        leaf_page = decode_page(f.read(pager.page_size_bytes))
    assert meta_page == _meta()
    assert leaf_page == LeafPage(last_modified_lsn=0, right_sibling_page_id=0, slots=[])


def test_reopen(path):
    pager = Pager(path, page_size_bytes=256)

    # GIVEN a pager that has been opened, read from, and closed
    try:
        pager.open()
        meta1 = pager.get_meta()
        leaf1 = decode_page(pager.read_page(1))
    finally:
        pager.close()

    # WHEN reopening the same path
    try:
        pager.open()
        meta2 = pager.get_meta()
        leaf2 = decode_page(pager.read_page(1))
    finally:
        pager.close()

    # THEN the meta and bootstrap leaf round-trip identically
    assert meta1 == meta2 == _meta()
    assert leaf1 == leaf2 == LeafPage(last_modified_lsn=0, right_sibling_page_id=0, slots=[])


def test_open_with_conflicting_page_size(path):
    # GIVEN a database file created with one page size
    with Pager(path, page_size_bytes=256) as pager:
        assert pager.page_size_bytes == 256

    # WHEN reopening with a different page size
    with Pager(path, page_size_bytes=1024) as pager:
        # THEN the original page size from the file wins
        assert pager.page_size_bytes == 256


def test_open_with_broken_crc(path):
    # The encoded meta page is `<8sIIQQQQ>` = 48 bytes followed by a 4-byte CRC.
    crc_offset = 48

    # GIVEN a meta page on disk whose CRC has been clobbered with arbitrary bytes
    valid = encode_meta_page(_meta())
    corrupted = valid[:crc_offset] + b"\x01\x02\x03\x04"
    path.write_bytes(corrupted)

    # WHEN opening the pager
    # THEN it raises a checksum error reporting the stored vs. recomputed CRCs
    actual_crc = zlib.crc32(valid[:crc_offset])
    msg = f"Checksum mismatch: expected 0x04030201, actual 0x{actual_crc:08x}"
    with pytest.raises(DBChecksumError, match=msg):
        with Pager(path, page_size_bytes=256):
            pass


def test_read_write_page(path):
    with Pager(path, page_size_bytes=256) as pager:
        # GIVEN an open pager and a full page worth of arbitrary bytes
        written_data = bytes(range(256))

        # WHEN writing the bytes and reading them back
        pager.write_page(1, written_data)
        read_data = pager.read_page(1)

    # THEN the data round-trips byte-for-byte
    assert read_data == written_data


def test_update_meta(path):
    with Pager(path, page_size_bytes=256) as pager:
        # GIVEN an open pager with the bootstrap meta page on disk
        file_contents_before = path.read_bytes()
        initial_meta = pager.get_meta()

        # WHEN updating meta in memory without flushing
        pager.update_meta(next_page_id=42)
        updated_meta = pager.get_meta()
        file_contents_after = path.read_bytes()

    # THEN in-memory meta reflects the change but the file on disk is untouched
    assert file_contents_before == file_contents_after
    assert initial_meta == _meta()
    assert updated_meta == _meta(next_page_id=42)


def test_flush_meta(path):
    # GIVEN a pager whose meta has been updated in memory and explicitly flushed
    with Pager(path, page_size_bytes=256) as pager:
        initial_meta = pager.get_meta()
        pager.update_meta(root_page_id=99, next_page_id=42)
        pager.flush_meta()

    # WHEN reopening the file
    with Pager(path, page_size_bytes=256) as pager:
        updated_meta = pager.get_meta()

    # THEN the flushed values persisted across the reopen
    assert initial_meta == _meta()
    assert updated_meta == _meta(root_page_id=99, next_page_id=42)


def test_allocate_page(path):
    with Pager(path, page_size_bytes=256) as pager:
        # GIVEN a fresh pager with just the meta page and the bootstrap leaf
        # WHEN bump-allocating three more pages
        pager.allocate_page()
        pager.allocate_page()
        pager.allocate_page()
        final_meta = pager.get_meta()

    # THEN the file grows by three pages and next_page_id advances accordingly
    assert path.stat().st_size == pager.page_size_bytes * 5
    assert final_meta == _meta(next_page_id=5)


def test_read_nonexistent(path):
    with Pager(path, page_size_bytes=256) as pager:
        # GIVEN a fresh pager whose file only holds the meta + bootstrap leaf
        # WHEN reading a page id past the end of the file
        # THEN it raises a corruption error pointing at the missing page
        with pytest.raises(DBCorruptedError, match="Unexpected end of file while reading page 2"):
            pager.read_page(2)


def test_free_then_allocate_recycles_page_id(path):
    # The first free against an empty freelist bump-allocates a head page, so next_page_id
    # advances by one beyond the bump count.
    with Pager(path, page_size_bytes=256) as pager:
        # GIVEN a pager with two bump-allocated pages past the bootstrap leaf
        id_a = pager.allocate_page()
        id_b = pager.allocate_page()

        # WHEN one page is freed and a new page is allocated
        pager.free_page(id_a)
        recycled = pager.allocate_page()
        meta = pager.get_meta()

    # THEN the new allocation returns the previously freed id, and the freelist head sits
    # at the next bump-allocated slot
    assert id_a == 2
    assert id_b == 3
    assert recycled == id_a
    assert meta.freelist_head_page_id == 4


def test_allocate_recycles_exhausted_head(path):
    with Pager(path, page_size_bytes=256) as pager:
        # GIVEN a freelist whose only entry has just been popped, leaving the head page empty
        freed_id = pager.allocate_page()
        pager.free_page(freed_id)
        head_id = pager.get_meta().freelist_head_page_id
        pager.allocate_page()

        # WHEN allocating once more
        recycled_head = pager.allocate_page()
        meta_after = pager.get_meta()

    # THEN the exhausted head page itself is handed back, and the freelist becomes empty
    assert recycled_head == head_id
    assert meta_after.freelist_head_page_id == 0


def test_free_overflows_into_new_head(path):
    # A 64-byte page holds (64 - 32) / 8 = 4 freed ids per head page.
    page_size = 64
    with Pager(path, page_size_bytes=page_size) as pager:
        # GIVEN five bump-allocated pages, which is one more than fits on a single head page
        to_free = [pager.allocate_page() for _ in range(5)]

        # WHEN they are all freed in order
        for page_id in to_free:
            pager.free_page(page_id)

        new_head_id = pager.get_meta().freelist_head_page_id
        pager.flush_dirty_freelist_pages()
        new_head = decode_page(pager.read_page(new_head_id))
        old_head = decode_page(pager.read_page(new_head.next_freelist_page_id))

    # THEN the new head carries only the overflow entry and links back to the now-full old head
    assert isinstance(new_head, FreelistPage)
    assert isinstance(old_head, FreelistPage)
    assert new_head.freed_page_ids == [to_free[-1]]
    assert old_head.freed_page_ids == to_free[:4]
