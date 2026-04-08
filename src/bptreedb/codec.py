from __future__ import annotations

import zlib
from enum import IntEnum
from struct import Struct
from typing import IO
from typing import Any

from bptreedb.entities import WALDeleteRecord
from bptreedb.entities import WALPutRecord
from bptreedb.entities import WALRecord
from bptreedb.exceptions import DBChecksumError

_LENGTH_FIELD = Struct("<I")
_CRC32_FIELD = Struct("<I")
_WAL_RECORD_HEADER = Struct("<QB")


class WALOperationType(IntEnum):
    PUT = 0x01
    DELETE = 0x02


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
        s = self._as_struct(spec)
        value = s.unpack_from(self.data, self.offset)
        self.offset += s.size
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
        s = self._as_struct(spec)
        offset = len(self._buffer)
        self._buffer.extend(b"\x00" * s.size)
        s.pack_into(self._buffer, offset, *values)

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
            raise ValueError(f"Unknown operation type {type(record)}")  # noqa: TRY003

    header_writer.write_struct(_WAL_RECORD_HEADER, record.lsn, op_type)
    length_field_value = len(header_writer) + len(payload_writer) + _CRC32_FIELD.size

    result_writer = BufferWriter()
    result_writer.write_struct(_LENGTH_FIELD, length_field_value)
    result_writer.write_bytes(header_writer)
    result_writer.write_bytes(payload_writer)
    result_writer.write_struct(_CRC32_FIELD, result_writer.crc32())

    return bytes(result_writer)


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
            raise ValueError(f"Unknown operation type {op_type}")  # noqa: TRY003


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
