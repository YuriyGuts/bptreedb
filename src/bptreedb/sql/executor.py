"""Cursor: the user-facing handle returned by `DB.execute`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from bptreedb.sql.operators import CreateTable
from bptreedb.sql.operators import Delete
from bptreedb.sql.operators import DropTable
from bptreedb.sql.operators import Insert
from bptreedb.sql.operators import Operator
from bptreedb.sql.operators import Update
from bptreedb.sql.types import Row
from bptreedb.sql.types import SQLType

if TYPE_CHECKING:
    pass

ColumnDescription = tuple[str, SQLType]


class Cursor:
    """
    Iterable handle over a SQL statement result.

    For `SELECT`, iterating yields the result rows and `rowcount` becomes the number of
    rows yielded *so far*. For `INSERT`/`UPDATE`/`DELETE`, the cursor is fully drained
    on creation and `rowcount` is the number of rows mutated. For DDL, `rowcount` is 0.
    """

    def __init__(self, plan: Operator) -> None:
        """Wrap a logical-plan operator. DML/DDL operators are run eagerly."""
        self.description: list[ColumnDescription] = list(plan.schema)
        self._iterator: Iterator[Row] | None = None
        self._rowcount = 0
        self._closed = False

        if isinstance(plan, Insert | Update | Delete | CreateTable | DropTable):
            # Mutation/DDL: run eagerly so any errors surface synchronously at execute().
            result = plan.run()
            self._rowcount = result.rowcount
            self._iterator = iter(())
        else:
            self._iterator = self._counting_iter(iter(plan))

    def _counting_iter(self, source: Iterator[Row]) -> Iterator[Row]:
        for row in source:
            self._rowcount += 1
            yield row

    @property
    def rowcount(self) -> int:
        """Number of rows produced (SELECT) or affected (DML) so far."""
        return self._rowcount

    def __iter__(self) -> Iterator[Row]:
        """Iterate over result rows."""
        if self._iterator is None:
            return iter(())
        return self._iterator

    def fetchone(self) -> Row | None:
        """Return the next result row, or `None` if exhausted."""
        if self._iterator is None:
            return None
        try:
            return next(self._iterator)
        except StopIteration:
            return None

    def fetchall(self) -> list[Row]:
        """Drain the cursor and return all remaining rows."""
        if self._iterator is None:
            return []
        return list(self._iterator)

    def close(self) -> None:
        """Discard the underlying iterator."""
        self._iterator = None
        self._closed = True
