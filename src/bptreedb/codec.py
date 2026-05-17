"""Wire-format encoders and decoders for pages and WAL records."""

from __future__ import annotations

import zlib
from enum import IntEnum
from struct import Struct
from typing import IO
from typing import Any

from bptreedb.entities import FreelistPage
from bptreedb.entities import InternalPage
from bptreedb.entities import InternalSlot
from bptreedb.entities import LeafPage
from bptreedb.entities import LeafSlot
from bptreedb.entities import MetaPage
from bptreedb.entities import WALCheckpointRecord
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
_META_PAGE_NO_CRC = Struct("<8sIIQQQQ")
_PAGE_HEADER = Struct("<B3sIIIQQ")
_SLOT_ENTRY = Struct("<II")

DATA_FILE_MAGIC_PREFIX = b"BPTREEDB"
DATA_FILE_VERSION = 1
MIN_PAGE_SIZE = _META_PAGE_NO_CRC.size + _CRC32_FIELD.size


class BufferReader:
    """Cursor-based reader for sequentially decoding fields out of a byte buffer."""

    def __init__(self, data: bytes) -> None:
        """
        Wrap an existing byte buffer and start reading from offset zero.

        Parameters
        ----------
        data
            The bytes to read from.
        """
        self.data = data
        self.offset = 0

    @staticmethod
    def _as_struct(spec: str | Struct) -> Struct:
        if isinstance(spec, Struct):
            return spec
        return Struct(spec)

    def read_struct(self, spec: str | Struct) -> tuple:
        """
        Unpack a `struct` spec from the current offset and advance past it.

        Parameters
        ----------
        spec
            A `struct.Struct` instance or a format string.

        Returns
        -------
        The tuple of values produced by `struct.unpack_from`.
        """
        st = self._as_struct(spec)
        value = st.unpack_from(self.data, self.offset)
        self.offset += st.size
        return value

    def read_bytes(self, length: int) -> bytes:
        """
        Read a fixed number of raw bytes from the current offset and advance past them.

        Parameters
        ----------
        length
            Number of bytes to read.

        Returns
        -------
        The bytes that were read.
        """
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value

    def read_length_prefixed_bytes(self, length_spec: str | Struct = _LENGTH_FIELD) -> bytes:
        """
        Read a length prefix followed by that many bytes of payload.

        Parameters
        ----------
        length_spec
            Struct describing the length prefix; defaults to the standard 32-bit length field.

        Returns
        -------
        The payload bytes (the length prefix itself is not included).
        """
        length = self.read_struct(length_spec)[0]
        return self.read_bytes(length)


class BufferWriter:
    """Append-only builder for encoding fields into a byte buffer."""

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buffer = bytearray()

    @staticmethod
    def _as_struct(spec: str | Struct) -> Struct:
        if isinstance(spec, Struct):
            return spec
        return Struct(spec)

    def write_struct(self, spec: str | Struct, *values: Any) -> None:  # noqa: ANN401
        """
        Pack a `struct` spec with the given values and append it to the buffer.

        Parameters
        ----------
        spec
            A `struct.Struct` instance or a format string.
        values
            Values to pack, in the order expected by `spec`.
        """
        st = self._as_struct(spec)
        offset = len(self._buffer)
        self._buffer.extend(bytes(st.size))
        st.pack_into(self._buffer, offset, *values)

    def write_bytes(
        self,
        value: bytes | bytearray | memoryview | BufferWriter,
    ) -> None:
        """
        Append a raw byte sequence (or the contents of another `BufferWriter`) to the buffer.

        Parameters
        ----------
        value
            The bytes to append. A nested `BufferWriter` is unwrapped and its bytes are copied in.
        """
        if isinstance(value, BufferWriter):
            self._buffer += value._buffer
        else:
            self._buffer += value

    def write_length_prefixed_bytes(
        self,
        value: bytes | bytearray | memoryview | BufferWriter,
        length_spec: str | Struct = _LENGTH_FIELD,
    ) -> None:
        """
        Write a length prefix followed by the given payload.

        Parameters
        ----------
        value
            The payload to write.
        length_spec
            Struct describing the length prefix; defaults to the standard 32-bit length field.
        """
        length = len(value._buffer) if isinstance(value, BufferWriter) else len(value)
        self.write_struct(length_spec, length)
        self.write_bytes(value)

    def write_crc32(self) -> None:
        """Append a CRC32 covering everything previously written into the buffer."""
        self.write_struct(_CRC32_FIELD, self.crc32())

    def tell(self) -> int:
        """
        Return the current buffer position.

        Returns
        -------
        The number of bytes currently in the buffer.
        """
        return len(self._buffer)

    def build(self) -> bytes:
        """
        Materialize the buffer.

        Returns
        -------
        The buffer's accumulated contents as an immutable `bytes` object.
        """
        return bytes(self._buffer)

    def crc32(self) -> int:
        """
        Compute the CRC32 of the buffer's current contents.

        Returns
        -------
        The CRC32 value.
        """
        return zlib.crc32(self._buffer)

    def __bytes__(self) -> bytes:
        """Allow `bytes(writer)` to materialize the buffer."""
        return self.build()

    def __len__(self) -> int:
        """Return the number of bytes currently in the buffer."""
        return len(self._buffer)


class WALOperationType(IntEnum):
    """Tag byte identifying the kind of operation recorded in a WAL entry."""

    PUT = 0x01
    DELETE = 0x02
    CHECKPOINT = 0x03


class PageType(IntEnum):
    """Tag byte identifying the kind of page stored at a given page slot."""

    INTERNAL = 0x01
    LEAF = 0x02
    FREELIST = 0x03


def verify_crc32(data: bytes) -> None:
    """
    Verify that the trailing CRC32 field of `data` matches the CRC of the preceding bytes.

    Parameters
    ----------
    data
        A byte buffer whose last four bytes are the expected CRC32.

    Raises
    ------
    DBChecksumError
        If the CRC computed from the payload does not match the trailing field.
    """
    actual_crc32 = zlib.crc32(data[: -_CRC32_FIELD.size])
    expected_crc32 = _CRC32_FIELD.unpack(data[-_CRC32_FIELD.size :])[0]
    if actual_crc32 != expected_crc32:
        raise DBChecksumError(expected_crc32, actual_crc32)


def encode_wal_record(record: WALRecord) -> bytes:
    """
    Encode a WAL record into its on-disk wire format (length prefix + body + CRC).

    Parameters
    ----------
    record
        The record to encode.

    Returns
    -------
    The encoded bytes.
    """
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
        case WALCheckpointRecord():
            op_type = WALOperationType.CHECKPOINT
            payload_writer.write_struct(_UINT64_FIELD, record.root_page_id)
            payload_writer.write_struct(_UINT64_FIELD, record.freelist_head)
            payload_writer.write_struct(_UINT64_FIELD, record.next_page_id)
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
    """
    Decode a single WAL record from the given byte buffer.

    Parameters
    ----------
    data
        The bytes that make up the full record (including length prefix and CRC).

    Returns
    -------
    The decoded WAL record.

    Raises
    ------
    DBChecksumError
        If the CRC at the end of `data` does not match the payload.
    """
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
        case WALOperationType.CHECKPOINT:
            root_page_id = reader.read_struct(_UINT64_FIELD)[0]
            freelist_head = reader.read_struct(_UINT64_FIELD)[0]
            next_page_id = reader.read_struct(_UINT64_FIELD)[0]
            return WALCheckpointRecord(
                lsn=lsn,
                root_page_id=root_page_id,
                freelist_head=freelist_head,
                next_page_id=next_page_id,
            )
        case _:
            raise ValueError(f"Unknown operation type {op_type}")


def decode_next_wal_record_from_file(file: IO[bytes]) -> WALRecord:
    """
    Read and decode the next WAL record from an open file at its current position.

    Parameters
    ----------
    file
        A binary file object positioned at the start of a WAL record.

    Returns
    -------
    The decoded WAL record.

    Raises
    ------
    EOFError
        If the file ends before a full record can be read.
    """
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
    """
    Encode the meta page to its on-disk form, padded out to the configured page size.

    Parameters
    ----------
    page
        The meta page to encode.

    Returns
    -------
    The full-sized page buffer ready to be written to disk.
    """
    writer = BufferWriter()
    writer.write_struct(
        _META_PAGE_NO_CRC,
        DATA_FILE_MAGIC_PREFIX,
        DATA_FILE_VERSION,
        page.page_size_bytes,
        page.root_page_id,
        page.next_page_id,
        page.freelist_head_page_id,
        page.last_checkpoint_lsn,
    )
    writer.write_crc32()
    zero_padding = bytes(page.page_size_bytes - len(writer))
    return bytes(writer) + zero_padding


def decode_meta_page(data: bytes) -> MetaPage:
    """
    Decode the meta page from its on-disk form.

    Parameters
    ----------
    data
        The raw page buffer read from disk.

    Returns
    -------
    The decoded meta page.

    Raises
    ------
    DBCorruptedError
        If the magic prefix is missing.
    DBChecksumError
        If the CRC32 field does not match the encoded payload.
    """
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
        freelist_head_page_id=unpacked[5],
        last_checkpoint_lsn=unpacked[6],
    )


def encode_page(page: InternalPage | LeafPage | FreelistPage, page_size_bytes: int) -> bytes:
    """
    Encode a page into its fixed-size on-disk representation.

    Internal and leaf pages use a slotted layout: the header and slot directory grow forward from
    the start of the page, while individual records are packed backward from the end. Freelist
    pages just store a flat array of freed page IDs.

    Parameters
    ----------
    page
        The page to encode.
    page_size_bytes
        Size of the output buffer, in bytes; any unused space is zero-padded.

    Returns
    -------
    The full-sized page buffer ready to be written to disk.
    """
    page_buffer = bytearray(page_size_bytes)
    record_end_ptr = len(page_buffer)

    match page:
        case InternalPage():
            page_type = PageType.INTERNAL
            page_id_field_value = page.leftmost_child_page_id
            count_value = len(page.slots)
        case LeafPage():
            page_type = PageType.LEAF
            page_id_field_value = page.right_sibling_page_id
            count_value = len(page.slots)
        case FreelistPage():
            page_type = PageType.FREELIST
            page_id_field_value = page.next_freelist_page_id
            count_value = len(page.freed_page_ids)
        case _:
            raise ValueError(f"Unknown page type {page}")

    match page:
        case InternalPage() | LeafPage():
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

            encoded_records = bytes(slot_writer)
            free_space_start = _PAGE_HEADER.size + _SLOT_ENTRY.size * len(page.slots)
            free_space_end = record_end_ptr
        case FreelistPage():
            freed_id_list_writer = BufferWriter()
            for page_id in page.freed_page_ids:
                freed_id_list_writer.write_struct(_PAGE_ID_FIELD, page_id)

            encoded_records = bytes(freed_id_list_writer)
            free_space_start = free_space_end = 0
        case _:
            raise ValueError(f"Unknown page type {page}")

    header_writer = BufferWriter()
    header_writer.write_struct(
        _PAGE_HEADER,
        page_type,
        bytes(3),
        count_value,
        free_space_start,
        free_space_end,
        page.last_modified_lsn,
        page_id_field_value,
    )
    page_buffer[0 : len(header_writer)] = bytes(header_writer)
    page_buffer[len(header_writer) : len(header_writer) + len(encoded_records)] = encoded_records
    return bytes(page_buffer)


def decode_page(data: bytes) -> InternalPage | LeafPage | FreelistPage:
    """
    Decode a page from its on-disk byte representation, dispatching on the page type tag.

    Parameters
    ----------
    data
        The raw page buffer read from disk.

    Returns
    -------
    The decoded page, with a concrete type chosen by the tag byte in the header.
    """
    reader = BufferReader(data)
    (
        page_type,
        _,
        count_value,
        free_space_start,
        free_space_end,
        last_modified_lsn,
        page_id_field_value,
    ) = reader.read_struct(_PAGE_HEADER)
    match page_type:
        case PageType.INTERNAL:
            slots = []
            for _ in range(count_value):
                record_offset, record_length = reader.read_struct(_SLOT_ENTRY)
                record = data[record_offset : record_offset + record_length]
                record_reader = BufferReader(record)
                key = record_reader.read_length_prefixed_bytes()
                child_page_id = record_reader.read_struct(_PAGE_ID_FIELD)[0]
                slots.append(InternalSlot(key=key, child_page_id=child_page_id))
            return InternalPage(
                last_modified_lsn=last_modified_lsn,
                leftmost_child_page_id=page_id_field_value,
                slots=slots,
            )
        case PageType.LEAF:
            slots = []
            for _ in range(count_value):
                record_offset, record_length = reader.read_struct(_SLOT_ENTRY)
                record = data[record_offset : record_offset + record_length]
                record_reader = BufferReader(record)
                key = record_reader.read_length_prefixed_bytes()
                value = record_reader.read_length_prefixed_bytes()
                slots.append(LeafSlot(key=key, value=value))
            return LeafPage(
                last_modified_lsn=last_modified_lsn,
                right_sibling_page_id=page_id_field_value,
                slots=slots,
            )
        case PageType.FREELIST:
            freed_page_ids = []
            for _ in range(count_value):
                page_id = reader.read_struct(_PAGE_ID_FIELD)[0]
                freed_page_ids.append(page_id)
            return FreelistPage(
                last_modified_lsn=last_modified_lsn,
                next_freelist_page_id=page_id_field_value,
                freed_page_ids=freed_page_ids,
            )
        case _:
            raise ValueError(f"Unknown page type {page_type}")


def calculate_slot_size(slot: LeafSlot | InternalSlot, include_meta: bool = False) -> int:
    """
    Compute the number of encoded bytes a slot occupies on the page.

    Parameters
    ----------
    slot
        The slot whose encoded size is being computed.
    include_meta
        When true, the slot directory entry pointing at the slot is included as well.
        This is what callers normally want when sizing the page as a whole.

    Returns
    -------
    The encoded byte size of the slot.
    """
    base_size = _SLOT_ENTRY.size if include_meta else 0
    match slot:
        case InternalSlot():
            return base_size + _LENGTH_FIELD.size + len(slot.key) + _PAGE_ID_FIELD.size
        case LeafSlot():
            return base_size + _LENGTH_FIELD.size * 2 + len(slot.key) + len(slot.value)
        case _:
            raise ValueError(f"Unknown slot type: {type(slot)}")


def calculate_page_size(page: InternalPage | LeafPage) -> int:
    """
    Compute the encoded byte size of a page.

    Parameters
    ----------
    page
        The page whose encoded size is being computed.

    Returns
    -------
    The total size in bytes: page header + slot directory + records.
    """
    encoded_page_size = _PAGE_HEADER.size
    for slot in page.slots:
        encoded_page_size += calculate_slot_size(slot, include_meta=True)
    return encoded_page_size


def calculate_leaf_record_size(key: bytes, value: bytes) -> int:
    """
    Compute the encoded byte size of a leaf record.

    Parameters
    ----------
    key
        The leaf key.
    value
        The associated value.

    Returns
    -------
    Two length prefixes plus `len(key) + len(value)`.
    """
    return _LENGTH_FIELD.size * 2 + len(key) + len(value)


def get_max_leaf_record_size(page_size_bytes: int) -> int:
    """
    Compute the largest leaf record that can safely fit on a page of the given size.

    Parameters
    ----------
    page_size_bytes
        Size of a page in bytes.

    Returns
    -------
    The maximum record size, in bytes.
    """
    # The 20% cap (not 25%) ensures that no single slot is large enough to force a split into
    # underpopulated half-pages which are impossible to balance without introducing new techniques.
    return (page_size_bytes - _PAGE_HEADER.size) // 5 - _SLOT_ENTRY.size


def get_max_freed_ids_per_freelist_page(page_size_bytes: int) -> int:
    """
    Compute the maximum number of freed page IDs that fit on a single freelist page.

    Parameters
    ----------
    page_size_bytes
        Size of a page in bytes.

    Returns
    -------
    The capacity of one freelist page, measured in freed-page-ID entries.
    """
    return (page_size_bytes - _PAGE_HEADER.size) // _PAGE_ID_FIELD.size
