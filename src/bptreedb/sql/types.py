"""SQL value model: type tags, column descriptors, table schemas."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import IntEnum

# A SQL value is one of these Python types; `None` represents SQL NULL.
SQLValue = int | float | str | bool | None

# A logical row passed between operators. Plain `tuple` keeps things hashable and fast.
Row = tuple[SQLValue, ...]

# Column descriptor in an operator's output schema: `(qualified_name, sql_type)`.
# `qualified_name` is the form `alias.column` for base scans, or `column` for synthetic
# columns from projection/aggregation.
TupleSchemaEntry = tuple[str, "SQLType"]
TupleSchema = list[TupleSchemaEntry]


class SQLType(IntEnum):
    """Wire-stable type tag for a SQL column or value."""

    NULL = 0
    INT = 1
    REAL = 2
    TEXT = 3
    BOOL = 4


# Sentinel `pk_position` value meaning "this column is not part of the primary key".
NOT_IN_PK: int = 0xFF


@dataclass(frozen=True)
class Column:
    """Description of a single column in a table schema."""

    name: str
    sql_type: SQLType
    # 0-based ordinal within the primary key, or `NOT_IN_PK` if this column is not a PK column.
    pk_position: int = NOT_IN_PK


@dataclass(frozen=True)
class TableSchema:
    """Persistent description of a SQL table."""

    table_id: int
    name: str
    columns: tuple[Column, ...]
    # PK columns sorted by `pk_position`. Empty if the table has no PRIMARY KEY declaration.
    pk_columns: tuple[Column, ...] = field(default=())

    def column_index(self, name: str) -> int:
        """Return the 0-based ordinal of `name` in `columns`, or raise `KeyError`."""
        for i, column in enumerate(self.columns):
            if column.name == name:
                return i
        raise KeyError(name)

    def has_pk(self) -> bool:
        """Return whether the table declares any PRIMARY KEY columns."""
        return len(self.pk_columns) > 0
