import bisect
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from operator import attrgetter

from bptreedb.codec import calculate_leaf_record_size
from bptreedb.codec import calculate_page_size
from bptreedb.codec import calculate_slot_size
from bptreedb.codec import decode_page
from bptreedb.codec import encode_page
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
    parent_page_id: int
    parent_slot_index: int


@dataclass
class LeafSearchResult:
    leaf_page_id: int
    leaf_page: LeafPage
    path: list[PathItem]


class BPlusTree:
    HALF_FULL_THRESHOLD = 0.4

    def __init__(self, pager: Pager) -> None:
        self.pager = pager

    @property
    def page_size_bytes(self) -> int:
        return self.pager.get_meta().page_size_bytes

    @property
    def root_page_id(self) -> int:
        return self.pager.get_meta().root_page_id

    def search(self, key: bytes) -> bytes | None:
        # Find the leaf node that may contain the key.
        leaf_page = self._find_leaf(key).leaf_page

        # Find the key slot.
        slot_idx = bisect.bisect_left(leaf_page.slots, key, key=_KEY_GETTER)
        if not 0 <= slot_idx < len(leaf_page.slots) or leaf_page.slots[slot_idx].key != key:
            return None
        return leaf_page.slots[slot_idx].value

    def insert(self, key: bytes, value: bytes) -> None:
        self._raise_if_record_too_large(key, value)

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
        self._balance_and_write_tree(leaf_page_id, leaf_page, leaf_result.path)

    def delete(self, key: bytes) -> bool:
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
        self._balance_and_write_tree(leaf_page_id, leaf_page, leaf_result.path)

        return True

    def scan(
        self,
        start_key_inclusive: bytes | None,
        end_key_exclusive: bytes | None,
        version_checker: Callable[[], None] | None = None,
    ) -> Iterator[tuple[bytes, bytes]]:
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
                if version_checker is not None:
                    version_checker()
                yield slot.key, slot.value

            # Keep walking the leaf chain via right sibling pointers.
            if leaf_page.right_sibling_page_id > 0:
                leaf_page = self._read_page(leaf_page.right_sibling_page_id)
                start_slot_idx = 0
                assert isinstance(leaf_page, LeafPage)
            else:
                return

    def _read_page(self, page_id: int) -> LeafPage | InternalPage:
        return decode_page(self.pager.read_page(page_id))

    def _write_page(self, page_id: int, page: LeafPage | InternalPage) -> None:
        self.pager.write_page(page_id, encode_page(page, self.pager.page_size_bytes))

    def _raise_if_record_too_large(self, key: bytes, value: bytes) -> None:
        max_leaf_record_size = get_max_leaf_record_size(self.page_size_bytes)
        current_leaf_record_size = calculate_leaf_record_size(key, value)
        if current_leaf_record_size > max_leaf_record_size:
            raise DBRecordTooLargeError(limit=max_leaf_record_size, actual=current_leaf_record_size)

    def _is_page_overpopulated(self, page: LeafPage | InternalPage) -> bool:
        return calculate_page_size(page) > self.page_size_bytes

    def _is_page_underpopulated(self, page: LeafPage | InternalPage) -> bool:
        return calculate_page_size(page) < self.HALF_FULL_THRESHOLD * self.page_size_bytes

    def _find_leaf(self, key: bytes) -> LeafSearchResult:
        path: list[PathItem] = []
        page_id = self.root_page_id
        page = self._read_page(page_id)

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
            page = self._read_page(child_page_id)

        return LeafSearchResult(leaf_page_id=page_id, leaf_page=page, path=path)

    def _balance_and_write_tree(
        self,
        modified_page_id: int,
        modified_page: LeafPage | InternalPage,
        path: list[PathItem],
    ) -> None:
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
                )
                page_stack.append(modified_parent)
            elif self._is_page_underpopulated(page) and page_id != self.root_page_id:
                modified_parent = self._redistribute_or_merge_page(
                    page_id=page_id,
                    parent_page_id=path_item.parent_page_id if path_item else None,
                    parent_slot_idx=path_item.parent_slot_index if path_item else None,
                    page=page,
                )
                if modified_parent:
                    page_stack.append(modified_parent)
            else:
                self._write_page(page_id, page)

    def _split_page(
        self,
        page_id: int,
        parent_page_id: int | None,
        page: LeafPage | InternalPage,
    ) -> tuple[int, InternalPage]:
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
                    right_sibling_page_id=page.right_sibling_page_id,
                    slots=page.slots[split_slot_idx:],
                )
                promoted_key = new_sibling_page.slots[0].key
                page.slots = page.slots[:split_slot_idx]
                page.right_sibling_page_id = new_sibling_page_id
            case InternalPage():
                # Allocate a sibling internal page, promote the median,
                # move everything after the median to the new page.
                new_sibling_page_id = self.pager.allocate_page()
                new_sibling_page = InternalPage(
                    leftmost_child_page_id=page.slots[split_slot_idx].child_page_id,
                    slots=page.slots[split_slot_idx + 1 :],
                )
                promoted_key = page.slots[split_slot_idx].key
                page.slots = page.slots[:split_slot_idx]

            case _:
                raise ValueError(f"Unexpected page type: {type(page).__name__}")

        self._write_page(page_id, page)
        self._write_page(new_sibling_page_id, new_sibling_page)

        # Did we just split the root?
        if parent_page_id is None:
            new_root_page_id = self.pager.allocate_page()
            new_root_page = InternalPage(
                leftmost_child_page_id=page_id,
                slots=[InternalSlot(key=promoted_key, child_page_id=new_sibling_page_id)],
            )
            self._write_page(new_root_page_id, new_root_page)
            self.pager.update_meta(root_page_id=new_root_page_id)
            return new_root_page_id, new_root_page
        else:
            parent_page = self._read_page(parent_page_id)
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

        Falls back to the byte midpoint when no balanced index exists.
        """
        total_body = slot_cumulative_sizes[-1]
        byte_midpoint = bisect.bisect_right(slot_cumulative_sizes, total_body // 2)

        # Try the byte midpoint first, then scan outward.
        # The first balanced index wins. If none balance, fall back to the byte midpoint.
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
        match page:
            case LeafPage():
                left = LeafPage(right_sibling_page_id=0, slots=page.slots[:split_slot_idx])
                right = LeafPage(right_sibling_page_id=0, slots=page.slots[split_slot_idx:])
            case InternalPage():
                left = InternalPage(leftmost_child_page_id=0, slots=page.slots[:split_slot_idx])
                right = InternalPage(
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

        left_sibling_page = None if left_sibling_id < 0 else self._read_page(left_sibling_id)
        right_sibling_page = None if right_sibling_id < 0 else self._read_page(right_sibling_id)

        # Find out if we can get away with just redistributing the slots among adjacent siblings.
        sibling_info = (
            (left_sibling_id, left_sibling_page),
            (right_sibling_id, right_sibling_page),
        )
        return sibling_info

    def _redistribute_or_merge_page(  # noqa: PLR0912
        self,
        page_id: int,
        parent_page_id: int | None,
        parent_slot_idx: int | None,
        page: LeafPage | InternalPage,
    ) -> tuple[int, InternalPage] | None:
        # Underpopulated root is allowed.
        if parent_page_id is None:
            return None

        parent_page = self._read_page(parent_page_id)
        assert isinstance(parent_page, InternalPage)
        assert isinstance(parent_slot_idx, int)

        sibling_info = self._determine_same_parent_siblings(parent_page, parent_slot_idx)
        for sibling_page_id, sibling_page in sibling_info:
            if not sibling_page:
                continue

            is_left_sibling = sibling_page.slots[-1].key < page.slots[0].key
            if self._try_redistribute_slots(
                donor_page=sibling_page,
                recipient_page=page,
                parent_page=parent_page,
                parent_slot_idx=parent_slot_idx if is_left_sibling else parent_slot_idx + 1,
            ):
                self._write_page(page_id, page)
                self._write_page(sibling_page_id, sibling_page)

                # Redistribution overwrote a parent separator with a key of possibly different
                # length, which may have over/underpopulated the parent page.
                # Defer writing the parent when that happens and return it to the cascade,
                # which will split (or merge) it before writing.
                if self._is_page_overpopulated(parent_page) or self._is_page_underpopulated(
                    parent_page
                ):
                    return parent_page_id, parent_page

                self._write_page(parent_page_id, parent_page)
                return None

        # If we cannot just redistribute sibling slots, we have to merge sibling pages.
        for sibling_page_id, sibling_page in sibling_info:
            if not sibling_page:
                continue

            # We always merge the right page into the left page, then keep the left page.
            is_left_sibling = sibling_page.slots[-1].key < page.slots[0].key
            if is_left_sibling:
                merge_left_page_id = sibling_page_id
                merge_left_page = sibling_page.copy()
                merge_right_page = page.copy()
            else:
                merge_left_page_id = page_id
                merge_left_page = page.copy()
                merge_right_page = sibling_page.copy()

            match merge_left_page:
                case LeafPage():
                    assert isinstance(merge_right_page, LeafPage)
                    merge_left_page.slots += merge_right_page.slots
                    merge_left_page.right_sibling_page_id = merge_right_page.right_sibling_page_id
                case InternalPage():
                    # Pull down the separator from the parent.
                    assert isinstance(merge_right_page, InternalPage)
                    parent_slot = (
                        parent_page.slots[parent_slot_idx]
                        if is_left_sibling
                        else parent_page.slots[parent_slot_idx + 1]
                    )
                    merge_left_page.slots.append(
                        InternalSlot(
                            key=parent_slot.key,
                            child_page_id=merge_right_page.leftmost_child_page_id,
                        )
                    )
                    merge_left_page.slots += merge_right_page.slots
                case _:
                    raise ValueError(f"Unexpected page type: {type(merge_left_page).__name__}")

            # Can we merge with this sibling without overflowing the page?
            if calculate_page_size(merge_left_page) > self.page_size_bytes:
                continue

            # Remove the separator that used to point at the merged-away page.
            parent_page.slots.pop(parent_slot_idx if is_left_sibling else parent_slot_idx + 1)

            self._write_page(merge_left_page_id, merge_left_page)
            self._write_page(parent_page_id, parent_page)

            # If we've just merged the only remaining leaves in the tree, collapse the root.
            if not parent_page.slots and parent_page_id == self.root_page_id:
                self.pager.update_meta(root_page_id=parent_page.leftmost_child_page_id)
                return None

            return parent_page_id, parent_page

        msg = "Expected to either redistribute or merge the pages. This should not happen."
        raise AssertionError(msg)

    def _try_redistribute_slots(
        self,
        donor_page: LeafPage | InternalPage,
        recipient_page: LeafPage | InternalPage,
        parent_page: InternalPage,
        parent_slot_idx: int,
    ) -> bool:
        donor_is_left = donor_page.slots[-1].key < recipient_page.slots[0].key
        parent_slot = parent_page.slots[parent_slot_idx]

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
