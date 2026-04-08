class DBError(Exception):
    pass


class DBClosedError(DBError):
    def __init__(self, message: str = "The database is not opened") -> None:
        super().__init__(message)


class DBCorruptedError(DBError):
    def __init__(self, message: str = "The database is corrupted") -> None:
        super().__init__(message)


class DBChecksumError(DBCorruptedError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"Checksum mismatch: expected 0x{expected:08x}, actual 0x{actual:08x}")


class DBRecordTooLargeError(DBError):
    def __init__(self, limit: int, actual: int) -> None:
        msg = f"The database record is too large (limit: {limit} bytes, actual: {actual} bytes)"
        super().__init__(msg)


class DBConcurrentPageModificationError(DBError):
    def __init__(self, message: str = "The database was modified during iteration") -> None:
        super().__init__(message)
