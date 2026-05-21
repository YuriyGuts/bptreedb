"""Build a logical-plan tree from a sqlglot AST.

The planner is the only module that imports `sqlglot`. Everything below it consumes our
own `Expr` and `Operator` types. The dispatch pattern is exhaustive: any sqlglot node
we don't explicitly handle raises `SQLParseError`, so unsupported SQL fails loudly
instead of silently producing wrong plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from bptreedb.sql.errors import SQLParseError
from bptreedb.sql.errors import SQLProgrammingError
from bptreedb.sql.errors import SQLSchemaError
from bptreedb.sql.errors import SQLTypeError
from bptreedb.sql.expr import Aggregate
from bptreedb.sql.expr import BinOp
from bptreedb.sql.expr import Case
from bptreedb.sql.expr import ColumnRef
from bptreedb.sql.expr import Expr
from bptreedb.sql.expr import FuncCall
from bptreedb.sql.expr import IsNull
from bptreedb.sql.expr import Literal
from bptreedb.sql.expr import UnaryOp
from bptreedb.sql.operators import AggregateSpec
from bptreedb.sql.operators import CreateTable
from bptreedb.sql.operators import Delete
from bptreedb.sql.operators import DropTable
from bptreedb.sql.operators import Filter
from bptreedb.sql.operators import HashAggregate
from bptreedb.sql.operators import Insert
from bptreedb.sql.operators import Limit as LimitOp
from bptreedb.sql.operators import NestedLoopJoin
from bptreedb.sql.operators import Operator
from bptreedb.sql.operators import Project
from bptreedb.sql.operators import Sort
from bptreedb.sql.operators import TableScan
from bptreedb.sql.operators import Update
from bptreedb.sql.operators import Values
from bptreedb.sql.types import NOT_IN_PK
from bptreedb.sql.types import Column
from bptreedb.sql.types import SQLType
from bptreedb.sql.types import SQLValue
from bptreedb.sql.types import TupleSchema

if TYPE_CHECKING:
    from bptreedb.db import DB
    from bptreedb.sql.catalog import Catalog


# Map sqlglot binary expression classes to our `BinOp.op` strings.
_BINOP_CLASSES: dict[type, str] = {
    exp.EQ: "=",
    exp.NEQ: "<>",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.Add: "+",
    exp.Sub: "-",
    exp.Mul: "*",
    exp.Div: "/",
    exp.Mod: "%",
    exp.And: "AND",
    exp.Or: "OR",
    exp.DPipe: "||",
}

# sqlglot DataType.Type -> our SQLType.
_SQL_TYPES: dict[exp.DataType.Type, SQLType] = {
    exp.DataType.Type.INT: SQLType.INT,
    exp.DataType.Type.BIGINT: SQLType.INT,
    exp.DataType.Type.SMALLINT: SQLType.INT,
    exp.DataType.Type.TINYINT: SQLType.INT,
    exp.DataType.Type.TEXT: SQLType.TEXT,
    exp.DataType.Type.VARCHAR: SQLType.TEXT,
    exp.DataType.Type.CHAR: SQLType.TEXT,
    exp.DataType.Type.FLOAT: SQLType.REAL,
    exp.DataType.Type.DOUBLE: SQLType.REAL,
    exp.DataType.Type.DECIMAL: SQLType.REAL,
    exp.DataType.Type.BOOLEAN: SQLType.BOOL,
}

# Aggregate function classes -> uppercase name. `Count(this=Star())` is treated as `COUNT(*)`.
_AGGREGATE_CLASSES: dict[type, str] = {
    exp.Count: "COUNT",
    exp.Sum: "SUM",
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.Avg: "AVG",
}

# Scalar function classes -> uppercase name. Stops the function-call dispatch from
# walking arbitrary unknown classes.
_SCALAR_FUNCTION_CLASSES: dict[type, str] = {
    exp.Coalesce: "COALESCE",
    exp.Lower: "LOWER",
    exp.Upper: "UPPER",
    exp.Abs: "ABS",
    exp.Length: "LENGTH",
}


@dataclass
class _Scope:
    """
    Column resolution context for an operator tree.

    `entries` is the tuple schema (`(qualified_name, sql_type)`) of the operator the
    expression is being bound against. `aliases` maps an alias (`u`) and the bare
    table name (`users`) to the index range covered by that source within the tuple.
    """

    entries: TupleSchema
    # alias -> (start_index, end_index_exclusive, columns_by_name)
    aliases: dict[str, tuple[int, int, dict[str, tuple[int, SQLType]]]] = field(
        default_factory=dict
    )

    def resolve(self, table: str | None, column: str) -> tuple[int, SQLType]:
        """Resolve `table.column` (or `column` if `table is None`) to a tuple index + type."""
        if table is not None:
            entry = self.aliases.get(table)
            if entry is None:
                raise SQLSchemaError(f"unknown table alias: {table!r}")
            _start, _end, by_name = entry
            if column not in by_name:
                raise SQLSchemaError(f"unknown column {column!r} in {table!r}")
            return by_name[column]

        # Unqualified: must be unique across all aliases.
        candidates: list[tuple[int, SQLType]] = []
        for _alias, (_start, _end, by_name) in self.aliases.items():
            if column in by_name:
                candidates.append(by_name[column])
        if not candidates:
            raise SQLSchemaError(f"unknown column: {column!r}")
        if len(candidates) > 1:
            raise SQLSchemaError(f"ambiguous column: {column!r}")
        return candidates[0]


def _build_scope(operator: Operator, aliases: list[str]) -> _Scope:
    """
    Build a `_Scope` for an operator whose schema concatenates per-alias columns.

    `aliases` is the list of aliases that contribute to `operator.schema`, in the same
    order the schema entries appear.
    """
    scope = _Scope(entries=list(operator.schema))
    # Group entries by their alias prefix.
    grouped: dict[str, dict[str, tuple[int, SQLType]]] = {a: {} for a in aliases}
    ranges: dict[str, list[int]] = {a: [] for a in aliases}
    for i, (qualified_name, sql_type) in enumerate(operator.schema):
        prefix, _, col = qualified_name.partition(".")
        if not col:
            # Unqualified column (e.g. from a Project), still resolvable by name.
            for alias in aliases:
                grouped.setdefault(alias, {})
            continue
        if prefix in grouped:
            grouped[prefix][col] = (i, sql_type)
            ranges[prefix].append(i)
    for alias in aliases:
        idxs = ranges.get(alias, [])
        start = min(idxs) if idxs else 0
        end = max(idxs) + 1 if idxs else 0
        scope.aliases[alias] = (start, end, grouped.get(alias, {}))
    return scope


class Planner:
    """Translates a SQL string into a logical-plan `Operator` tree."""

    def __init__(self, catalog: Catalog, db: DB) -> None:
        """Bind the planner to a catalog and an opened database."""
        self.catalog = catalog
        self.db = db
        self._parameters: tuple[SQLValue, ...] = ()
        self._placeholder_index = 0

    def plan(self, sql: str, parameters: tuple[SQLValue, ...] = ()) -> Operator:
        """Parse `sql` and produce a top-level `Operator`."""
        self._parameters = parameters
        self._placeholder_index = 0
        try:
            tree = sqlglot.parse_one(sql, dialect="sqlite")
        except sqlglot.errors.ParseError as e:
            raise SQLParseError(str(e)) from e
        if tree is None:
            raise SQLParseError("empty SQL statement")

        plan = self._dispatch_statement(tree)

        # Every `?` must have been consumed exactly once.
        if self._placeholder_index != len(parameters):
            raise SQLProgrammingError(
                f"SQL has {self._placeholder_index} placeholder(s) but "
                f"{len(parameters)} parameter(s) were supplied",
            )
        return plan

    # ---- Top-level dispatch ----

    def _dispatch_statement(self, node: exp.Expression) -> Operator:
        if isinstance(node, exp.Select):
            return self._plan_select(node)
        if isinstance(node, exp.Insert):
            return self._plan_insert(node)
        if isinstance(node, exp.Update):
            return self._plan_update(node)
        if isinstance(node, exp.Delete):
            return self._plan_delete(node)
        if isinstance(node, exp.Create):
            return self._plan_create(node)
        if isinstance(node, exp.Drop):
            return self._plan_drop(node)
        raise SQLParseError(f"unsupported statement: {type(node).__name__}")

    # ---- DDL ----

    def _plan_create(self, node: exp.Create) -> CreateTable:  # noqa: PLR0912
        if (node.args.get("kind") or "").upper() != "TABLE":
            raise SQLParseError("only CREATE TABLE is supported")
        schema_node = node.this
        if not isinstance(schema_node, exp.Schema):
            raise SQLParseError("CREATE TABLE requires column definitions")
        table_node = schema_node.this
        if not isinstance(table_node, exp.Table):
            raise SQLParseError("CREATE TABLE: missing table name")
        name = table_node.name

        columns: list[Column] = []
        # `pk_columns_by_position` defers PK assignment for column-level constraints, so a
        # `PRIMARY KEY(a, b)` table-level constraint can still set positions ourselves later.
        column_level_pk: list[str] = []
        table_level_pk: list[str] = []

        for child in schema_node.expressions:
            if isinstance(child, exp.ColumnDef):
                col_name = child.name
                sql_type = self._map_data_type(child.args.get("kind"))
                is_pk = False
                for constraint in child.args.get("constraints") or []:
                    kind = constraint.kind if isinstance(constraint, exp.ColumnConstraint) else None
                    if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        is_pk = True
                    elif kind is None:
                        continue
                    else:
                        raise SQLParseError(
                            f"unsupported column constraint: {type(kind).__name__}",
                        )
                if is_pk:
                    column_level_pk.append(col_name)
                columns.append(Column(col_name, sql_type))
            elif isinstance(child, exp.PrimaryKey):
                for ordered in child.expressions:
                    inner = ordered.this if isinstance(ordered, exp.Ordered) else ordered
                    if not isinstance(inner, exp.Column):
                        raise SQLParseError("PRIMARY KEY expects column references")
                    table_level_pk.append(inner.name)
            else:
                raise SQLParseError(
                    f"unsupported CREATE TABLE element: {type(child).__name__}",
                )

        pk_names = table_level_pk or column_level_pk
        if pk_names:
            seen = set()
            position_map: dict[str, int] = {}
            for i, name_ in enumerate(pk_names):
                if name_ in seen:
                    raise SQLSchemaError(f"duplicate PK column: {name_!r}")
                seen.add(name_)
                position_map[name_] = i
            columns = [
                Column(c.name, c.sql_type, pk_position=position_map.get(c.name, NOT_IN_PK))
                for c in columns
            ]

        return CreateTable(catalog=self.catalog, name=name, columns=tuple(columns))

    def _plan_drop(self, node: exp.Drop) -> DropTable:
        if (node.args.get("kind") or "").upper() != "TABLE":
            raise SQLParseError("only DROP TABLE is supported")
        table_node = node.this
        if not isinstance(table_node, exp.Table):
            raise SQLParseError("DROP TABLE: missing table name")
        return DropTable(catalog=self.catalog, name=table_node.name)

    def _map_data_type(self, node: exp.Expression | None) -> SQLType:
        if not isinstance(node, exp.DataType):
            raise SQLParseError("missing column type")
        sql_type = _SQL_TYPES.get(node.this)
        if sql_type is None:
            raise SQLParseError(f"unsupported column type: {node.this}")
        return sql_type

    # ---- DML: INSERT ----

    def _plan_insert(self, node: exp.Insert) -> Insert:
        table_node = node.this
        column_names: list[str] | None = None
        if isinstance(table_node, exp.Schema):
            # `INSERT INTO t (a, b) VALUES ...`
            inner_table = table_node.this
            if not isinstance(inner_table, exp.Table):
                raise SQLParseError("INSERT: missing table name")
            column_names = [c.name for c in table_node.expressions if isinstance(c, exp.Identifier)]
            table_node = inner_table
        if not isinstance(table_node, exp.Table):
            raise SQLParseError("INSERT: missing table name")

        schema = self.catalog.get_table(table_node.name)
        target_names = (
            column_names if column_names is not None else [c.name for c in schema.columns]
        )

        expression = node.expression
        if isinstance(expression, exp.Values):
            rows = self._plan_values_for_insert(expression, schema, target_names)
            source: Operator = Values(
                rows=rows, schema=[(c.name, c.sql_type) for c in schema.columns]
            )
        elif isinstance(expression, exp.Select):
            # `INSERT INTO t SELECT ...`
            select_plan = self._plan_select(expression)
            source = self._reshape_select_for_insert(select_plan, schema, target_names)
        else:
            raise SQLParseError(
                f"unsupported INSERT source: {type(expression).__name__}",
            )

        alternative = (node.args.get("alternative") or "").upper()
        if alternative not in ("", "REPLACE", "IGNORE"):
            raise SQLParseError(f"unsupported INSERT alternative: {alternative!r}")
        if alternative == "IGNORE":
            on_conflict = "ignore"
        elif alternative == "REPLACE":
            on_conflict = "replace"
        else:
            on_conflict = "raise"
        return Insert(db=self.db, table=schema, source=source, on_conflict=on_conflict)

    def _plan_values_for_insert(
        self,
        values_node: exp.Values,
        schema,  # noqa: ANN001
        target_names: list[str],
    ) -> list[tuple[Expr, ...]]:
        # Validate target column names up front.
        target_positions: list[int] = []
        for name in target_names:
            try:
                target_positions.append(schema.column_index(name))
            except KeyError as e:
                raise SQLSchemaError(f"unknown column {name!r} in {schema.name!r}") from e

        rows: list[tuple[Expr, ...]] = []
        for tup in values_node.expressions:
            if not isinstance(tup, exp.Tuple):
                raise SQLParseError("INSERT VALUES expects parenthesized tuples")
            if len(tup.expressions) != len(target_names):
                raise SQLProgrammingError(
                    f"VALUES row has {len(tup.expressions)} value(s), expected {len(target_names)}",
                )
            # Build per-column Expr list aligned to the table's full column order.
            per_col: list[Expr] = [Literal(None)] * len(schema.columns)
            for src_idx, target_idx in enumerate(target_positions):
                per_col[target_idx] = self._translate_expr(
                    tup.expressions[src_idx], _Scope(entries=[])
                )
            rows.append(tuple(per_col))
        return rows

    def _reshape_select_for_insert(
        self,
        select_op: Operator,
        schema,  # noqa: ANN001
        target_names: list[str],
    ) -> Operator:
        if len(select_op.schema) != len(target_names):
            raise SQLProgrammingError(
                f"INSERT...SELECT: SELECT produces {len(select_op.schema)} column(s), "
                f"target expects {len(target_names)}",
            )
        target_positions: list[int] = []
        for name in target_names:
            try:
                target_positions.append(schema.column_index(name))
            except KeyError as e:
                raise SQLSchemaError(f"unknown column {name!r} in {schema.name!r}") from e

        # Build a `Project` that maps SELECT outputs into table column order, padding
        # missing columns with NULL.
        full_exprs: list[Expr] = [Literal(None)] * len(schema.columns)
        for src_idx, target_idx in enumerate(target_positions):
            full_exprs[target_idx] = ColumnRef(src_idx)
        return Project(
            child=select_op,
            output_names=[c.name for c in schema.columns],
            expressions=full_exprs,
            output_types=[c.sql_type for c in schema.columns],
        )

    # ---- DML: UPDATE / DELETE ----

    def _plan_update(self, node: exp.Update) -> Update:
        table_node = node.this
        if not isinstance(table_node, exp.Table):
            raise SQLParseError("UPDATE: missing table name")
        schema = self.catalog.get_table(table_node.name)
        scope = self._scope_for_table(schema, table_node.name)

        assignments = []
        for assignment in node.expressions:
            if not isinstance(assignment, exp.EQ):
                raise SQLParseError(f"unsupported UPDATE assignment: {type(assignment).__name__}")
            target = assignment.this
            if not isinstance(target, exp.Column):
                raise SQLParseError("UPDATE assignment target must be a column")
            try:
                column = schema.columns[schema.column_index(target.name)]
            except KeyError as e:
                raise SQLSchemaError(f"unknown column {target.name!r}") from e
            value_expr = self._translate_expr(assignment.expression, scope)
            assignments.append((column, value_expr))

        predicate = None
        where = node.args.get("where")
        if isinstance(where, exp.Where):
            predicate = self._translate_expr(where.this, scope)

        return Update(
            db=self.db,
            table=schema,
            assignments=assignments,
            predicate=predicate,
        )

    def _plan_delete(self, node: exp.Delete) -> Delete:
        table_node = node.this
        if not isinstance(table_node, exp.Table):
            raise SQLParseError("DELETE: missing table name")
        schema = self.catalog.get_table(table_node.name)
        scope = self._scope_for_table(schema, table_node.name)
        predicate = None
        where = node.args.get("where")
        if isinstance(where, exp.Where):
            predicate = self._translate_expr(where.this, scope)
        return Delete(db=self.db, table=schema, predicate=predicate)

    def _scope_for_table(self, schema, alias: str) -> _Scope:  # noqa: ANN001
        entries = [(f"{alias}.{c.name}", c.sql_type) for c in schema.columns]
        scope = _Scope(entries=entries)
        by_name = {c.name: (i, c.sql_type) for i, c in enumerate(schema.columns)}
        scope.aliases[alias] = (0, len(entries), by_name)
        # The table is also addressable by its real name when an alias is used.
        if alias != schema.name:
            scope.aliases[schema.name] = (0, len(entries), by_name)
        return scope

    # ---- SELECT ----

    def _plan_select(self, node: exp.Select) -> Operator:  # noqa: PLR0912, PLR0915
        # Build the FROM tree, including joins.
        from_node = node.args.get("from")
        if not isinstance(from_node, exp.From):
            raise SQLParseError("SELECT without FROM is not supported")
        plan, aliases = self._plan_from_source(from_node.this)
        for join in node.args.get("joins") or []:
            plan, aliases = self._plan_join(plan, aliases, join)

        scope = _build_scope(plan, aliases)

        where = node.args.get("where")
        if isinstance(where, exp.Where):
            predicate = self._translate_expr(where.this, scope)
            plan = Filter(child=plan, predicate=predicate)

        # Collect select-list (name, ast-node) pairs; expand `SELECT *`.
        select_pairs: list[tuple[str, exp.Expression | Expr]] = []
        for projection in node.expressions:
            if isinstance(projection, exp.Star):
                for i, (qualified, _t) in enumerate(plan.schema):
                    select_pairs.append((qualified.split(".", 1)[-1], ColumnRef(i)))
            elif isinstance(projection, exp.Alias):
                select_pairs.append((projection.alias, projection.this))
            else:
                select_pairs.append((projection.sql(dialect="sqlite"), projection))

        # First-pass translation: every select-list entry becomes an `Expr` in the
        # pre-aggregation scope. `Aggregate(...)` survives as-is for later rewriting.
        select_exprs: list[tuple[str, Expr]] = []
        for name, payload in select_pairs:
            if isinstance(payload, Expr):
                select_exprs.append((name, payload))
            else:
                select_exprs.append((name, self._translate_expr(payload, scope)))

        having_node = node.args.get("having")
        having_expr: Expr | None = None
        if isinstance(having_node, exp.Having):
            having_expr = self._translate_expr(having_node.this, scope)

        # ORDER BY keys are translated in the same scope as SELECT-list expressions so
        # that aggregation rewriting (if any) reaches them uniformly. Users can therefore
        # `ORDER BY COUNT(*)` or `ORDER BY u.age` regardless of which columns the SELECT
        # ultimately projects.
        order_node = node.args.get("order")
        order_entries: list[tuple[Expr, bool, bool]] = []
        if isinstance(order_node, exp.Order):
            for ordered in order_node.expressions:
                if not isinstance(ordered, exp.Ordered):
                    raise SQLParseError(f"unsupported ORDER BY entry: {type(ordered).__name__}")
                key_expr = self._translate_expr(ordered.this, scope)
                ascending = not ordered.args.get("desc", False)
                nulls_first = ordered.args.get("nulls_first", ascending)
                order_entries.append((key_expr, ascending, not nulls_first))

        group_node = node.args.get("group")
        has_aggregates = any(_expr_contains_aggregate(e) for _, e in select_exprs)
        if having_expr is not None:
            has_aggregates = has_aggregates or _expr_contains_aggregate(having_expr)
        if any(_expr_contains_aggregate(e) for e, _, _ in order_entries):
            has_aggregates = True

        if group_node is not None or has_aggregates:
            plan, scope, select_exprs, having_expr, order_entries = self._apply_aggregation(
                plan=plan,
                scope=scope,
                group_node=group_node,
                select_exprs=select_exprs,
                having_expr=having_expr,
                order_entries=order_entries,
            )

        if having_expr is not None:
            plan = Filter(child=plan, predicate=having_expr)

        if order_entries:
            plan = Sort(child=plan, keys=order_entries)

        # Final SELECT-list projection comes after ORDER BY so the sort can see any
        # post-aggregation columns referenced by the keys.
        proj_exprs = [e for _, e in select_exprs]
        proj_names = [n for n, _ in select_exprs]
        proj_types = [_expr_type(e, scope) for e in proj_exprs]
        plan = Project(
            child=plan,
            output_names=proj_names,
            expressions=proj_exprs,
            output_types=proj_types,
        )

        limit_node = node.args.get("limit")
        offset_node = node.args.get("offset")
        if limit_node is not None or offset_node is not None:
            limit_value: int | None = None
            offset_value = 0
            if isinstance(limit_node, exp.Limit):
                limit_value = _expect_int_literal(limit_node.expression, "LIMIT")
            if isinstance(offset_node, exp.Offset):
                offset_value = _expect_int_literal(offset_node.expression, "OFFSET")
            plan = LimitOp(child=plan, limit=limit_value, offset=offset_value)

        return plan

    def _plan_from_source(self, source: exp.Expression) -> tuple[Operator, list[str]]:
        if isinstance(source, exp.Table):
            schema = self.catalog.get_table(source.name)
            alias_node = source.args.get("alias")
            alias = alias_node.name if isinstance(alias_node, exp.TableAlias) else source.name
            return TableScan(db=self.db, table=schema, alias=alias), [alias]
        if isinstance(source, exp.Subquery):
            inner = source.this
            if not isinstance(inner, exp.Select):
                raise SQLParseError(f"unsupported subquery contents: {type(inner).__name__}")
            inner_plan = self._plan_select(inner)
            alias_node = source.args.get("alias")
            if not isinstance(alias_node, exp.TableAlias):
                raise SQLParseError("subquery in FROM must have an alias")
            alias = alias_node.name
            # Re-label the schema under the alias so column resolution works through the wrapper.
            new_schema: TupleSchema = [
                (f"{alias}.{name.split('.', 1)[-1]}", t) for name, t in inner_plan.schema
            ]
            return _RenamedScope(child=inner_plan, schema=new_schema), [alias]
        raise SQLParseError(f"unsupported FROM source: {type(source).__name__}")

    def _plan_join(
        self,
        left_plan: Operator,
        left_aliases: list[str],
        join_node: exp.Join,
    ) -> tuple[Operator, list[str]]:
        join_kind = (join_node.args.get("kind") or "").upper()
        join_side = (join_node.args.get("side") or "").upper()
        if join_side or join_kind not in ("", "INNER"):
            raise SQLParseError(f"unsupported JOIN: {join_side or join_kind!r}")

        right_source = join_node.this
        right_plan, right_aliases = self._plan_from_source(right_source)
        combined = NestedLoopJoin(left=left_plan, right=right_plan, predicate=None)
        all_aliases = left_aliases + right_aliases
        scope = _build_scope(combined, all_aliases)
        on_node = join_node.args.get("on")
        if on_node is not None:
            combined.predicate = self._translate_expr(on_node, scope)
        return combined, all_aliases

    def _apply_aggregation(  # noqa: PLR0913
        self,
        plan: Operator,
        scope: _Scope,
        group_node: exp.Group | None,
        select_exprs: list[tuple[str, Expr]],
        having_expr: Expr | None,
        order_entries: list[tuple[Expr, bool, bool]],
    ) -> tuple[
        Operator,
        _Scope,
        list[tuple[str, Expr]],
        Expr | None,
        list[tuple[Expr, bool, bool]],
    ]:
        # GROUP BY expressions are translated against the pre-aggregation scope.
        group_exprs: list[Expr] = []
        group_types: list[SQLType] = []
        if group_node is not None:
            for entry in group_node.expressions:
                e = self._translate_expr(entry, scope)
                group_exprs.append(e)
                group_types.append(_expr_type(e, scope))

        # Collect every distinct `Aggregate(...)` sub-expression from the SELECT list +
        # HAVING expression so we can assign them positions in the HashAggregate output.
        aggregate_specs: list[AggregateSpec] = []
        aggregate_positions: dict[Aggregate, int] = {}

        def register(agg: Aggregate) -> int:
            if agg in aggregate_positions:
                return aggregate_positions[agg]
            output_type = _aggregate_output_type(agg, scope)
            aggregate_specs.append(
                AggregateSpec(name=agg.name, arg=agg.arg, output_type=output_type)
            )
            idx = len(aggregate_specs) - 1
            aggregate_positions[agg] = idx
            return idx

        for _, e in select_exprs:
            _collect_aggregate_exprs(e, register)
        if having_expr is not None:
            _collect_aggregate_exprs(having_expr, register)
        for key_expr, _asc, _nl in order_entries:
            _collect_aggregate_exprs(key_expr, register)

        agg_plan = HashAggregate(
            child=plan,
            group_keys=group_exprs,
            group_key_types=group_types,
            aggregates=aggregate_specs,
        )

        # Post-aggregation scope: tuple is `(*group_keys, *aggregates)`.
        post_entries: TupleSchema = [(f"group_{i}", t) for i, t in enumerate(group_types)] + [
            (f"agg_{i}", a.output_type) for i, a in enumerate(aggregate_specs)
        ]
        post_scope = _Scope(entries=post_entries)

        # Rewrite SELECT-list and HAVING `Expr`s: replace each Aggregate with a ColumnRef
        # into its slot, and replace any sub-expression that matches a GROUP BY entry
        # with a ColumnRef into the corresponding group slot.
        group_count = len(group_exprs)

        def rewrite(expr: Expr) -> Expr:
            for i, group_expr in enumerate(group_exprs):
                if expr == group_expr:
                    return ColumnRef(i)
            if isinstance(expr, Aggregate):
                return ColumnRef(group_count + aggregate_positions[expr])
            return _walk_expr(expr, rewrite)

        rewritten_select: list[tuple[str, Expr]] = [(n, rewrite(e)) for n, e in select_exprs]
        rewritten_having = rewrite(having_expr) if having_expr is not None else None
        rewritten_order: list[tuple[Expr, bool, bool]] = [
            (rewrite(e), asc, nl) for e, asc, nl in order_entries
        ]
        return agg_plan, post_scope, rewritten_select, rewritten_having, rewritten_order

    # ---- Expression translation ----

    def _translate_expr(self, node: exp.Expression, scope: _Scope) -> Expr:  # noqa: PLR0911, PLR0912
        if isinstance(node, exp.Paren):
            return self._translate_expr(node.this, scope)
        if isinstance(node, exp.Literal):
            return Literal(_literal_value(node))
        if isinstance(node, exp.Boolean):
            return Literal(bool(node.this))
        if isinstance(node, exp.Null):
            return Literal(None)
        if isinstance(node, exp.Placeholder):
            if self._placeholder_index >= len(self._parameters):
                raise SQLProgrammingError("not enough parameters supplied for ? placeholders")
            value = self._parameters[self._placeholder_index]
            self._placeholder_index += 1
            return Literal(value)
        if isinstance(node, exp.Neg):
            return UnaryOp("-", self._translate_expr(node.this, scope))
        if isinstance(node, exp.Not):
            return UnaryOp("NOT", self._translate_expr(node.this, scope))
        if isinstance(node, exp.Is):
            right = node.expression
            if isinstance(right, exp.Null):
                return IsNull(self._translate_expr(node.this, scope), negate=False)
            raise SQLParseError("only `IS NULL`/`IS NOT NULL` are supported")
        if isinstance(node, exp.Column):
            return _resolve_column_ref(node, scope)
        if isinstance(node, exp.Case):
            return _translate_case(node, self._translate_expr, scope)

        if type(node) in _AGGREGATE_CLASSES:
            return self._translate_aggregate(node, scope)

        if type(node) in _BINOP_CLASSES:
            return BinOp(
                _BINOP_CLASSES[type(node)],
                self._translate_expr(node.this, scope),
                self._translate_expr(node.expression, scope),
            )

        # Scalar functions.
        if type(node) in _SCALAR_FUNCTION_CLASSES:
            name = _SCALAR_FUNCTION_CLASSES[type(node)]
            args: list[exp.Expression] = []
            if node.this is not None:
                args.append(node.this)
            args.extend(node.args.get("expressions") or [])
            return FuncCall(name, tuple(self._translate_expr(a, scope) for a in args))

        # `exp.Anonymous` covers unknown user functions; reject loudly.
        if isinstance(node, exp.Anonymous):
            raise SQLParseError(f"unknown function: {node.this!r}")

        raise SQLParseError(f"unsupported expression: {type(node).__name__}: {node.sql()!r}")

    def _translate_aggregate(self, node: exp.Expression, scope: _Scope) -> Aggregate:
        name = _AGGREGATE_CLASSES[type(node)]
        if name == "COUNT" and isinstance(node.this, exp.Star):
            return Aggregate(name="COUNT", arg=None)
        arg_expr = self._translate_expr(node.this, scope)
        return Aggregate(name=name, arg=arg_expr)


# ---- Helpers ----


@dataclass
class _RenamedScope(Operator):
    """Wraps another operator and presents its rows under a different qualified-name schema."""

    child: Operator
    schema: TupleSchema = field(default_factory=list)

    def __iter__(self):  # noqa: ANN204
        return iter(self.child)


def _literal_value(node: exp.Literal) -> SQLValue:
    raw = node.this
    if node.is_string:
        return raw
    # sqlglot stores numeric literals as strings; parse as int if possible, else float.
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return float(raw)
    return raw


def _resolve_column_ref(node: exp.Column, scope: _Scope) -> Expr:
    table_id = node.args.get("table")
    table_name = table_id.name if isinstance(table_id, exp.Identifier) else None
    column_name = node.name
    index, _sql_type = scope.resolve(table_name, column_name)
    return ColumnRef(index)


def _translate_case(node: exp.Case, translate, scope: _Scope) -> Case:  # noqa: ANN001
    whens = []
    for w in node.args.get("ifs") or []:
        cond = translate(w.this, scope)
        then = translate(w.args.get("true"), scope)
        whens.append((cond, then))
    default = node.args.get("default")
    else_expr = translate(default, scope) if default is not None else None
    return Case(whens=tuple(whens), else_=else_expr)


def _expr_contains_aggregate(expr: Expr) -> bool:
    """Return whether `expr` has any `Aggregate` sub-expression."""
    if isinstance(expr, Aggregate):
        return True
    return any(_expr_contains_aggregate(child) for child in _expr_children(expr))


def _collect_aggregate_exprs(expr: Expr, register) -> None:  # noqa: ANN001
    """Walk `expr` and call `register(agg)` for every `Aggregate` sub-expression."""
    if isinstance(expr, Aggregate):
        register(expr)
        return
    for child in _expr_children(expr):
        _collect_aggregate_exprs(child, register)


def _expr_children(expr: Expr) -> list[Expr]:  # noqa: PLR0911
    """Return the direct `Expr` children of `expr`, for walking."""
    if isinstance(expr, BinOp):
        return [expr.left, expr.right]
    if isinstance(expr, UnaryOp):
        return [expr.arg]
    if isinstance(expr, IsNull):
        return [expr.arg]
    if isinstance(expr, Case):
        out: list[Expr] = []
        for cond, then in expr.whens:
            out.extend([cond, then])
        if expr.else_ is not None:
            out.append(expr.else_)
        return out
    if isinstance(expr, FuncCall):
        return list(expr.args)
    if isinstance(expr, Aggregate):
        return [] if expr.arg is None else [expr.arg]
    return []


def _walk_expr(expr: Expr, transform) -> Expr:  # noqa: ANN001, PLR0911
    """
    Apply `transform` recursively to `expr` and return a new `Expr` with each child mapped.

    Used by the post-aggregation rewriter to replace aggregate and group-by references.
    """
    if isinstance(expr, BinOp):
        return BinOp(expr.op, transform(expr.left), transform(expr.right))
    if isinstance(expr, UnaryOp):
        return UnaryOp(expr.op, transform(expr.arg))
    if isinstance(expr, IsNull):
        return IsNull(transform(expr.arg), negate=expr.negate)
    if isinstance(expr, Case):
        new_whens = tuple((transform(c), transform(t)) for c, t in expr.whens)
        new_else = transform(expr.else_) if expr.else_ is not None else None
        return Case(whens=new_whens, else_=new_else)
    if isinstance(expr, FuncCall):
        return FuncCall(expr.name, tuple(transform(a) for a in expr.args))
    if isinstance(expr, Aggregate):
        # Aggregates are atomic for the rewriter; should be matched at the caller before recursing.
        return expr
    return expr


def _aggregate_output_type(agg: Aggregate, scope: _Scope) -> SQLType:
    name = agg.name.upper()
    if name == "COUNT":
        return SQLType.INT
    if name == "AVG":
        return SQLType.REAL
    if agg.arg is None:
        return SQLType.NULL
    return _expr_type(agg.arg, scope)


def _expect_int_literal(node: exp.Expression | None, kind: str) -> int:
    if not isinstance(node, exp.Literal) or node.is_string:
        raise SQLParseError(f"{kind} expects an integer literal")
    return int(node.this)


def _expr_type(expr: Expr, scope: _Scope) -> SQLType:  # noqa: PLR0911, PLR0912
    """Best-effort type inference for an `Expr` against `scope`.

    Conservative: comparisons/logical -> BOOL, IS NULL -> BOOL, arithmetic -> INT (or REAL
    if either operand is REAL), TEXT concat -> TEXT, function calls follow a small table.
    Literals carry their own type. Column refs read straight from the scope.
    """
    if isinstance(expr, Literal):
        return _value_sql_type(expr.value)
    if isinstance(expr, ColumnRef):
        return scope.entries[expr.index][1]
    if isinstance(expr, BinOp):
        if expr.op in BinOp.COMPARISONS or expr.op in BinOp.LOGICAL:
            return SQLType.BOOL
        if expr.op == "||":
            return SQLType.TEXT
        left = _expr_type(expr.left, scope)
        right = _expr_type(expr.right, scope)
        if SQLType.REAL in (left, right):
            return SQLType.REAL
        if expr.op == "/":
            return SQLType.REAL
        return SQLType.INT
    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            return SQLType.BOOL
        return _expr_type(expr.arg, scope)
    if isinstance(expr, IsNull):
        return SQLType.BOOL
    if isinstance(expr, Case):
        for _cond, then in expr.whens:
            t = _expr_type(then, scope)
            if t is not SQLType.NULL:
                return t
        if expr.else_ is not None:
            return _expr_type(expr.else_, scope)
        return SQLType.NULL
    if isinstance(expr, FuncCall):
        name = expr.name.upper()
        if name in ("LOWER", "UPPER"):
            return SQLType.TEXT
        if name == "LENGTH":
            return SQLType.INT
        if name == "ABS":
            return _expr_type(expr.args[0], scope) if expr.args else SQLType.INT
        if name == "COALESCE":
            for a in expr.args:
                t = _expr_type(a, scope)
                if t is not SQLType.NULL:
                    return t
            return SQLType.NULL
    if isinstance(expr, Aggregate):
        return SQLType.INT  # planner sets output_type on AggregateSpec; this fallback is unused
    return SQLType.NULL


def _value_sql_type(value: SQLValue) -> SQLType:
    if value is None:
        return SQLType.NULL
    if isinstance(value, bool):
        return SQLType.BOOL
    if isinstance(value, int):
        return SQLType.INT
    if isinstance(value, float):
        return SQLType.REAL
    if isinstance(value, str):
        return SQLType.TEXT
    raise SQLTypeError(f"unsupported literal type: {type(value).__name__}")
