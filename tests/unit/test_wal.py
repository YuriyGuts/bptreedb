import pytest

from bptreedb.codec import encode_wal_record
from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.exceptions import DBCorruptedError
from bptreedb.wal import WAL


@pytest.fixture
def make_wal(tmp_path):
    def _make_wal(contents):
        path = tmp_path / "wal"
        path.write_bytes(contents)
        return WAL(path)

    return _make_wal


def collect_replay(wal):
    records = []
    wal.replay(records.append)
    return records


def test_replay_empty_wal_no_records(make_wal):
    # GIVEN an empty WAL file
    contents = b""
    with make_wal(contents) as wal:
        # WHEN replaying the WAL
        records = collect_replay(wal)
        # THEN it should return an empty list
        assert isinstance(records, list)
        assert records == []


def test_replay_wal_with_single_record(make_wal):
    # GIVEN a WAL file with a single PUT record
    record = WALPutRecord(lsn=1, key=b"foo", value=b"bar")
    contents = encode_wal_record(record)
    with make_wal(contents) as wal:
        # WHEN replaying the WAL
        records = collect_replay(wal)
        # THEN it should return an interator with that record
        assert records == [record]


def test_replay_wal_with_multiple_records(make_wal):
    # GIVEN a WAL file multiple records
    put_record_1 = WALPutRecord(lsn=1, key=b"foo", value=b"bar")
    delete_record = WALDeleteRecord(lsn=2, key=b"foo")
    put_record_2 = WALPutRecord(lsn=3, key=b"baz", value=b"qux")

    contents = bytearray()
    contents += encode_wal_record(put_record_1)
    contents += encode_wal_record(delete_record)
    contents += encode_wal_record(put_record_2)

    with make_wal(bytes(contents)) as wal:
        # WHEN replaying the WAL
        records = collect_replay(wal)
        # THEN it should return an interator with all records in order
        assert records == [put_record_1, delete_record, put_record_2]


def test_replay_broken_wal_last_record(make_wal):
    # GIVEN a WAL file with 3 records where the last record is unfinished
    put_record_1 = WALPutRecord(lsn=1, key=b"foo", value=b"bar")
    delete_record = WALDeleteRecord(lsn=2, key=b"foo")
    put_record_2 = WALPutRecord(lsn=3, key=b"baz", value=b"qux")

    contents = bytearray()
    contents += encode_wal_record(put_record_1)
    contents += encode_wal_record(delete_record)
    contents += encode_wal_record(put_record_2)[:5]

    with make_wal(bytes(contents)) as wal:
        # WHEN replaying the WAL
        records = collect_replay(wal)
        # THEN it should return an interator with all records until the broken one
        assert records == [put_record_1, delete_record]
        # THEN it should truncate the file after the last known good record
        assert wal.path.stat().st_size == len(contents) - 5


def test_replay_broken_wal_broken_crc_mid_file(make_wal):
    # GIVEN a WAL file with 3 records where the middle record has a broken CRC
    put_record_1 = WALPutRecord(lsn=1, key=b"foo", value=b"bar")
    delete_record = WALDeleteRecord(lsn=2, key=b"foo")
    put_record_2 = WALPutRecord(lsn=3, key=b"baz", value=b"qux")

    contents = bytearray()
    contents += encode_wal_record(put_record_1)
    contents += encode_wal_record(delete_record)[:-4] + b"\x01\x02\x03\x04"
    contents += encode_wal_record(put_record_2)

    with make_wal(bytes(contents)) as wal:
        # WHEN replaying the WAL
        # THEN it should raise an exception
        msg = "WAL contains a broken record followed by a valid record"
        with pytest.raises(DBCorruptedError, match=msg):
            collect_replay(wal)


def test_replay_broken_wal_out_of_order_lsn(make_wal):
    # GIVEN a WAL file with 3 records where the last record has a non-sequential LSN
    put_record_1 = WALPutRecord(lsn=1, key=b"foo", value=b"bar")
    delete_record = WALDeleteRecord(lsn=2, key=b"foo")
    put_record_2 = WALPutRecord(lsn=4, key=b"baz", value=b"qux")

    contents = bytearray()
    contents += encode_wal_record(put_record_1)
    contents += encode_wal_record(delete_record)
    contents += encode_wal_record(put_record_2)

    with make_wal(bytes(contents)) as wal:
        # WHEN replaying the WAL
        # THEN it should raise an exception
        msg = "WAL contains non-sequential LSNs: 2 followed by 4"
        with pytest.raises(DBCorruptedError, match=msg):
            collect_replay(wal)


def test_append_put(make_wal):
    # GIVEN an empty WAL
    with make_wal(b"") as wal:
        # WHEN appending two PUT records
        wal.append_put(b"foo", b"bar")
        wal.append_put(b"baz", b"qux")
        # THEN both records should replay and have sequential LSNs
        records = collect_replay(wal)
        assert records == [
            WALPutRecord(lsn=1, key=b"foo", value=b"bar"),
            WALPutRecord(lsn=2, key=b"baz", value=b"qux"),
        ]


def test_append_delete(make_wal):
    # GIVEN an empty WAL
    with make_wal(b"") as wal:
        # WHEN appending two DELETE records
        wal.append_delete(b"foo")
        wal.append_delete(b"qux")
        # THEN both records should replay and have sequential LSNs
        records = collect_replay(wal)
        assert records == [
            WALDeleteRecord(lsn=1, key=b"foo"),
            WALDeleteRecord(lsn=2, key=b"qux"),
        ]


def test_append_after_replay(make_wal):
    # GIVEN a WAL file with preexisting records
    put_record_1 = WALPutRecord(lsn=1, key=b"foo", value=b"bar")
    delete_record = WALDeleteRecord(lsn=2, key=b"foo")

    preexisting_wal_contents = bytearray()
    preexisting_wal_contents += encode_wal_record(put_record_1)
    preexisting_wal_contents += encode_wal_record(delete_record)

    with make_wal(preexisting_wal_contents) as wal:
        # WHEN replaying the WAL and appending a new record
        preexisting_wal_records = collect_replay(wal)
        wal.append_put(key=b"baz", value=b"qux")
        # THEN the new record shows up in the WAL with the correct sequential LSN
        replayed_records = collect_replay(wal)
        expected_new_wal_records = [WALPutRecord(lsn=3, key=b"baz", value=b"qux")]
        assert replayed_records == preexisting_wal_records + expected_new_wal_records
