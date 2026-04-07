class DBError(Exception):
    pass


class DBClosedError(DBError):
    def __init__(self, message: str = "The database is not opened") -> None:
        super().__init__(message)
