"""B+ tree algorithms: search, insertion, deletion, range scans, and rebalancing."""

import bisect
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from operator import attrgetter

from bptreedb.cache import BufferPool
from bptreedb.codec import calculate_leaf_record_size
from bptreedb.codec import calculate_page_size
from bptreedb.codec import calculate_slot_size
from bptreedb.codec import get_max_leaf_record_size
from bptreedb.entities import InternalPage
from bptreedb.entities import InternalSlot
from bptreedb.entities import LeafPage
from bptreedb.entities import LeafSlot
from bptreedb.exceptions import DBRecordTooLargeError
from bptreedb.pager import Pager

_KEY_GETTER = attrgetter("key")


@dataclass
class PathItem:
    """A single step in the path from the root to a leaf, recording how we descended."""

    parent_page_id: int
    parent_slot_index: int


@dataclass
class LeafSearchResult:
    """Outcome of a root-to-leaf descent: the leaf page reached and the path taken to get there."""

    leaf_page_id: int
    leaf_page: LeafPage
    path: list[PathItem]


@dataclass
class TreeStats:
    """A statistics object that tracks structural changes to the tree."""

    leaf_splits: int = 0
    internal_splits: int = 0
    leaf_merges: int = 0
    internal_merges: int = 0
    leaf_redistributes: int = 0
    internal_redistributes: int = 0
    root_collapses: int = 0

    def reset(self) -> None:
        """Reset all stats to initial values."""
        self.leaf_splits = 0
        self.internal_splits = 0
        self.leaf_merges = 0
        self.internal_merges = 0
        self.leaf_redistributes = 0
        self.internal_redistributes = 0
        self.root_collapses = 0


class BPlusTree:
    """A disk-backed B+ tree that delegates persistence to a pager and a buffer pool."""

    HALF_FULL_THRESHOLD = 0.4

    def __init__(self, pager: Pager, buffer_pool: BufferPool) -> None:
        """
        Create a tree backed by the given pager and buffer pool.

        Parameters
        ----------
        pager
            The pager that owns the underlying page file.
        buffer_pool
            The buffer pool used to read, mutate, and write pages.
        """
        self.pager = pager
        self.buffer_pool = buffer_pool
        self.stats = TreeStats()

    @property
    def page_size_bytes(self) -> int:
        """Page size of the underlying pager, in bytes."""
        return self.pager.get_meta().page_size_bytes

    @property
    def root_page_id(self) -> int:
        """Current root page ID, looked up via the pager's meta page."""
        return self.pager.get_meta().root_page_id

    def search(self, key: bytes) -> bytes | None:
        """
        Look up the value associated with `key`.

        Parameters
        ----------
        key
            The key to look up.

        Returns
        -------
        The stored value, or `None` if no such key exists.
        """
        # Find the leaf node that may contain the key.
        leaf_page = self._find_leaf(key).leaf_page

        # Find the key slot.
        slot_idx = bisect.bisect_left(leaf_page.slots, key, key=_KEY_GETTER)
        if not 0 <= slot_idx < len(leaf_page.slots) or leaf_page.slots[slot_idx].key != key:
            return None
        return leaf_page.slots[slot_idx].value

    def insert(self, key: bytes, value: bytes, lsn: int) -> None:
        """
        Insert or overwrite the value for `key`.

        Parameters
        ----------
        key
            The key to insert or update.
        value
            The value to associate with the key.
        lsn
            LSN to stamp onto every page modified by this operation.

        Raises
        ------
        DBRecordTooLargeError
            If the encoded `(key, value)` pair would exceed the per-page record size limit.
        """
        self.raise_if_record_too_large(key, value)

        # Find a leaf page to insert into.
        leaf_result = self._find_leaf(key)
        leaf_page_id = leaf_result.leaf_page_id
        leaf_page = leaf_result.leaf_page

        # Search the key.
        slot_idx = bisect.bisect_left(leaf_page.slots, key, key=_KEY_GETTER)

        # If it exists, overwrite the value.
        # Otherwise, insert a new leaf slot, preserving the key order.
        if 0 <= slot_idx < len(leaf_page.slots) and leaf_page.slots[slot_idx].key == key:
            leaf_page.slots[slot_idx].value = value
        else:
            bisect.insort(leaf_page.slots, LeafSlot(key=key, value=value), key=_KEY_GETTER)

        # Rebalance if the page overflows.
        self._balance_and_write_tree(leaf_page_id, leaf_page, leaf_result.path, lsn)

    def delete(self, key: bytes, lsn: int) -> bool:
        """
        Remove `key` from the tree.

        Parameters
        ----------
        key
            The key to remove.
        lsn
            LSN to stamp onto every page modified by this operation.

        Returns
        -------
        `True` if the key existed and was removed, `False` if it was not present.
        """
        # Find the leaf node that may contain the key.
        leaf_result = self._find_leaf(key)
        leaf_page_id = leaf_result.leaf_page_id
        leaf_page = leaf_result.leaf_page

        # Search the key.
        slot_idx = bisect.bisect_left(leaf_page.slots, key, key=_KEY_GETTER)

        # If the key did not actually exist in the leaf page, do nothing.
        if not 0 <= slot_idx < len(leaf_page.slots) or leaf_page.slots[slot_idx].key != key:
            return False

        # Remove the leaf slot corresponding to the key.
        leaf_page.slots.pop(slot_idx)

        # Rebalance if the page ends up being underpopulated.
        self._balance_and_write_tree(leaf_page_id, leaf_page, leaf_result.path, lsn)

        return True

    def scan(
        self,
        start_key_inclusive: bytes | None,
        end_key_exclusive: bytes | None,
        version_checker: Callable[[], None] | None = None,
    ) -> Iterator[tuple[bytes, bytes]]:
        """
        Yield key/value pairs within the half-open range `[start, end)` in key order.

        Walks the leaf sibling chain after the first descent.

        Parameters
        ----------
        start_key_inclusive
            Lower bound on the key; `None` is treated as the very first key.
        end_key_exclusive
            Upper bound on the key (exclusive); `None` means "scan to the end".
        version_checker
            Optional callback invoked before and after every yield. Use it to raise from the
            caller side when a concurrent mutation is detected.

        Returns
        -------
        An iterator over `(key, value)` tuples in key order.
        """
        # Find the leaf node that may contain the start key.
        start_key_inclusive = start_key_inclusive or b""
        leaf_result = self._find_leaf(start_key_inclusive)
        leaf_page = leaf_result.leaf_page
        start_slot_idx = bisect.bisect_left(leaf_page.slots, start_key_inclusive, key=_KEY_GETTER)

        while True:
            for slot in islice(leaf_page.slots, start_slot_idx, None):
                # We've reached `end_key_exclusive`. No point in looking further.
                if end_key_exclusive is not None and slot.key >= end_key_exclusive:
                    return

                # Pre-yield: catches mutations that happened before this iteration started.
                if version_checker is not None:
                    version_checker()

                yield slot.key, slot.value

                # Post-yield: catches mutations during the yield, e.g. a concurrent delete which
                # erases the slots we were iterating over, leading to a silent StopIteration.
                if version_checker is not None:
                    version_checker()

            # Keep walking the leaf chain via right sibling pointers.
            if leaf_page.right_sibling_page_id > 0:
                leaf_page = self.buffer_pool.get(leaf_page.right_sibling_page_id)
                start_slot_idx = 0
                assert isinstance(leaf_page, LeafPage)
            else:
                return

    def raise_if_record_too_large(self, key: bytes, value: bytes) -> None:
        """
        Bounds-check a key/value pair against the per-record size limit.

        Parameters
        ----------
        key
            The proposed key.
        value
            The proposed value.

        Raises
        ------
        DBRecordTooLargeError
            If the encoded `(key, value)` pair would not fit on a leaf page.
        """
        max_leaf_record_size = get_max_leaf_record_size(self.page_size_bytes)
        current_leaf_record_size = calculate_leaf_record_size(key, value)
        if current_leaf_record_size > max_leaf_record_size:
            raise DBRecordTooLargeError(limit=max_leaf_record_size, actual=current_leaf_record_size)

    def _is_page_overpopulated(self, page: LeafPage | InternalPage) -> bool:
        """
        Check whether the page would overflow its on-disk byte budget if written as-is.

        Parameters
        ----------
        page
            The page being checked.

        Returns
        -------
        `True` when the encoded size exceeds the page size, `False` otherwise.
        """
        return calculate_page_size(page) > self.page_size_bytes

    def _is_page_underpopulated(self, page: LeafPage | InternalPage) -> bool:
        """
        Check whether the page sits below the half-full threshold.

        Parameters
        ----------
        page
            The page being checked.

        Returns
        -------
        `True` when the encoded size is below the threshold, `False` otherwise.
        """
        return calculate_page_size(page) < self.HALF_FULL_THRESHOLD * self.page_size_bytes

    def _find_leaf(self, key: bytes) -> LeafSearchResult:
        """
        Descend from the root to the leaf page that may contain `key`.

        Parameters
        ----------
        key
            The key driving the descent.

        Returns
        -------
        The reached leaf page, its ID, and the path taken to get there.
        """
        path: list[PathItem] = []
        page_id = self.root_page_id
        page = self.buffer_pool.get(page_id)

        # Descend the tree, following the internal separators, until we reach the leaf.
        while isinstance(page, InternalPage):
            # Find out which child link to follow.
            slot_idx = bisect.bisect_right(page.slots, key, key=_KEY_GETTER) - 1
            child_page_id = (
                page.leftmost_child_page_id
                if slot_idx == -1
                else page.slots[slot_idx].child_page_id
            )

            # Remember the path segment and descend.
            path.append(PathItem(parent_page_id=page_id, parent_slot_index=slot_idx))
            page_id = child_page_id
            page = self.buffer_pool.get(child_page_id)

        return LeafSearchResult(leaf_page_id=page_id, leaf_page=page, path=path)

    def _balance_and_write_tree(
        self,
        modified_page_id: int,
        modified_page: LeafPage | InternalPage,
        path: list[PathItem],
        lsn: int,
    ) -> None:
        """
        Walk back up the recorded path, splitting or merging pages as needed.

        Each affected page is marked dirty in the buffer pool; the actual writes happen later
        on eviction or flush. A split or merge can ripple into the parent, which is why this
        runs as a stack-based cascade rather than a single pass.

        Parameters
        ----------
        modified_page_id
            ID of the leaf (or internal) page that was just modified.
        modified_page
            The corresponding page object (already updated in memory).
        path
            The root-to-leaf path captured during the original descent.
        lsn
            LSN to stamp onto every page touched by the cascade.
        """
        page_stack = [(modified_page_id, modified_page)]
        path_stack = path[:]

        while page_stack:
            page_id, page = page_stack.pop()
            path_item = None if not path_stack else path_stack.pop()

            if self._is_page_overpopulated(page):
                modified_parent = self._split_page(
                    page_id=page_id,
                    parent_page_id=path_item.parent_page_id if path_item else None,
                    page=page,
                    lsn=lsn,
                )
                page_stack.append(modified_parent)
            elif self._is_page_underpopulated(page) and page_id != self.root_page_id:
                modified_parent = self._redistribute_or_merge_page(
                    page_id=page_id,
                    parent_page_id=path_item.parent_page_id if path_item else None,
                    parent_slot_idx=path_item.parent_slot_index if path_item else None,
                    page=page,
                    lsn=lsn,
                )
                if modified_parent:
                    page_stack.append(modified_parent)
            else:
                self.buffer_pool.mark_dirty(page_id, lsn)

    def _split_page(
        self,
        page_id: int,
        parent_page_id: int | None,
        page: LeafPage | InternalPage,
        lsn: int,
    ) -> tuple[int, InternalPage]:
        """
        Split an overpopulated page into two and propagate the new separator to the parent.

        Parameters
        ----------
        page_id
            ID of the page being split.
        parent_page_id
            ID of the parent, or `None` if `page` is the current root.
        page
            The page being split (mutated in place to become the left half).
        lsn
            LSN to stamp onto the affected pages.

        Returns
        -------
        The parent that needs to be revisited by the rebalancing cascade. When `page` was the
        root, a fresh page is allocated to become the new root and is returned here.
        """
        # Find a split point.
        slot_cumulative_sizes = []
        slot_size_sum = 0
        for slot in page.slots:
            slot_size_sum += calculate_slot_size(slot, include_meta=True)
            slot_cumulative_sizes.append(slot_size_sum)

        split_slot_idx = self._choose_split_point(page, slot_cumulative_sizes)

        match page:
            case LeafPage():
                # Allocate a sibling leaf and weave it into the sibling chain.
                new_sibling_page_id = self.pager.allocate_page()
                new_sibling_page = LeafPage(
                    last_modified_lsn=lsn,
                    right_sibling_page_id=page.right_sibling_page_id,
                    slots=page.slots[split_slot_idx:],
                )
                promoted_key = new_sibling_page.slots[0].key
                page.slots = page.slots[:split_slot_idx]
                page.right_sibling_page_id = new_sibling_page_id
                self.stats.leaf_splits += 1
            case InternalPage():
                # Allocate a sibling internal page, promote the median,
                # move everything after the median to the new page.
                new_sibling_page_id = self.pager.allocate_page()
                new_sibling_page = InternalPage(
                    last_modified_lsn=lsn,
                    leftmost_child_page_id=page.slots[split_slot_idx].child_page_id,
                    slots=page.slots[split_slot_idx + 1 :],
                )
                promoted_key = page.slots[split_slot_idx].key
                page.slots = page.slots[:split_slot_idx]
                self.stats.internal_splits += 1

            case _:
                raise ValueError(f"Unexpected page type: {type(page).__name__}")

        self.buffer_pool.mark_dirty(page_id, lsn)
        self.buffer_pool.insert(new_sibling_page_id, new_sibling_page, lsn)

        # Did we just split the root?
        if parent_page_id is None:
            new_root_page_id = self.pager.allocate_page()
            new_root_page = InternalPage(
                last_modified_lsn=lsn,
                leftmost_child_page_id=page_id,
                slots=[InternalSlot(key=promoted_key, child_page_id=new_sibling_page_id)],
            )
            self.buffer_pool.insert(new_root_page_id, new_root_page, lsn)
            self.pager.update_meta(root_page_id=new_root_page_id)
            return new_root_page_id, new_root_page
        else:
            parent_page = self.buffer_pool.get(parent_page_id)
            assert isinstance(parent_page, InternalPage)
            slot = InternalSlot(key=promoted_key, child_page_id=new_sibling_page_id)
            bisect.insort(parent_page.slots, slot, key=_KEY_GETTER)
            return parent_page_id, parent_page

    def _choose_split_point(
        self,
        page: LeafPage | InternalPage,
        slot_cumulative_sizes: list[int],
    ) -> int:
        """
        Find a split point for splitting the page into two.

        We prefer the byte midpoint so halves are close in size, but when a skewed slot-size
        distribution would leave one half below the underpopulated threshold, we scan outward
        for a split index that balances both halves.

        Parameters
        ----------
        page
            The page being split.
        slot_cumulative_sizes
            Running sum of slot sizes (including slot directory entries), indexed by slot.

        Returns
        -------
        The slot index at which to split. Falls back to the byte midpoint when no balanced
        index exists.
        """
        total_body = slot_cumulative_sizes[-1]
        byte_midpoint = bisect.bisect_right(slot_cumulative_sizes, total_body // 2)

        # Walk outward from the byte midpoint (+1, -1, +2, -2, ...) and take the first
        # candidate that balances; fall back to the midpoint if none do.
        slot_count = len(slot_cumulative_sizes)
        offsets = [0]
        for offset in range(1, slot_count):
            offsets.extend((offset, -offset))

        for offset in offsets:
            candidate = byte_midpoint + offset
            if 1 <= candidate < slot_count and self._split_halves_are_balanced(page, candidate):
                return candidate

        return byte_midpoint

    def _split_halves_are_balanced(
        self,
        page: LeafPage | InternalPage,
        split_slot_idx: int,
    ) -> bool:
        """
        Check whether a hypothetical split leaves both halves above the underpopulation threshold.

        Parameters
        ----------
        page
            The page that would be split.
        split_slot_idx
            The candidate split index.

        Returns
        -------
        `True` if both halves end up healthy, `False` otherwise.
        """
        match page:
            case LeafPage():
                left = LeafPage(
                    last_modified_lsn=0,
                    right_sibling_page_id=0,
                    slots=page.slots[:split_slot_idx],
                )
                right = LeafPage(
                    last_modified_lsn=0,
                    right_sibling_page_id=0,
                    slots=page.slots[split_slot_idx:],
                )
            case InternalPage():
                left = InternalPage(
                    last_modified_lsn=0,
                    leftmost_child_page_id=0,
                    slots=page.slots[:split_slot_idx],
                )
                right = InternalPage(
                    last_modified_lsn=0,
                    leftmost_child_page_id=0,
                    slots=page.slots[split_slot_idx + 1 :],
                )
            case _:
                raise ValueError(f"Unexpected page type: {type(page).__name__}")
        return not (self._is_page_underpopulated(left) or self._is_page_underpopulated(right))

    def _determine_same_parent_siblings(
        self,
        parent_page: InternalPage,
        parent_slot_idx: int,
    ) -> tuple[
        tuple[int, None | LeafPage | InternalPage],
        tuple[int, None | LeafPage | InternalPage],
    ]:
        """
        Look up the left and right siblings of a child page, restricted to the same parent.

        Parameters
        ----------
        parent_page
            The parent of the page whose siblings we want.
        parent_slot_idx
            Slot index that points at the page under `parent_page`. Use `-1` for the leftmost
            child.

        Returns
        -------
        A `((left_id, left_page), (right_id, right_page))` tuple. Either side's page is `None`
        when no sibling exists at that position under this parent.
        """
        # Determine if the page has a left sibling and/or a right sibling within the same parent.
        left_sibling_id = -1
        right_sibling_id = -1

        if parent_slot_idx == 0:
            left_sibling_id = parent_page.leftmost_child_page_id
        elif 0 < parent_slot_idx < len(parent_page.slots):
            left_sibling_id = parent_page.slots[parent_slot_idx - 1].child_page_id

        if parent_slot_idx == -1 and len(parent_page.slots) >= 1:
            right_sibling_id = parent_page.slots[0].child_page_id
        elif 0 <= parent_slot_idx < len(parent_page.slots) - 1:
            right_sibling_id = parent_page.slots[parent_slot_idx + 1].child_page_id

        left_sibling_page = None if left_sibling_id < 0 else self.buffer_pool.get(left_sibling_id)
        right_sibling_page = (
            None if right_sibling_id < 0 else self.buffer_pool.get(right_sibling_id)
        )

        # Find out if we can get away with just redistributing the slots among adjacent siblings.
        sibling_info = (
            (left_sibling_id, left_sibling_page),
            (right_sibling_id, right_sibling_page),
        )
        return sibling_info

    def _redistribute_or_merge_page(  # noqa: PLR0912, PLR0915
        self,
        page_id: int,
        parent_page_id: int | None,
        parent_slot_idx: int | None,
        page: LeafPage | InternalPage,
        lsn: int,
    ) -> tuple[int, InternalPage] | None:
        """
        Rebalance an underpopulated page by redistributing slots with a sibling, or merging.

        Parameters
        ----------
        page_id
            ID of the underpopulated page.
        parent_page_id
            ID of the parent, or `None` if `page` is the root (in which case nothing is done).
        parent_slot_idx
            Slot index that points at `page` under its parent. Use `-1` for the leftmost child.
        page
            The underpopulated page itself.
        lsn
            LSN to stamp onto every page touched by the rebalance.

        Returns
        -------
        The parent that still needs to be revisited by the cascade, or `None` if the rebalance
        was self-contained (e.g. it ended in a root collapse).
        """
        # Underpopulated root is allowed.
        if parent_page_id is None:
            return None

        parent_page = self.buffer_pool.get(parent_page_id)
        assert isinstance(parent_page, InternalPage)
        assert isinstance(parent_slot_idx, int)

        sibling_info = self._determine_same_parent_siblings(parent_page, parent_slot_idx)

        # Try redistributing slots from a sibling first.
        for pos_idx, (sibling_page_id, sibling_page) in enumerate(sibling_info):
            if sibling_page is None:
                continue

            donor_is_left = pos_idx == 0
            separator_idx = parent_slot_idx if donor_is_left else parent_slot_idx + 1
            if self._try_redistribute_slots(
                donor_page=sibling_page,
                recipient_page=page,
                parent_page=parent_page,
                parent_separator_idx=separator_idx,
                donor_is_left=donor_is_left,
            ):
                self.buffer_pool.mark_dirty(page_id, lsn)
                self.buffer_pool.mark_dirty(sibling_page_id, lsn)

                if isinstance(page, LeafPage):
                    self.stats.leaf_redistributes += 1
                else:
                    self.stats.internal_redistributes += 1

                # Redistribution overwrote a parent separator with a key of possibly different
                # length, which may have over/underpopulated the parent page.
                # Defer writing the parent when that happens and return it to the cascade,
                # which will split (or merge) it before writing.
                if self._is_page_overpopulated(parent_page) or self._is_page_underpopulated(
                    parent_page
                ):
                    return parent_page_id, parent_page

                self.buffer_pool.mark_dirty(parent_page_id, lsn)
                return None

        # Redistribution didn't work, fall through to merging. We always merge into the left page.
        for pos_idx, (sibling_page_id, sibling_page) in enumerate(sibling_info):
            if sibling_page is None:
                continue

            sibling_is_left = pos_idx == 0
            if sibling_is_left:
                merge_into_page_id, merge_from_page_id = sibling_page_id, page_id
                merge_into_page, merge_from_page = sibling_page, page
                separator_idx = parent_slot_idx
            else:
                merge_into_page_id, merge_from_page_id = page_id, sibling_page_id
                merge_into_page, merge_from_page = page, sibling_page
                separator_idx = parent_slot_idx + 1

            # Build a probe page first so we can bail if the merge would overflow,
            # without leaving the originals half-mutated.
            probe: LeafPage | InternalPage
            if isinstance(merge_into_page, LeafPage):
                assert isinstance(merge_from_page, LeafPage)
                probe = LeafPage(
                    last_modified_lsn=0,
                    right_sibling_page_id=merge_from_page.right_sibling_page_id,
                    slots=merge_into_page.slots + merge_from_page.slots,
                )
            else:
                assert isinstance(merge_into_page, InternalPage)
                assert isinstance(merge_from_page, InternalPage)
                pulled_down = InternalSlot(
                    key=parent_page.slots[separator_idx].key,
                    child_page_id=merge_from_page.leftmost_child_page_id,
                )
                probe = InternalPage(
                    last_modified_lsn=0,
                    leftmost_child_page_id=merge_into_page.leftmost_child_page_id,
                    slots=merge_into_page.slots + [pulled_down] + merge_from_page.slots,
                )

            if calculate_page_size(probe) > self.page_size_bytes:
                continue

            # Apply the merged content to the surviving page and drop the parent's separator.
            if isinstance(merge_into_page, LeafPage):
                assert isinstance(probe, LeafPage)
                merge_into_page.slots = probe.slots
                merge_into_page.right_sibling_page_id = probe.right_sibling_page_id
            else:
                assert isinstance(probe, InternalPage)
                merge_into_page.slots = probe.slots
            parent_page.slots.pop(separator_idx)

            self.buffer_pool.mark_dirty(merge_into_page_id, lsn)
            self.buffer_pool.mark_dirty(parent_page_id, lsn)
            self.buffer_pool.delete(merge_from_page_id)
            self.pager.free_page(merge_from_page_id)

            if isinstance(merge_into_page, LeafPage):
                self.stats.leaf_merges += 1
            else:
                self.stats.internal_merges += 1

            # If we've just merged the only remaining leaves in the tree, collapse the root.
            if not parent_page.slots and parent_page_id == self.root_page_id:
                old_root_id = self.root_page_id
                self.pager.update_meta(root_page_id=parent_page.leftmost_child_page_id)
                self.buffer_pool.delete(old_root_id)
                self.pager.free_page(old_root_id)
                self.stats.root_collapses += 1
                return None

            return parent_page_id, parent_page

        # Unreachable: the `page_size / 5` record cap bounds both pages below 40% and the
        # pulled-down separator below ~20%, so two underpopulated pages always fit together.
        msg = "Expected to either redistribute or merge the pages. This should not happen."
        raise AssertionError(msg)

    def _try_redistribute_slots(
        self,
        donor_page: LeafPage | InternalPage,
        recipient_page: LeafPage | InternalPage,
        parent_page: InternalPage,
        parent_separator_idx: int,
        donor_is_left: bool,
    ) -> bool:
        """
        Move slots from `donor_page` into `recipient_page` until the recipient is healthy.

        Parameters
        ----------
        donor_page
            The sibling page that gives up slots.
        recipient_page
            The underpopulated page being rescued.
        parent_page
            The shared parent of both siblings.
        parent_separator_idx
            Slot index inside `parent_page` whose separator key gets rewritten during transfer.
        donor_is_left
            True if `donor_page` sits to the left of `recipient_page` under the shared parent.

        Returns
        -------
        `True` on success. If the donor would itself fall below the threshold during the
        transfer, the operation is rolled back and `False` is returned.
        """
        parent_slot = parent_page.slots[parent_separator_idx]

        donor_backup = donor_page.copy()
        recipient_backup = recipient_page.copy()
        parent_backup = parent_page.copy()

        while self._is_page_underpopulated(recipient_page):
            match donor_page:
                case LeafPage():
                    assert isinstance(recipient_page, LeafPage)
                    if donor_is_left:
                        recipient_page.slots.insert(0, donor_page.slots.pop())
                        parent_slot.key = recipient_page.slots[0].key
                    else:
                        recipient_page.slots.append(donor_page.slots.pop(0))
                        parent_slot.key = donor_page.slots[0].key
                case InternalPage():
                    assert isinstance(recipient_page, InternalPage)
                    if donor_is_left:
                        recipient_page.slots.insert(
                            0,
                            InternalSlot(
                                key=parent_slot.key,
                                child_page_id=recipient_page.leftmost_child_page_id,
                            ),
                        )
                        recipient_page.leftmost_child_page_id = donor_page.slots[-1].child_page_id
                        parent_slot.key = donor_page.slots[-1].key
                        donor_page.slots.pop()
                    else:
                        recipient_page.slots.append(
                            InternalSlot(
                                key=parent_slot.key,
                                child_page_id=donor_page.leftmost_child_page_id,
                            )
                        )
                        parent_slot.key = donor_page.slots[0].key
                        donor_page.leftmost_child_page_id = donor_page.slots[0].child_page_id
                        donor_page.slots.pop(0)

            # If we've exhausted the donor, this is a failure. Restore the pages and exit.
            if self._is_page_underpopulated(donor_page):
                parent_page.slots = parent_backup.slots
                donor_page.slots = donor_backup.slots  # ty: ignore[invalid-assignment]
                recipient_page.slots = recipient_backup.slots  # ty: ignore[invalid-assignment]
                if isinstance(donor_page, InternalPage):
                    assert isinstance(donor_backup, InternalPage)
                    assert isinstance(recipient_page, InternalPage)
                    assert isinstance(recipient_backup, InternalPage)
                    donor_page.leftmost_child_page_id = donor_backup.leftmost_child_page_id
                    recipient_page.leftmost_child_page_id = recipient_backup.leftmost_child_page_id
                return False

        return True
