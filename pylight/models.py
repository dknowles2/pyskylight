"""Data models for Skylight API resources.

Field coverage tracks what has actually been observed in captured traffic. Where
the upstream reference documents a resource but not its attributes, the model is
intentionally thin: use :attr:`~pylight.jsonapi.ApiObject.attributes` to reach
whatever the server returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .jsonapi import ApiObject, alias, relationship, relationships

__all__ = [
    "Category",
    "Chore",
    "TaskBoxItem",
    "SkylightList",
    "ListItem",
    "ListKind",
    "ChoreStatus",
    "ListItemStatus",
    "ApplyTo",
    "Frame",
    "Device",
    "Alarm",
    "CalendarEvent",
    "SourceCalendar",
    "Reward",
    "RewardPoint",
    "Nudge",
    "User",
    "Token",
]


class ChoreStatus:
    """Known values for :attr:`Chore.status` and chore completions."""

    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class ListItemStatus:
    """Known values for :attr:`ListItem.status`."""

    PENDING = "pending"
    COMPLETED = "completed"


class ListKind:
    """Known values for :attr:`SkylightList.kind`."""

    SHOPPING = "shopping"
    TODO = "to_do"


class ApplyTo:
    """Recurrence scope for chore updates and deletes."""

    THIS = "this"
    THIS_AND_FUTURE = "this_and_future"
    ALL = "all"


@dataclass(frozen=True, kw_only=True)
class Category(ApiObject):
    """A family member profile.

    Skylight calls these "categories" in the API and "profiles" in the UI.
    """

    label: str | None = None
    color: str | None = None
    selected_for_chore_chart: bool | None = None
    linked_to_profile: bool | None = None
    profile_pic_url: str | None = None


@dataclass(frozen=True, kw_only=True)
class Chore(ApiObject):
    """A chore (task) instance.

    For recurring chores the API returns one resource per occurrence, and
    :attr:`id` is an instance id of the form ``"<chore_id>-<date>"``. Use
    :attr:`chore_id` when addressing the underlying chore.
    """

    chore_id: int | None = field(default=None, metadata=alias("id"))
    summary: str | None = None
    status: str | None = None
    start: date | None = None
    start_time: str | None = None
    completed_on: str | None = None
    is_future: bool | None = None
    recurring: bool | None = None
    recurring_until: str | None = None
    recurrence_set: str | None = None
    reward_points: int | None = None
    emoji_icon: str | None = None
    routine: bool | None = None
    position: int | None = None
    category_id: str | None = field(default=None, metadata=relationship("category"))

    @property
    def completed(self) -> bool:
        """Whether this occurrence has been marked complete."""
        return bool(self.completed_on) or self.status == ChoreStatus.COMPLETED


@dataclass(frozen=True, kw_only=True)
class TaskBoxItem(ApiObject):
    """An unscheduled task sitting in the frame's task box (inbox)."""

    item_id: int | None = field(default=None, metadata=alias("id"))
    summary: str | None = None
    emoji_icon: str | None = None
    routine: bool | None = None
    reward_points: int | None = None


@dataclass(frozen=True, kw_only=True)
class ListItem(ApiObject):
    """An entry on a grocery or to-do list."""

    label: str | None = None
    status: str | None = None
    section: str | None = None
    position: int | None = None
    created_at: datetime | None = None

    @property
    def completed(self) -> bool:
        """Whether the item is checked off."""
        return self.status == ListItemStatus.COMPLETED


@dataclass(frozen=True, kw_only=True)
class SkylightList(ApiObject):
    """A grocery or to-do list.

    Named ``SkylightList`` rather than ``List`` to avoid colliding with
    :class:`typing.List` at call sites.
    """

    label: str | None = None
    color: str | None = None
    kind: str | None = None
    default_grocery_list: bool | None = None
    list_item_ids: list[str] = field(default_factory=list, metadata=relationships("list_items"))
    items: list[ListItem] = field(default_factory=list, compare=False)
    sections: list[dict[str, Any]] = field(default_factory=list, compare=False)


@dataclass(frozen=True, kw_only=True)
class Frame(ApiObject):
    """A Skylight frame — one household's device, calendar, and content.

    The upstream reference has not captured a frame body, so only the resource
    id is modeled. Everything else is available via :attr:`attributes`.
    """

    @property
    def name(self) -> str | None:
        """Best-effort display name, if the response carries one."""
        attributes = self.attributes
        for key in ("name", "label", "frame_name"):
            if isinstance(value := attributes.get(key), str):
                return value
        return None


@dataclass(frozen=True, kw_only=True)
class Device(ApiObject):
    """A physical Skylight device registered to a frame."""


@dataclass(frozen=True, kw_only=True)
class Alarm(ApiObject):
    """An alarm configured on a device."""


@dataclass(frozen=True, kw_only=True)
class CalendarEvent(ApiObject):
    """A calendar event.

    Attribute shape has not been captured upstream; read :attr:`attributes`.
    """

    category_id: str | None = field(default=None, metadata=relationship("category"))
    source_calendar_id: str | None = field(default=None, metadata=relationship("source_calendar"))


@dataclass(frozen=True, kw_only=True)
class SourceCalendar(ApiObject):
    """An external calendar synced into a frame."""


@dataclass(frozen=True, kw_only=True)
class Reward(ApiObject):
    """A reward that can be redeemed with points."""


@dataclass(frozen=True, kw_only=True)
class RewardPoint(ApiObject):
    """A reward point balance or ledger entry."""


@dataclass(frozen=True, kw_only=True)
class Nudge(ApiObject):
    """A nudge (reminder) configured on a frame."""


@dataclass(frozen=True, kw_only=True)
class User(ApiObject):
    """The authenticated user.

    ``GET /api/user`` is not JSON:API-shaped in observed traffic, so this is
    built from a plain object; :attr:`raw` holds the whole response.
    """

    email: str | None = None
    name: str | None = None

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> User:
        """Build a user from either a JSON:API document or a plain object."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and "attributes" in data:
            return cls.from_resource(data)
        body: dict[str, Any] = data if isinstance(data, dict) else payload
        nested = body.get("user")
        user: dict[str, Any] = nested if isinstance(nested, dict) else body
        return cls(
            id=str(user.get("id", "")),
            raw={"type": "user", "id": str(user.get("id", "")), "attributes": user},
            email=user.get("email"),
            name=user.get("name"),
        )


@dataclass(frozen=True, kw_only=True)
class Token:
    """An OAuth token set.

    Attributes:
        access_token: Bearer token for API requests.
        refresh_token: Token used to mint a new access token.
        expires_at: Absolute expiry, derived from ``expires_in`` at issue time.
        token_type: Always ``"Bearer"`` in observed traffic.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    token_type: str = "Bearer"

    @property
    def is_expired(self) -> bool:
        """Whether the access token is past its expiry."""
        if self.expires_at is None:
            return False
        return datetime.now(self.expires_at.tzinfo) >= self.expires_at
