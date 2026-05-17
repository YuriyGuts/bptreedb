import pytest

from bptreedb.codec import decode_meta_page
from bptreedb.codec import decode_next_wal_record_from_file
from bptreedb.codec import decode_page
from bptreedb.codec import decode_wal_record
from bptreedb.codec import encode_meta_page
from bptreedb.codec import encode_page
from bptreedb.codec import encode_wal_record
from bptreedb.codec import get_max_freed_ids_per_freelist_page
from bptreedb.entities import FreelistPage
from bptreedb.entities import InternalPage
from bptreedb.entities import InternalSlot
from bptreedb.entities import LeafPage
from bptreedb.entities import LeafSlot
from bptreedb.entities import MetaPage
from bptreedb.entities import WALCheckpointRecord
from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.exceptions import DBChecksumError


@pytest.fixture
def make_wal(tmp_path):
    def _make_wal(contents):
        path = tmp_path / "wal"
        path.write_bytes(contents)
        return path

    return _make_wal


def test_wal_put_record_encode_decode():
    # GIVEN a WAL PUT record
    record = WALPutRecord(
        lsn=123,
        key=b"foo",
        value=b"45678",
    )
    # WHEN encoding it to wire format
    encoded = encode_wal_record(record)
    # THEN it should produce the correct bytes
    assert encoded == (
        b"\x1d\x00\x00\x00"
        b"\x7b\x00\x00\x00\x00\x00\x00\x00\x01"
        b"\x03\x00\x00\x00foo"
        b"\x05\x00\x00\x0045678"
        b"\x93\xb8\x3a\x96"
    )

    # WHEN decoding the encoded data back
    decoded = decode_wal_record(encoded)
    # THEN it should produce the original record
    assert decoded == record


def test_wal_delete_record_encode_decode():
    # GIVEN a WAL PUT record
    record = WALDeleteRecord(
        lsn=123,
        key=b"foo",
    )
    # WHEN encoding it to wire format
    encoded = encode_wal_record(record)
    # THEN it should produce the correct bytes
    assert encoded == (
        b"\x14\x00\x00\x00\x7b\x00\x00\x00\x00\x00\x00\x00\x02\x03\x00\x00\x00foo\x86\xe2\xbc\x24"
    )

    # WHEN decoding the encoded data back
    decoded = decode_wal_record(encoded)
    # THEN it should produce the original record
    assert decoded == record


def test_wal_record_decode_bad_crc():
    # GIVEN an encoded WAL record with a bad CRC
    record = WALPutRecord(
        lsn=123,
        key=b"foo",
        value=b"45678",
    )
    encoded = bytearray(encode_wal_record(record))
    encoded[-4:] = b"\x01\x02\x03\x04"
    encoded = bytes(encoded)

    # WHEN decoding it
    # THEN it should detect the broken CRC
    with pytest.raises(
        DBChecksumError,
        match="Checksum mismatch: expected 0x04030201, actual 0x963ab893",
    ):
        decode_wal_record(encoded)


def test_decode_next_wal_record_from_file(make_wal):
    # GIVEN a WAL file containing a valid record
    record = WALPutRecord(
        lsn=123,
        key=b"foo",
        value=b"45678",
    )
    encoded = encode_wal_record(record)
    wal_path = make_wal(encoded)

    with open(wal_path, "rb") as file:
        # WHEN decoding it
        decoded_record = decode_next_wal_record_from_file(file)
        # THEN it should match the original record
        assert decoded_record == record
        # THEN the file cursor should move exactly past the record
        assert file.tell() == len(encoded)


def test_decode_next_wal_record_from_file_torn_at_length_field(make_wal):
    # GIVEN an WAL containing a record torn in the middle of the length field
    wal_path = make_wal(b"\x01\x02")
    with open(wal_path, "rb") as file:
        # WHEN decoding it
        # THEN it should detect the broken length field as an unexpected EOF
        with pytest.raises(EOFError):
            decode_next_wal_record_from_file(file)


def test_decode_next_wal_record_from_file_torn_at_payload(make_wal):
    # GIVEN an WAL containing a record torn in the middle of the payload
    wal_path = make_wal(b"\x10\x00\x00\x00\x01\x02\x03\x04\x05")
    with open(wal_path, "rb") as file:
        # WHEN decoding it
        # THEN it should detect the broken length field as an unexpected EOF
        with pytest.raises(EOFError):
            decode_next_wal_record_from_file(file)


def test_meta_page_encode_decode():
    page = MetaPage(
        page_size_bytes=256,
        root_page_id=12345,
        next_page_id=67890,
        freelist_head_page_id=555,
        last_checkpoint_lsn=99,
    )
    encoded = encode_meta_page(page)
    expected_payload = (
        b"BPTREEDB\x01\x00\x00\x00"
        b"\x00\x01\x00\x00"
        b"\x39\x30\x00\x00\x00\x00\x00\x00"
        b"\x32\x09\x01\x00\x00\x00\x00\x00"
        b"\x2b\x02\x00\x00\x00\x00\x00\x00"
        b"\x63\x00\x00\x00\x00\x00\x00\x00"
        b"\xa0\x5c\x4d\x1f"
    )
    padding = bytes(204)
    assert len(encoded) == page.page_size_bytes
    assert encoded == expected_payload + padding

    decoded = decode_meta_page(encoded)
    assert decoded == page


def test_wal_checkpoint_record_encode_decode():
    # GIVEN a CHECKPOINT WAL record
    record = WALCheckpointRecord(lsn=123, root_page_id=5, freelist_head=0, next_page_id=7)
    # WHEN encoding it to wire format
    encoded = encode_wal_record(record)
    # THEN it should produce the correct bytes
    assert encoded == (
        b"\x25\x00\x00\x00"
        b"\x7b\x00\x00\x00\x00\x00\x00\x00\x03"
        b"\x05\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x07\x00\x00\x00\x00\x00\x00\x00"
        b"\x9d\xf3\x11\x8e"
    )

    # WHEN decoding the encoded data back
    decoded = decode_wal_record(encoded)
    # THEN it should produce the original record
    assert decoded == record


def test_internal_page_encode_decode():
    page = InternalPage(
        last_modified_lsn=23456,
        leftmost_child_page_id=12345,
        slots=[
            InternalSlot(b"bar", 42),
            InternalSlot(b"foo", 43),
            InternalSlot(b"quux", 44),
        ],
    )
    expected_header = (
        b"\x01\x00\x00\x00"
        b"\x03\x00\x00\x00"
        b"\x38\x00\x00\x00"
        b"\xd2\x00\x00\x00"
        b"\xa0\x5b\x00\x00\x00\x00\x00\x00"
        b"\x39\x30\x00\x00\x00\x00\x00\x00"
    )
    expected_slots = (
        b"\xf1\x00\x00\x00\x0f\x00\x00\x00"
        b"\xe2\x00\x00\x00\x0f\x00\x00\x00"
        b"\xd2\x00\x00\x00\x10\x00\x00\x00"
    )
    expected_records = (
        b"\x04\x00\x00\x00quux\x2c\x00\x00\x00\x00\x00\x00\x00"
        b"\x03\x00\x00\x00foo\x2b\x00\x00\x00\x00\x00\x00\x00"
        b"\x03\x00\x00\x00bar\x2a\x00\x00\x00\x00\x00\x00\x00"
    )
    expected_free_space = bytes(154)
    encoded = encode_page(page, page_size_bytes=256)
    assert encoded == expected_header + expected_slots + expected_free_space + expected_records

    decoded = decode_page(encoded)
    assert decoded == page


def test_leaf_page_encode_decode():
    page = LeafPage(
        last_modified_lsn=12345,
        right_sibling_page_id=67890,
        slots=[
            LeafSlot(b"baz", b"qux"),
            LeafSlot(b"corge", b"thud"),
            LeafSlot(b"foo", b"bar"),
        ],
    )
    expected_header = (
        b"\x02\x00\x00\x00"
        b"\x03\x00\x00\x00"
        b"\x38\x00\x00\x00"
        b"\xd3\x00\x00\x00"
        b"\x39\x30\x00\x00\x00\x00\x00\x00"
        b"\x32\x09\x01\x00\x00\x00\x00\x00"
    )
    expected_slots = (
        b"\xf2\x00\x00\x00\x0e\x00\x00\x00"
        b"\xe1\x00\x00\x00\x11\x00\x00\x00"
        b"\xd3\x00\x00\x00\x0e\x00\x00\x00"
    )
    expected_records = (
        b"\x03\x00\x00\x00foo\x03\x00\x00\x00bar"
        b"\x05\x00\x00\x00corge\x04\x00\x00\x00thud"
        b"\x03\x00\x00\x00baz\x03\x00\x00\x00qux"
    )
    expected_free_space = bytes(155)
    encoded = encode_page(page, page_size_bytes=256)
    assert encoded == expected_header + expected_slots + expected_free_space + expected_records

    decoded = decode_page(encoded)
    assert decoded == page


def test_freelist_page_encode_decode():
    # GIVEN a freelist page with a few freed ids and a successor link
    page = FreelistPage(
        last_modified_lsn=99,
        next_freelist_page_id=77,
        freed_page_ids=[10, 20, 30],
    )
    # WHEN encoding it to wire format
    encoded = encode_page(page, page_size_bytes=256)
    # THEN it should produce the correct bytes
    expected_header = (
        b"\x03\x00\x00\x00"
        b"\x03\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x63\x00\x00\x00\x00\x00\x00\x00"
        b"\x4d\x00\x00\x00\x00\x00\x00\x00"
    )
    expected_freed_ids = (
        b"\x0a\x00\x00\x00\x00\x00\x00\x00"
        b"\x14\x00\x00\x00\x00\x00\x00\x00"
        b"\x1e\x00\x00\x00\x00\x00\x00\x00"
    )
    expected_padding = bytes(200)
    assert encoded == expected_header + expected_freed_ids + expected_padding

    # WHEN decoding the encoded data back
    decoded = decode_page(encoded)
    # THEN it should produce the original page
    assert decoded == page


def test_freelist_page_encode_decode_empty():
    # GIVEN a freelist page with no freed ids and no successor
    page = FreelistPage(
        last_modified_lsn=0,
        next_freelist_page_id=0,
        freed_page_ids=[],
    )
    # WHEN encoding it to wire format
    encoded = encode_page(page, page_size_bytes=256)
    # THEN the header is all zero past the type tag and the body is full padding
    expected_header = (
        b"\x03\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00"
    )
    assert encoded == expected_header + bytes(224)

    # WHEN decoding the encoded data back
    # THEN it should produce the original page
    assert decode_page(encoded) == page


def test_get_max_freed_ids_per_freelist_page():
    # GIVEN the freelist layout reserves a 32-byte header and 8 bytes per freed id
    # WHEN computing the capacity at typical page sizes
    # THEN it should return the body size divided by entry size
    assert get_max_freed_ids_per_freelist_page(256) == (256 - 32) // 8
    assert get_max_freed_ids_per_freelist_page(4096) == (4096 - 32) // 8
