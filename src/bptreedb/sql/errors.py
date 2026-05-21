"""Exceptions raised by the SQL layer."""

from __future__ import annotations

from bptreedb.exceptions import DBError


class SQLError(DBError):
    """Base class for all errors raised by the SQL layer."""


class SQLParseError(SQLError):
    """Raised when a SQL statement cannot be parsed or contains unsupported syntax."""


class SQLSchemaError(SQLError):
    """Raised on schema problems: unknown table/column, duplicate names, missing PK."""


class SQLTypeError(SQLError):
    """Raised when an expression sees a type that does not fit its operator."""


class SQLConstraintError(SQLError):
    """Raised when an INSERT/UPDATE violates a constraint (PK uniqueness, NULL in PK, etc.)."""


class SQLProgrammingError(SQLError):
    """Raised for caller-side misuse such as a wrong number of `?` parameters."""
