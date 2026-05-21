"""Byte encodings for SQL row keys and row values.

Two distinct encodings live here:

* **Order-preserving** for primary-key components, so that
  `sorted(encode_pk_component(v) for v in xs) == [encode_pk_component(v) for v in sorted(xs)]`
  holds for each individual SQL type. This is what makes range scans (`WHERE pk < X`,
  `ORDER BY pk`) trivial: the underlying B+ tree's lexicographic key order matches the
  SQL ordering directly.
* **Tagged**, non-order-preserving for row values. Compact and self-describing.
"""

from __future__ import annotations

import math
import struct

from bptreedb.codec import BufferReader
from bptreedb.codec import BufferWriter
from bptreedb.sql.errors import SQLConstraintError
from bptreedb.sql.errors import SQLTypeError
from bptreedb.sql.types import NOT_IN_PK
from bptreedb.sql.types import SQLType
from bptreedb.sql.types import SQLValue
from bptreedb.sql.types import TableSchema

# Top-level key prefix bytes; see plan "Storage layout on KV".
KEY_PREFIX_META = b"\x00"
KEY_PREFIX_ROW = b"\x01"

_TABLE_ID_FIELD = struct.Struct(">Q")
_SIGN_BIT_MASK = 0x8000_0000_0000_0000
_MAX_TABLE_ID = 0xFFFF_FFFF_FFFF_FFFF

_TEXT_TERMINATOR = b"\x00\x00"


def table_key_range(table_id: int) -> tuple[bytes, bytes]:
    r"""
    Return `(start_inclusive, end_exclusive)` covering every row key for `table_id`.

    The end bound is the start of the next table's range. For the maximum `table_id`
    we fall back to the smallest key strictly greater than any row, namely a single
    `\x02` byte (the first reserved prefix).
    """
    start = KEY_PREFIX_ROW + _TABLE_ID_FIELD.pack(table_id)
    if table_id + 1 <= _MAX_TABLE_ID:
        end = KEY_PREFIX_ROW + _TABLE_ID_FIELD.pack(table_id + 1)
    else:
        end = b"\x02"
    return start, end


def encode_pk_component(value: SQLValue, sql_type: SQLType) -> bytes:
    """
    Encode a single primary-key column value into order-preserving bytes.

    Raises
    ------
    SQLConstraintError
        If `value` is `None` (NULL is not allowed in primary keys), or `value` is a
        floating-point `NaN` (no defined position in the total order).
    SQLTypeError
        If `value` is not a Python instance compatible with `sql_type`.
    """
    if value is None:
        raise SQLConstraintError("NULL is not allowed in a primary key column")

    if sql_type is SQLType.INT:
        if not isinstance(value, int) or isinstance(value, bool):
            raise SQLTypeError(f"INTEGER PK expects int, got {type(value).__name__}")
        return struct.pack(">Q", (value & 0xFFFF_FFFF_FFFF_FFFF) ^ _SIGN_BIT_MASK)

    if sql_type is SQLType.REAL:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise SQLTypeError(f"REAL PK expects float, got {type(value).__name__}")
        f = float(value)
        if math.isnan(f):
            raise SQLConstraintError("NaN is not allowed in a REAL primary key column")
        raw = struct.pack(">d", f)
        if raw[0] & 0x80:
            # Negative: flip all bits so smaller (more negative) sorts first.
            return bytes(b ^ 0xFF for b in raw)
        # Non-negative: flip only the sign bit so they sort above the negatives.
        return bytes([raw[0] ^ 0x80, *raw[1:]])

    if sql_type is SQLType.TEXT:
        if not isinstance(value, str):
            raise SQLTypeError(f"TEXT PK expects str, got {type(value).__name__}")
        # Escape `\x00` so component boundaries are unambiguous in composite PKs.
        escaped = value.encode("utf-8").replace(b"\x00", b"\x00\xff")
        return escaped + _TEXT_TERMINATOR

    if sql_type is SQLType.BOOL:
        if not isinstance(value, bool):
            raise SQLTypeError(f"BOOL PK expects bool, got {type(value).__name__}")
        return b"\x01" if value else b"\x00"

    raise SQLTypeError(f"unsupported PK type: {sql_type!r}")


def decode_pk_component(reader: BufferReader, sql_type: SQLType) -> SQLValue:
    """
    Decode one primary-key column value previously written by `encode_pk_component`.

    Advances the reader past the component.
    """
    if sql_type is SQLType.INT:
        (raw,) = reader.read_struct(_TABLE_ID_FIELD)
        signed = raw ^ _SIGN_BIT_MASK
        if signed & _SIGN_BIT_MASK:
            signed -= 1 << 64
        return signed

    if sql_type is SQLType.REAL:
        eight = reader.read_bytes(8)
        if eight[0] & 0x80:
            unflipped = bytes([eight[0] ^ 0x80, *eight[1:]])
        else:
            unflipped = bytes(b ^ 0xFF for b in eight)
        return struct.unpack(">d", unflipped)[0]

    if sql_type is SQLType.TEXT:
        # Scan for an unescaped `\x00\x00` terminator (an escaped null is `\x00\xff`).
        data = reader.data
        offset = reader.offset
        i = offset
        while True:
            j = data.index(b"\x00", i)
            if j + 1 < len(data) and data[j + 1] == 0x00:
                payload = data[offset:j]
                reader.offset = j + 2
                return payload.replace(b"\x00\xff", b"\x00").decode("utf-8")
            # Escaped null: skip past `\x00\xff` and keep scanning.
            i = j + 2

    if sql_type is SQLType.BOOL:
        return reader.read_bytes(1) == b"\x01"

    raise SQLTypeError(f"unsupported PK type: {sql_type!r}")


def encode_row_key(table_id: int, pk_values: tuple[SQLValue, ...], schema: TableSchema) -> bytes:
    """
    Encode the full row key for a tuple of primary-key values.

    `pk_values` must be in the same order as `schema.pk_columns`.
    """
    if not schema.has_pk():
        raise SQLConstraintError(f"table {schema.name!r} has no primary key")
    if len(pk_values) != len(schema.pk_columns):
        raise SQLConstraintError(
            f"expected {len(schema.pk_columns)} PK component(s), got {len(pk_values)}",
        )
    buf = BufferWriter()
    buf.write_bytes(KEY_PREFIX_ROW)
    buf.write_struct(_TABLE_ID_FIELD, table_id)
    for value, column in zip(pk_values, schema.pk_columns, strict=True):
        buf.write_bytes(encode_pk_component(value, column.sql_type))
    return buf.build()


def decode_row_key_pk(key: bytes, schema: TableSchema) -> tuple[SQLValue, ...]:
    """
    Extract the PK component values from a row key produced by `encode_row_key`.

    The leading prefix byte and table-id are skipped.
    """
    reader = BufferReader(key)
    reader.offset = len(KEY_PREFIX_ROW) + _TABLE_ID_FIELD.size
    return tuple(decode_pk_component(reader, column.sql_type) for column in schema.pk_columns)


# Per-value tags for the row value encoding. These differ from `SQLType` only because
# `SQLType.NULL` doubles as a column-type tag, while value tags also distinguish "no value"
# from a typed value. We reuse `SQLType` numeric values for clarity.
_VALUE_NULL_TAG = int(SQLType.NULL)
_VALUE_INT_TAG = int(SQLType.INT)
_VALUE_REAL_TAG = int(SQLType.REAL)
_VALUE_TEXT_TAG = int(SQLType.TEXT)
_VALUE_BOOL_TAG = int(SQLType.BOOL)

_NON_PK_COUNT_FIELD = struct.Struct("<H")
_VALUE_INT_FIELD = struct.Struct("<q")
_VALUE_REAL_FIELD = struct.Struct("<d")
_VALUE_LEN_FIELD = struct.Struct("<I")


def encode_row_value(non_pk_values: tuple[SQLValue, ...], schema: TableSchema) -> bytes:
    """
    Encode the non-PK columns of a row into a tagged value buffer.

    `non_pk_values` must be in declaration order over the non-PK columns of `schema`.
    """
    non_pk_columns = [column for column in schema.columns if column.pk_position == NOT_IN_PK]
    if len(non_pk_values) != len(non_pk_columns):
        raise SQLTypeError(
            f"expected {len(non_pk_columns)} non-PK value(s), got {len(non_pk_values)}",
        )

    buf = BufferWriter()
    buf.write_struct(_NON_PK_COUNT_FIELD, len(non_pk_columns))
    for value, column in zip(non_pk_values, non_pk_columns, strict=True):
        if value is None:
            buf.write_struct("<B", _VALUE_NULL_TAG)
            continue

        if column.sql_type is SQLType.INT:
            if not isinstance(value, int) or isinstance(value, bool):
                raise SQLTypeError(f"INTEGER expects int, got {type(value).__name__}")
            buf.write_struct("<B", _VALUE_INT_TAG)
            buf.write_struct(_VALUE_INT_FIELD, value)
        elif column.sql_type is SQLType.REAL:
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise SQLTypeError(f"REAL expects float, got {type(value).__name__}")
            buf.write_struct("<B", _VALUE_REAL_TAG)
            buf.write_struct(_VALUE_REAL_FIELD, float(value))
        elif column.sql_type is SQLType.TEXT:
            if not isinstance(value, str):
                raise SQLTypeError(f"TEXT expects str, got {type(value).__name__}")
            payload = value.encode("utf-8")
            buf.write_struct("<B", _VALUE_TEXT_TAG)
            buf.write_length_prefixed_bytes(payload, _VALUE_LEN_FIELD)
        elif column.sql_type is SQLType.BOOL:
            if not isinstance(value, bool):
                raise SQLTypeError(f"BOOL expects bool, got {type(value).__name__}")
            buf.write_struct("<B", _VALUE_BOOL_TAG)
            buf.write_struct("<B", 1 if value else 0)
        else:
            raise SQLTypeError(f"unsupported value type: {column.sql_type!r}")
    return buf.build()


def decode_row_value(data: bytes, schema: TableSchema) -> list[SQLValue]:
    """
    Decode the non-PK column values from a row value buffer.

    Returns a list aligned with the non-PK columns of `schema`, in declaration order.
    """
    reader = BufferReader(data)
    (n,) = reader.read_struct(_NON_PK_COUNT_FIELD)
    non_pk_columns = [column for column in schema.columns if column.pk_position == NOT_IN_PK]
    if n != len(non_pk_columns):
        raise SQLTypeError(
            f"row value has {n} non-PK column(s), schema declares {len(non_pk_columns)}",
        )

    values: list[SQLValue] = []
    for _ in range(n):
        (tag,) = reader.read_struct("<B")
        if tag == _VALUE_NULL_TAG:
            values.append(None)
        elif tag == _VALUE_INT_TAG:
            (v,) = reader.read_struct(_VALUE_INT_FIELD)
            values.append(v)
        elif tag == _VALUE_REAL_TAG:
            (v,) = reader.read_struct(_VALUE_REAL_FIELD)
            values.append(v)
        elif tag == _VALUE_TEXT_TAG:
            payload = reader.read_length_prefixed_bytes(_VALUE_LEN_FIELD)
            values.append(payload.decode("utf-8"))
        elif tag == _VALUE_BOOL_TAG:
            (v,) = reader.read_struct("<B")
            values.append(v == 1)
        else:
            raise SQLTypeError(f"unknown value tag: {tag}")
    return values


def assemble_full_row(
    pk_values: tuple[SQLValue, ...],
    non_pk_values: list[SQLValue],
    schema: TableSchema,
) -> tuple[SQLValue, ...]:
    """
    Interleave decoded PK and non-PK column values back into declaration order.

    `pk_values` is in `schema.pk_columns` order; `non_pk_values` is in declaration order
    over the non-PK columns of `schema`. The output is in `schema.columns` order,
    which is the row shape operators consume.
    """
    pk_lookup = {col.pk_position: pv for col, pv in zip(schema.pk_columns, pk_values, strict=True)}
    non_pk_iter = iter(non_pk_values)
    out: list[SQLValue] = []
    for column in schema.columns:
        if column.pk_position == NOT_IN_PK:
            out.append(next(non_pk_iter))
        else:
            out.append(pk_lookup[column.pk_position])
    return tuple(out)
