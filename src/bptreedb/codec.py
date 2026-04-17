from __future__ import annotations

import zlib
from enum import IntEnum
from struct import Struct
from typing import IO
from typing import Any

from bptreedb.entities import InternalPage
from bptreedb.entities import InternalSlot
from bptreedb.entities import LeafPage
from bptreedb.entities import LeafSlot
from bptreedb.entities import MetaPage
from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.entities import WALRecord
from bptreedb.exceptions import DBChecksumError
from bptreedb.exceptions import DBCorruptedError

_UINT32_FIELD = Struct("<I")
_UINT64_FIELD = Struct("<Q")
_LENGTH_FIELD = _UINT32_FIELD
_CRC32_FIELD = _UINT32_FIELD
_PAGE_ID_FIELD = _UINT64_FIELD

_WAL_RECORD_HEADER = Struct("<QB")
_META_PAGE_NO_CRC = Struct("<8sIIQQ")
_PAGE_HEADER = Struct("<B3sIIIQ")
_SLOT_ENTRY = Struct("<II")

DATA_FILE_MAGIC_PREFIX = b"BPTREEDB"
DATA_FILE_VERSION = 1
MIN_PAGE_SIZE = _META_PAGE_NO_CRC.size + _CRC32_FIELD.size


class BufferReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    @staticmethod
    def _as_struct(spec: str | Struct) -> Struct:
        if isinstance(spec, Struct):
            return spec
        return Struct(spec)

    def read_struct(self, spec: str | Struct) -> tuple:
        st = self._as_struct(spec)
        value = st.unpack_from(self.data, self.offset)
        self.offset += st.size
        return value

    def read_bytes(self, length: int) -> bytes:
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value

    def read_length_prefixed_bytes(self, length_spec: str | Struct = _LENGTH_FIELD) -> bytes:
        length = self.read_struct(length_spec)[0]
        return self.read_bytes(length)


class BufferWriter:
    def __init__(self) -> None:
        self._buffer = bytearray()

    @staticmethod
    def _as_struct(spec: str | Struct) -> Struct:
        if isinstance(spec, Struct):
            return spec
        return Struct(spec)

    def write_struct(self, spec: str | Struct, *values: Any) -> None:  # noqa: ANN401
        st = self._as_struct(spec)
        offset = len(self._buffer)
        self._buffer.extend(bytes(st.size))
        st.pack_into(self._buffer, offset, *values)

    def write_bytes(
        self,
        value: bytes | bytearray | memoryview | BufferWriter,
    ) -> None:
        if isinstance(value, BufferWriter):
            self._buffer += value._buffer
        else:
            self._buffer += value

    def write_length_prefixed_bytes(
        self,
        value: bytes | bytearray | memoryview | BufferWriter,
        length_spec: str | Struct = _LENGTH_FIELD,
    ) -> None:
        length = len(value._buffer) if isinstance(value, BufferWriter) else len(value)
        self.write_struct(length_spec, length)
        self.write_bytes(value)

    def write_crc32(self) -> None:
        self.write_struct(_CRC32_FIELD, self.crc32())

    def tell(self) -> int:
        return len(self._buffer)

    def build(self) -> bytes:
        return bytes(self._buffer)

    def crc32(self) -> int:
        return zlib.crc32(self._buffer)

    def __bytes__(self) -> bytes:
        return self.build()

    def __len__(self) -> int:
        return len(self._buffer)


class WALOperationType(IntEnum):
    PUT = 0x01
    DELETE = 0x02


class PageType(IntEnum):
    INTERNAL = 0x01
    LEAF = 0x02


def verify_crc32(data: bytes) -> None:
    actual_crc32 = zlib.crc32(data[: -_CRC32_FIELD.size])
    expected_crc32 = _CRC32_FIELD.unpack(data[-_CRC32_FIELD.size :])[0]
    if actual_crc32 != expected_crc32:
        raise DBChecksumError(expected_crc32, actual_crc32)


def encode_wal_record(record: WALRecord) -> bytes:
    header_writer = BufferWriter()
    payload_writer = BufferWriter()

    match record:
        case WALPutRecord():
            op_type = WALOperationType.PUT
            payload_writer.write_length_prefixed_bytes(record.key)
            payload_writer.write_length_prefixed_bytes(record.value)
        case WALDeleteRecord():
            op_type = WALOperationType.DELETE
            payload_writer.write_length_prefixed_bytes(record.key)
        case _:
            raise ValueError(f"Unknown operation type {type(record)}")

    header_writer.write_struct(_WAL_RECORD_HEADER, record.lsn, op_type)
    length_field_value = len(header_writer) + len(payload_writer) + _CRC32_FIELD.size

    record_writer = BufferWriter()
    record_writer.write_struct(_LENGTH_FIELD, length_field_value)
    record_writer.write_bytes(header_writer)
    record_writer.write_bytes(payload_writer)
    record_writer.write_crc32()

    return bytes(record_writer)


def decode_wal_record(data: bytes) -> WALRecord:
    verify_crc32(data)
    reader = BufferReader(data)
    reader.read_bytes(_LENGTH_FIELD.size)
    lsn, op_type = reader.read_struct(_WAL_RECORD_HEADER)

    match op_type:
        case WALOperationType.PUT:
            key = reader.read_length_prefixed_bytes()
            value = reader.read_length_prefixed_bytes()
            return WALPutRecord(lsn=lsn, key=key, value=value)
        case WALOperationType.DELETE:
            key = reader.read_length_prefixed_bytes()
            return WALDeleteRecord(lsn=lsn, key=key)
        case _:
            raise ValueError(f"Unknown operation type {op_type}")


def decode_next_wal_record_from_file(file: IO[bytes]) -> WALRecord:
    length_bytes = file.read(_LENGTH_FIELD.size)
    if len(length_bytes) != _LENGTH_FIELD.size:
        raise EOFError()

    # We allow the records to be arbitrarily large (4 bytes), and we trust the length field.
    # In theory, bit rot can land precisely on the length field and corrupt it,
    # but this is genuinely rare.
    record_length = _LENGTH_FIELD.unpack(length_bytes)[0]
    record_body = file.read(record_length)
    if len(record_body) != record_length:
        raise EOFError()

    return decode_wal_record(length_bytes + record_body)


def encode_meta_page(page: MetaPage) -> bytes:
    writer = BufferWriter()
    writer.write_struct(
        _META_PAGE_NO_CRC,
        DATA_FILE_MAGIC_PREFIX,
        DATA_FILE_VERSION,
        page.page_size_bytes,
        page.root_page_id,
        page.next_page_id,
    )
    writer.write_crc32()
    zero_padding = bytes(page.page_size_bytes - len(writer))
    return bytes(writer) + zero_padding


def decode_meta_page(data: bytes) -> MetaPage:
    data = data[: _META_PAGE_NO_CRC.size + _CRC32_FIELD.size]

    if data[: len(DATA_FILE_MAGIC_PREFIX)] != DATA_FILE_MAGIC_PREFIX:
        raise DBCorruptedError("Magic prefix not found")
    verify_crc32(data)

    reader = BufferReader(data)
    unpacked = reader.read_struct(_META_PAGE_NO_CRC)
    return MetaPage(
        page_size_bytes=unpacked[2],
        root_page_id=unpacked[3],
        next_page_id=unpacked[4],
    )


def encode_page(page: InternalPage | LeafPage, page_size_bytes: int) -> bytes:
    page_buffer = bytearray(page_size_bytes)
    record_end_ptr = len(page_buffer)
    slot_writer = BufferWriter()

    for slot in page.slots:
        record_writer = BufferWriter()
        match slot:
            case InternalSlot():
                record_writer.write_length_prefixed_bytes(slot.key)
                record_writer.write_struct(_PAGE_ID_FIELD, slot.child_page_id)
            case LeafSlot():
                record_writer.write_length_prefixed_bytes(slot.key)
                record_writer.write_length_prefixed_bytes(slot.value)
            case _:
                raise ValueError(f"Unknown slot type {slot}")

        record = bytes(record_writer)
        record_end_ptr -= len(record)
        page_buffer[record_end_ptr : record_end_ptr + len(record)] = record
        slot_writer.write_struct(_SLOT_ENTRY, record_end_ptr, len(record))

    match page:
        case InternalPage():
            page_type = PageType.INTERNAL
            page_id_field_value = page.leftmost_child_page_id
        case LeafPage():
            page_type = PageType.LEAF
            page_id_field_value = page.right_sibling_page_id
        case _:
            raise ValueError(f"Unknown page type {page}")

    free_space_start = _PAGE_HEADER.size + _SLOT_ENTRY.size * len(page.slots)
    free_space_end = record_end_ptr

    header_writer = BufferWriter()
    header_writer.write_struct(
        _PAGE_HEADER,
        page_type,
        bytes(3),
        len(page.slots),
        free_space_start,
        free_space_end,
        page_id_field_value,
    )

    page_buffer[0 : len(header_writer)] = bytes(header_writer)
    page_buffer[len(header_writer) : len(header_writer) + len(slot_writer)] = bytes(slot_writer)
    return bytes(page_buffer)


def decode_page(data: bytes) -> InternalPage | LeafPage:
    reader = BufferReader(data)
    page_type, _, slot_count, free_space_start, free_space_end, page_id_field_value = (
        reader.read_struct(_PAGE_HEADER)
    )
    match page_type:
        case PageType.INTERNAL:
            slots = []
            for _ in range(slot_count):
                record_offset, record_length = reader.read_struct(_SLOT_ENTRY)
                record = data[record_offset : record_offset + record_length]
                record_reader = BufferReader(record)
                key = record_reader.read_length_prefixed_bytes()
                child_page_id = record_reader.read_struct(_PAGE_ID_FIELD)[0]
                slots.append(InternalSlot(key=key, child_page_id=child_page_id))
            return InternalPage(
                leftmost_child_page_id=page_id_field_value,
                slots=slots,
            )
        case PageType.LEAF:
            slots = []
            for _ in range(slot_count):
                record_offset, record_length = reader.read_struct(_SLOT_ENTRY)
                record = data[record_offset : record_offset + record_length]
                record_reader = BufferReader(record)
                key = record_reader.read_length_prefixed_bytes()
                value = record_reader.read_length_prefixed_bytes()
                slots.append(LeafSlot(key=key, value=value))
            return LeafPage(
                right_sibling_page_id=page_id_field_value,
                slots=slots,
            )
        case _:
            raise ValueError(f"Unknown page type {page_type}")


def calculate_slot_size(slot: LeafSlot | InternalSlot, include_meta: bool = False) -> int:
    base_size = _SLOT_ENTRY.size if include_meta else 0
    match slot:
        case InternalSlot():
            return base_size + _LENGTH_FIELD.size + len(slot.key) + _PAGE_ID_FIELD.size
        case LeafSlot():
            return base_size + _LENGTH_FIELD.size * 2 + len(slot.key) + len(slot.value)
        case _:
            raise ValueError(f"Unknown slot type: {type(slot)}")


def calculate_page_size(page: InternalPage | LeafPage) -> int:
    encoded_page_size = _PAGE_HEADER.size
    for slot in page.slots:
        encoded_page_size += calculate_slot_size(slot, include_meta=True)
    return encoded_page_size


def calculate_leaf_record_size(key: bytes, value: bytes) -> int:
    return _LENGTH_FIELD.size * 2 + len(key) + len(value)


def get_max_leaf_record_size(page_size_bytes: int) -> int:
    # The 20% cap (not 25%) ensures that no single slot is large enough to force a split into
    # underpopulated half-pages which are impossible to balance without introducing new techniques.
    return (page_size_bytes - _PAGE_HEADER.size) // 5 - _SLOT_ENTRY.size
