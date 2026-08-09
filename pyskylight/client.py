"""Async client for the (unofficial) Skylight API."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from types import TracebackType
from typing import Any

import aiohttp

from .auth import Auth
from .const import API_PREFIX, API_VERSION, BASE_URL, DEFAULT_TIMEOUT, USER_AGENT
from .exceptions import (
    ApiError,
    NotAuthorizedError,
    NotFoundError,
    RateLimitError,
    SkylightError,
)
from .jsonapi import Document
from .models import (
    Alarm,
    CalendarEvent,
    Category,
    Chore,
    ChoreGroups,
    ChoreStatus,
    Device,
    Frame,
    ListItem,
    MealCategory,
    Message,
    Nudge,
    Recipe,
    Reward,
    RewardPoint,
    SkylightList,
    SourceCalendar,
    TaskBoxItem,
    User,
)

_LOGGER = logging.getLogger(__name__)

__all__ = ["Skylight"]

#: Decoded JSON from an endpoint whose response shape has not been captured yet.
_JSON = Any


def _fmt(value: Any) -> str:
    """Render a query parameter the way the API expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return ",".join(_fmt(v) for v in value)
    return str(value)


def _params(**kwargs: Any) -> dict[str, str]:
    """Drop ``None`` values and stringify the rest."""
    return {k: _fmt(v) for k, v in kwargs.items() if v is not None}


def _as_list(value: Sequence[str] | str | None) -> list[str] | None:
    """Normalize an RRULE argument to the list the API expects."""
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


def _body(**kwargs: Any) -> _JSON:
    """Drop ``None`` values from a request body."""
    return {k: v for k, v in kwargs.items() if v is not None}


class Skylight:
    """An async Skylight API client.

    Args:
        auth: Supplies the bearer token. See :class:`~pyskylight.auth.PasswordAuth`
            and :class:`~pyskylight.auth.TokenAuth`.
        session: An existing :class:`aiohttp.ClientSession` to use. When omitted,
            the client creates and owns one.
        base_url: Override the API host. Useful for tests.
        timeout: Total timeout, in seconds, for each request.

    Example:
        >>> async with Skylight(PasswordAuth(email, password)) as skylight:
        ...     frames = await skylight.get_frames()
        ...     chores = await skylight.get_chores(frames[0].id)

    Methods returning ``dict`` cover endpoints whose response shape has not been
    captured yet; they hand back the decoded JSON untouched.
    """

    def __init__(
        self,
        auth: Auth,
        *,
        session: aiohttp.ClientSession | None = None,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._auth = auth
        self._session = session
        self._owns_session = session is None
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ---------------------------------------------------------------- plumbing

    async def __aenter__(self) -> Skylight:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the session and auth handler this client owns."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        if self._owns_session:
            self._session = None
        await self._auth.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if not self._owns_session:
                raise SkylightError("the supplied aiohttp session is closed")
            self._session = aiohttp.ClientSession()
        return self._session

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Skylight-Api-Version": API_VERSION,
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        """Make a raw authenticated request.

        Args:
            method: HTTP method.
            path: Path relative to the API host, e.g. ``/api/frames``.
            params: Query parameters, already stringified.
            json: Request body, serialized as JSON.

        Returns:
            The decoded JSON body, or ``None`` for empty responses.

        Raises:
            AuthenticationError: If the token was rejected and the auth handler
                could not mint a new one.
            NotAuthorizedError: On 401/403, after one refresh attempt.
            NotFoundError: On 404.
            RateLimitError: On 429.
            ApiError: On any other unsuccessful status.
        """
        token = await self._auth.access_token()
        result = await self._send(method, path, token, params, json)
        if result is not _RETRY:
            return result
        # The token was rejected: refresh once, then let the error stand.
        token = await self._auth.refresh()
        result = await self._send(method, path, token, params, json, allow_retry=False)
        return None if result is _RETRY else result

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        params: Mapping[str, str] | None,
        json: Any,
        allow_retry: bool = True,
    ) -> Any:
        url = f"{self._base_url}{path}"
        started = time.monotonic()
        async with self._get_session().request(
            method,
            url,
            params=dict(params) if params else None,
            json=json,
            headers=self._headers(token),
            timeout=self._timeout,
        ) as resp:
            # Method, path and status only. Bodies carry chore summaries and
            # calendar entries, headers carry the bearer token, and none of that
            # belongs in a log the user is about to paste into an issue.
            _LOGGER.debug(
                "%s %s -> %s in %dms",
                method,
                path,
                resp.status,
                (time.monotonic() - started) * 1000,
            )
            if resp.status == 304 or resp.status == 204:
                return None
            body: Any = None
            if resp.content_length != 0:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
            if resp.status < 400:
                return body

            if resp.status in (401, 403) and allow_retry:
                return _RETRY

            raw_errors = body.get("errors") if isinstance(body, dict) else None
            errors = _flatten_errors(raw_errors) or None
            message = _error_message(body) or resp.reason or "request failed"
            error_type: type[ApiError] = ApiError
            if resp.status in (401, 403):
                error_type = NotAuthorizedError
            elif resp.status == 404:
                error_type = NotFoundError
            elif resp.status == 429:
                error_type = RateLimitError
            raise error_type(resp.status, message, errors=errors, url=url)

    async def _get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=_params(**params))

    # ------------------------------------------------------------------- user

    async def get_user(self) -> User:
        """Get the authenticated user's profile."""
        return User.from_response(await self._get(f"{API_PREFIX}/user") or {})

    async def update_user(self, **fields: Any) -> _JSON:
        """Update the authenticated user."""
        return await self.request("PUT", f"{API_PREFIX}/user", json={"user": fields})

    async def update_user_profile(self, **fields: Any) -> _JSON:
        """Update profile fields on the authenticated user."""
        return await self.request("PATCH", f"{API_PREFIX}/user/profile", json=fields)

    async def set_push_notifications(self, enabled: bool) -> _JSON:
        """Toggle push notifications."""
        return await self.request(
            "PATCH", f"{API_PREFIX}/user/push_toggler", json={"enabled": enabled}
        )

    async def set_marketing_emails(self, enabled: bool) -> _JSON:
        """Toggle marketing emails."""
        return await self.request(
            "PATCH", f"{API_PREFIX}/user/klaviyo_toggler", json={"enabled": enabled}
        )

    async def get_reminder_profile(self) -> _JSON:
        """Get reminder settings."""
        return await self._get(f"{API_PREFIX}/reminder_profile")

    async def update_reminder_profile(self, **fields: Any) -> _JSON:
        """Update reminder settings."""
        return await self.request("PUT", f"{API_PREFIX}/reminder_profile", json=fields)

    async def get_plus_access(self) -> _JSON:
        """Get Skylight Plus subscription details."""
        return await self._get(f"{API_PREFIX}/plus_access")

    # ----------------------------------------------------------------- frames

    async def get_frames(self) -> list[Frame]:
        """List every frame the user can access."""
        return Frame.from_document(await self._get(f"{API_PREFIX}/frames"))

    async def get_frame(self, frame_id: str | int) -> Frame:
        """Get a single frame."""
        return Frame.one_from_document(await self._get(f"{API_PREFIX}/frames/{frame_id}"))

    async def get_calendar_frames(self) -> list[Frame]:
        """List the frames the user can see the calendar for.

        Despite the singular-looking path, this returns a collection of
        ``approved_viewer_frame`` resources.
        """
        return Frame.from_document(await self._get(f"{API_PREFIX}/frames/calendar"))

    async def get_photo_frames(self) -> list[Frame]:
        """List the frames the user can see photos for."""
        return Frame.from_document(await self._get(f"{API_PREFIX}/frames/photo"))

    async def update_frame(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Update frame settings.

        Warning:
            Display settings do not belong here. Sending ``brightness``,
            ``sleeps_at``, ``slideshow_speed``, ``show_caption`` and friends to
            this endpoint returns ``200`` and changes nothing — verified against
            a live frame. Use :meth:`update_device`, which does apply them.
        """
        return await self.request("PUT", f"{API_PREFIX}/frames/{frame_id}", json=fields)

    async def rename_frame(self, frame_id: str | int, name: str) -> _JSON:
        """Rename a frame.

        Note:
            Skylight refuses this for activated hardware, answering
            ``422 Contact help@myskylight.com to rename this device``. It
            succeeds for frames with no display attached.
        """
        return await self.request(
            "PUT", f"{API_PREFIX}/frames/{frame_id}/rename", json={"name": name}
        )

    async def get_household_config(self, frame_id: str | int) -> _JSON:
        """Get the frame's household configuration."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/household_config")

    async def update_household_config(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Update the frame's household configuration."""
        return await self.request(
            "PATCH", f"{API_PREFIX}/frames/{frame_id}/household_config", json=fields
        )

    async def get_frame_users(self, frame_id: str | int) -> _JSON:
        """List users with access to a frame."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/users")

    # ------------------------------------------------------------- categories

    async def get_categories(self, frame_id: str | int) -> list[Category]:
        """List family member profiles on a frame."""
        return Category.from_document(await self._get(f"{API_PREFIX}/frames/{frame_id}/categories"))

    async def get_category(self, frame_id: str | int, category_id: str | int) -> Category:
        """Get one family member profile."""
        return Category.one_from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/categories/{category_id}")
        )

    async def create_category(
        self,
        frame_id: str | int,
        label: str,
        color: str,
        **attributes: Any,
    ) -> Category:
        """Create a family member profile.

        Args:
            frame_id: The frame to create the profile on.
            label: The profile's display name.
            color: Hex color, e.g. ``"#00526D"``. Required and validated by the
                API; :meth:`get_colors` returns the supported palette.
            **attributes: Any other profile attributes, passed through as-is.
        """
        return Category.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/categories",
                json=_body(label=label, color=color, **attributes),
            )
        )

    async def update_category(
        self, frame_id: str | int, category_id: str | int, **attributes: Any
    ) -> Category:
        """Update a family member profile."""
        return Category.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/categories/{category_id}",
                json=dict(attributes),
            )
        )

    async def delete_category(self, frame_id: str | int, category_id: str | int) -> None:
        """Delete a family member profile."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/categories/{category_id}")

    # ----------------------------------------------------------------- chores

    async def get_chores(
        self,
        frame_id: str | int,
        *,
        after: date | str | None = None,
        before: date | str | None = None,
        include_late: bool | None = None,
        linked_to_profile: bool = False,
    ) -> list[Chore]:
        """List chore occurrences for a frame within a date range.

        Args:
            frame_id: The frame to query.
            after: Earliest occurrence date (inclusive).
            before: Latest occurrence date (inclusive).
            include_late: Include overdue occurrences from before ``after``.
            linked_to_profile: Restrict to chores linked to a family profile.
        """
        return Chore.from_document(
            await self._get(
                f"{API_PREFIX}/frames/{frame_id}/chores",
                after=after,
                before=before,
                include_late=include_late,
                filter="linked_to_profile" if linked_to_profile else None,
            )
        )

    async def get_all_chores(self, frame_id: str | int, **params: Any) -> ChoreGroups:
        """Get every chore, bucketed by urgency.

        This endpoint does not return a JSON:API document: chores arrive grouped
        under ``late``, ``today``, ``today_timed``, ``any_day``, and ``future``,
        with routines in a parallel structure. See
        :class:`~pyskylight.models.ChoreGroups`.
        """
        return ChoreGroups.from_response(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/chores/all", **params)
        )

    async def search_chores(self, frame_id: str | int, query: str) -> list[Chore]:
        """Search chores by text."""
        return Chore.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/chores/search", q=query)
        )

    async def create_chore(
        self,
        frame_id: str | int,
        summary: str,
        category_id: str | int,
        *,
        start: date | str | None = None,
        start_time: str | None = None,
        status: str | None = None,
        recurring: bool | None = None,
        recurrence_set: Sequence[str] | str | None = None,
        **attributes: Any,
    ) -> Chore:
        """Create a chore for one family profile.

        Args:
            frame_id: The frame to create the chore on.
            summary: Chore title.
            category_id: Family profile to assign the chore to. Required — the
                API rejects a chore with no category (``422 Category is
                required``). Use :meth:`create_chores` to assign several.
            start: Start date.
            start_time: Time of day, e.g. ``"10:00"``.
            status: Initial status, usually ``"pending"``.
            recurring: Whether the chore repeats.
            recurrence_set: One or more RRULE strings, e.g.
                ``"RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"``.
            **attributes: Any other chore attributes, passed through as-is.
        """
        return Chore.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/chores",
                json=_body(
                    summary=summary,
                    category_id=category_id,
                    start=_fmt(start) if start is not None else None,
                    start_time=start_time,
                    status=status,
                    recurring=recurring,
                    recurrence_set=_as_list(recurrence_set),
                    **attributes,
                ),
            )
        )

    async def create_chores(
        self,
        frame_id: str | int,
        summary: str,
        category_ids: Sequence[str | int],
        *,
        start: date | str | None = None,
        start_time: str | None = None,
        recurring: bool | None = None,
        recurrence_set: Sequence[str] | str | None = None,
        **attributes: Any,
    ) -> list[Chore]:
        """Create the same chore for several family profiles at once.

        Args:
            frame_id: The frame to create the chores on.
            summary: Chore title.
            category_ids: Family profiles to assign the chore to.
            start: Start date.
            start_time: Time of day, e.g. ``"10:00"``.
            recurring: Whether the chore repeats.
            recurrence_set: One or more RRULE strings.
            **attributes: Any other chore attributes, passed through as-is.

        Returns:
            The created chores — one per profile.
        """
        return Chore.from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/chores/create_multiple",
                json=_body(
                    summary=summary,
                    category_ids=[str(c) for c in category_ids],
                    start=_fmt(start) if start is not None else None,
                    start_time=start_time,
                    recurring=recurring,
                    recurrence_set=_as_list(recurrence_set),
                    **attributes,
                ),
            )
        )

    async def update_chore(
        self,
        frame_id: str | int,
        chore_id: str | int,
        *,
        apply_to: str | None = None,
        **fields: Any,
    ) -> Chore:
        """Update a chore.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id — :attr:`Chore.chore_id`, not the
                per-occurrence :attr:`Chore.id`.
            apply_to: Recurrence scope for a repeating chore. See
                :class:`~pyskylight.models.ApplyTo`. Optional.
            **fields: Chore fields to change.
        """
        return Chore.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}",
                json=_body(**fields, apply_to=apply_to),
            )
        )

    async def delete_chore(
        self, frame_id: str | int, chore_id: str | int, *, apply_to: str | None = None
    ) -> None:
        """Delete a chore.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id.
            apply_to: Recurrence scope, for repeating chores only. See
                :class:`~pyskylight.models.ApplyTo`. Must be left unset for
                one-time chores, which the API rejects with ``400 one-time
                chores should not have a value for apply_to``.
        """
        await self.request(
            "DELETE",
            f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}",
            json=_body(apply_to=apply_to) or None,
        )

    async def move_chore(
        self,
        frame_id: str | int,
        chore_id: str | int,
        *,
        before: str | int | None = None,
        after: str | int | None = None,
    ) -> _JSON:
        """Reorder a chore relative to another one.

        Position is expressed as a neighbour, not an index: pass exactly one of
        ``before`` or ``after``. The API rejects anything else with
        ``422 position must include at least one of `before` or `after```.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The chore to move.
            before: Place it immediately before this chore id.
            after: Place it immediately after this chore id.
        """
        if (before is None) == (after is None):
            raise ValueError("pass exactly one of `before` or `after`")
        position = {"before": str(before)} if before is not None else {"after": str(after)}
        return await self.request(
            "POST",
            f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}/move",
            json={"position": position},
        )

    async def set_chore_status(
        self,
        frame_id: str | int,
        chore_id: str | int,
        status: str,
        *,
        instance_date: date | str | None = None,
        **fields: Any,
    ) -> Chore:
        """Mark a chore complete or pending.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id — :attr:`Chore.chore_id`.
            status: ``"complete"`` or ``"pending"``. See
                :class:`~pyskylight.models.ChoreStatus`; note the API accepts
                ``"complete"``, not ``"completed"``.
            instance_date: Which occurrence to act on. Required for recurring
                chores (``422 instance_date can't be blank``) and rejected for
                one-time ones (``422 instance_date must be blank``).
            **fields: Any other completion fields, passed through as-is.

        Note:
            Whether ``category_id`` belongs here depends on the chore, and
            getting it wrong is a 422 either way:

            * An **assigned** chore rejects it. The completion is credited to
              the chore's own category automatically.
            * An **up-for-grabs** chore requires it — that is how the API
              records who claimed the chore, since it belongs to nobody. See
              :attr:`~pyskylight.models.Chore.up_for_grabs`.
        """
        return Chore.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}/completions",
                json=_body(
                    status=status,
                    instance_date=_fmt(instance_date) if instance_date is not None else None,
                    **fields,
                ),
            )
        )

    async def complete_chore(
        self,
        frame_id: str | int,
        chore_id: str | int,
        *,
        instance_date: date | str | None = None,
        **fields: Any,
    ) -> Chore:
        """Mark a chore complete.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id.
            instance_date: Required for recurring chores, rejected for one-time.
            **fields: Any other completion fields.
        """
        return await self.set_chore_status(
            frame_id, chore_id, ChoreStatus.COMPLETE, instance_date=instance_date, **fields
        )

    async def uncomplete_chore(
        self,
        frame_id: str | int,
        chore_id: str | int,
        *,
        instance_date: date | str | None = None,
        **fields: Any,
    ) -> Chore:
        """Mark a chore pending again.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id.
            instance_date: Required for recurring chores, rejected for one-time.
            **fields: Any other completion fields.
        """
        return await self.set_chore_status(
            frame_id, chore_id, ChoreStatus.PENDING, instance_date=instance_date, **fields
        )

    # --------------------------------------------------------------- task box

    async def get_task_box_items(self, frame_id: str | int) -> list[TaskBoxItem]:
        """List items in the frame's task box (inbox)."""
        return TaskBoxItem.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/task_box/items")
        )

    async def create_task_box_item(
        self,
        frame_id: str | int,
        summary: str,
        *,
        emoji_icon: str | None = None,
        routine: bool | None = None,
        reward_points: int | None = None,
        **attributes: Any,
    ) -> TaskBoxItem:
        """Add an item to the task box."""
        return TaskBoxItem.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/task_box/items",
                json=_body(
                    summary=summary,
                    emoji_icon=emoji_icon,
                    routine=routine,
                    reward_points=reward_points,
                    **attributes,
                ),
            )
        )

    async def update_task_box_item(
        self, frame_id: str | int, item_id: str | int, **attributes: Any
    ) -> TaskBoxItem:
        """Update a task box item."""
        return TaskBoxItem.one_from_document(
            await self.request(
                "PATCH",
                f"{API_PREFIX}/frames/{frame_id}/task_box/items/{item_id}",
                json=dict(attributes),
            )
        )

    async def delete_task_box_item(self, frame_id: str | int, item_id: str | int) -> None:
        """Delete a task box item."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/task_box/items/{item_id}")

    async def get_task_notification_settings(self, frame_id: str | int) -> _JSON:
        """Get task notification settings."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/task_notification_settings")

    async def update_task_notification_settings(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Update task notification settings."""
        return await self.request(
            "PATCH",
            f"{API_PREFIX}/frames/{frame_id}/task_notification_settings",
            json=fields,
        )

    # ------------------------------------------------------------------ lists

    async def get_lists(self, frame_id: str | int) -> list[SkylightList]:
        """List the frame's grocery and to-do lists.

        The list resources carry item ids but not the items themselves; call
        :meth:`get_list` for a list's contents.
        """
        return SkylightList.from_document(await self._get(f"{API_PREFIX}/frames/{frame_id}/lists"))

    async def get_list(self, frame_id: str | int, list_id: str | int) -> SkylightList:
        """Get a list, with its items and sections resolved."""
        payload = await self._get(f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}")
        document = Document(payload)
        sections = document.meta.get("sections")
        return dataclasses.replace(
            SkylightList.one_from_document(payload),
            items=[ListItem.from_resource(r) for r in document.included_of("list_item")],
            sections=sections if isinstance(sections, list) else [],
        )

    async def create_list(
        self,
        frame_id: str | int,
        label: str,
        kind: str,
        color: str,
        **attributes: Any,
    ) -> SkylightList:
        """Create a list.

        Args:
            frame_id: The frame to create the list on.
            label: List name.
            kind: ``"shopping"`` or ``"to_do"``. See
                :class:`~pyskylight.models.ListKind`. Required by the API.
            color: Hex color, e.g. ``"#00526D"``. Required and validated;
                :meth:`get_colors` returns the supported palette.
            **attributes: Any other list attributes, passed through as-is.
        """
        return SkylightList.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/lists",
                json=_body(label=label, kind=kind, color=color, **attributes),
            )
        )

    async def update_list(
        self, frame_id: str | int, list_id: str | int, **attributes: Any
    ) -> SkylightList:
        """Update a list."""
        return SkylightList.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}",
                json=dict(attributes),
            )
        )

    async def delete_list(self, frame_id: str | int, list_id: str | int) -> None:
        """Delete a list."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}")

    async def get_list_items(self, frame_id: str | int, list_id: str | int) -> list[ListItem]:
        """List a list's items."""
        return ListItem.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items")
        )

    async def create_list_item(
        self,
        frame_id: str | int,
        list_id: str | int,
        label: str,
        *,
        section: str | None = None,
        **attributes: Any,
    ) -> ListItem:
        """Add an item to a list."""
        return ListItem.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items",
                json=_body(label=label, section=section, **attributes),
            )
        )

    async def update_list_item(
        self,
        frame_id: str | int,
        list_id: str | int,
        item_id: str | int,
        **attributes: Any,
    ) -> ListItem:
        """Update a list item."""
        return ListItem.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items/{item_id}",
                json=dict(attributes),
            )
        )

    async def set_list_item_status(
        self, frame_id: str | int, list_id: str | int, item_id: str | int, status: str
    ) -> ListItem:
        """Check or uncheck a list item.

        Args:
            frame_id: The frame the list belongs to.
            list_id: The list the item belongs to.
            item_id: The item to update.
            status: See :class:`~pyskylight.models.ListItemStatus`.
        """
        return await self.update_list_item(frame_id, list_id, item_id, status=status)

    async def delete_list_item(
        self, frame_id: str | int, list_id: str | int, item_id: str | int
    ) -> None:
        """Delete a list item."""
        await self.request(
            "DELETE", f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items/{item_id}"
        )

    async def delete_list_items(
        self, frame_id: str | int, list_id: str | int, item_ids: Sequence[str | int]
    ) -> None:
        """Bulk delete list items."""
        await self.request(
            "DELETE",
            f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items/bulk_destroy",
            json={"ids": [str(i) for i in item_ids]},
        )

    async def move_list_item(
        self,
        frame_id: str | int,
        list_id: str | int,
        item_id: str | int,
        **fields: Any,
    ) -> _JSON:
        """Move a list item to a new position."""
        return await self.request(
            "POST",
            f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items/{item_id}/move",
            json=fields,
        )

    async def move_list_items_to_section(
        self,
        frame_id: str | int,
        list_id: str | int,
        item_ids: Sequence[str | int],
        section: str,
    ) -> _JSON:
        """Bulk move list items into a section."""
        return await self.request(
            "PUT",
            f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items/bulk_update_section",
            json={"ids": [str(i) for i in item_ids], "section": section},
        )

    # -------------------------------------------------------- calendar events

    async def get_calendar_events(
        self,
        frame_id: str | int,
        date_min: date | str,
        date_max: date | str,
        *,
        timezone: str | None = None,
        include: Sequence[str] | str | None = None,
    ) -> list[CalendarEvent]:
        """List calendar events in a date range.

        Args:
            frame_id: The frame to query.
            date_min: Start of the range (required by the API).
            date_max: End of the range (required by the API).
            timezone: IANA timezone name used to resolve all-day boundaries.
            include: Related resources to side-load, e.g.
                ``["categories", "calendar_account"]``.
        """
        return CalendarEvent.from_document(
            await self._get(
                f"{API_PREFIX}/frames/{frame_id}/calendar_events",
                date_min=date_min,
                date_max=date_max,
                timezone=timezone,
                include=include,
            )
        )

    async def create_calendar_event(self, frame_id: str | int, **fields: Any) -> CalendarEvent:
        """Create a calendar event."""
        return CalendarEvent.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/calendar_events",
                json=dict(fields),
            )
        )

    async def update_calendar_event(
        self, frame_id: str | int, event_id: str | int, **fields: Any
    ) -> CalendarEvent:
        """Update a calendar event."""
        return CalendarEvent.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/calendar_events/{event_id}",
                json=dict(fields),
            )
        )

    async def delete_calendar_event(self, frame_id: str | int, event_id: str | int) -> None:
        """Delete a calendar event."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/calendar_events/{event_id}")

    async def search_calendar_events(self, frame_id: str | int, query: str) -> list[CalendarEvent]:
        """Search calendar events by text."""
        return CalendarEvent.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/calendar_events/search", q=query)
        )

    async def get_countdowns(self, frame_id: str | int, timezone: str) -> list[CalendarEvent]:
        """List countdown events.

        Args:
            frame_id: The frame to query.
            timezone: IANA timezone name. Required by the API — a missing value
                is rejected with ``422 Timezone is required``. The frame's own
                :attr:`Frame.timezone` is the usual choice.
        """
        return CalendarEvent.from_document(
            await self._get(
                f"{API_PREFIX}/frames/{frame_id}/calendar_events/countdowns",
                timezone=timezone,
            )
        )

    async def get_source_calendars(self, frame_id: str | int) -> list[SourceCalendar]:
        """List calendars synced into a frame."""
        return SourceCalendar.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/source_calendars")
        )

    async def get_source_calendar(
        self, frame_id: str | int, calendar_id: str | int
    ) -> SourceCalendar:
        """Get one synced calendar."""
        return SourceCalendar.one_from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/source_calendars/{calendar_id}")
        )

    async def update_source_calendar(
        self, frame_id: str | int, calendar_id: str | int, **fields: Any
    ) -> SourceCalendar:
        """Update a synced calendar."""
        return SourceCalendar.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/source_calendars/{calendar_id}",
                json=dict(fields),
            )
        )

    async def delete_source_calendar(self, frame_id: str | int, calendar_id: str | int) -> None:
        """Remove a synced calendar."""
        await self.request(
            "DELETE", f"{API_PREFIX}/frames/{frame_id}/source_calendars/{calendar_id}"
        )

    async def get_calendar_accounts(self, frame_id: str | int) -> _JSON:
        """List connected calendar accounts (Google, Apple, ...)."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/calendars")

    async def add_webcal(self, frame_id: str | int, url: str, **fields: Any) -> _JSON:
        """Subscribe the frame to a WebCal/iCal URL."""
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/webcal_accounts", json={"url": url, **fields}
        )

    async def get_event_notification_settings(self, frame_id: str | int) -> _JSON:
        """Get event notification settings."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/event_notification_settings")

    async def update_event_notification_settings(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Update event notification settings."""
        return await self.request(
            "PUT",
            f"{API_PREFIX}/frames/{frame_id}/event_notification_settings",
            json=fields,
        )

    # ---------------------------------------------------------------- devices

    async def get_devices(self, frame_id: str | int) -> list[Device]:
        """List physical devices registered to a frame."""
        return Device.from_document(await self._get(f"{API_PREFIX}/frames/{frame_id}/devices"))

    async def get_device(self, frame_id: str | int, device_id: str | int) -> Device:
        """Get one device."""
        return Device.one_from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}")
        )

    async def update_device(
        self, frame_id: str | int, device_id: str | int, **fields: Any
    ) -> Device:
        """Change settings on a physical display.

        This is where display settings live. ``PUT /api/frames/{id}`` accepts
        the same field names and returns ``200``, but silently applies nothing —
        see :meth:`update_frame`.

        Verified writable against a live display: ``brightness``, ``nightlight``,
        ``nightlight_brightness``, ``sleep_sound_volume``, ``sleeps_at``,
        ``wakes_at``, ``slideshow_speed``, ``show_caption``, ``blur_effect``,
        ``side_by_side``, ``show_heart``, and ``name``.

        ``nightlight_color`` is an enum — see
        :class:`~pyskylight.models.NightlightColor`. ``sleep_mode`` accepts only
        its current value; anything else returns a 500.

        Warning:
            Writable is not the same as supported. The ``nightlight*`` and
            ``sleep_sound*`` fields are Skylight Buddy settings, and a calendar
            display still returns them, accepts writes, persists them, and
            validates the colour enum — a ``200`` here proves only that the
            server stored the value. Unlike :meth:`create_alarm`, nothing is
            rejected. Skylight's own client offers these controls only for a
            device whose ``role`` is ``"buddy"``, and never touches
            ``nightlight_color`` at all. Gate on :attr:`Device.role`; see
            ``docs/api-notes.md``.

        Args:
            frame_id: The frame the device belongs to.
            device_id: The device to update.
            **fields: Device fields to change.
        """
        return Device.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}",
                json=dict(fields),
            )
        )

    async def rename_device(self, frame_id: str | int, device_id: str | int, name: str) -> Device:
        """Rename a device."""
        return await self.update_device(frame_id, device_id, name=name)

    async def delete_device(self, frame_id: str | int, device_id: str | int) -> None:
        """Remove a device from the frame."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}")

    async def get_alarms(self, frame_id: str | int, device_id: str | int) -> list[Alarm]:
        """List alarms configured on a device."""
        return Alarm.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}/alarms")
        )

    async def create_alarm(self, frame_id: str | int, device_id: str | int, **fields: Any) -> _JSON:
        """Create an alarm on a device.

        Warning:
            Alarms are a Skylight Buddy feature. On a calendar display this
            returns ``422 Device must be a buddy device`` regardless of the
            body — the check runs before field validation, so the accepted
            fields are unknown. Verified against a ``15-CAL-2.0``.
        """
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}/alarms", json=fields
        )

    async def update_alarm(
        self,
        frame_id: str | int,
        device_id: str | int,
        alarm_id: str | int,
        **fields: Any,
    ) -> _JSON:
        """Update an alarm."""
        return await self.request(
            "PATCH",
            f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}/alarms/{alarm_id}",
            json=fields,
        )

    async def delete_alarm(
        self, frame_id: str | int, device_id: str | int, alarm_id: str | int
    ) -> None:
        """Delete an alarm."""
        await self.request(
            "DELETE",
            f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}/alarms/{alarm_id}",
        )

    # ---------------------------------------------------------------- rewards

    async def get_rewards(
        self, frame_id: str | int, *, redeemed_at_min: datetime | str | None = None
    ) -> list[Reward]:
        """List rewards.

        Args:
            frame_id: The frame to query.
            redeemed_at_min: Only include rewards redeemed at or after this time.
        """
        return Reward.from_document(
            await self._get(
                f"{API_PREFIX}/frames/{frame_id}/rewards", redeemed_at_min=redeemed_at_min
            )
        )

    async def get_reward(self, frame_id: str | int, reward_id: str | int) -> Reward:
        """Get one reward."""
        return Reward.one_from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/rewards/{reward_id}")
        )

    async def create_rewards(
        self,
        frame_id: str | int,
        name: str,
        point_value: int,
        category_ids: Sequence[str | int],
        **fields: Any,
    ) -> list[Reward]:
        """Create a reward for one or more family profiles.

        Args:
            frame_id: The frame to create the reward on.
            name: Reward name.
            point_value: Points needed to redeem it.
            category_ids: Profiles the reward applies to. Required — the API
                rejects a missing list with ``422 Category ids is required``.
            **fields: Any other reward fields, passed through as-is.

        Returns:
            The created rewards — one per profile.
        """
        return Reward.from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/rewards",
                json=_body(
                    name=name,
                    point_value=point_value,
                    category_ids=[str(c) for c in category_ids],
                    **fields,
                ),
            )
        )

    async def update_reward(
        self, frame_id: str | int, reward_id: str | int, **fields: Any
    ) -> _JSON:
        """Update a reward."""
        return await self.request(
            "PATCH", f"{API_PREFIX}/frames/{frame_id}/rewards/{reward_id}", json=fields
        )

    async def delete_reward(self, frame_id: str | int, reward_id: str | int) -> None:
        """Delete a reward."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/rewards/{reward_id}")

    async def redeem_reward(
        self, frame_id: str | int, reward_id: str | int, **fields: Any
    ) -> _JSON:
        """Redeem a reward."""
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/rewards/{reward_id}/redeem", json=fields
        )

    async def unredeem_reward(
        self, frame_id: str | int, reward_id: str | int, **fields: Any
    ) -> _JSON:
        """Undo a reward redemption."""
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/rewards/{reward_id}/unredeem", json=fields
        )

    async def get_reward_points(self, frame_id: str | int) -> list[RewardPoint]:
        """Get per-profile reward point balances.

        This endpoint returns a plain JSON array, not a JSON:API document.
        """
        return RewardPoint.from_response(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/reward_points")
        )

    async def update_reward_points(
        self, frame_id: str | int, category_ids: Sequence[str | int], points: int, **fields: Any
    ) -> _JSON:
        """Award or adjust reward points for one or more profiles.

        Args:
            frame_id: The frame to update.
            category_ids: Profiles to credit. Required — the API rejects a
                missing list with ``422 Category ids is required``.
            points: Points to add. Negative values subtract.
            **fields: Any other fields, passed through as-is.
        """
        return await self.request(
            "POST",
            f"{API_PREFIX}/frames/{frame_id}/reward_points",
            json=_body(category_ids=[str(c) for c in category_ids], points=points, **fields),
        )

    # ----------------------------------------------------------------- nudges

    async def get_nudges(
        self, frame_id: str | int, after: date | str, before: date | str
    ) -> list[Nudge]:
        """List nudges (reminders) in a date range.

        Args:
            frame_id: The frame to query.
            after: Earliest date to include.
            before: Upper bound, which behaves as midnight UTC on that date
                rather than as an inclusive day — pass the day *after* the last
                one you want. Both bounds are required by the API, which rejects
                a missing one with ``422 After/Before is required``.

        Note:
            Delivered nudges are not cleaned up, so this is a history as well as
            a schedule.
        """
        return Nudge.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/nudges", after=after, before=before)
        )

    async def create_nudge(
        self,
        frame_id: str | int,
        body: str,
        deliver_at: datetime | str,
        category_ids: Sequence[str | int],
        **fields: Any,
    ) -> Nudge:
        """Create a nudge — a spoken reminder played on the frame.

        Args:
            frame_id: The frame to create the nudge on.
            body: What the nudge says. Required (``422 Body can't be blank``).
            deliver_at: When to play it. Required (``422 Deliver at can't be
                blank``), but not otherwise validated: a time in the past is
                accepted without complaint.
            category_ids: Profiles the nudge is for. Required; an empty list is
                ``422 Category ids is required``.
            **fields: Any other nudge fields — ``recurring``, ``rrule``,
                ``recurring_until``, ``voice_kind``, ``audio_url``.

        Note:
            The speech is rendered in the cloud: the returned nudge has a null
            ``audio_url``, which fills in with a presigned MP3 URL within about
            ten seconds. ``voice_kind`` defaults to ``kirk_voice``; an unknown
            value returns a 500 rather than a validation error, and no endpoint
            lists the valid voices.

        Warning:
            A calendar display never plays a nudge. Two sent to a real
            ``15-CAL-2.0`` — one for the current moment, one scheduled ahead —
            were created, rendered, and listed, and neither was ever heard. This
            looks like the Skylight Buddy split that alarms make explicit, except
            that nudges hang off the frame rather than a device, so there is
            nothing to reject the write. Success here says the resource exists,
            not that anyone will hear it.
        """
        return Nudge.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/nudges",
                json=_body(
                    body=body,
                    deliver_at=_fmt(deliver_at),
                    category_ids=[str(c) for c in category_ids],
                    **fields,
                ),
            )
        )

    async def update_nudge(self, frame_id: str | int, nudge_id: str | int, **fields: Any) -> _JSON:
        """Update a nudge."""
        return await self.request(
            "PATCH", f"{API_PREFIX}/frames/{frame_id}/nudges/{nudge_id}", json=fields
        )

    async def delete_nudge(self, frame_id: str | int, nudge_id: str | int) -> None:
        """Delete a nudge."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/nudges/{nudge_id}")

    # ------------------------------------------------------ photos & messages

    async def get_messages(self, frame_id: str | int, **params: Any) -> list[Message]:
        """List the frame's photo feed, newest first.

        Args:
            frame_id: The frame to query.
            **params: Query parameters. ``page`` selects a page; the size is
                fixed at 30 and is not negotiable — ``per_page`` and ``limit``
                are both accepted and ignored. The response's
                ``meta.num_pages`` says how many there are.
        """
        return Message.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/messages", **params)
        )

    async def get_message(self, frame_id: str | int, message_id: str | int) -> Message:
        """Get one message."""
        return Message.one_from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/messages/{message_id}")
        )

    async def update_message_caption(
        self, frame_id: str | int, message_id: str | int, caption: str
    ) -> _JSON:
        """Update a photo caption."""
        return await self.request(
            "PUT",
            f"{API_PREFIX}/frames/{frame_id}/messages/{message_id}/caption",
            json={"caption": caption},
        )

    async def delete_message(self, frame_id: str | int, message_id: str | int) -> None:
        """Delete a message."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/messages/{message_id}")

    async def get_albums(self, frame_id: str | int) -> _JSON:
        """List albums."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/albums")

    async def create_album(self, frame_id: str | int, title: str, **fields: Any) -> _JSON:
        """Create an album.

        Args:
            frame_id: The frame to create the album on.
            title: Album title. The field is ``title``, not ``name``.
            **fields: Any other album fields, passed through as-is.
        """
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/albums", json=_body(title=title, **fields)
        )

    async def delete_album(self, frame_id: str | int, album_id: str | int) -> None:
        """Delete an album."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/albums/{album_id}")

    # ------------------------------------------------------------------ meals

    async def get_meal_categories(self, frame_id: str | int) -> list[MealCategory]:
        """List the meal planner's slots — Breakfast, Lunch, Dinner, Snack."""
        return MealCategory.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/meals/categories")
        )

    async def get_meal_recipes(self, frame_id: str | int) -> list[Recipe]:
        """List recipes, with their meal categories side-loaded."""
        return Recipe.from_document(
            await self._get(
                f"{API_PREFIX}/frames/{frame_id}/meals/recipes", include="meal_category"
            )
        )

    async def create_meal_recipe(
        self, frame_id: str | int, summary: str, meal_category_id: str | int, **fields: Any
    ) -> Recipe:
        """Create a recipe.

        Args:
            frame_id: The frame to create the recipe on.
            summary: The recipe's name — the API has no ``title`` field.
            meal_category_id: Which planner slot it belongs to. Required; without
                it the call is a bare ``422`` naming no field.
            **fields: Any other recipe fields — chiefly ``description``, the free
                text holding both ingredients and method.
        """
        return Recipe.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/meals/recipes",
                params=_params(include="meal_category"),
                json=_body(summary=summary, meal_category_id=str(meal_category_id), **fields),
            )
        )

    async def add_recipe_to_grocery_list(self, frame_id: str | int, recipe_id: str | int) -> _JSON:
        """Add a recipe's ingredients to the frame's default grocery list.

        Returns:
            The recipe, with the queued job's id under
            ``meta.auto_creation_intent_id``.

        Note:
            The work happens **after** the response. Skylight parses the
            ingredients out of the free-text ``description`` server-side, and the
            items appear on the list a few seconds later — about ten in
            practice. A caller that re-reads the list immediately sees nothing.

        Warning:
            The destination is not a choice. Ingredients always land on the list
            whose :attr:`~pyskylight.models.SkylightList.default_grocery_list` is
            set, verified on a frame carrying two shopping lists: the second
            stayed empty.
        """
        return await self.request(
            "POST",
            f"{API_PREFIX}/frames/{frame_id}/meals/recipes/{recipe_id}/add_to_grocery_list",
        )

    async def get_meal_sittings(
        self, frame_id: str | int, date_min: date | str, date_max: date | str
    ) -> _JSON:
        """Get meal plan slots in a date range.

        Args:
            frame_id: The frame to query.
            date_min: Start of the range. Required by the API — a missing value
                is rejected with ``422 Date min is required``.
            date_max: End of the range.
        """
        return await self._get(
            f"{API_PREFIX}/frames/{frame_id}/meals/sittings",
            date_min=date_min,
            date_max=date_max,
        )

    # -------------------------------------------------------------- utilities

    async def get_activities(self) -> _JSON:
        """Get the activity feed."""
        return await self._get(f"{API_PREFIX}/activities")

    async def get_colors(self) -> _JSON:
        """Get the color palette used for profiles and lists."""
        return await self._get(f"{API_PREFIX}/colors")

    async def get_avatars(self) -> _JSON:
        """Get the available profile avatars."""
        return await self._get(f"{API_PREFIX}/avatars")


class _Retry:
    """Sentinel telling :meth:`Skylight.request` to refresh and try again."""

    __slots__ = ()


_RETRY = _Retry()


def _flatten_errors(errors: Any) -> list[str]:
    """Reduce an ``errors`` payload to a flat list of readable strings.

    The API uses two shapes and does not say which it will pick. A whole-request
    complaint arrives as a list::

        {"errors": ["only repeating chores can be skipped"]}

    while a per-field one arrives as a mapping of field to complaints, which is
    what the chore completions endpoint returns::

        {"errors": {"instance_date": ["must be blank"]}}

    The field name is the useful half of that second shape — "must be blank"
    alone says nothing — so it is kept, joined to its message the way the
    sentence reads.
    """
    if isinstance(errors, dict):
        return [
            f"{field} {message}" if isinstance(message, str) else f"{field} {message!r}"
            for field, messages in errors.items()
            for message in (messages if isinstance(messages, list) else [messages])
        ]
    if isinstance(errors, list):
        out = []
        for error in errors:
            if isinstance(error, str):
                out.append(error)
            elif isinstance(error, dict):
                out.append(str(error.get("detail") or error.get("title") or error))
            else:
                out.append(str(error))
        return out
    return []


def _error_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    if messages := _flatten_errors(body.get("errors")):
        return "; ".join(messages)
    for key in ("error_description", "error", "message"):
        if isinstance(value := body.get(key), str):
            return value
    return None
