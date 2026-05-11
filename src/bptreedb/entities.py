from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass
class WALRecord:
    lsn: int


@dataclass
class WALPutRecord(WALRecord):
    key: bytes
    value: bytes


@dataclass
class WALDeleteRecord(WALRecord):
    key: bytes


@dataclass
class MetaPage:
    page_size_bytes: int
    root_page_id: int
    next_page_id: int

    def copy(self) -> MetaPage:
        return copy.deepcopy(self)


@dataclass
class InternalSlot:
    key: bytes
    child_page_id: int


@dataclass
class InternalPage:
    last_modified_lsn: int
    leftmost_child_page_id: int
    slots: list[InternalSlot]

    def copy(self) -> InternalPage:
        return copy.deepcopy(self)


@dataclass
class LeafSlot:
    key: bytes
    value: bytes


@dataclass
class LeafPage:
    last_modified_lsn: int
    right_sibling_page_id: int
    slots: list[LeafSlot]

    def copy(self) -> LeafPage:
        return copy.deepcopy(self)
