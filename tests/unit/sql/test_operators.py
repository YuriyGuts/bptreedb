from bptreedb.db import DB
from bptreedb.sql.catalog import Catalog
from bptreedb.sql.expr import BinOp
from bptreedb.sql.expr import ColumnRef
from bptreedb.sql.expr import Literal
from bptreedb.sql.operators import AggregateSpec
from bptreedb.sql.operators import Filter
from bptreedb.sql.operators import HashAggregate
from bptreedb.sql.operators import Insert
from bptreedb.sql.operators import Limit as LimitOp
from bptreedb.sql.operators import NestedLoopJoin
from bptreedb.sql.operators import Project
from bptreedb.sql.operators import Sort
from bptreedb.sql.operators import TableScan
from bptreedb.sql.operators import Values
from bptreedb.sql.types import Column
from bptreedb.sql.types import SQLType


def _seed(tmp_path):
    db = DB(tmp_path)
    db.open()
    cat = Catalog(db)
    cat.ensure_initialized()
    users = cat.create_table(
        "users",
        (
            Column("id", SQLType.INT, pk_position=0),
            Column("name", SQLType.TEXT),
            Column("age", SQLType.INT),
        ),
    )
    seed_rows = [
        (Literal(1), Literal("alice"), Literal(30)),
        (Literal(2), Literal("bob"), Literal(25)),
        (Literal(3), Literal("cathy"), Literal(40)),
    ]
    Insert(db=db, table=users, source=Values(rows=seed_rows, schema=[])).run()
    return db, users


def test_table_scan_yields_in_pk_order(tmp_path):
    db, users = _seed(tmp_path)
    try:
        rows = list(TableScan(db=db, table=users, alias="u"))
        assert [r[0] for r in rows] == [1, 2, 3]
    finally:
        db.close()


def test_filter_drops_null_and_false(tmp_path):
    db, users = _seed(tmp_path)
    try:
        scan = TableScan(db=db, table=users, alias="u")
        pred = BinOp(">", ColumnRef(2), Literal(28))
        rows = list(Filter(child=scan, predicate=pred))
        assert {r[1] for r in rows} == {"alice", "cathy"}
    finally:
        db.close()


def test_project_evaluates_expressions(tmp_path):
    db, users = _seed(tmp_path)
    try:
        scan = TableScan(db=db, table=users, alias="u")
        proj = Project(
            child=scan,
            output_names=["name_upper"],
            expressions=[ColumnRef(1)],  # just project name straight through
            output_types=[SQLType.TEXT],
        )
        names = [r[0] for r in proj]
        assert names == ["alice", "bob", "cathy"]
    finally:
        db.close()


def test_sort_descending(tmp_path):
    db, users = _seed(tmp_path)
    try:
        scan = TableScan(db=db, table=users, alias="u")
        sort = Sort(child=scan, keys=[(ColumnRef(2), False, True)])  # desc
        ages = [r[2] for r in sort]
        assert ages == [40, 30, 25]
    finally:
        db.close()


def test_limit_with_offset(tmp_path):
    db, users = _seed(tmp_path)
    try:
        scan = TableScan(db=db, table=users, alias="u")
        lim = LimitOp(child=scan, limit=1, offset=1)
        assert [r[1] for r in lim] == ["bob"]
    finally:
        db.close()


def test_hash_aggregate_empty_input_yields_one_row_when_no_grouping(tmp_path):
    # GIVEN a HashAggregate with no GROUP BY over an empty input
    db, users = _seed(tmp_path)
    try:
        empty = Filter(child=TableScan(db=db, table=users, alias="u"), predicate=Literal(False))
        agg = HashAggregate(
            child=empty,
            group_keys=[],
            group_key_types=[],
            aggregates=[
                AggregateSpec(name="COUNT", arg=None, output_type=SQLType.INT),
                AggregateSpec(name="SUM", arg=ColumnRef(2), output_type=SQLType.INT),
                AggregateSpec(name="AVG", arg=ColumnRef(2), output_type=SQLType.REAL),
            ],
        )
        # THEN: COUNT(*) is 0, SUM is NULL, AVG is NULL.
        assert list(agg) == [(0, None, None)]
    finally:
        db.close()


def test_hash_aggregate_with_group_by(tmp_path):
    db, users = _seed(tmp_path)
    try:
        # Add a duplicate age so we have a real group with >1 row.
        Insert(
            db=db,
            table=users,
            source=Values(rows=[(Literal(4), Literal("dan"), Literal(25))], schema=[]),
        ).run()
        scan = TableScan(db=db, table=users, alias="u")
        agg = HashAggregate(
            child=scan,
            group_keys=[ColumnRef(2)],
            group_key_types=[SQLType.INT],
            aggregates=[
                AggregateSpec(name="COUNT", arg=None, output_type=SQLType.INT),
            ],
        )
        result = dict(agg)
        assert result == {25: 2, 30: 1, 40: 1}
    finally:
        db.close()


def test_nested_loop_join_inner(tmp_path):
    db, users = _seed(tmp_path)
    try:
        cat = Catalog(db)
        orders = cat.create_table(
            "orders",
            (
                Column("oid", SQLType.INT, pk_position=0),
                Column("uid", SQLType.INT),
            ),
        )
        Insert(
            db=db,
            table=orders,
            source=Values(
                rows=[
                    (Literal(100), Literal(1)),
                    (Literal(101), Literal(2)),
                ],
                schema=[],
            ),
        ).run()
        left = TableScan(db=db, table=users, alias="u")
        right = TableScan(db=db, table=orders, alias="o")
        # users.id (idx 0) = orders.uid (idx 4)
        pred = BinOp("=", ColumnRef(0), ColumnRef(4))
        join = NestedLoopJoin(left=left, right=right, predicate=pred)
        rows = list(join)
        assert sorted(r[3] for r in rows) == [100, 101]  # the oids returned
    finally:
        db.close()
