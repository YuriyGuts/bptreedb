from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from bptreedb.db import DB

# A small alphabet keeps key collisions frequent so the property test exercises
# overwrites and deletes against existing keys, not just inserts.
_key_strategy = st.binary(min_size=1, max_size=4)
_value_strategy = st.binary(max_size=16)
_scan_bound_strategy = st.one_of(st.none(), _key_strategy)

_op_strategy = st.one_of(
    st.tuples(st.just("put"), _key_strategy, _value_strategy),
    st.tuples(st.just("get"), _key_strategy),
    st.tuples(st.just("delete"), _key_strategy),
    st.tuples(st.just("scan"), _scan_bound_strategy, _scan_bound_strategy),
)


@given(ops=st.lists(_op_strategy, max_size=200))
@settings(max_examples=200, deadline=None)
def test_db_matches_expected_content(ops, tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("db")
    with DB(db_dir) as db:
        reference = {}
        for op in ops:
            match op[0]:
                case "put":
                    _, key, value = op
                    db.put(key, value)
                    reference[key] = value
                case "get":
                    _, key = op
                    assert db.get(key) == reference.get(key)
                case "delete":
                    _, key = op
                    assert db.delete(key) == (key in reference)
                    reference.pop(key, None)
                case "scan":
                    _, start, end = op
                    expected = sorted(
                        (key, value)
                        for key, value in reference.items()
                        if (start is None or key >= start) and (end is None or key < end)
                    )
                    assert list(db.scan(start, end)) == expected
