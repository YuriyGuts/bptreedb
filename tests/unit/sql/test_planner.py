import pytest

from bptreedb.db import DB
from bptreedb.sql.catalog import Catalog
from bptreedb.sql.errors import SQLParseError
from bptreedb.sql.errors import SQLProgrammingError
from bptreedb.sql.errors import SQLSchemaError
from bptreedb.sql.operators import CreateTable
from bptreedb.sql.operators import Delete
from bptreedb.sql.operators import DropTable
from bptreedb.sql.operators import Filter
from bptreedb.sql.operators import HashAggregate
from bptreedb.sql.operators import Insert
from bptreedb.sql.operators import Limit as LimitOp
from bptreedb.sql.operators import NestedLoopJoin
from bptreedb.sql.operators import Project
from bptreedb.sql.operators import Sort
from bptreedb.sql.operators import TableScan
from bptreedb.sql.operators import Update
from bptreedb.sql.planner import Planner


@pytest.fixture
def planner(tmp_path):
    db = DB(tmp_path)
    db.open()
    cat = Catalog(db)
    cat.ensure_initialized()
    p = Planner(cat, db)
    p.plan("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)").run()
    p.plan("CREATE TABLE orders (oid INTEGER PRIMARY KEY, uid INTEGER, total REAL)").run()
    yield p
    db.close()


def _find(operator, predicate):
    """Walk a plan tree until we find an operator matching `predicate`."""
    if predicate(operator):
        return operator
    for attr in ("child", "left", "right", "source"):
        child = getattr(operator, attr, None)
        if child is not None:
            found = _find(child, predicate)
            if found is not None:
                return found
    return None


def test_create_table_plan(planner):
    plan = planner.plan("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
    assert isinstance(plan, CreateTable)
    assert plan.name == "t"
    assert [c.name for c in plan.columns] == ["a", "b"]
    assert plan.columns[0].pk_position == 0
    assert plan.columns[1].pk_position == 0xFF


def test_drop_table_plan(planner):
    plan = planner.plan("DROP TABLE users")
    assert isinstance(plan, DropTable)
    assert plan.name == "users"


def test_insert_plan_shape(planner):
    plan = planner.plan("INSERT INTO users VALUES (1, 'a', 10)")
    assert isinstance(plan, Insert)


def test_update_plan_has_filter(planner):
    plan = planner.plan("UPDATE users SET name = 'x' WHERE id = 1")
    assert isinstance(plan, Update)
    assert plan.predicate is not None


def test_delete_plan_has_filter(planner):
    plan = planner.plan("DELETE FROM users WHERE id > 5")
    assert isinstance(plan, Delete)
    assert plan.predicate is not None


def test_select_basic_shape(planner):
    # GIVEN a simple SELECT
    plan = planner.plan("SELECT name FROM users WHERE age > 18 ORDER BY name LIMIT 5")
    # THEN the top-of-plan is Project, with Sort/Limit beneath, then Filter, then TableScan.
    assert isinstance(plan, LimitOp)
    assert isinstance(plan.child, Project)
    assert isinstance(plan.child.child, Sort)
    assert _find(plan, lambda op: isinstance(op, Filter)) is not None
    assert _find(plan, lambda op: isinstance(op, TableScan)) is not None


def test_select_join_uses_nested_loop(planner):
    plan = planner.plan(
        "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.uid",
    )
    assert _find(plan, lambda op: isinstance(op, NestedLoopJoin)) is not None


def test_select_with_aggregate_inserts_hash_aggregate(planner):
    plan = planner.plan("SELECT COUNT(*) FROM users")
    assert _find(plan, lambda op: isinstance(op, HashAggregate)) is not None


def test_select_having_filters_after_aggregation(planner):
    plan = planner.plan(
        "SELECT age, COUNT(*) FROM users GROUP BY age HAVING COUNT(*) > 1",
    )
    # GIVEN a HAVING clause
    # THEN the plan must have a Filter sitting above the HashAggregate.
    hash_agg = _find(plan, lambda op: isinstance(op, HashAggregate))
    assert hash_agg is not None
    filter_above = _find(plan, lambda op: isinstance(op, Filter) and op.child is hash_agg)
    assert filter_above is not None


def test_unsupported_statement_raises(planner):
    with pytest.raises(SQLParseError):
        planner.plan("BEGIN TRANSACTION")


def test_unknown_table_raises(planner):
    with pytest.raises(SQLSchemaError):
        planner.plan("SELECT * FROM does_not_exist")


def test_unknown_column_raises(planner):
    with pytest.raises(SQLSchemaError):
        planner.plan("SELECT nope FROM users")


def test_wrong_parameter_count_raises(planner):
    # GIVEN one placeholder and two parameters
    # WHEN we plan
    with pytest.raises(SQLProgrammingError):
        planner.plan("SELECT * FROM users WHERE id = ?", parameters=(1, 2))
    # GIVEN one placeholder and zero parameters
    with pytest.raises(SQLProgrammingError):
        planner.plan("SELECT * FROM users WHERE id = ?")
