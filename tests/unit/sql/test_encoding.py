import math

import pytest

from bptreedb.codec import BufferReader
from bptreedb.sql.encoding import assemble_full_row
from bptreedb.sql.encoding import decode_pk_component
from bptreedb.sql.encoding import decode_row_key_pk
from bptreedb.sql.encoding import decode_row_value
from bptreedb.sql.encoding import encode_pk_component
from bptreedb.sql.encoding import encode_row_key
from bptreedb.sql.encoding import encode_row_value
from bptreedb.sql.encoding import table_key_range
from bptreedb.sql.errors import SQLConstraintError
from bptreedb.sql.errors import SQLTypeError
from bptreedb.sql.types import Column
from bptreedb.sql.types import SQLType
from bptreedb.sql.types import TableSchema


def _roundtrip_pk(value, sql_type):
    encoded = encode_pk_component(value, sql_type)
    decoded = decode_pk_component(BufferReader(encoded), sql_type)
    return encoded, decoded


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
        -1,
        100,
        -100,
        2**63 - 1,
        -(2**63),
    ],
)
def test_pk_int_roundtrip(value):
    # GIVEN an integer PK value
    # WHEN encoding and decoding it
    _, decoded = _roundtrip_pk(value, SQLType.INT)
    # THEN the value should round-trip exactly
    assert decoded == value


def test_pk_int_is_order_preserving():
    # GIVEN a mix of negative, zero, and positive integers
    values = [-(2**63), -1000, -1, 0, 1, 1000, 2**63 - 1]
    # WHEN we sort by encoded bytes
    encoded = sorted(encode_pk_component(v, SQLType.INT) for v in values)
    decoded = [decode_pk_component(BufferReader(e), SQLType.INT) for e in encoded]
    # THEN numeric order matches lexicographic order
    assert decoded == sorted(values)


@pytest.mark.parametrize("value", [0.0, -0.0, 1.5, -1.5, 1e300, -1e300, math.inf, -math.inf])
def test_pk_real_roundtrip(value):
    # GIVEN a finite/infinite REAL PK value (NaN is rejected separately)
    # WHEN encoding/decoding
    _, decoded = _roundtrip_pk(value, SQLType.REAL)
    # THEN the bit pattern survives.
    if math.isinf(value):
        assert math.isinf(decoded)
        assert (decoded > 0) == (value > 0)
    else:
        assert decoded == value


def test_pk_real_is_order_preserving():
    values = [-math.inf, -1e10, -1.0, -0.0, 0.0, 1.0, 1e10, math.inf]
    encoded = sorted(encode_pk_component(v, SQLType.REAL) for v in values)
    decoded = [decode_pk_component(BufferReader(e), SQLType.REAL) for e in encoded]
    # -0.0 and 0.0 compare equal so the decoded list may swap them, but order is preserved.
    assert decoded[0] == -math.inf
    assert decoded[-1] == math.inf
    assert decoded == sorted(values)


def test_pk_real_rejects_nan():
    with pytest.raises(SQLConstraintError):
        encode_pk_component(math.nan, SQLType.REAL)


@pytest.mark.parametrize("value", ["", "a", "abc", "z" * 64, "ünïcödé"])
def test_pk_text_roundtrip(value):
    _, decoded = _roundtrip_pk(value, SQLType.TEXT)
    assert decoded == value


def test_pk_text_handles_embedded_null():
    # GIVEN a string that contains the byte we use as a key terminator
    value = "a\x00b\x00c"
    # WHEN we round-trip
    _, decoded = _roundtrip_pk(value, SQLType.TEXT)
    # THEN it survives intact.
    assert decoded == value


def test_pk_text_is_order_preserving_with_embedded_nulls():
    # GIVEN strings of differing lengths, some with embedded \x00 bytes
    values = ["", "a", "a\x00", "a\x00a", "aa", "b"]
    encoded = sorted(encode_pk_component(v, SQLType.TEXT) for v in values)
    decoded = [decode_pk_component(BufferReader(e), SQLType.TEXT) for e in encoded]
    # THEN the lexicographic byte order matches Python string order.
    assert decoded == sorted(values)


@pytest.mark.parametrize("value", [True, False])
def test_pk_bool_roundtrip(value):
    _, decoded = _roundtrip_pk(value, SQLType.BOOL)
    assert decoded == value


def test_pk_rejects_null():
    with pytest.raises(SQLConstraintError):
        encode_pk_component(None, SQLType.INT)


def test_pk_rejects_wrong_python_type():
    # GIVEN an INTEGER PK column
    # WHEN a string is supplied
    with pytest.raises(SQLTypeError):
        encode_pk_component("not an int", SQLType.INT)


def _make_simple_schema(table_id: int = 1):
    """A representative table: one INT PK, one TEXT, one REAL non-PK."""
    columns = (
        Column("id", SQLType.INT, pk_position=0),
        Column("name", SQLType.TEXT),
        Column("score", SQLType.REAL),
    )
    pk_columns = (columns[0],)
    return TableSchema(table_id=table_id, name="t", columns=columns, pk_columns=pk_columns)


def test_row_key_layout_is_table_prefix_plus_pk():
    # GIVEN a single-INT-PK schema with table_id = 0x2a
    schema = _make_simple_schema(table_id=0x2A)
    # WHEN we encode a row key
    key = encode_row_key(0x2A, (5,), schema)
    # THEN the layout is exactly: prefix \x01, then table_id BE, then encoded PK component.
    assert key == (b"\x01\x00\x00\x00\x00\x00\x00\x00\x2a\x80\x00\x00\x00\x00\x00\x00\x05")


def test_row_value_layout_for_three_typed_columns():
    # GIVEN a schema with a TEXT and REAL non-PK column
    schema = _make_simple_schema()
    # WHEN we encode a value tuple (name='hi', score=2.0)
    payload = encode_row_value(("hi", 2.0), schema)
    # THEN the layout is: count(LE u16), then per-column [tag][payload]
    assert payload == (
        b"\x02\x00"  # 2 non-PK columns (LE u16)
        b"\x03"  # tag = TEXT
        b"\x02\x00\x00\x00"  # len = 2 (LE u32)
        b"hi"
        b"\x02"  # tag = REAL
        b"\x00\x00\x00\x00\x00\x00\x00\x40"  # IEEE-754 LE for 2.0
    )


def test_row_value_handles_null_payload():
    schema = _make_simple_schema()
    payload = encode_row_value((None, None), schema)
    assert decode_row_value(payload, schema) == [None, None]


def test_assemble_full_row_interleaves_pk_and_non_pk_in_declaration_order():
    schema = _make_simple_schema()
    # GIVEN pk values + non-pk values
    pk = (42,)
    non_pk = ["alice", 3.14]
    # WHEN we assemble
    full = assemble_full_row(pk, non_pk, schema)
    # THEN the result is in declaration order with the PK reinserted in its column slot.
    assert full == (42, "alice", 3.14)


def test_table_key_range_covers_exactly_one_table():
    start, end = table_key_range(5)
    assert start < end
    assert start.startswith(b"\x01")
    # Next-table boundary makes the end of table 5 = start of table 6.
    assert end == table_key_range(6)[0]


def test_row_key_roundtrip_through_decode():
    schema = _make_simple_schema()
    key = encode_row_key(schema.table_id, (42,), schema)
    decoded = decode_row_key_pk(key, schema)
    assert decoded == (42,)
