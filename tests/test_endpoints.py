"""Every public endpoint method issues exactly one well-formed request.

This sweeps the client by introspection rather than listing methods by hand, so
a newly added endpoint is covered the moment it exists. It catches the mistakes
that unit tests for individual endpoints usually miss: a typo in an f-string
path, a missing path segment, or the wrong HTTP verb.
"""

import inspect
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, get_args, get_origin

import pytest

from pylight import Skylight, TokenAuth

# Satisfies every return decoder: `data` as a lone resource still yields a
# one-item list via Document.data_list, so list- and single-returning methods
# are both happy with it.
GENERIC_PAYLOAD: dict[str, Any] = {
    "data": {"type": "thing", "id": "1", "attributes": {}},
    "meta": {},
    "included": [],
}

# Methods that are plumbing rather than endpoints.
SKIP = {"close", "request"}

# Arguments that cannot be guessed from the signature alone.
OVERRIDES: dict[str, dict[str, Any]] = {
    # Requires exactly one of before/after, both keyword-only and optional.
    "move_chore": {"after": "2"},
}

# Endpoints whose HTTP verb doesn't follow from the method name. These are
# upstream quirks, verified against the live API.
VERB_EXCEPTIONS = {
    "update_reward_points": "POST",  # POST /reward_points awards points
    "move_list_items_to_section": "PUT",  # PUT .../bulk_update_section
    "update_task_box_item": "PATCH",
    "update_alarm": "PATCH",
    "update_nudge": "PATCH",
    "update_user_profile": "PATCH",
    "update_household_config": "PATCH",
    "update_task_notification_settings": "PATCH",
    "delete_list_items": "DELETE",
    "update_reward": "PATCH",  # PATCH /rewards/{id}
    "set_push_notifications": "PATCH",  # PATCH /user/push_toggler
    "set_marketing_emails": "PATCH",  # PATCH /user/klaviyo_toggler
}

EXPECTED_METHOD = {
    "get": "GET",
    "create": "POST",
    "update": "PUT",
    "delete": "DELETE",
    "set": "PUT",
    "add": "POST",
    "rename": "PUT",
    "redeem": "POST",
    "unredeem": "POST",
    "move": "POST",
    "complete": "PUT",
    "uncomplete": "PUT",
    "search": "GET",
}


def _value_for(name: str, annotation: Any) -> Any:
    """Invent a plausible argument from a parameter's name and type."""
    origin = get_origin(annotation)
    if origin in (list, Sequence) or annotation in (Sequence, list):
        return ["1"]
    args = get_args(annotation)
    if args and any(a in (date, datetime) for a in args):
        return "2026-01-01"
    if name.endswith("_ids"):
        return ["1"]
    if name.endswith("_id") or name == "id":
        return "1"
    if name in ("date_min", "date_max", "after", "before", "start", "deliver_at"):
        return "2026-01-01"
    if annotation is int or (args and int in args and str not in args):
        return 1
    if annotation is bool:
        return True
    return "x"


def _call_args(func: Any) -> tuple[list[Any], dict[str, Any]]:
    # eval_str resolves the string annotations that `from __future__ import
    # annotations` leaves behind, so int params get ints and not "x".
    signature = inspect.signature(func, eval_str=True)
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name == "self" or param.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if param.default is not inspect.Parameter.empty:
            continue  # optional: exercise the default
        value = _value_for(name, param.annotation)
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            keywords[name] = value
        else:
            positional.append(value)
    return positional, keywords


def _endpoint_methods() -> list[str]:
    return sorted(
        name
        for name, member in inspect.getmembers(Skylight, inspect.isfunction)
        if not name.startswith("_") and name not in SKIP and inspect.iscoroutinefunction(member)
    )


ENDPOINTS = _endpoint_methods()


def test_sweep_covers_the_whole_client():
    """Guard against the sweep silently going empty."""
    assert len(ENDPOINTS) > 60, ENDPOINTS


@pytest.mark.parametrize("name", ENDPOINTS)
async def test_endpoint_issues_one_well_formed_request(api, name):
    async with Skylight(TokenAuth("t"), base_url=api.url) as client:
        method = getattr(client, name)
        positional, keywords = _call_args(method)
        keywords.update(OVERRIDES.get(name, {}))
        api.queue(GENERIC_PAYLOAD)
        await method(*positional, **keywords)

    assert len(api.requests) == 1, f"{name} made {len(api.requests)} requests"
    request = api.requests[0]
    assert request.path.startswith("/api/"), request.path
    assert "//" not in request.path, f"{name} built {request.path}"
    assert "None" not in request.path, f"{name} interpolated a None: {request.path}"
    assert "{" not in request.path, f"{name} left an unformatted placeholder: {request.path}"

    verb = VERB_EXCEPTIONS.get(name) or EXPECTED_METHOD.get(name.split("_")[0])
    if verb:
        assert request.method == verb, f"{name} used {request.method}, expected {verb}"


@pytest.mark.parametrize("name", [n for n in ENDPOINTS if n.startswith("get_")])
async def test_get_endpoints_send_no_body(api, name):
    """A GET with a JSON body is a sign of a copy-paste error."""
    async with Skylight(TokenAuth("t"), base_url=api.url) as client:
        method = getattr(client, name)
        positional, keywords = _call_args(method)
        keywords.update(OVERRIDES.get(name, {}))
        api.queue(GENERIC_PAYLOAD)
        await method(*positional, **keywords)

    request = api.requests[0]
    assert request.method == "GET", f"{name} is a getter but used {request.method}"
    assert request.body is None, f"{name} sent a body on a GET: {request.body}"
