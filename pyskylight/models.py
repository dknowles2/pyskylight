"""Data models for Skylight API resources.

Field coverage comes from live captures against a real account, cross-checked
against the reverse-engineered OpenAPI spec. Because the API sends
``additionalProperties`` freely, every model also keeps its raw resource: reach
anything unmodeled via :attr:`~pyskylight.jsonapi.ApiObject.attributes`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .jsonapi import ApiObject, alias, relationship, relationships

__all__ = [
    "Alarm",
    "ApplyTo",
    "CalendarEvent",
    "Category",
    "Chore",
    "ChoreGroups",
    "ChoreStatus",
    "Device",
    "Frame",
    "ListItem",
    "ListItemStatus",
    "ListKind",
    "NightlightColor",
    "Nudge",
    "Reward",
    "RewardPoint",
    "SkylightList",
    "SourceCalendar",
    "TaskBoxItem",
    "Token",
    "User",
]


class ChoreStatus:
    """Values accepted by the chore completions endpoint.

    The API accepts ``"complete"``, not ``"completed"``; ``"skipped"`` is
    rejected. :attr:`Chore.status` on a fetched chore reads ``"pending"``.
    """

    COMPLETE = "complete"
    PENDING = "pending"


class ListItemStatus:
    """Known values for :attr:`ListItem.status`."""

    PENDING = "pending"
    COMPLETED = "completed"


class ListKind:
    """Known values for :attr:`SkylightList.kind`."""

    SHOPPING = "shopping"
    TODO = "to_do"


class NightlightColor:
    """Values accepted by a device's ``nightlight_color``.

    Probed against a live display; ``white``, ``warm`` and ``purple`` are
    rejected with ``422 Nightlight color is not included in the list``.
    """

    OFF = "off"
    RED = "red"
    ORANGE = "orange"
    YELLOW = "yellow"
    GREEN = "green"
    BLUE = "blue"
    PINK = "pink"

    ALL = (OFF, RED, ORANGE, YELLOW, GREEN, BLUE, PINK)


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

    category_id: int | None = field(default=None, metadata=alias("id"))
    label: str | None = None
    color: str | None = None
    selected_for_chore_chart: bool | None = None
    linked_to_profile: bool | None = None
    profile_picture_urls: dict[str, Any] = field(default_factory=dict)
    avatar_id: str | None = field(default=None, metadata=relationship("avatar"))
    family_member_id: str | None = field(default=None, metadata=relationship("family_member"))
    source_calendar_ids: list[str] = field(
        default_factory=list, metadata=relationships("source_calendars")
    )

    @property
    def profile_picture_url(self) -> str | None:
        """The largest available profile picture, if one is set."""
        urls = self.profile_picture_urls
        for key in ("original", "xl", "large", "medium", "small"):
            if isinstance(value := urls.get(key), str) and value:
                return value
        return None


@dataclass(frozen=True, kw_only=True)
class Chore(ApiObject):
    """A chore (task) occurrence.

    Recurring chores are returned one resource per occurrence, and :attr:`id` is
    an occurrence id of the form ``"<chore_id>-<date>"``. :attr:`chore_id` is the
    addressable chore — pass it to update, delete, and completion calls.
    """

    chore_id: str | None = field(default=None, metadata=alias("group"))
    series: str | None = None
    summary: str | None = None
    description: str | None = None
    status: str | None = None
    start: date | None = None
    start_time: str | None = None
    completed_on: date | None = None
    completed_at: datetime | None = None
    is_future: bool | None = None
    recurring: bool | None = None
    recurring_until: str | None = None
    recurrence_set: list[str] = field(default_factory=list)
    renewal_interval: int | None = None
    renewal_unit: str | None = None
    reward_points: int | None = None
    emoji_icon: str | None = None
    routine: bool | None = None
    position: int | None = None
    origin: str | None = None
    up_for_grabs: bool | None = None
    timer_seconds: int | None = None
    category_id: str | None = field(default=None, metadata=relationship("category"))
    completed_category_id: str | None = field(
        default=None, metadata=relationship("completed_category")
    )

    @property
    def completed(self) -> bool:
        """Whether this occurrence has been marked complete."""
        return (
            self.completed_on is not None
            or self.completed_at is not None
            or self.status in (ChoreStatus.COMPLETE, "completed")
        )


@dataclass(frozen=True, kw_only=True)
class ChoreGroups:
    """The bucketed response from ``GET /api/frames/{id}/chores/all``.

    Attributes:
        chores: Chores keyed by bucket — ``late``, ``today``, ``today_timed``,
            ``any_day``, ``future``.
        routines: Routine chores, bucketed the same way.
        raw: The undecoded response body.
    """

    chores: dict[str, list[Chore]] = field(default_factory=dict)
    routines: dict[str, list[Chore]] = field(default_factory=dict)
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_response(cls, payload: Any) -> ChoreGroups:
        """Build from the raw response body."""
        body: dict[str, Any] = payload if isinstance(payload, dict) else {}
        return cls(
            chores=cls._buckets(body.get("chores")),
            routines=cls._buckets(body.get("routines")),
            raw=body,
        )

    @staticmethod
    def _buckets(value: Any) -> dict[str, list[Chore]]:
        if not isinstance(value, dict):
            return {}
        return {name: Chore.from_document(bucket) for name, bucket in value.items()}

    @property
    def all(self) -> list[Chore]:
        """Every chore across every bucket, routines included."""
        return [
            chore
            for group in (self.chores, self.routines)
            for bucket in group.values()
            for chore in bucket
        ]


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
    draft: bool | None = None
    hide_on_device: bool | None = None
    list_item_ids: list[str] = field(default_factory=list, metadata=relationships("list_items"))
    items: list[ListItem] = field(default_factory=list, compare=False)
    sections: list[dict[str, Any]] = field(default_factory=list, compare=False)


@dataclass(frozen=True, kw_only=True)
class Frame(ApiObject):
    """A Skylight frame — one household's device, calendar, and content."""

    name: str | None = None
    household_name: str | None = None
    timezone: str | None = None
    access: str | None = None
    mine: bool | None = None
    plus: bool | None = None
    activated: bool | None = None
    activated_at: datetime | None = None
    user_created_at: datetime | None = None
    destroyed_at: datetime | None = None
    apps: list[str] = field(default_factory=list)
    feature_bundle: dict[str, Any] = field(default_factory=dict)
    brightness: int | None = None
    blur_effect: bool | None = None
    current_album_id: int | None = None
    currently_sleeping: bool | None = None
    sleep_mode_on: bool | None = None
    sleeps_at: str | None = None
    wakes_at: str | None = None
    slideshow_speed: int | None = None
    slideshow_style: int | None = None
    side_by_side: bool | None = None
    show_caption: bool | None = None
    show_heart: bool | None = None
    start_sound: bool | None = None
    message_viewability: str | None = None
    notification_email: str | None = None
    open_to_public: bool | None = None
    share_token: str | None = None
    gift_status: str | None = None
    gift_recipient_name: str | None = None
    trialing: bool | None = None
    trial_expires_at: datetime | None = None
    assistant_household_id: str | None = None
    hardware_model: str | None = None
    owner_name: str | None = None
    owner_birthday: date | None = None
    device_ids: list[str] = field(default_factory=list, metadata=relationships("devices"))
    user_id: str | None = field(default=None, metadata=relationship("user"))


@dataclass(frozen=True, kw_only=True)
class Device(ApiObject):
    """A physical Skylight device registered to a frame."""

    name: str | None = None
    role: str | None = None
    activated: bool | None = None
    timezone: str | None = None
    category_id: int | None = None
    brightness: int | None = None
    blur_effect: bool | None = None
    current_album_id: int | None = None
    currently_sleeping: bool | None = None
    sleep_mode: str | None = None
    sleep_mode_on: bool | None = None
    sleeps_at: str | None = None
    wakes_at: str | None = None
    sleep_sound: str | None = None
    sleep_sound_volume: int | None = None
    nightlight: bool | None = None
    nightlight_brightness: int | None = None
    nightlight_color: str | None = None
    slideshow_speed: int | None = None
    slideshow_style: int | None = None
    side_by_side: bool | None = None
    show_caption: bool | None = None
    show_heart: bool | None = None
    start_sound: bool | None = None


@dataclass(frozen=True, kw_only=True)
class Alarm(ApiObject):
    """An alarm configured on a device.

    No alarm body has been captured yet; read :attr:`attributes`.
    """


@dataclass(frozen=True, kw_only=True)
class CalendarEvent(ApiObject):
    """A calendar event."""

    summary: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    all_day: bool | None = None
    timezone: str | None = None
    location: str | None = None
    lat: float | None = None
    lng: float | None = None
    kind: str | None = None
    status: str | None = None
    source: str | None = None
    uid: str | None = None
    calendar_id: str | None = None
    master_event_id: str | None = None
    recurring: bool | None = None
    recurring_config: bool | None = None
    rrule: list[str] = field(default_factory=list)
    countdown_enabled: bool | None = None
    editable: bool | None = None
    owner_email: str | None = None
    invited_emails: list[str] = field(default_factory=list)
    supports_notification_settings: bool | None = None
    category_id: str | None = field(default=None, metadata=relationship("category"))
    category_ids: list[str] = field(default_factory=list, metadata=relationships("categories"))
    calendar_account_id: str | None = field(default=None, metadata=relationship("calendar_account"))


@dataclass(frozen=True, kw_only=True)
class SourceCalendar(ApiObject):
    """An external calendar synced into a frame."""

    label: str | None = None
    kind: str | None = None
    role: str | None = None
    source_id: str | None = None
    editable: bool | None = None
    default_for_new_events: bool | None = None
    calendar_account_id: str | None = field(default=None, metadata=relationship("calendar_account"))


@dataclass(frozen=True, kw_only=True)
class Reward(ApiObject):
    """A reward that can be redeemed with points."""

    name: str | None = None
    description: str | None = None
    emoji_icon: str | None = None
    point_value: int | None = None
    redeemed_at: datetime | None = None
    respawn_on_redemption: bool | None = None
    origin: str | None = None
    category_id: str | None = field(default=None, metadata=relationship("category"))


@dataclass(frozen=True, kw_only=True)
class RewardPoint:
    """A family member's reward point balance.

    ``GET /api/frames/{id}/reward_points`` returns a plain JSON array rather
    than a JSON:API document, so this is not an :class:`ApiObject`.
    """

    category_id: int | None = None
    current_point_balance: int | None = None
    lifetime_points_earned: int | None = None
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_response(cls, payload: Any) -> list[RewardPoint]:
        """Build a list of balances from the raw response body."""
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        return [
            cls(
                category_id=row.get("category_id"),
                current_point_balance=row.get("current_point_balance"),
                lifetime_points_earned=row.get("lifetime_points_earned"),
                raw=row,
            )
            for row in rows
            if isinstance(row, dict)
        ]


@dataclass(frozen=True, kw_only=True)
class Nudge(ApiObject):
    """A nudge — a spoken reminder played on the frame at a set time."""

    nudge_id: int | None = field(default=None, metadata=alias("id"))
    body: str | None = None
    deliver_at: datetime | None = None
    recurring: bool | None = None
    recurring_until: datetime | None = None
    rrule: list[str] = field(default_factory=list)
    voice_kind: str | None = None
    audio_url: str | None = None


@dataclass(frozen=True, kw_only=True)
class User(ApiObject):
    """The authenticated user.

    ``GET /api/user`` is not JSON:API-shaped in observed traffic, so this is
    built from a plain object; :attr:`raw` holds the whole response.
    """

    email: str | None = None
    name: str | None = None
    phone: str | None = None
    birthday: date | None = None
    created_at: datetime | None = None
    subscription_status: str | None = None
    plus_billing_provider: str | None = None
    was_plus_purchaser: bool | None = None
    trial_days_remaining: int | None = None
    trial_expires_at: datetime | None = None
    email_mfa_enabled: bool | None = None
    agreed_to_marketing: bool | None = None
    notification_preference: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> User:
        """Build a user from either a JSON:API document or a plain object."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and "attributes" in data:
            user = cls.from_resource(data)
        else:
            body: dict[str, Any] = data if isinstance(data, dict) else payload
            nested = body.get("user")
            attributes: dict[str, Any] = nested if isinstance(nested, dict) else body
            user = cls.from_resource(
                {
                    "type": "user",
                    "id": str(attributes.get("id", "")),
                    "attributes": attributes,
                }
            )
        if user.name is None and isinstance(name := user.profile.get("name"), str):
            # The display name lives on the nested profile object, not the user.
            object.__setattr__(user, "name", name)
        return user


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
