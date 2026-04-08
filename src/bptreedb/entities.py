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
