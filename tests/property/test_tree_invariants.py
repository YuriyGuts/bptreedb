from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from bptreedb.cache import BufferPool
from bptreedb.debug import assert_tree_invariants
from bptreedb.pager import Pager
from bptreedb.tree import BPlusTree

# A small page size forces frequent splits and merges within a short op sequence, so the fuzz test
# exercises the multi-level tree machinery rather than lingering in a single-leaf regime.
_PAGE_SIZE_BYTES = 256

# Max leaf record body at this page size is 38 bytes; subtracting the two 4-byte length prefixes
# leaves 30 bytes for `len(key) + len(value)`.
# A small key alphabet keeps collisions frequent, which exercises overwrites and real deletes.
# Variable value lengths produce enough slot-size variance to exercise the smart split algorithm.
_key_strategy = st.binary(min_size=1, max_size=4)
_value_strategy = st.binary(min_size=0, max_size=10)

_op_strategy = st.one_of(
    st.tuples(st.just("insert"), _key_strategy, _value_strategy),
    st.tuples(st.just("delete"), _key_strategy),
    st.tuples(st.just("search"), _key_strategy),
)


@given(ops=st.lists(_op_strategy, max_size=1000))
@settings(max_examples=200, deadline=None)
def test_tree_invariants_hold_under_random_ops(ops, tmp_path_factory):
    pager_dir = tmp_path_factory.mktemp("tree")
    with Pager(path=pager_dir / "pager.dat", page_size_bytes=_PAGE_SIZE_BYTES) as pager:
        tree = BPlusTree(pager=pager, buffer_pool=BufferPool(pager=pager, capacity_pages=256))
        oracle: dict[bytes, bytes] = {}
        lsn = 1

        for op in ops:
            match op[0]:
                case "insert":
                    _, key, value = op
                    tree.insert(key, value, lsn)
                    lsn += 1
                    oracle[key] = value
                case "delete":
                    _, key = op
                    assert tree.delete(key, lsn) == (key in oracle)
                    lsn += 1
                    oracle.pop(key, None)
                case "search":
                    _, key = op
                    assert tree.search(key) == oracle.get(key)

            assert_tree_invariants(tree)

        # After the whole sequence, every oracle key still reads back correctly.
        for key, value in oracle.items():
            assert tree.search(key) == value
