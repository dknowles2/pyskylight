"""Async client for the (unofficial) Skylight API."""

from __future__ import annotations

import dataclasses
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
    ApplyTo,
    CalendarEvent,
    Category,
    Chore,
    ChoreGroups,
    Device,
    Frame,
    ListItem,
    Nudge,
    Reward,
    RewardPoint,
    SkylightList,
    SourceCalendar,
    TaskBoxItem,
    User,
)

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


def _body(**kwargs: Any) -> _JSON:
    """Drop ``None`` values from a request body."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _resource(
    resource_type: str,
    attributes: Mapping[str, Any],
    relationships: Mapping[str, Any] | None = None,
) -> _JSON:
    """Wrap attributes in a JSON:API single-resource request document."""
    data: _JSON = {"type": resource_type, "attributes": dict(attributes)}
    if relationships:
        data["relationships"] = dict(relationships)
    return {"data": data}


def _to_one(resource_type: str, resource_id: str | int | None) -> _JSON | None:
    if resource_id is None:
        return None
    return {"data": {"type": resource_type, "id": str(resource_id)}}


class Skylight:
    """An async Skylight API client.

    Args:
        auth: Supplies the bearer token. See :class:`~pylight.auth.PasswordAuth`
            and :class:`~pylight.auth.TokenAuth`.
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
        async with self._get_session().request(
            method,
            url,
            params=dict(params) if params else None,
            json=json,
            headers=self._headers(token),
            timeout=self._timeout,
        ) as resp:
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
            errors = raw_errors if isinstance(raw_errors, list) else None
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
        """Update frame settings."""
        return await self.request("PUT", f"{API_PREFIX}/frames/{frame_id}", json=fields)

    async def rename_frame(self, frame_id: str | int, name: str) -> _JSON:
        """Rename a frame."""
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
        *,
        color: str | None = None,
        **attributes: Any,
    ) -> Category:
        """Create a family member profile."""
        payload = _resource("category", _body(label=label, color=color, **attributes))
        return Category.one_from_document(
            await self.request("POST", f"{API_PREFIX}/frames/{frame_id}/categories", json=payload)
        )

    async def update_category(
        self, frame_id: str | int, category_id: str | int, **attributes: Any
    ) -> Category:
        """Update a family member profile."""
        payload = _resource("category", attributes)
        return Category.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/categories/{category_id}",
                json=payload,
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
        :class:`~pylight.models.ChoreGroups`.
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
        *,
        start: date | str | None = None,
        start_time: str | None = None,
        status: str | None = None,
        recurring: bool | None = None,
        recurrence_set: str | None = None,
        category_id: str | int | None = None,
        **attributes: Any,
    ) -> Chore:
        """Create a single chore.

        Args:
            frame_id: The frame to create the chore on.
            summary: Chore title.
            start: Start date.
            start_time: Time of day, e.g. ``"10:00"``.
            status: Initial status, usually ``"pending"``.
            recurring: Whether the chore repeats.
            recurrence_set: An RRULE string, e.g.
                ``"RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"``.
            category_id: Family profile to assign the chore to.
            **attributes: Any other chore attributes, passed through as-is.
        """
        payload = _resource(
            "chore",
            _body(
                summary=summary,
                start=_fmt(start) if start is not None else None,
                start_time=start_time,
                status=status,
                recurring=recurring,
                recurrence_set=recurrence_set,
                **attributes,
            ),
            _body(category=_to_one("category", category_id)),
        )
        return Chore.one_from_document(
            await self.request("POST", f"{API_PREFIX}/frames/{frame_id}/chores", json=payload)
        )

    async def create_chores(
        self, frame_id: str | int, chores: Sequence[Mapping[str, Any]]
    ) -> _JSON:
        """Create several chores in one call.

        Args:
            frame_id: The frame to create the chores on.
            chores: Chore field mappings, e.g. ``[{"summary": "Dishes", ...}]``.
        """
        return await self.request(
            "POST",
            f"{API_PREFIX}/frames/{frame_id}/chores/create_multiple",
            json={"chores": [dict(c) for c in chores]},
        )

    async def update_chore(
        self,
        frame_id: str | int,
        chore_id: str | int,
        *,
        apply_to: str = ApplyTo.THIS,
        **fields: Any,
    ) -> _JSON:
        """Update a chore.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id — :attr:`Chore.chore_id`, not the
                per-occurrence :attr:`Chore.id`.
            apply_to: Recurrence scope. See :class:`~pylight.models.ApplyTo`.
            **fields: Chore fields to change.
        """
        return await self.request(
            "PUT",
            f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}",
            json={**fields, "apply_to": apply_to},
        )

    async def delete_chore(
        self, frame_id: str | int, chore_id: str | int, *, apply_to: str = ApplyTo.THIS
    ) -> None:
        """Delete a chore, for the given recurrence scope."""
        await self.request(
            "DELETE",
            f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}",
            json={"apply_to": apply_to},
        )

    async def move_chore(self, frame_id: str | int, chore_id: str | int, **fields: Any) -> _JSON:
        """Reorder or move a chore."""
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}/move", json=fields
        )

    async def set_chore_status(
        self,
        frame_id: str | int,
        chore_id: str | int,
        status: str,
        *,
        instance_date: date | str | None = None,
        instance_time: str | None = None,
        category_id: str | int | None = None,
        completed_on: datetime | str | None = None,
    ) -> _JSON:
        """Mark a chore occurrence complete, incomplete, or skipped.

        Args:
            frame_id: The frame the chore belongs to.
            chore_id: The underlying chore id.
            status: See :class:`~pylight.models.ChoreStatus`.
            instance_date: Which occurrence, for recurring chores.
            instance_time: Occurrence time of day, if the chore has one.
            category_id: Which family profile completed it.
            completed_on: Completion timestamp.
        """
        return await self.request(
            "PUT",
            f"{API_PREFIX}/frames/{frame_id}/chores/{chore_id}/completions",
            json=_body(
                status=status,
                instance_date=_fmt(instance_date) if instance_date is not None else None,
                instance_time=instance_time,
                category_id=str(category_id) if category_id is not None else None,
                completed_on=_fmt(completed_on) if completed_on is not None else None,
            ),
        )

    async def complete_chore(
        self, frame_id: str | int, chore_id: str | int, **kwargs: Any
    ) -> _JSON:
        """Mark a chore occurrence complete."""
        return await self.set_chore_status(frame_id, chore_id, "completed", **kwargs)

    async def uncomplete_chore(
        self, frame_id: str | int, chore_id: str | int, **kwargs: Any
    ) -> _JSON:
        """Mark a chore occurrence pending again."""
        return await self.set_chore_status(frame_id, chore_id, "pending", **kwargs)

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
        payload = _resource(
            "task_box_item",
            _body(
                summary=summary,
                emoji_icon=emoji_icon,
                routine=routine,
                reward_points=reward_points,
                **attributes,
            ),
        )
        return TaskBoxItem.one_from_document(
            await self.request(
                "POST", f"{API_PREFIX}/frames/{frame_id}/task_box/items", json=payload
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
                json=_resource("task_box_item", attributes),
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
        *,
        kind: str | None = None,
        color: str | None = None,
        **attributes: Any,
    ) -> SkylightList:
        """Create a list.

        Args:
            frame_id: The frame to create the list on.
            label: List name.
            kind: ``"shopping"`` or ``"to_do"``. See
                :class:`~pylight.models.ListKind`.
            color: Hex color, e.g. ``"#A6A6BE"``.
            **attributes: Any other list attributes, passed through as-is.
        """
        payload = _resource("list", _body(label=label, kind=kind, color=color, **attributes))
        return SkylightList.one_from_document(
            await self.request("POST", f"{API_PREFIX}/frames/{frame_id}/lists", json=payload)
        )

    async def update_list(
        self, frame_id: str | int, list_id: str | int, **attributes: Any
    ) -> SkylightList:
        """Update a list."""
        return SkylightList.one_from_document(
            await self.request(
                "PUT",
                f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}",
                json=_resource("list", attributes),
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
        payload = _resource("list_item", _body(label=label, section=section, **attributes))
        return ListItem.one_from_document(
            await self.request(
                "POST",
                f"{API_PREFIX}/frames/{frame_id}/lists/{list_id}/list_items",
                json=payload,
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
                json=_resource("list_item", attributes),
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
            status: See :class:`~pylight.models.ListItemStatus`.
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
                json=_resource("calendar_event", fields),
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
                json=_resource("calendar_event", fields),
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
                json=_resource("source_calendar", fields),
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

    async def rename_device(self, frame_id: str | int, device_id: str | int, name: str) -> _JSON:
        """Rename a device."""
        return await self.request(
            "PUT", f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}", json={"name": name}
        )

    async def delete_device(self, frame_id: str | int, device_id: str | int) -> None:
        """Remove a device from the frame."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}")

    async def get_alarms(self, frame_id: str | int, device_id: str | int) -> list[Alarm]:
        """List alarms configured on a device."""
        return Alarm.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/devices/{device_id}/alarms")
        )

    async def create_alarm(self, frame_id: str | int, device_id: str | int, **fields: Any) -> _JSON:
        """Create an alarm on a device."""
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

    async def create_rewards(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Create one or more rewards."""
        return await self.request("POST", f"{API_PREFIX}/frames/{frame_id}/rewards", json=fields)

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

    async def update_reward_points(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Award or adjust reward points."""
        return await self.request(
            "POST", f"{API_PREFIX}/frames/{frame_id}/reward_points", json=fields
        )

    # ----------------------------------------------------------------- nudges

    async def get_nudges(
        self, frame_id: str | int, after: date | str, before: date | str
    ) -> list[Nudge]:
        """List nudges (reminders) in a date range.

        Args:
            frame_id: The frame to query.
            after: Earliest date to include.
            before: Latest date to include. Both bounds are required by the API,
                which rejects a missing one with ``422 After/Before is required``.
        """
        return Nudge.from_document(
            await self._get(f"{API_PREFIX}/frames/{frame_id}/nudges", after=after, before=before)
        )

    async def create_nudge(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Create a nudge."""
        return await self.request("POST", f"{API_PREFIX}/frames/{frame_id}/nudges", json=fields)

    async def update_nudge(self, frame_id: str | int, nudge_id: str | int, **fields: Any) -> _JSON:
        """Update a nudge."""
        return await self.request(
            "PATCH", f"{API_PREFIX}/frames/{frame_id}/nudges/{nudge_id}", json=fields
        )

    async def delete_nudge(self, frame_id: str | int, nudge_id: str | int) -> None:
        """Delete a nudge."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/nudges/{nudge_id}")

    # ------------------------------------------------------ photos & messages

    async def get_messages(self, frame_id: str | int, **params: Any) -> _JSON:
        """List the frame's photo/message feed."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/messages", **params)

    async def get_message(self, frame_id: str | int, message_id: str | int) -> _JSON:
        """Get one message."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/messages/{message_id}")

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

    async def create_album(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Create an album."""
        return await self.request("POST", f"{API_PREFIX}/frames/{frame_id}/albums", json=fields)

    async def delete_album(self, frame_id: str | int, album_id: str | int) -> None:
        """Delete an album."""
        await self.request("DELETE", f"{API_PREFIX}/frames/{frame_id}/albums/{album_id}")

    # ------------------------------------------------------------------ meals

    async def get_meal_categories(self, frame_id: str | int) -> _JSON:
        """Get meal categories."""
        return await self._get(f"{API_PREFIX}/frames/{frame_id}/meals/categories")

    async def get_meal_recipes(self, frame_id: str | int) -> _JSON:
        """List recipes, with their meal categories side-loaded."""
        return await self._get(
            f"{API_PREFIX}/frames/{frame_id}/meals/recipes", include="meal_category"
        )

    async def create_meal_recipe(self, frame_id: str | int, **fields: Any) -> _JSON:
        """Create a recipe."""
        return await self.request(
            "POST",
            f"{API_PREFIX}/frames/{frame_id}/meals/recipes",
            params=_params(include="meal_category"),
            json=fields,
        )

    async def add_recipe_to_grocery_list(self, frame_id: str | int, recipe_id: str | int) -> _JSON:
        """Add a recipe's ingredients to the grocery list."""
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


def _error_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, str):
            return "; ".join(e for e in errors if isinstance(e, str))
        if isinstance(first, dict):
            return str(first.get("detail") or first.get("title") or first)
    for key in ("error_description", "error", "message"):
        if isinstance(value := body.get(key), str):
            return value
    return None
