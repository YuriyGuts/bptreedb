from bptreedb.db import DB


def test_db_put_durability(tmp_path):
    # GIVEN a database with four keys
    with DB(tmp_path) as db1:
        db1.put(b"foo", b"\x01")
        db1.put(b"bar", b"\x02")
        db1.put(b"baz", b"\x03")
        db1.put(b"qux", b"\x04")

    # WHEN closing and reopening the database
    # THEN all keys should be still there
    with DB(tmp_path) as db2:
        assert list(db2.scan(None, None)) == [
            (b"bar", b"\x02"),
            (b"baz", b"\x03"),
            (b"foo", b"\x01"),
            (b"qux", b"\x04"),
        ]


def test_db_delete_durability(tmp_path):
    # GIVEN a database with four keys
    with DB(tmp_path) as db1:
        db1.put(b"foo", b"\x01")
        db1.put(b"bar", b"\x02")
        db1.put(b"baz", b"\x03")
        db1.put(b"qux", b"\x04")

        # WHEN deleting a few keys and reopening the DB
        db1.delete(b"baz")
        db1.delete(b"foo")

    # THEN the deleted keys should no longer be there
    with DB(tmp_path) as db2:
        assert list(db2.scan(None, None)) == [
            (b"bar", b"\x02"),
            (b"qux", b"\x04"),
        ]


def test_db_reopen_after_close(tmp_path):
    db = DB(tmp_path)
    try:
        db.open()
        db.put(b"foo", b"bar")
        db.put(b"baz", b"qux")
    finally:
        db.close()

    try:
        db.open()
        reopened_records = list(db.scan(None, None))
        assert reopened_records == [(b"baz", b"qux"), (b"foo", b"bar")]
    finally:
        db.close()
