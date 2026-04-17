import random

import pytest

from bptreedb.codec import calculate_page_size
from bptreedb.codec import encode_page
from bptreedb.debug import assert_tree_invariants
from bptreedb.debug import bfs_walk_tree
from bptreedb.entities import InternalPage
from bptreedb.entities import InternalSlot
from bptreedb.entities import LeafPage
from bptreedb.entities import LeafSlot
from bptreedb.exceptions import DBRecordTooLargeError
from bptreedb.pager import Pager
from bptreedb.tree import BPlusTree


@pytest.fixture
def pager(tmp_path):
    pager_path = tmp_path / "pager.dat"
    with Pager(path=pager_path, page_size_bytes=256) as pager:
        yield pager


@pytest.fixture
def make_tree(pager):
    """Create a B+ tree given the specified (id, page) tuples."""

    def _make_tree(
        pages: list[tuple[int, LeafPage | InternalPage]],
        root_page_id: int,
    ):
        largest_page_id = max(page[0] for page in pages)
        while pager.page_count() < largest_page_id + 1:
            pager.allocate_page()

        for page_id, page in sorted(pages, key=lambda p: p[0]):
            pager.write_page(page_id, encode_page(page, pager.page_size_bytes))
        pager.update_meta(root_page_id=root_page_id)

        tree = BPlusTree(pager=pager)
        assert_tree_invariants(tree)
        return tree

    return _make_tree


@pytest.fixture
def empty_tree(make_tree):
    return make_tree(
        pages=[(1, LeafPage(right_sibling_page_id=0, slots=[]))],
        root_page_id=1,
    )


@pytest.fixture
def single_leaf_root_tree(make_tree):
    # Short records are fine because the root is allowed to be underpopulated.
    pages = [
        (
            1,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"baz", value=b"qux"),
                    LeafSlot(key=b"corge", value=b"thud"),
                    LeafSlot(key=b"foo", value=b"bar"),
                ],
            ),
        ),
    ]
    return make_tree(pages=pages, root_page_id=1)


def test_search_single_leaf_root(single_leaf_root_tree):
    # GIVEN a tree with a single leaf node in the root
    tree = single_leaf_root_tree

    # WHEN searching for various keys
    # THEN it should return all keys that were inserted, and correctly report missing keys
    assert tree.search(b"baz") == b"qux"
    assert tree.search(b"corge") == b"thud"
    assert tree.search(b"foo") == b"bar"
    assert tree.search(b"fred") is None


def test_scan_single_leaf_root(single_leaf_root_tree):
    # GIVEN a tree with a single leaf node in the root
    tree = single_leaf_root_tree

    # WHEN searching for various key ranges
    # THEN it should return all inserted keys which match the search criteria
    assert list(tree.scan(b"aaa", b"zzz")) == [
        (b"baz", b"qux"),
        (b"corge", b"thud"),
        (b"foo", b"bar"),
    ]
    assert list(tree.scan(b"yyy", b"zzz")) == []
    assert list(tree.scan(b"baz", b"corge")) == [
        (b"baz", b"qux"),
    ]
    assert list(tree.scan(None, b"foo")) == [
        (b"baz", b"qux"),
        (b"corge", b"thud"),
    ]
    assert list(tree.scan(b"corge", None)) == [
        (b"corge", b"thud"),
        (b"foo", b"bar"),
    ]
    assert list(tree.scan(None, None)) == [
        (b"baz", b"qux"),
        (b"corge", b"thud"),
        (b"foo", b"bar"),
    ]


def test_insert_record_too_large(empty_tree):
    # GIVEN an empty tree
    tree = empty_tree

    # WHEN inserting a record that exceeds the record limit
    # THEN it raises an exception
    msg = r"The database record is too large \(limit: 38 bytes, actual: 39 bytes\)"
    with pytest.raises(DBRecordTooLargeError, match=msg):
        tree.insert(b"k" * 15, b"v" * 16)


def test_insert_overwrite_empty(empty_tree):
    # GIVEN an empty tree
    tree = empty_tree

    # WHEN inserting a new key
    tree.insert(b"foo", b"bar")

    # THEN it can be read back
    assert tree.search(b"foo") == b"bar"

    # WHEN overwriting the key with a new value
    tree.insert(b"foo", b"qux")

    # THEN the new value is read back
    assert tree.search(b"foo") == b"qux"
    assert list(tree.scan(b"foo", b"fop")) == [(b"foo", b"qux")]


def test_insert_single_leaf_root(single_leaf_root_tree):
    # GIVEN a tree with a single leaf node in the root
    tree = single_leaf_root_tree

    # WHEN inserting a new key
    tree.insert(b"xyz", b"prs")

    # THEN the search operation should work correctly for the new key and the preexisting keys
    assert tree.search(b"baz") == b"qux"
    assert tree.search(b"corge") == b"thud"
    assert tree.search(b"foo") == b"bar"
    assert tree.search(b"xyz") == b"prs"
    assert tree.search(b"fred") is None


def test_insert_until_root_split(empty_tree):
    # GIVEN an empty tree
    tree = empty_tree
    assert tree.pager.page_count() == 2

    # 20-byte keys + 4-byte values keep the post-split halves above threshold.
    records = [(f"record_id_{i:010d}".encode(), f"v{i:03d}".encode()) for i in range(1, 7)]

    # WHEN inserting multiple records (out of order), causing the leaf root to split
    for key, value in [records[3], records[0], records[5], records[1], records[4], records[2]]:
        tree.insert(key, value)

    # THEN the tree should allocate two more pages (new sibling leaf, new internal root)
    assert tree.pager.page_count() == 4
    assert tree.pager.get_meta().root_page_id == 3

    assert tree._read_page(3) == InternalPage(
        leftmost_child_page_id=1,
        slots=[InternalSlot(key=records[3][0], child_page_id=2)],
    )
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=2,
        slots=[LeafSlot(key=k, value=v) for k, v in records[:3]],
    )
    assert tree._read_page(2) == LeafPage(
        right_sibling_page_id=0,
        slots=[LeafSlot(key=k, value=v) for k, v in records[3:]],
    )

    # THEN all keys should still be retrievable in order
    assert list(tree.scan(None, None)) == records


def test_insert_until_internal_split(empty_tree):
    # GIVEN an empty tree
    tree = empty_tree

    # WHEN inserting enough keys to split the internal root as well as the leaves
    item_count = 30
    records = [
        (f"record_{i:04d}_xxxxxxxx".encode(), f"v{i:03d}".encode()) for i in range(item_count)
    ]

    for key, value in records:
        tree.insert(key, value)
        assert_tree_invariants(tree)

    # THEN the tree should have reached three levels
    bfs_walk = bfs_walk_tree(tree)
    assert len(bfs_walk) == 3
    assert len(bfs_walk[0]) == 1  # single root

    # THEN the original single-leaf root must have been replaced
    assert tree.pager.get_meta().root_page_id != 1

    # THEN all keys should still be retrievable in order
    for key, value in records:
        assert tree.search(key) == value
    assert list(tree.scan(None, None)) == records


def test_delete_existing_key(single_leaf_root_tree):
    # GIVEN a tree with a single leaf node in the root
    tree = single_leaf_root_tree

    # WHEN deleting an existing key
    # THEN it should return True
    assert tree.delete(b"foo") is True
    # THEN further attempts to delete the same key should return False
    assert tree.delete(b"foo") is False


def test_delete_nonexistent_key(single_leaf_root_tree):
    # GIVEN a tree with a single leaf node in the root
    tree = single_leaf_root_tree

    # WHEN deleting a nonexistent key
    # THEN it should return False
    assert tree.delete(b"xyz") is False


def test_delete_leaf_redistribution_left(make_tree):
    # GIVEN a 2-level tree
    pages = [
        (
            3,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[InternalSlot(key=b"long_record_key_0002", child_page_id=2)],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0000", value=b"v000"),
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0002", value=b"v002"),
                    LeafSlot(key=b"long_record_key_0003", value=b"v003"),
                    LeafSlot(key=b"long_record_key_0004", value=b"v004"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=3)

    # WHEN deleting a key from the left child, causing it to become underpopulated
    tree.delete(b"long_record_key_0001")

    # THEN the tree should self-rebalance by moving slots from the right sibling
    assert tree.pager.page_count() == 4
    assert tree.pager.get_meta().root_page_id == 3
    assert tree._read_page(3) == InternalPage(
        leftmost_child_page_id=1,
        slots=[InternalSlot(key=b"long_record_key_0003", child_page_id=2)],
    )
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=2,
        slots=[
            LeafSlot(key=b"long_record_key_0000", value=b"v000"),
            LeafSlot(key=b"long_record_key_0002", value=b"v002"),
        ],
    )
    assert tree._read_page(2) == LeafPage(
        right_sibling_page_id=0,
        slots=[
            LeafSlot(key=b"long_record_key_0003", value=b"v003"),
            LeafSlot(key=b"long_record_key_0004", value=b"v004"),
        ],
    )
    assert_tree_invariants(tree)


def test_delete_leaf_redistribution_right(make_tree):
    # GIVEN a 2-level tree
    pages = [
        (
            3,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[InternalSlot(key=b"long_record_key_0003", child_page_id=2)],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0000", value=b"v000"),
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                    LeafSlot(key=b"long_record_key_0002", value=b"v002"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0003", value=b"v003"),
                    LeafSlot(key=b"long_record_key_0004", value=b"v004"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=3)

    # WHEN deleting a key from the right child, causing it to become underpopulated
    tree.delete(b"long_record_key_0004")

    # THEN the tree should self-rebalance by moving slots from the left sibling
    assert tree.pager.page_count() == 4
    assert tree.pager.get_meta().root_page_id == 3
    assert tree._read_page(3) == InternalPage(
        leftmost_child_page_id=1,
        slots=[InternalSlot(key=b"long_record_key_0002", child_page_id=2)],
    )
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=2,
        slots=[
            LeafSlot(key=b"long_record_key_0000", value=b"v000"),
            LeafSlot(key=b"long_record_key_0001", value=b"v001"),
        ],
    )
    assert tree._read_page(2) == LeafPage(
        right_sibling_page_id=0,
        slots=[
            LeafSlot(key=b"long_record_key_0002", value=b"v002"),
            LeafSlot(key=b"long_record_key_0003", value=b"v003"),
        ],
    )
    assert_tree_invariants(tree)


def test_delete_leaf_merge_left(make_tree):
    # GIVEN a 2-level tree
    pages = [
        (
            4,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[
                    InternalSlot(key=b"long_record_key_0002", child_page_id=2),
                    InternalSlot(key=b"long_record_key_0004", child_page_id=3),
                ],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0000", value=b"v000"),
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=3,
                slots=[
                    LeafSlot(key=b"long_record_key_0002", value=b"v002"),
                    LeafSlot(key=b"long_record_key_0003", value=b"v003"),
                ],
            ),
        ),
        (
            3,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0004", value=b"v004"),
                    LeafSlot(key=b"long_record_key_0005", value=b"v005"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=4)

    # WHEN deleting a key from the right leaf, causing it to become underpopulated
    tree.delete(b"long_record_key_0004")

    # THEN the tree should self-rebalance by merging the right leaf with the middle leaf
    assert tree.pager.page_count() == 5
    assert tree.pager.get_meta().root_page_id == 4
    assert tree._read_page(4) == InternalPage(
        leftmost_child_page_id=1,
        slots=[
            InternalSlot(key=b"long_record_key_0002", child_page_id=2),
        ],
    )
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=2,
        slots=[
            LeafSlot(key=b"long_record_key_0000", value=b"v000"),
            LeafSlot(key=b"long_record_key_0001", value=b"v001"),
        ],
    )
    assert tree._read_page(2) == LeafPage(
        right_sibling_page_id=0,
        slots=[
            LeafSlot(key=b"long_record_key_0002", value=b"v002"),
            LeafSlot(key=b"long_record_key_0003", value=b"v003"),
            LeafSlot(key=b"long_record_key_0005", value=b"v005"),
        ],
    )
    assert_tree_invariants(tree)


def test_delete_leaf_merge_right(make_tree):
    # GIVEN a 2-level tree
    pages = [
        (
            4,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[
                    InternalSlot(key=b"long_record_key_0002", child_page_id=2),
                    InternalSlot(key=b"long_record_key_0004", child_page_id=3),
                ],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0000", value=b"v000"),
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=3,
                slots=[
                    LeafSlot(key=b"long_record_key_0002", value=b"v002"),
                    LeafSlot(key=b"long_record_key_0003", value=b"v003"),
                ],
            ),
        ),
        (
            3,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0004", value=b"v004"),
                    LeafSlot(key=b"long_record_key_0005", value=b"v005"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=4)

    # WHEN deleting a key from the left leaf, causing it to become underpopulated
    tree.delete(b"long_record_key_0000")

    # THEN the tree should self-rebalance by merging the left leaf with the middle leaf
    assert tree.pager.page_count() == 5
    assert tree.pager.get_meta().root_page_id == 4
    assert tree._read_page(4) == InternalPage(
        leftmost_child_page_id=1,
        slots=[
            InternalSlot(key=b"long_record_key_0004", child_page_id=3),
        ],
    )
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=3,
        slots=[
            LeafSlot(key=b"long_record_key_0001", value=b"v001"),
            LeafSlot(key=b"long_record_key_0002", value=b"v002"),
            LeafSlot(key=b"long_record_key_0003", value=b"v003"),
        ],
    )
    assert tree._read_page(3) == LeafPage(
        right_sibling_page_id=0,
        slots=[
            LeafSlot(key=b"long_record_key_0004", value=b"v004"),
            LeafSlot(key=b"long_record_key_0005", value=b"v005"),
        ],
    )
    assert_tree_invariants(tree)


def test_delete_leaf_merge_left_at_slot_0(make_tree):
    # Exercises the merge-at-slot-0 path: the underpopulated leaf sits at
    # parent.slots[0], so its left same-parent sibling is the leftmost_child.

    # GIVEN a 2-level tree
    pages = [
        (
            4,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[
                    InternalSlot(key=b"long_record_key_0020", child_page_id=2),
                    InternalSlot(key=b"long_record_key_0040", child_page_id=3),
                ],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0000", value=b"v000"),
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=3,
                slots=[
                    LeafSlot(key=b"long_record_key_0020", value=b"v020"),
                    LeafSlot(key=b"long_record_key_0021", value=b"v021"),
                ],
            ),
        ),
        (
            3,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0040", value=b"v040"),
                    LeafSlot(key=b"long_record_key_0041", value=b"v041"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=4)

    # WHEN deleting a key from page 2, making it underpopulated
    tree.delete(b"long_record_key_0020")

    # THEN the underpopulated leaf (page 2) should be merged into the leftmost_child (page 1)
    assert tree.pager.page_count() == 5
    assert tree.pager.get_meta().root_page_id == 4
    assert tree._read_page(4) == InternalPage(
        leftmost_child_page_id=1,
        slots=[
            InternalSlot(key=b"long_record_key_0040", child_page_id=3),
        ],
    )
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=3,
        slots=[
            LeafSlot(key=b"long_record_key_0000", value=b"v000"),
            LeafSlot(key=b"long_record_key_0001", value=b"v001"),
            LeafSlot(key=b"long_record_key_0021", value=b"v021"),
        ],
    )
    assert tree._read_page(3) == LeafPage(
        right_sibling_page_id=0,
        slots=[
            LeafSlot(key=b"long_record_key_0040", value=b"v040"),
            LeafSlot(key=b"long_record_key_0041", value=b"v041"),
        ],
    )
    assert_tree_invariants(tree)

    # THEN every surviving key should still be retrievable
    survivors = [
        (b"long_record_key_0000", b"v000"),
        (b"long_record_key_0001", b"v001"),
        (b"long_record_key_0021", b"v021"),
        (b"long_record_key_0040", b"v040"),
        (b"long_record_key_0041", b"v041"),
    ]
    for key, value in survivors:
        assert tree.search(key) == value
    assert tree.search(b"long_record_key_0020") is None
    assert list(tree.scan(None, None)) == survivors


def test_delete_leaf_merge_and_root_collapse(make_tree):
    # GIVEN a 2-level tree
    pages = [
        (
            3,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[InternalSlot(key=b"long_record_key_0002", child_page_id=2)],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0000", value=b"v000"),
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0002", value=b"v002"),
                    LeafSlot(key=b"long_record_key_0003", value=b"v003"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=3)

    # WHEN deleting a key from the left child, causing it to become underpopulated
    tree.delete(b"long_record_key_0000")

    # THEN the tree should self-rebalance by merging the leaf nodes and collapsing the root
    assert tree.pager.page_count() == 4
    assert tree.pager.get_meta().root_page_id == 1
    assert tree._read_page(1) == LeafPage(
        right_sibling_page_id=0,
        slots=[
            LeafSlot(key=b"long_record_key_0001", value=b"v001"),
            LeafSlot(key=b"long_record_key_0002", value=b"v002"),
            LeafSlot(key=b"long_record_key_0003", value=b"v003"),
        ],
    )
    assert_tree_invariants(tree)


def test_delete_cascades_into_internal_redistribute(make_tree):
    # GIVEN a 3-level tree
    pages = [
        (
            10,
            InternalPage(
                leftmost_child_page_id=11,
                slots=[InternalSlot(key=b"long_record_key_0007", child_page_id=12)],
            ),
        ),
        (
            11,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[
                    InternalSlot(key=b"long_record_key_0003", child_page_id=2),
                    InternalSlot(key=b"long_record_key_0005", child_page_id=3),
                ],
            ),
        ),
        (
            12,
            InternalPage(
                leftmost_child_page_id=4,
                slots=[
                    InternalSlot(key=b"long_record_key_0009", child_page_id=5),
                    InternalSlot(key=b"long_record_key_0011", child_page_id=6),
                    InternalSlot(key=b"long_record_key_0013", child_page_id=7),
                ],
            ),
        ),
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(key=b"long_record_key_0001", value=b"v001"),
                    LeafSlot(key=b"long_record_key_0002", value=b"v002"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=3,
                slots=[
                    LeafSlot(key=b"long_record_key_0003", value=b"v003"),
                    LeafSlot(key=b"long_record_key_0004", value=b"v004"),
                ],
            ),
        ),
        (
            3,
            LeafPage(
                right_sibling_page_id=4,
                slots=[
                    LeafSlot(key=b"long_record_key_0005", value=b"v005"),
                    LeafSlot(key=b"long_record_key_0006", value=b"v006"),
                ],
            ),
        ),
        (
            4,
            LeafPage(
                right_sibling_page_id=5,
                slots=[
                    LeafSlot(key=b"long_record_key_0007", value=b"v007"),
                    LeafSlot(key=b"long_record_key_0008", value=b"v008"),
                ],
            ),
        ),
        (
            5,
            LeafPage(
                right_sibling_page_id=6,
                slots=[
                    LeafSlot(key=b"long_record_key_0009", value=b"v009"),
                    LeafSlot(key=b"long_record_key_0010", value=b"v010"),
                ],
            ),
        ),
        (
            6,
            LeafPage(
                right_sibling_page_id=7,
                slots=[
                    LeafSlot(key=b"long_record_key_0011", value=b"v011"),
                    LeafSlot(key=b"long_record_key_0012", value=b"v012"),
                ],
            ),
        ),
        (
            7,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(key=b"long_record_key_0013", value=b"v013"),
                    LeafSlot(key=b"long_record_key_0014", value=b"v014"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=10)

    # WHEN deleting key_0004
    # This will merge leaf 2 into leaf 1, underpopulate internal 11,
    # and force it to redistribute with internal 12.
    tree.delete(b"long_record_key_0004")

    # THEN every surviving key must still be retrievable
    survivors = [
        (f"long_record_key_{idx:04d}".encode(), f"v{idx:03d}".encode())
        for idx in [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    ]
    for key, value in survivors:
        assert tree.search(key) == value
    assert tree.search(b"long_record_key_0004") is None


def test_delete_cascades_into_internal_merge(make_tree):
    # A single delete cascades from a leaf merge into an internal merge. The
    # internal merge must pull down root's separator so that the merged leaf's
    # subtree isn't orphaned.

    # GIVEN a 3-level tree (L0 with 2 slots > 3x L1 with 2 slots each > 9x L2 leaves)
    pages = [
        # Root
        (
            13,
            InternalPage(
                leftmost_child_page_id=10,
                slots=[
                    InternalSlot(key=b"long_record_key_0030", child_page_id=11),
                    InternalSlot(key=b"long_record_key_0060", child_page_id=12),
                ],
            ),
        ),
        # Internal pages
        (
            10,
            InternalPage(
                leftmost_child_page_id=1,
                slots=[
                    InternalSlot(key=b"long_record_key_0010", child_page_id=2),
                    InternalSlot(key=b"long_record_key_0020", child_page_id=3),
                ],
            ),
        ),
        (
            11,
            InternalPage(
                leftmost_child_page_id=4,
                slots=[
                    InternalSlot(key=b"long_record_key_0040", child_page_id=5),
                    InternalSlot(key=b"long_record_key_0050", child_page_id=6),
                ],
            ),
        ),
        (
            12,
            InternalPage(
                leftmost_child_page_id=7,
                slots=[
                    InternalSlot(key=b"long_record_key_0070", child_page_id=8),
                    InternalSlot(key=b"long_record_key_0080", child_page_id=9),
                ],
            ),
        ),
        # Leaves
        (
            1,
            LeafPage(
                right_sibling_page_id=2,
                slots=[
                    LeafSlot(b"long_record_key_0000", b"v000"),
                    LeafSlot(b"long_record_key_0005", b"v005"),
                ],
            ),
        ),
        (
            2,
            LeafPage(
                right_sibling_page_id=3,
                slots=[
                    LeafSlot(b"long_record_key_0010", b"v010"),
                    LeafSlot(b"long_record_key_0015", b"v015"),
                ],
            ),
        ),
        (
            3,
            LeafPage(
                right_sibling_page_id=4,
                slots=[
                    LeafSlot(b"long_record_key_0020", b"v020"),
                    LeafSlot(b"long_record_key_0025", b"v025"),
                ],
            ),
        ),
        (
            4,
            LeafPage(
                right_sibling_page_id=5,
                slots=[
                    LeafSlot(b"long_record_key_0030", b"v030"),
                    LeafSlot(b"long_record_key_0035", b"v035"),
                ],
            ),
        ),
        (
            5,
            LeafPage(
                right_sibling_page_id=6,
                slots=[
                    LeafSlot(b"long_record_key_0040", b"v040"),
                    LeafSlot(b"long_record_key_0045", b"v045"),
                ],
            ),
        ),
        (
            6,
            LeafPage(
                right_sibling_page_id=7,
                slots=[
                    LeafSlot(b"long_record_key_0050", b"v050"),
                    LeafSlot(b"long_record_key_0055", b"v055"),
                ],
            ),
        ),
        (
            7,
            LeafPage(
                right_sibling_page_id=8,
                slots=[
                    LeafSlot(b"long_record_key_0060", b"v060"),
                    LeafSlot(b"long_record_key_0065", b"v065"),
                ],
            ),
        ),
        (
            8,
            LeafPage(
                right_sibling_page_id=9,
                slots=[
                    LeafSlot(b"long_record_key_0070", b"v070"),
                    LeafSlot(b"long_record_key_0075", b"v075"),
                ],
            ),
        ),
        (
            9,
            LeafPage(
                right_sibling_page_id=0,
                slots=[
                    LeafSlot(b"long_record_key_0080", b"v080"),
                    LeafSlot(b"long_record_key_0085", b"v085"),
                ],
            ),
        ),
    ]
    tree = make_tree(pages=pages, root_page_id=13)

    # WHEN deleting key60 (leaf 7 merges with leaf 8, then internal 12 merges with internal 11)
    tree.delete(b"long_record_key_0060")

    # THEN the tree should flatten to 2 levels with internal 12's subtree absorbed into page 11
    assert tree.pager.get_meta().root_page_id == 13
    assert tree._read_page(13) == InternalPage(
        leftmost_child_page_id=10,
        slots=[
            InternalSlot(key=b"long_record_key_0030", child_page_id=11),
        ],
    )
    assert tree._read_page(10) == InternalPage(
        leftmost_child_page_id=1,
        slots=[
            InternalSlot(key=b"long_record_key_0010", child_page_id=2),
            InternalSlot(key=b"long_record_key_0020", child_page_id=3),
        ],
    )
    # page 11 must include the pulled-down separator (key60) pointing at page 7.
    assert tree._read_page(11) == InternalPage(
        leftmost_child_page_id=4,
        slots=[
            InternalSlot(key=b"long_record_key_0040", child_page_id=5),
            InternalSlot(key=b"long_record_key_0050", child_page_id=6),
            InternalSlot(key=b"long_record_key_0060", child_page_id=7),
            InternalSlot(key=b"long_record_key_0080", child_page_id=9),
        ],
    )
    assert tree._read_page(7) == LeafPage(
        right_sibling_page_id=9,
        slots=[
            LeafSlot(key=b"long_record_key_0065", value=b"v065"),
            LeafSlot(key=b"long_record_key_0070", value=b"v070"),
            LeafSlot(key=b"long_record_key_0075", value=b"v075"),
        ],
    )
    assert_tree_invariants(tree)

    # THEN every key that was in the tree before the delete (minus key60) must still be retrievable.
    survivors = [
        (f"long_record_key_00{idx:02d}".encode(), f"v0{idx:02d}".encode())
        for idx in [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 65, 70, 75, 80, 85]
    ]
    for key, value in survivors:
        assert tree.search(key) == value
    assert tree.search(b"long_record_key_0060") is None
    assert list(tree.scan(None, None)) == survivors


def test_split_picks_balanced_point_under_skewed_slot_sizes(tmp_path):
    # The byte-midpoint split would leave the left half at 1524 B, below the
    # 1638 B underpopulated threshold. The smart split must pick a different index.
    page_size = 4096
    pager_path = tmp_path / "pager.dat"
    with Pager(path=pager_path, page_size_bytes=page_size) as pager:
        # GIVEN an overpopulated 4 KB leaf with a skewed slot-size distribution
        small_slots = [LeafSlot(key=f"a{i}".encode(), value=b"_" * 132) for i in range(10)]
        med_slot = LeafSlot(key=b"b0", value=b"_" * 782)
        large_slots = [LeafSlot(key=f"c{i}".encode(), value=b"_" * 615) for i in range(3)]
        all_slots = small_slots + [med_slot] + large_slots
        overpopulated_leaf = LeafPage(right_sibling_page_id=0, slots=list(all_slots))
        assert calculate_page_size(overpopulated_leaf) > page_size

        pager.allocate_page()
        pager.write_page(1, encode_page(overpopulated_leaf, page_size))
        pager.update_meta(root_page_id=1)
        tree = BPlusTree(pager=pager)

        # WHEN splitting the leaf
        tree._split_page(page_id=1, parent_page_id=None, page=overpopulated_leaf)

        # THEN both halves should be at or above the underpopulated threshold,
        # and, combined, they should still contain every original slot
        new_root = tree._read_page(tree.pager.get_meta().root_page_id)
        assert isinstance(new_root, InternalPage)
        left_half = tree._read_page(1)
        right_half = tree._read_page(new_root.slots[0].child_page_id)
        assert not tree._is_page_underpopulated(left_half)
        assert not tree._is_page_underpopulated(right_half)
        assert list(left_half.slots) + list(right_half.slots) == all_slots


def test_invariants_hold_at_4kb_with_mixed_key_lengths(tmp_path):
    # Random ops at 4 KB with varied key/value lengths exercise both unbalanced
    # splits and redistributes that inflate parent separators.
    page_size = 4096
    pager_path = tmp_path / "pager.dat"
    with Pager(path=pager_path, page_size_bytes=page_size) as pager:
        # GIVEN an empty 4 KB tree and an oracle to cross-check every read
        tree = BPlusTree(pager=pager)
        oracle: dict[bytes, bytes] = {}

        # Seed=7 reliably triggers a parent-inflating redistribute within 500 ops.
        rng = random.Random(7)
        max_kv = (page_size - 24) // 5 - 8 - 8
        length_choices = (1, 2, 4, 16, 64, 256, 400, 600)

        # WHEN running 500 random insert/delete ops with varied key/value lengths
        for _ in range(500):
            if oracle and rng.random() < 0.25:
                key = rng.choice(list(oracle))
                assert tree.delete(key) is True
                oracle.pop(key)
            else:
                key_len = min(rng.choice(length_choices), max_kv)
                val_len = rng.randint(0, min(rng.choice(length_choices), max_kv - key_len))
                key = rng.randbytes(key_len)
                value = rng.randbytes(val_len)
                tree.insert(key, value)
                oracle[key] = value

            # THEN invariants should hold after every op
            assert_tree_invariants(tree)

        # THEN every surviving key should still read back the value the oracle expects
        for key, expected in oracle.items():
            assert tree.search(key) == expected


def test_stats_counters_record_splits_and_reset(empty_tree):
    # GIVEN a fresh empty tree
    tree = empty_tree
    assert tree.stats.leaf_splits == 0
    assert tree.stats.internal_splits == 0

    # WHEN inserting enough keys to force at least one leaf split on a 256-byte page
    for i in range(100):
        tree.insert(f"key-{i:04d}".encode(), b"v" * 16)

    # THEN the split counter should have advanced
    assert tree.stats.leaf_splits >= 1

    # WHEN resetting the stats
    tree.stats.reset()

    # THEN every field should be back to zero
    assert tree.stats.leaf_splits == 0
    assert tree.stats.internal_splits == 0
    assert tree.stats.leaf_merges == 0
    assert tree.stats.internal_merges == 0
    assert tree.stats.leaf_redistributes == 0
    assert tree.stats.internal_redistributes == 0
    assert tree.stats.root_collapses == 0
