"""Errors raised while validating and summarizing environmental data."""


class DataError(Exception):
    status = 500

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class ValidationError(DataError, ValueError):
    status = 422


class InsufficientDataError(DataError, ValueError):
    status = 422


class ProviderError(DataError):
    status = 502
