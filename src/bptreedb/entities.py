"""Domain entities shared across the storage engine: WAL records and on-disk pages."""

from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass
class WALRecord:
    """Base class for all entries appended to the write-ahead log."""

    lsn: int


@dataclass
class WALPutRecord(WALRecord):
    """A WAL entry for a key/value insertion or overwrite."""

    key: bytes
    value: bytes


@dataclass
class WALDeleteRecord(WALRecord):
    """A WAL entry for a key deletion."""

    key: bytes


@dataclass
class WALCheckpointRecord(WALRecord):
    """A WAL entry marking a successful checkpoint and capturing the meta state at that moment."""

    root_page_id: int
    freelist_head: int
    next_page_id: int


@dataclass
class MetaPage:
    """The first page in the data file; holds the global state of the database."""

    page_size_bytes: int
    root_page_id: int
    next_page_id: int
    freelist_head_page_id: int
    last_checkpoint_lsn: int

    def copy(self) -> MetaPage:
        """Return a deep copy of this meta page."""
        return copy.deepcopy(self)


@dataclass
class InternalSlot:
    """A single key/child-pointer entry inside an internal B+ tree page."""

    key: bytes
    child_page_id: int


@dataclass
class InternalPage:
    """An internal (non-leaf) node of the B+ tree: routes searches via separator keys."""

    last_modified_lsn: int
    leftmost_child_page_id: int
    slots: list[InternalSlot]

    def copy(self) -> InternalPage:
        """Return a deep copy of this internal page."""
        return copy.deepcopy(self)


@dataclass
class LeafSlot:
    """A single key/value entry inside a leaf B+ tree page."""

    key: bytes
    value: bytes


@dataclass
class LeafPage:
    """A leaf node of the B+ tree: holds the actual key/value pairs and a right-sibling link."""

    last_modified_lsn: int
    right_sibling_page_id: int
    slots: list[LeafSlot]

    def copy(self) -> LeafPage:
        """Return a deep copy of this leaf page."""
        return copy.deepcopy(self)


@dataclass
class FreelistPage:
    """A page that tracks IDs of freed pages available for reuse during allocation."""

    last_modified_lsn: int
    next_freelist_page_id: int
    freed_page_ids: list[int]
