import zlib

import pytest

from bptreedb.codec import decode_meta_page
from bptreedb.codec import decode_page
from bptreedb.codec import encode_meta_page
from bptreedb.entities import LeafPage
from bptreedb.entities import MetaPage
from bptreedb.exceptions import DBChecksumError
from bptreedb.exceptions import DBCorruptedError
from bptreedb.pager import Pager


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data.db"


def test_fresh_directory_bootstrap(path):
    with Pager(path, page_size_bytes=256) as pager:
        assert path.stat().st_size == pager.page_size_bytes * 2

    with open(path, "rb") as f:
        meta_page = decode_meta_page(f.read(pager.page_size_bytes))
        leaf_page = decode_page(f.read(pager.page_size_bytes))

        assert meta_page == MetaPage(
            page_size_bytes=256,
            root_page_id=1,
            next_page_id=2,
            last_checkpoint_lsn=0,
        )
        assert leaf_page == LeafPage(
            last_modified_lsn=0,
            right_sibling_page_id=0,
            slots=[],
        )


def test_reopen(path):
    pager = Pager(path, page_size_bytes=256)

    try:
        pager.open()
        meta1 = pager.get_meta()
        leaf1 = decode_page(pager.read_page(1))
    finally:
        pager.close()

    try:
        pager.open()
        meta2 = pager.get_meta()
        leaf2 = decode_page(pager.read_page(1))
    finally:
        pager.close()

    assert (
        meta1
        == meta2
        == MetaPage(page_size_bytes=256, root_page_id=1, next_page_id=2, last_checkpoint_lsn=0)
    )
    assert leaf1 == leaf2 == LeafPage(last_modified_lsn=0, right_sibling_page_id=0, slots=[])


def test_open_with_conflicting_page_size(path):
    with Pager(path, page_size_bytes=256) as pager:
        assert pager.page_size_bytes == 256
    with Pager(path, page_size_bytes=1024) as pager:
        assert pager.page_size_bytes == 256


def test_open_with_broken_crc(path):
    # The encoded meta page is `<8sIIQQQ>` = 40 bytes followed by a 4-byte CRC.
    crc_offset = 40
    valid = encode_meta_page(
        MetaPage(page_size_bytes=256, root_page_id=1, next_page_id=2, last_checkpoint_lsn=0)
    )
    corrupted = valid[:crc_offset] + b"\x01\x02\x03\x04"
    path.write_bytes(corrupted)

    actual_crc = zlib.crc32(valid[:crc_offset])
    msg = f"Checksum mismatch: expected 0x04030201, actual 0x{actual_crc:08x}"
    with pytest.raises(DBChecksumError, match=msg):
        with Pager(path, page_size_bytes=256):
            pass


def test_read_write_page(path):
    with Pager(path, page_size_bytes=256) as pager:
        written_data = bytes(range(256))
        pager.write_page(1, written_data)
        read_data = pager.read_page(1)
        assert read_data == written_data


def test_update_meta(path):
    with Pager(path, page_size_bytes=256) as pager:
        file_contents_before = path.read_bytes()
        initial_meta = pager.get_meta()
        pager.update_meta(next_page_id=42)
        updated_meta = pager.get_meta()
        file_contents_after = path.read_bytes()

    assert file_contents_before == file_contents_after
    assert initial_meta == MetaPage(
        page_size_bytes=256, root_page_id=1, next_page_id=2, last_checkpoint_lsn=0
    )
    assert updated_meta == MetaPage(
        page_size_bytes=256, root_page_id=1, next_page_id=42, last_checkpoint_lsn=0
    )


def test_flush_meta(path):
    with Pager(path, page_size_bytes=256) as pager:
        initial_meta = pager.get_meta()
        pager.update_meta(root_page_id=99, next_page_id=42)
        pager.flush_meta()

    with Pager(path, page_size_bytes=256) as pager:
        updated_meta = pager.get_meta()

    assert initial_meta == MetaPage(
        page_size_bytes=256, root_page_id=1, next_page_id=2, last_checkpoint_lsn=0
    )
    assert updated_meta == MetaPage(
        page_size_bytes=256, root_page_id=99, next_page_id=42, last_checkpoint_lsn=0
    )


def test_allocate_page(path):
    with Pager(path, page_size_bytes=256) as pager:
        pager.allocate_page()
        pager.allocate_page()
        pager.allocate_page()
        final_meta = pager.get_meta()

    assert path.stat().st_size == pager.page_size_bytes * 5
    assert final_meta == MetaPage(
        page_size_bytes=256, root_page_id=1, next_page_id=5, last_checkpoint_lsn=0
    )


def test_read_nonexistent(path):
    with Pager(path, page_size_bytes=256) as pager:
        with pytest.raises(DBCorruptedError, match="Unexpected end of file while reading page 2"):
            pager.read_page(2)
