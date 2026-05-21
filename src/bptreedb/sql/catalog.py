"""Persistent catalog of SQL tables, stored under reserved keys in the KV layer."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from bptreedb.codec import BufferReader
from bptreedb.codec import BufferWriter
from bptreedb.sql.encoding import KEY_PREFIX_META
from bptreedb.sql.encoding import table_key_range
from bptreedb.sql.errors import SQLSchemaError
from bptreedb.sql.types import NOT_IN_PK
from bptreedb.sql.types import Column
from bptreedb.sql.types import SQLType
from bptreedb.sql.types import TableSchema

if TYPE_CHECKING:
    from bptreedb.db import DB

# Keys under `KEY_PREFIX_META`. The second byte selects a sub-namespace; the rest is the entry.
#   \x00\x00counter         -> u64 BE next-table-id
#   \x00\x00format_version  -> u32 LE schema version (currently 1)
#   \x00\x01name\x00<name>  -> encoded TableSchema
#   \x00\x02id\x00<id>      -> table name (reverse lookup)
_SUBKEY_COUNTERS = b"\x00"
_SUBKEY_BY_NAME = b"\x01name\x00"
_SUBKEY_BY_ID = b"\x02id\x00"

_KEY_TABLE_ID_COUNTER = KEY_PREFIX_META + _SUBKEY_COUNTERS + b"counter"
_KEY_FORMAT_VERSION = KEY_PREFIX_META + _SUBKEY_COUNTERS + b"format_version"

_COUNTER_FIELD = struct.Struct(">Q")
_FORMAT_VERSION_FIELD = struct.Struct("<I")

SQL_FORMAT_VERSION = 1


def _name_key(name: str) -> bytes:
    return KEY_PREFIX_META + _SUBKEY_BY_NAME + name.encode("utf-8")


def _id_key(table_id: int) -> bytes:
    return KEY_PREFIX_META + _SUBKEY_BY_ID + _COUNTER_FIELD.pack(table_id)


def _encode_schema(schema: TableSchema) -> bytes:
    """Encode a `TableSchema` into the catalog's by-name value format."""
    buf = BufferWriter()
    buf.write_struct(">Q", schema.table_id)
    buf.write_struct(">H", len(schema.columns))
    for column in schema.columns:
        name_bytes = column.name.encode("utf-8")
        buf.write_struct(">H", len(name_bytes))
        buf.write_bytes(name_bytes)
        buf.write_struct("<B", int(column.sql_type))
        buf.write_struct("<B", column.pk_position)
    return buf.build()


def _decode_schema(data: bytes, name: str) -> TableSchema:
    reader = BufferReader(data)
    (table_id,) = reader.read_struct(">Q")
    (n_cols,) = reader.read_struct(">H")
    columns: list[Column] = []
    for _ in range(n_cols):
        (name_len,) = reader.read_struct(">H")
        col_name = reader.read_bytes(name_len).decode("utf-8")
        (type_tag,) = reader.read_struct("<B")
        (pk_pos,) = reader.read_struct("<B")
        columns.append(Column(col_name, SQLType(type_tag), pk_position=pk_pos))
    pk_columns = tuple(
        sorted((c for c in columns if c.pk_position != NOT_IN_PK), key=lambda c: c.pk_position),
    )
    return TableSchema(table_id=table_id, name=name, columns=tuple(columns), pk_columns=pk_columns)


class Catalog:
    """Read/write access to the SQL catalog backed by the KV layer."""

    def __init__(self, db: DB) -> None:
        """Bind the catalog to an opened `DB` instance."""
        self.db = db

    def ensure_initialized(self) -> None:
        """Stamp the SQL format version on a fresh database; refuse mismatched versions."""
        existing = self.db.get(_KEY_FORMAT_VERSION)
        if existing is None:
            self.db.put(_KEY_FORMAT_VERSION, _FORMAT_VERSION_FIELD.pack(SQL_FORMAT_VERSION))
            return
        (version,) = _FORMAT_VERSION_FIELD.unpack(existing)
        if version != SQL_FORMAT_VERSION:
            raise SQLSchemaError(
                f"on-disk SQL format version {version} is not supported "
                f"(expected {SQL_FORMAT_VERSION})",
            )

    def _next_table_id(self) -> int:
        """Read-modify-write the table-id counter, returning the freshly allocated id."""
        raw = self.db.get(_KEY_TABLE_ID_COUNTER)
        current = _COUNTER_FIELD.unpack(raw)[0] if raw is not None else 0
        new_id = current + 1
        self.db.put(_KEY_TABLE_ID_COUNTER, _COUNTER_FIELD.pack(new_id))
        return new_id

    def create_table(
        self,
        name: str,
        columns: tuple[Column, ...],
    ) -> TableSchema:
        """
        Create a new table with the given columns and persist it.

        Raises
        ------
        SQLSchemaError
            If a table with `name` already exists, columns have duplicate names, or
            PK declarations are inconsistent (non-contiguous positions, etc.).
        """
        self.ensure_initialized()
        if self.db.get(_name_key(name)) is not None:
            raise SQLSchemaError(f"table {name!r} already exists")

        seen: set[str] = set()
        for column in columns:
            if column.name in seen:
                raise SQLSchemaError(f"duplicate column name {column.name!r}")
            seen.add(column.name)

        pk_columns = sorted(
            (c for c in columns if c.pk_position != NOT_IN_PK),
            key=lambda c: c.pk_position,
        )
        for expected_position, column in enumerate(pk_columns):
            if column.pk_position != expected_position:
                raise SQLSchemaError(
                    f"PK column {column.name!r} has position {column.pk_position}, "
                    f"expected {expected_position} (must be contiguous starting at 0)",
                )

        table_id = self._next_table_id()
        schema = TableSchema(
            table_id=table_id,
            name=name,
            columns=tuple(columns),
            pk_columns=tuple(pk_columns),
        )
        self.db.put(_name_key(name), _encode_schema(schema))
        self.db.put(_id_key(table_id), name.encode("utf-8"))
        return schema

    def drop_table(self, name: str) -> TableSchema:
        """
        Remove a table from the catalog and erase all of its rows.

        Returns the dropped schema, or raises `SQLSchemaError` if the table did not exist.
        """
        schema = self.get_table(name)

        # Erase rows: a single scan-and-delete loop is fine since v1 has no big-table goals.
        # `list()` materializes keys before deleting, since we can't mutate during scan.
        start, end = table_key_range(schema.table_id)
        row_keys = [k for k, _ in self.db.scan(start, end)]
        for key in row_keys:
            self.db.delete(key)

        self.db.delete(_name_key(name))
        self.db.delete(_id_key(schema.table_id))
        return schema

    def get_table(self, name: str) -> TableSchema:
        """Look up a table schema by name, or raise `SQLSchemaError`."""
        raw = self.db.get(_name_key(name))
        if raw is None:
            raise SQLSchemaError(f"no such table: {name!r}")
        return _decode_schema(raw, name)

    def list_tables(self) -> list[TableSchema]:
        """Return all currently registered table schemas, in lexicographic name order."""
        start = KEY_PREFIX_META + _SUBKEY_BY_NAME
        end = KEY_PREFIX_META + bytes([_SUBKEY_BY_NAME[0] + 1])
        # `list()` so we don't hold an iterator across subsequent reads.
        entries = list(self.db.scan(start, end))
        result: list[TableSchema] = []
        prefix_len = len(start)
        for key, value in entries:
            name = key[prefix_len:].decode("utf-8")
            result.append(_decode_schema(value, name))
        return result
