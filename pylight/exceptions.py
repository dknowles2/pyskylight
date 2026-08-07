"""Exceptions raised by pylight."""

from __future__ import annotations

from typing import Any

__all__ = [
    "SkylightError",
    "AuthenticationError",
    "NotAuthorizedError",
    "NotFoundError",
    "RateLimitError",
    "ApiError",
]


class SkylightError(Exception):
    """Base class for all errors raised by pylight."""


class AuthenticationError(SkylightError):
    """Raised when the login flow could not produce an access token.

    This means the credentials were rejected, or the login flow changed shape.
    """


class ApiError(SkylightError):
    """Raised when the API returns an unsuccessful HTTP status."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        errors: list[Any] | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.errors = errors or []
        self.url = url


class NotAuthorizedError(ApiError):
    """Raised on HTTP 401/403. The token is missing, expired, or insufficient."""


class NotFoundError(ApiError):
    """Raised on HTTP 404."""


class RateLimitError(ApiError):
    """Raised on HTTP 429."""
