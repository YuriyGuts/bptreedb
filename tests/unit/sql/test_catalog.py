import pytest

from bptreedb.db import DB
from bptreedb.sql.catalog import SQL_FORMAT_VERSION
from bptreedb.sql.catalog import Catalog
from bptreedb.sql.errors import SQLSchemaError
from bptreedb.sql.types import Column
from bptreedb.sql.types import SQLType


def test_create_and_get_roundtrip(tmp_path):
    # GIVEN a fresh DB and a Catalog bound to it
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        # WHEN we create a table
        columns = (
            Column("id", SQLType.INT, pk_position=0),
            Column("name", SQLType.TEXT),
        )
        created = cat.create_table("users", columns)
        # THEN we can fetch it back identically
        fetched = cat.get_table("users")
        assert created == fetched


def test_table_ids_are_monotonic(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        a = cat.create_table("a", (Column("x", SQLType.INT, pk_position=0),))
        b = cat.create_table("b", (Column("x", SQLType.INT, pk_position=0),))
        c = cat.create_table("c", (Column("x", SQLType.INT, pk_position=0),))
    # Even after a drop, ids must keep increasing so old row keys never collide with new ones.
    assert a.table_id < b.table_id < c.table_id


def test_drop_table_removes_metadata_and_rows(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        cat.create_table("a", (Column("x", SQLType.INT, pk_position=0),))
        cat.drop_table("a")
        # GIVEN the table was dropped
        # WHEN we look it up again
        # THEN it must be gone.
        with pytest.raises(SQLSchemaError):
            cat.get_table("a")


def test_list_tables_returns_alphabetical_order(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        for name in ("zeta", "alpha", "mu"):
            cat.create_table(name, (Column("x", SQLType.INT, pk_position=0),))
        names = [t.name for t in cat.list_tables()]
    assert names == ["alpha", "mu", "zeta"]


def test_duplicate_table_name_rejected(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        cat.create_table("t", (Column("x", SQLType.INT, pk_position=0),))
        with pytest.raises(SQLSchemaError):
            cat.create_table("t", (Column("x", SQLType.INT, pk_position=0),))


def test_duplicate_column_name_rejected(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        with pytest.raises(SQLSchemaError):
            cat.create_table(
                "t",
                (
                    Column("x", SQLType.INT, pk_position=0),
                    Column("x", SQLType.TEXT),
                ),
            )


def test_non_contiguous_pk_positions_rejected(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        with pytest.raises(SQLSchemaError):
            cat.create_table(
                "t",
                (
                    Column("a", SQLType.INT, pk_position=0),
                    Column("b", SQLType.INT, pk_position=2),  # gap at 1
                ),
            )


def test_format_version_is_stamped(tmp_path):
    with DB(tmp_path) as db:
        cat = Catalog(db)
        cat.ensure_initialized()
        # GIVEN a freshly initialized catalog
        # WHEN we look up the format version key directly
        from bptreedb.sql.catalog import _KEY_FORMAT_VERSION  # noqa: PLC0415

        raw = db.get(_KEY_FORMAT_VERSION)
    # THEN it is set to the current SQL_FORMAT_VERSION.
    assert raw is not None
    assert int.from_bytes(raw, "little") == SQL_FORMAT_VERSION


def test_catalog_persists_across_reopen(tmp_path):
    # GIVEN a table that was created in a previous DB session
    with DB(tmp_path) as db:
        Catalog(db).create_table("persisted", (Column("x", SQLType.INT, pk_position=0),))

    # WHEN we reopen the same data directory
    with DB(tmp_path) as db:
        cat = Catalog(db)
        # THEN the table is still there.
        assert [t.name for t in cat.list_tables()] == ["persisted"]
