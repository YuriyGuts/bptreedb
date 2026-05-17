"""Exception hierarchy raised by the database engine."""


class DBError(Exception):
    """Base class for all errors raised by the database engine."""


class DBClosedError(DBError):
    """Raised when an operation is attempted on a database that has not been opened."""

    def __init__(self, message: str = "The database is not opened") -> None:
        """
        Build the error with an optional custom message.

        Parameters
        ----------
        message
            Human-readable error message; the default is used when none is supplied.
        """
        super().__init__(message)


class DBCorruptedError(DBError):
    """Raised when the on-disk state appears to be inconsistent or damaged."""

    def __init__(self, message: str = "The database is corrupted") -> None:
        """
        Build the error with an optional custom message.

        Parameters
        ----------
        message
            Human-readable error message; the default is used when none is supplied.
        """
        super().__init__(message)


class DBChecksumError(DBCorruptedError):
    """Raised when a CRC check fails while decoding a page or WAL record."""

    def __init__(self, expected: int, actual: int) -> None:
        """
        Build an error describing the checksum mismatch.

        Parameters
        ----------
        expected
            The CRC32 value read from disk.
        actual
            The CRC32 value computed from the payload.
        """
        super().__init__(f"Checksum mismatch: expected 0x{expected:08x}, actual 0x{actual:08x}")


class DBRecordTooLargeError(DBError):
    """Raised when a key/value pair exceeds the per-page record size limit."""

    def __init__(self, limit: int, actual: int) -> None:
        """
        Build an error describing the size violation.

        Parameters
        ----------
        limit
            Maximum allowed record size, in bytes.
        actual
            The actual size of the offending record, in bytes.
        """
        msg = f"The database record is too large (limit: {limit} bytes, actual: {actual} bytes)"
        super().__init__(msg)


class DBConcurrentPageModificationError(DBError):
    """Raised when a scan detects that the underlying tree was modified mid-iteration."""

    def __init__(self, message: str = "The database was modified during iteration") -> None:
        super().__init__(message)


class DBBufferPoolOverflowError(DBError):
    """Raised when every page in the buffer pool is dirty and no eviction candidate is available."""

    def __init__(self, message: str = "Buffer pool is at capacity, cannot add new pages") -> None:
        super().__init__(message)
