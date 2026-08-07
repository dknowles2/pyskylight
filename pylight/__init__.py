"""pylight — an async Python client for the (unofficial) Skylight API.

Example:
    >>> import asyncio
    >>> from pylight import PasswordAuth, Skylight
    >>>
    >>> async def main() -> None:
    ...     async with Skylight(PasswordAuth("me@example.com", "hunter2")) as skylight:
    ...         frame = (await skylight.get_frames())[0]
    ...         for chore in await skylight.get_chores(frame.id):
    ...             print(chore.summary, chore.start, chore.completed)
    >>> asyncio.run(main())  # doctest: +SKIP

This project is not affiliated with or endorsed by Skylight. The API is
reverse-engineered from observed traffic and can change without notice.

"""

from __future__ import annotations

from .auth import Auth, PasswordAuth, TokenAuth
from .client import Skylight
from .exceptions import (
    ApiError,
    AuthenticationError,
    NotAuthorizedError,
    NotFoundError,
    RateLimitError,
    SkylightError,
)
from .jsonapi import ApiObject, Document
from .models import (
    Alarm,
    ApplyTo,
    CalendarEvent,
    Category,
    Chore,
    ChoreGroups,
    ChoreStatus,
    Device,
    Frame,
    ListItem,
    ListItemStatus,
    ListKind,
    Nudge,
    Reward,
    RewardPoint,
    SkylightList,
    SourceCalendar,
    TaskBoxItem,
    Token,
    User,
)

__version__ = "0.1.0"

__all__ = [
    "Alarm",
    "ApiError",
    "ApiObject",
    "ApplyTo",
    "Auth",
    "AuthenticationError",
    "CalendarEvent",
    "Category",
    "Chore",
    "ChoreGroups",
    "ChoreStatus",
    "Device",
    "Document",
    "Frame",
    "ListItem",
    "ListItemStatus",
    "ListKind",
    "NotAuthorizedError",
    "NotFoundError",
    "Nudge",
    "PasswordAuth",
    "RateLimitError",
    "Reward",
    "RewardPoint",
    "Skylight",
    "SkylightError",
    "SkylightList",
    "SourceCalendar",
    "TaskBoxItem",
    "Token",
    "TokenAuth",
    "User",
    "__version__",
]
