from collections.abc import Iterator
from unittest.mock import patch

import pytest

from bptreedb.db import DB
from bptreedb.exceptions import DBClosedError


@pytest.fixture
def mock_close():
    with patch("bptreedb.db.DB.close") as p:
        yield p


@pytest.fixture
def db(tmp_path):
    with DB(tmp_path) as db:
        yield db


def test_context_manager(tmp_path, mock_close):
    # GIVEN a DB opened as a context manager
    with DB(tmp_path) as db:
        # WHEN working inside the context
        # THEN the return value is a DB instance
        assert db.is_opened
        assert isinstance(db, DB)

    # WHEN the context manager exits
    # THEN `close` is called.
    db.close.assert_called_once()


def test_closed_db_rejects_operations(db):
    db.close()
    with pytest.raises(DBClosedError, match="not opened"):
        db.put(b"foo", b"bar")
    with pytest.raises(DBClosedError, match="not opened"):
        db.get(b"foo")
    with pytest.raises(DBClosedError, match="not opened"):
        db.delete(b"foo")
    with pytest.raises(DBClosedError, match="not opened"):
        db.scan(None, None)


def test_get_nonexistent(db):
    # GIVEN an empty DB
    # WHEN reading a nonexistent key
    # THEN it should return None
    assert db.get(b"foo") is None


def test_put_followed_by_get(db):
    # GIVEN a DB with one key
    db.put(b"foo", b"bar")
    # WHEN reading it
    # THEN it should return its value
    assert db.get(b"foo") == b"bar"


def test_put_overwrite(db):
    # GIVEN a DB with one key
    db.put(b"foo", b"bar")
    # WHEN overwriting it
    db.put(b"foo", b"qux")
    # THEN the new value should be read back
    assert db.get(b"foo") == b"qux"


def test_delete_nonexistent(db):
    # GIVEN an empty DB
    # WHEN deleting a nonexistent key
    # THEN it should return False
    assert db.delete(b"foo") is False


def test_put_followed_by_delete(db):
    # GIVEN an DB with one key
    # WHEN deleting the key
    db.put(b"foo", b"bar")

    # THEN it should return True
    assert db.delete(b"foo") is True

    # WHEN trying to read the key via `get` or `scan`
    # THEN the key should no longer be there
    assert db.get(b"foo") is None
    assert list(db.scan(None, None)) == []


def test_scan_full_db_empty(db):
    # GIVEN an empty DB
    # WHEN scanning the full DB
    # THEN it should yield no tuples
    assert list(db.scan(None, None)) == []


def test_scan_full_db_nonempty(db):
    # GIVEN a DB with two keys
    db.put(b"foo", b"bar")
    db.put(b"baz", b"qux")

    # WHEN running a scan query
    iterator = db.scan(None, None)

    # THEN it should return an iterator
    assert isinstance(iterator, Iterator)

    # WHEN materializing the iterator
    # THEN it should return all keys and values in the DB
    assert list(iterator) == [(b"baz", b"qux"), (b"foo", b"bar")]


def test_scan_inclusive_exclusive_left_unbounded(db):
    # GIVEN a DB with two keys
    db.put(b"foo", b"bar")
    db.put(b"baz", b"qux")

    # WHEN running a scan query without the lower bound
    # THEN it should respect the upper bound
    assert list(db.scan(None, b"zzz")) == [(b"baz", b"qux"), (b"foo", b"bar")]
    assert list(db.scan(None, b"fop")) == [(b"baz", b"qux"), (b"foo", b"bar")]
    assert list(db.scan(None, b"foo")) == [(b"baz", b"qux")]
    assert list(db.scan(None, b"baz")) == []


def test_scan_inclusive_exclusive_right_unbounded(db):
    # GIVEN a DB with two keys
    db.put(b"foo", b"bar")
    db.put(b"baz", b"qux")

    # WHEN running a scan query without the upper bound
    # THEN it should respect the lower bound
    assert list(db.scan(b"baz", None)) == [(b"baz", b"qux"), (b"foo", b"bar")]
    assert list(db.scan(b"fop", None)) == []


def test_scan_inclusive_exclusive_bounded(db):
    # GIVEN a DB with two keys
    db.put(b"foo", b"bar")
    db.put(b"baz", b"qux")

    # WHEN running a scan query with upper and lower bounds
    # THEN it should respect both bounds
    assert list(db.scan(b"foo", b"baz")) == []
    assert list(db.scan(b"baa", b"foo")) == [(b"baz", b"qux")]
    assert list(db.scan(b"bba", b"fop")) == [(b"foo", b"bar")]
    assert list(db.scan(b"foo", b"foo")) == []


def test_all_keys_and_values_must_be_bytes(db):
    msg = "must have the bytes type"
    with pytest.raises(TypeError, match=msg):
        db.put("foo", b"bar")
    with pytest.raises(TypeError, match=msg):
        db.put(b"foo", "bar")
    with pytest.raises(TypeError, match=msg):
        db.delete("foo")
    with pytest.raises(TypeError, match=msg):
        db.get("foo")
    with pytest.raises(TypeError, match=msg):
        db.scan("foo", None)
    with pytest.raises(TypeError, match=msg):
        db.scan(None, "foo")
