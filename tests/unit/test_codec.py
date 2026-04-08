import pytest

from bptreedb.codec import decode_next_wal_record_from_file
from bptreedb.codec import decode_wal_record
from bptreedb.codec import encode_wal_record
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
