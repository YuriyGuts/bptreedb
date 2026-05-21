import pytest

from bptreedb import DB
from bptreedb.sql.errors import SQLConstraintError


def test_minimal_readme_example(tmp_path):
    # The exact example we promise in the README.
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
        db.execute("INSERT INTO users VALUES (1, 'alice', 30), (2, 'bob', 25)")
        cur = db.execute("SELECT name FROM users WHERE age > 26 ORDER BY name")
        assert list(cur) == [("alice",)]


def test_persistence_across_reopen(tmp_path):
    # GIVEN a table populated in one DB session
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    # WHEN we reopen the same data directory
    with DB(tmp_path) as db:
        # THEN the rows are still there.
        rows = db.execute("SELECT id, x FROM t ORDER BY id").fetchall()
    assert rows == [(1, "a"), (2, "b"), (3, "c")]


def test_drop_and_recreate_table_with_same_name(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 100)")
        db.execute("DROP TABLE t")
        # Recreated table starts empty even though row keys for the old table-id
        # could in principle exist; DROP must clean them up.
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, y TEXT)")
        rows = db.execute("SELECT * FROM t").fetchall()
    assert rows == []


def test_insert_select_from_another_table(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE src (id INTEGER PRIMARY KEY, v INTEGER)")
        db.execute("CREATE TABLE dst (id INTEGER PRIMARY KEY, v INTEGER)")
        db.execute("INSERT INTO src VALUES (1, 10), (2, 20), (3, 30)")
        cur = db.execute("INSERT INTO dst SELECT * FROM src")
        assert cur.rowcount == 3
        rows = db.execute("SELECT id, v FROM dst ORDER BY id").fetchall()
    assert rows == [(1, 10), (2, 20), (3, 30)]


def test_inner_join_end_to_end(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE TABLE orders (oid INTEGER PRIMARY KEY, uid INTEGER, total REAL)")
        db.execute("INSERT INTO users VALUES (1, 'alice'), (2, 'bob')")
        db.execute("INSERT INTO orders VALUES (100, 1, 9.99), (101, 2, 19.99), (102, 1, 5.0)")

        rows = db.execute(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.uid ORDER BY o.total",
        ).fetchall()
    assert rows == [("alice", 5.0), ("alice", 9.99), ("bob", 19.99)]


def test_aggregate_with_group_by_and_having(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT, v INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 'a', 10), (2, 'a', 20), (3, 'b', 5), (4, 'c', 100)")
        rows = db.execute(
            "SELECT k, SUM(v) FROM t GROUP BY k HAVING SUM(v) > 15 ORDER BY k"
        ).fetchall()
    assert rows == [("a", 30), ("c", 100)]


def test_update_pk_to_existing_value_is_rejected(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 10), (2, 20)")
        with pytest.raises(SQLConstraintError):
            db.execute("UPDATE t SET id = 1 WHERE id = 2")


def test_subquery_in_from(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)")
        rows = db.execute(
            "SELECT x FROM (SELECT * FROM t) AS sub WHERE x > 15 ORDER BY x"
        ).fetchall()
    assert rows == [(20,), (30,)]


def test_parameters_are_bound_positionally(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        rows = db.execute(
            "SELECT name FROM t WHERE id = ? OR name = ?",
            parameters=(99, "a"),
        ).fetchall()
    assert rows == [("a",)]


def test_null_handling_three_valued_logic(tmp_path):
    with DB(tmp_path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 'a', 10), (2, NULL, NULL)")
        # WHERE name = 'a' OR name = 'b' must DROP the NULL row (not include it).
        rows = db.execute("SELECT id FROM t WHERE name = 'a' OR name = 'b'").fetchall()
        assert rows == [(1,)]
        # WHERE name IS NULL must find it.
        rows = db.execute("SELECT id FROM t WHERE name IS NULL").fetchall()
        assert rows == [(2,)]
