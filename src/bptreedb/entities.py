from dataclasses import dataclass

DATA_FILE_MAGIC_PREFIX = b"BPTREEDB"
DATA_FILE_VERSION = 1


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
    # TODO: do these two fields belong here?
    magic: bytes
    version: int
    page_size_bytes: int
    root_page_id: int
    next_page_id: int


@dataclass
class InternalSlot:
    key: bytes
    child_page_id: int


@dataclass
class InternalPage:
    leftmost_child_page_id: int
    slots: list[InternalSlot]


@dataclass
class LeafSlot:
    key: bytes
    value: bytes


@dataclass
class LeafPage:
    right_sibling_page_id: int
    slots: list[LeafSlot]
