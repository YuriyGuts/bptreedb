"""Property tests: bptreedb's SQL should agree with SQLite on equivalent workloads.

Hypothesis generates a small synthetic schema and a sequence of INSERT/UPDATE/DELETE/SELECT
operations, then we run each statement against both engines and compare results.
"""

from __future__ import annotations

import math
import sqlite3

from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from bptreedb.db import DB

# Small alphabets so collisions happen frequently and we exercise overwrites/conflicts.
_NAMES = st.sampled_from(["alice", "bob", "cathy", "dan", "eve"])
_AGES = st.integers(min_value=0, max_value=99)
_IDS = st.integers(min_value=1, max_value=20)

_VALUES_TUPLE = st.tuples(_IDS, _NAMES, _AGES)


def _make_op(strategy_name: str):
    """Return a strategy yielding `(op_name, *args)` tuples."""
    if strategy_name == "insert":
        return st.tuples(st.just("insert"), _VALUES_TUPLE)
    if strategy_name == "delete":
        return st.tuples(st.just("delete"), _IDS)
    if strategy_name == "update_age":
        return st.tuples(st.just("update_age"), _IDS, _AGES)
    raise ValueError(strategy_name)


_OPS = st.lists(
    st.one_of(
        _make_op("insert"),
        _make_op("delete"),
        _make_op("update_age"),
    ),
    max_size=30,
)


def _apply(db_exec, op):  # noqa: ANN001
    """Apply a single op to either a DB or sqlite3 connection through `db_exec`."""
    kind = op[0]
    if kind == "insert":
        _, (uid, name, age) = op
        # OR REPLACE so a colliding id doesn't error out; both engines support it.
        db_exec(
            "INSERT OR REPLACE INTO users VALUES (?, ?, ?)",
            (uid, name, age),
        )
    elif kind == "delete":
        _, uid = op
        db_exec("DELETE FROM users WHERE id = ?", (uid,))
    elif kind == "update_age":
        _, uid, age = op
        db_exec("UPDATE users SET age = ? WHERE id = ?", (age, uid))


def _bptreedb_exec(db):
    def go(sql, params=()):
        db.execute(sql, params).fetchall()

    return go


def _sqlite_exec(conn):
    def go(sql, params=()):
        conn.execute(sql, params).fetchall()

    return go


def _compare(bp_rows, sl_rows):
    """Compare result rows after coercing floats to a stable representation."""

    def _norm(r):
        out = []
        for v in r:
            if isinstance(v, float) and math.isnan(v):
                out.append("NaN")
            else:
                out.append(v)
        return tuple(out)

    return [_norm(r) for r in bp_rows] == [_norm(r) for r in sl_rows]


@given(ops=_OPS)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_select_results_match_sqlite(ops, tmp_path_factory):
    tmpdir = tmp_path_factory.mktemp("bptreedb_sql_equiv")

    bp_db = DB(tmpdir)
    bp_db.open()
    bp_db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")

    sl_conn = sqlite3.connect(":memory:")
    sl_conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")

    bp_exec = _bptreedb_exec(bp_db)
    sl_exec = _sqlite_exec(sl_conn)

    try:
        for op in ops:
            _apply(bp_exec, op)
            _apply(sl_exec, op)

        queries = [
            ("SELECT id, name, age FROM users ORDER BY id", True),
            ("SELECT id FROM users WHERE age >= 30 ORDER BY id", True),
            ("SELECT COUNT(*) FROM users", False),
            ("SELECT name, COUNT(*) FROM users GROUP BY name ORDER BY name", True),
            ("SELECT MIN(age), MAX(age), SUM(age) FROM users", False),
        ]

        for sql, ordered in queries:
            bp_rows = bp_db.execute(sql).fetchall()
            sl_rows = sl_conn.execute(sql).fetchall()
            if not ordered:
                bp_rows = sorted(bp_rows, key=lambda r: tuple(_safe(v) for v in r))
                sl_rows = sorted(sl_rows, key=lambda r: tuple(_safe(v) for v in r))
            assert _compare(bp_rows, sl_rows), (
                f"DIVERGENCE on {sql!r}\nbptreedb: {bp_rows}\nsqlite:  {sl_rows}\nops: {ops}"
            )
    finally:
        bp_db.close()
        sl_conn.close()


def _safe(v):
    """Sort key that treats None as the smallest possible value."""
    if v is None:
        return (0,)
    return (1, v)


# Tighter property: ORDER BY over each scalar PK type must match SQLite. This is the
# most direct test of our order-preserving key encoding.
_INT_PK_VALUES = st.lists(st.integers(min_value=-1000, max_value=1000), max_size=20, unique=True)
_TEXT_PK_VALUES = st.lists(st.text(min_size=0, max_size=6), max_size=20, unique=True)


@given(values=_INT_PK_VALUES)
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_int_pk_order_matches_sqlite(values, tmp_path_factory):
    tmpdir = tmp_path_factory.mktemp("bptreedb_intpk")
    with DB(tmpdir) as bp_db:
        bp_db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        for v in values:
            bp_db.execute("INSERT INTO t VALUES (?)", (v,))
        bp_rows = bp_db.execute("SELECT id FROM t ORDER BY id").fetchall()

    sl_conn = sqlite3.connect(":memory:")
    sl_conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    for v in values:
        sl_conn.execute("INSERT INTO t VALUES (?)", (v,))
    sl_rows = sl_conn.execute("SELECT id FROM t ORDER BY id").fetchall()
    sl_conn.close()

    assert bp_rows == sl_rows


@given(values=_TEXT_PK_VALUES)
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_text_pk_order_matches_sqlite(values, tmp_path_factory):
    tmpdir = tmp_path_factory.mktemp("bptreedb_textpk")
    with DB(tmpdir) as bp_db:
        bp_db.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
        for v in values:
            bp_db.execute("INSERT INTO t VALUES (?)", (v,))
        bp_rows = bp_db.execute("SELECT k FROM t ORDER BY k").fetchall()

    sl_conn = sqlite3.connect(":memory:")
    sl_conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
    for v in values:
        sl_conn.execute("INSERT INTO t VALUES (?)", (v,))
    sl_rows = sl_conn.execute("SELECT k FROM t ORDER BY k").fetchall()
    sl_conn.close()

    assert bp_rows == sl_rows
