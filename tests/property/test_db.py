from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from bptreedb.db import DB

key_strategy = st.binary(min_size=1, max_size=8)
value_strategy = st.binary(max_size=16)

operation = st.one_of(
    st.tuples(st.just("put"), key_strategy, value_strategy),
    st.tuples(st.just("get"), key_strategy),
    st.tuples(st.just("delete"), key_strategy),
)


@given(ops=st.lists(operation, max_size=200))
@settings(max_examples=200, deadline=None)
def test_db_matches_expected_content(ops):
    with DB() as db:
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
