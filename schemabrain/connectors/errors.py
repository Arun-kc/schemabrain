"""Connector-specific exceptions."""


class TableNotFoundError(ValueError):
    """Raised when a requested table does not exist in the source database.

    Subclasses `ValueError` so existing `except ValueError` callers keep
    working, while new callers can catch this specific case.
    """
