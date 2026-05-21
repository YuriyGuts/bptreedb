"""Volcano-style query operators.

Each operator exposes a `schema` (column layout of the tuples it produces) and is
iterable. Read operators yield `Row` tuples; write operators yield zero rows and
report progress via `rowcount` on the surrounding executor.

Operators must be safely re-iterable: the right side of a nested-loop join restarts
its iterator for every left tuple. Iterators are evaluated lazily; materializing
operators (`Sort`, `HashAggregate`) buffer their input on first iteration.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from bptreedb.sql.encoding import assemble_full_row
from bptreedb.sql.encoding import decode_row_key_pk
from bptreedb.sql.encoding import decode_row_value
from bptreedb.sql.encoding import encode_row_key
from bptreedb.sql.encoding import encode_row_value
from bptreedb.sql.encoding import table_key_range
from bptreedb.sql.errors import SQLConstraintError
from bptreedb.sql.errors import SQLTypeError
from bptreedb.sql.expr import Expr
from bptreedb.sql.expr import evaluate
from bptreedb.sql.types import NOT_IN_PK
from bptreedb.sql.types import Column
from bptreedb.sql.types import Row
from bptreedb.sql.types import SQLType
from bptreedb.sql.types import SQLValue
from bptreedb.sql.types import TableSchema
from bptreedb.sql.types import TupleSchema

if TYPE_CHECKING:
    from bptreedb.db import DB
    from bptreedb.sql.catalog import Catalog


class Operator(ABC):
    """Base class for all query operators."""

    schema: TupleSchema

    @abstractmethod
    def __iter__(self) -> Iterator[Row]:
        """Iterate over the operator's output tuples."""


@dataclass
class Values(Operator):
    """Source operator for `INSERT ... VALUES`-style row literals."""

    rows: list[tuple[Expr, ...]]
    schema: TupleSchema

    def __iter__(self) -> Iterator[Row]:
        """Evaluate each row's expressions in declaration order."""
        for row_exprs in self.rows:
            yield tuple(evaluate(e, ()) for e in row_exprs)


@dataclass
class TableScan(Operator):
    """Full scan of a table's key range from the underlying KV store."""

    db: DB
    table: TableSchema
    alias: str
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Derive the output schema from the table definition."""
        self.schema = [(f"{self.alias}.{c.name}", c.sql_type) for c in self.table.columns]

    def __iter__(self) -> Iterator[Row]:
        """Yield one decoded row per KV entry in this table's range."""
        start, end = table_key_range(self.table.table_id)
        for key, value in self.db.scan(start, end):
            pk_values = decode_row_key_pk(key, self.table)
            non_pk_values = decode_row_value(value, self.table)
            yield assemble_full_row(pk_values, non_pk_values, self.table)


@dataclass
class Filter(Operator):
    """Drops tuples where `predicate` is not TRUE (NULL drops)."""

    child: Operator
    predicate: Expr
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Inherit the schema unchanged from the child operator."""
        self.schema = self.child.schema

    def __iter__(self) -> Iterator[Row]:
        """Yield only rows where the predicate evaluates to TRUE."""
        for row in self.child:
            result = evaluate(self.predicate, row)
            if result is True:
                yield row


@dataclass
class Project(Operator):
    """Computes output columns by evaluating a list of expressions."""

    child: Operator
    output_names: list[str]
    expressions: list[Expr]
    output_types: list[SQLType]
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Form the output schema from the supplied names and types."""
        self.schema = list(zip(self.output_names, self.output_types, strict=True))

    def __iter__(self) -> Iterator[Row]:
        """Evaluate every projection expression per input row."""
        for row in self.child:
            yield tuple(evaluate(e, row) for e in self.expressions)


# `(expr, ascending, nulls_last)` per sort key.
SortKey = tuple[Expr, bool, bool]


@dataclass
class Sort(Operator):
    """Materializes the child input and sorts it. Stable."""

    child: Operator
    keys: list[SortKey]
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Sort preserves the child schema."""
        self.schema = self.child.schema

    def __iter__(self) -> Iterator[Row]:
        """Buffer the child, then sort key-by-key from least to most significant."""
        rows = list(self.child)
        # Sort by least-significant key first so the final pass orders by the primary key.
        for expr, ascending, nulls_last in reversed(self.keys):

            def key_fn(row: Row, _expr: Expr = expr, _nl: bool = nulls_last) -> tuple:
                value = evaluate(_expr, row)
                if value is None:
                    return (1 if _nl else 0, 0)
                return (0 if _nl else 1, value)

            rows.sort(key=key_fn, reverse=not ascending)
        return iter(rows)


@dataclass
class Limit(Operator):
    """Skip `offset` rows, then yield up to `limit` rows."""

    child: Operator
    limit: int | None
    offset: int
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Limit preserves the child schema."""
        self.schema = self.child.schema

    def __iter__(self) -> Iterator[Row]:
        """Yield rows respecting the configured offset and limit."""
        remaining_skip = self.offset
        remaining_emit = self.limit if self.limit is not None else -1
        for row in self.child:
            if remaining_skip > 0:
                remaining_skip -= 1
                continue
            if remaining_emit == 0:
                break
            yield row
            if remaining_emit > 0:
                remaining_emit -= 1


@dataclass
class AggregateSpec:
    """One aggregate produced by `HashAggregate`."""

    name: str  # COUNT / SUM / MIN / MAX / AVG
    arg: Expr | None  # None for COUNT(*)
    output_type: SQLType


@dataclass
class HashAggregate(Operator):
    """
    Group-by + aggregates in one pass via an in-memory dict keyed by the group tuple.

    Output tuple layout: `(*group_keys, *aggregates)`. Names are
    `group_0..group_N-1`, `agg_0..agg_M-1` so callers can index by position.
    """

    child: Operator
    group_keys: list[Expr]
    group_key_types: list[SQLType]
    aggregates: list[AggregateSpec]
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Build the output schema: group keys followed by aggregates."""
        group_entries = [(f"group_{i}", t) for i, t in enumerate(self.group_key_types)]
        agg_entries = [(f"agg_{i}", a.output_type) for i, a in enumerate(self.aggregates)]
        self.schema = group_entries + agg_entries

    def __iter__(self) -> Iterator[Row]:
        """Single-pass aggregation; yields one tuple per distinct group key."""
        # Lazy import to break the cyclic dep between `expr` and `operators`.
        groups: dict[Row, list[_AggState]] = {}
        ordered_keys: list[Row] = []

        for row in self.child:
            key = tuple(evaluate(k, row) for k in self.group_keys)
            states = groups.get(key)
            if states is None:
                states = [_AggState.new(a) for a in self.aggregates]
                groups[key] = states
                ordered_keys.append(key)
            for spec, state in zip(self.aggregates, states, strict=True):
                value = None if spec.arg is None else evaluate(spec.arg, row)
                state.update(spec, value)

        # Empty input with no GROUP BY -> a single row of zero-rows aggregates.
        if not groups and not self.group_keys:
            states = [_AggState.new(a) for a in self.aggregates]
            yield tuple(s.finalize(a) for s, a in zip(states, self.aggregates, strict=True))
            return

        for key in ordered_keys:
            states = groups[key]
            agg_values = tuple(s.finalize(a) for s, a in zip(states, self.aggregates, strict=True))
            yield (*key, *agg_values)


class _AggState:
    """Running state for one aggregate. Constructed per group."""

    __slots__ = ("count", "max_value", "min_value", "sum_value")

    def __init__(self) -> None:
        self.count = 0
        self.sum_value: int | float = 0
        self.min_value: SQLValue = None
        self.max_value: SQLValue = None

    @classmethod
    def new(cls, _spec: AggregateSpec) -> _AggState:
        return cls()

    def update(self, spec: AggregateSpec, value: SQLValue) -> None:
        name = spec.name.upper()
        if name == "COUNT":
            if spec.arg is None:
                # COUNT(*) counts every row regardless of NULLs.
                self.count += 1
            elif value is not None:
                self.count += 1
            return

        if value is None:
            # SUM/MIN/MAX/AVG all skip NULLs.
            return

        if name in ("SUM", "AVG"):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SQLTypeError(f"{name} expects numeric, got {type(value).__name__}")
            self.sum_value += value
            self.count += 1
        elif name == "MIN":
            if self.min_value is None or value < self.min_value:  # ty: ignore[unsupported-operator]
                self.min_value = value
            self.count += 1
        elif name == "MAX":
            if self.max_value is None or value > self.max_value:  # ty: ignore[unsupported-operator]
                self.max_value = value
            self.count += 1
        else:
            raise SQLTypeError(f"unsupported aggregate: {spec.name!r}")

    def finalize(self, spec: AggregateSpec) -> SQLValue:
        name = spec.name.upper()
        if name == "COUNT":
            return self.count
        if name == "SUM":
            # SQL SUM of zero rows is NULL.
            return None if self.count == 0 else self.sum_value
        if name == "MIN":
            return self.min_value
        if name == "MAX":
            return self.max_value
        if name == "AVG":
            return None if self.count == 0 else self.sum_value / self.count
        raise SQLTypeError(f"unsupported aggregate: {spec.name!r}")


@dataclass
class NestedLoopJoin(Operator):
    """INNER nested-loop join. Right side is re-iterated for every left tuple."""

    left: Operator
    right: Operator
    predicate: Expr | None
    schema: TupleSchema = field(init=False)

    def __post_init__(self) -> None:
        """Concatenate left and right schemas."""
        self.schema = list(self.left.schema) + list(self.right.schema)

    def __iter__(self) -> Iterator[Row]:
        """Yield every left x right pair for which the predicate is TRUE (or None)."""
        for left_row in self.left:
            for right_row in self.right:
                combined = (*left_row, *right_row)
                if self.predicate is None:
                    yield combined
                else:
                    result = evaluate(self.predicate, combined)
                    if result is True:
                        yield combined


@dataclass
class _MutationResult:
    """Information returned by a DML operator after it finishes running."""

    rowcount: int


@dataclass
class Insert(Operator):
    """
    Insert rows into a table.

    `source` must yield tuples whose columns line up with `table.columns` in declaration
    order; the planner is responsible for reshaping any user-supplied column list into
    that layout (with NULL padding for unspecified columns).

    A duplicate PRIMARY KEY raises `SQLConstraintError` unless `on_conflict='replace'`,
    in which case the existing row is overwritten (the `INSERT OR REPLACE` variant).
    """

    db: DB
    table: TableSchema
    source: Operator
    on_conflict: str = "raise"  # 'raise' or 'replace'
    schema: TupleSchema = field(default_factory=list)

    def __iter__(self) -> Iterator[Row]:
        """INSERT yields no rows."""
        return iter(())

    def run(self) -> _MutationResult:
        """Encode each source row and persist it via `DB.put`. Returns affected-rowcount."""
        # Materialize so source iterators (e.g. a TableScan over the same table) aren't
        # invalidated by our puts via the DB's concurrent-modification check.
        source_rows = list(self.source)
        rowcount = 0
        non_pk_columns = [c for c in self.table.columns if c.pk_position == NOT_IN_PK]
        for source_row in source_rows:
            if len(source_row) != len(self.table.columns):
                raise SQLTypeError(
                    f"INSERT source row has {len(source_row)} values, "
                    f"table {self.table.name!r} has {len(self.table.columns)} columns",
                )

            pk_values = tuple(
                _coerce_value(
                    source_row[self.table.column_index(c.name)],
                    c.sql_type,
                )
                for c in self.table.pk_columns
            )
            non_pk_values = tuple(
                _coerce_value(
                    source_row[self.table.column_index(c.name)],
                    c.sql_type,
                )
                for c in non_pk_columns
            )

            key = encode_row_key(self.table.table_id, pk_values, self.table)
            existing = self.db.get(key)
            if existing is not None:
                if self.on_conflict == "raise":
                    raise SQLConstraintError(
                        f"duplicate primary key in {self.table.name!r}: {pk_values!r}",
                    )
                if self.on_conflict == "ignore":
                    continue
            value = encode_row_value(non_pk_values, self.table)
            self.db.put(key, value)
            rowcount += 1
        return _MutationResult(rowcount=rowcount)


def _coerce_value(value: SQLValue, sql_type: SQLType) -> SQLValue:
    """
    Coerce a Python value to the column's declared SQL type where it's safe to do so.

    Currently: int -> float promotion for REAL columns; everything else must already match.
    """
    if value is None:
        return None
    if sql_type is SQLType.REAL and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


@dataclass
class Update(Operator):
    """Update rows in a table by re-encoding values; PK changes are delete+put."""

    db: DB
    table: TableSchema
    assignments: list[tuple[Column, Expr]]
    predicate: Expr | None
    # Evaluator context: a `TableScan` over `table` aliased to its bare name produces the rows.
    schema: TupleSchema = field(default_factory=list)

    def __iter__(self) -> Iterator[Row]:
        """Update statement: yields nothing."""
        return iter(())

    def run(self) -> _MutationResult:
        """Scan, materialize matching rows, then re-encode and write each."""
        # Materialize first so we don't mutate the tree mid-scan; the DB raises
        # `DBConcurrentPageModificationError` otherwise.
        scan = TableScan(self.db, self.table, self.table.name)
        matched: list[Row] = []
        for row in scan:
            if self.predicate is not None:
                result = evaluate(self.predicate, row)
                if result is not True:
                    continue
            matched.append(row)

        rowcount = 0
        non_pk_columns = [c for c in self.table.columns if c.pk_position == NOT_IN_PK]
        for row in matched:
            new_row = list(row)
            updated_pk = False
            for column, expr in self.assignments:
                idx = self.table.column_index(column.name)
                new_row[idx] = _coerce_value(evaluate(expr, row), column.sql_type)
                if column.pk_position != NOT_IN_PK:
                    updated_pk = True

            old_pk_values = tuple(
                row[self.table.column_index(c.name)] for c in self.table.pk_columns
            )
            new_pk_values = tuple(
                new_row[self.table.column_index(c.name)] for c in self.table.pk_columns
            )
            new_non_pk_values = tuple(
                new_row[self.table.column_index(c.name)] for c in non_pk_columns
            )

            old_key = encode_row_key(self.table.table_id, old_pk_values, self.table)
            new_key = encode_row_key(self.table.table_id, new_pk_values, self.table)
            new_value = encode_row_value(new_non_pk_values, self.table)

            if updated_pk and new_key != old_key:
                if self.db.get(new_key) is not None:
                    raise SQLConstraintError(
                        f"UPDATE would duplicate primary key in {self.table.name!r}",
                    )
                self.db.delete(old_key)
                self.db.put(new_key, new_value)
            else:
                self.db.put(old_key, new_value)
            rowcount += 1
        return _MutationResult(rowcount=rowcount)


@dataclass
class Delete(Operator):
    """Delete matching rows from a table."""

    db: DB
    table: TableSchema
    predicate: Expr | None
    schema: TupleSchema = field(default_factory=list)

    def __iter__(self) -> Iterator[Row]:
        """Delete statement: yields nothing."""
        return iter(())

    def run(self) -> _MutationResult:
        """Scan, filter, then delete each row's key."""
        rowcount = 0
        # Collect keys to delete first so we don't mutate the tree mid-scan.
        scan = TableScan(self.db, self.table, self.table.name)
        keys_to_delete: list[bytes] = []
        for row in scan:
            if self.predicate is not None:
                result = evaluate(self.predicate, row)
                if result is not True:
                    continue
            pk_values = tuple(row[self.table.column_index(c.name)] for c in self.table.pk_columns)
            keys_to_delete.append(encode_row_key(self.table.table_id, pk_values, self.table))
        for key in keys_to_delete:
            if self.db.delete(key):
                rowcount += 1
        return _MutationResult(rowcount=rowcount)


@dataclass
class CreateTable(Operator):
    """DDL: register a new table schema in the catalog."""

    catalog: Catalog
    name: str
    columns: tuple[Column, ...]
    schema: TupleSchema = field(default_factory=list)

    def __iter__(self) -> Iterator[Row]:
        """CREATE TABLE statement: yields nothing."""
        return iter(())

    def run(self) -> _MutationResult:
        """Create the table; affected-row count is 0."""
        self.catalog.create_table(self.name, self.columns)
        return _MutationResult(rowcount=0)


@dataclass
class DropTable(Operator):
    """DDL: remove a table and all of its rows from the catalog."""

    catalog: Catalog
    name: str
    schema: TupleSchema = field(default_factory=list)

    def __iter__(self) -> Iterator[Row]:
        """DROP TABLE statement: yields nothing."""
        return iter(())

    def run(self) -> _MutationResult:
        """Drop the table; affected-row count is 0."""
        self.catalog.drop_table(self.name)
        return _MutationResult(rowcount=0)
