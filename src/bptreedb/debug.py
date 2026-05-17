"""Debug helpers for inspecting tree shape and verifying B+ tree invariants in tests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from bptreedb.entities import InternalPage
from bptreedb.entities import LeafPage
from bptreedb.tree import BPlusTree


@dataclass
class BPlusTreeNode:
    """A single materialized node in the in-memory tree representation."""

    page_id: int
    page: LeafPage | InternalPage
    children: list[BPlusTreeNode]


def page_id_to_node(tree: BPlusTree, page_id: int) -> BPlusTreeNode:
    """
    Materialize a `BPlusTreeNode` by fetching the page with the given ID through the cache.

    Parameters
    ----------
    tree
        The tree the page belongs to.
    page_id
        The ID of the page to wrap.

    Returns
    -------
    A node with an empty `children` list; the caller is responsible for populating it.
    """
    page = tree.buffer_pool.get(page_id)
    return BPlusTreeNode(page_id=page_id, page=page, children=[])


def bfs_walk_tree(tree: BPlusTree) -> list[list[BPlusTreeNode]]:
    """
    Walk the tree breadth-first.

    Parameters
    ----------
    tree
        The tree to traverse.

    Returns
    -------
    A list of levels, with position 0 holding the root and the last position holding the leaves.
    Each node has its `children` populated, so callers can navigate without re-fetching.
    """
    result = []
    queue = deque()
    meta = tree.pager.get_meta()
    root_node = page_id_to_node(tree, meta.root_page_id)

    queue.append((root_node, 0))
    while queue:
        node, level = queue.popleft()
        if len(result) < level + 1:
            result.append([])

        result[level].append(node)
        match node.page:
            case LeafPage():
                pass
            case InternalPage():
                leftmost_child_node = page_id_to_node(tree, node.page.leftmost_child_page_id)
                node.children.append(leftmost_child_node)
                queue.append((leftmost_child_node, level + 1))
                for child_slot in node.page.slots:
                    child_node = page_id_to_node(tree, child_slot.child_page_id)
                    node.children.append(child_node)
                    queue.append((child_node, level + 1))
            case _:
                raise ValueError(f"Unexpected page type: {type(node.page).__name__}")

    return result


def raise_invariant_error(msg: str, node: BPlusTreeNode, level: int) -> None:
    """
    Format an invariant-violation message with node context and raise it.

    Parameters
    ----------
    msg
        Short description of the invariant that was violated.
    node
        The offending node.
    level
        The level in the tree at which the node sits.

    Raises
    ------
    AssertionError
        Always; this function never returns.
    """
    formatted_msg = (
        f"{msg} (page ID: {node.page_id}, page type: {type(node.page).__name__}, level: {level})"
    )
    raise AssertionError(formatted_msg)


def _smallest_leaf_key(node: BPlusTreeNode) -> bytes:
    """Return the smallest leaf key reachable from `node`."""
    while isinstance(node.page, InternalPage):
        node = node.children[0]
    assert isinstance(node.page, LeafPage)
    return node.page.slots[0].key


def _largest_leaf_key(node: BPlusTreeNode) -> bytes:
    """Return the largest leaf key reachable from `node`."""
    while isinstance(node.page, InternalPage):
        node = node.children[-1]
    assert isinstance(node.page, LeafPage)
    return node.page.slots[-1].key


def assert_tree_invariants(tree: BPlusTree) -> None:  # noqa: PLR0912
    """
    Verify all structural invariants of a B+ tree.

    Checks performed: bounded page IDs, per-page key ordering, leaf/internal level discipline,
    fill thresholds for non-root pages, correctness of internal separators, and the integrity
    of the leaf sibling chain.

    Parameters
    ----------
    tree
        The tree to inspect.

    Raises
    ------
    AssertionError
        On the first invariant violation found.
    """
    meta = tree.pager.get_meta()
    bfs_walk = bfs_walk_tree(tree)
    level_count = len(bfs_walk)

    for level_idx, level in enumerate(bfs_walk):
        for node in level:
            page = node.page

            # Every page reachable from the tree has a page ID < next_page_id.
            if not node.page_id < meta.next_page_id:
                raise_invariant_error("Page ID exceeds meta's next page ID", node, level_idx)

            # All slots in all pages must be sorted by key.
            for i in range(0, len(page.slots) - 1):
                if not page.slots[i].key <= page.slots[i + 1].key:
                    raise_invariant_error("Slots are not sorted by key", node, level_idx)

            # Leaf pages can only be at the bottom.
            if level_idx < level_count - 1 and isinstance(page, LeafPage):
                raise_invariant_error("Unexpected leaf page at non-leaf level", node, level_idx)

            # Internal pages cannot be at the bottom.
            if level_idx == level_count - 1 and isinstance(page, InternalPage):
                raise_invariant_error("Unexpected internal page at leaf level", node, level_idx)

            # All non-root pages must be at least half full.
            if level_idx > 0 and tree._is_page_underpopulated(page):
                raise_invariant_error("Underpopulated non-root page", node, level_idx)

            # Internal node separators correctly partition the children.
            if level_idx < level_count - 1:
                assert isinstance(page, InternalPage)
                for slot_idx, slot in enumerate(page.slots):
                    left_child_max = _largest_leaf_key(node.children[slot_idx])
                    right_child_min = _smallest_leaf_key(node.children[slot_idx + 1])
                    if not (left_child_max < slot.key <= right_child_min):
                        raise_invariant_error("Invalid internal node separator", node, level_idx)

    # Leaf sibling pointers form a complete forward chain in key order with no cycles.
    leaf_nodes = bfs_walk[-1]
    seen_leaf_pages = set()
    for leaf_idx, leaf_node in enumerate(leaf_nodes):
        assert isinstance(leaf_node.page, LeafPage)
        if leaf_node.page_id in seen_leaf_pages:
            raise_invariant_error("Leaf node cycle", leaf_node, level_count - 1)
        seen_leaf_pages.add(leaf_node.page_id)

        if leaf_idx < len(leaf_nodes) - 1:
            if leaf_node.page.right_sibling_page_id != leaf_nodes[leaf_idx + 1].page_id:
                raise_invariant_error("Inconsistent sibling pointers", leaf_node, level_count - 1)
            if leaf_node.page.slots[-1].key > leaf_nodes[leaf_idx + 1].page.slots[0].key:
                raise_invariant_error("Siblings not ordered by key", leaf_node, level_count - 1)

    assert len(seen_leaf_pages) == len(leaf_nodes), "Not all nodes are linked by sibling pointers"
