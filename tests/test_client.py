"""Tests for the Skylight client's request handling and endpoint wiring."""

import logging
from datetime import datetime, timezone

import aiohttp
import pytest

from pyskylight import (
    ApiError,
    NotAuthorizedError,
    NotFoundError,
    RateLimitError,
    Skylight,
    TokenAuth,
)
from pyskylight.auth import Auth
from pyskylight.const import API_VERSION, USER_AGENT
from pyskylight.exceptions import AuthenticationError, SkylightError

FRAME = "5455113"


class RotatingAuth(Auth):
    """An auth handler that hands out a fresh token on refresh."""

    def __init__(self):
        self.token = "old"
        self.refreshes = 0

    async def access_token(self) -> str:
        return self.token

    async def refresh(self) -> str:
        self.refreshes += 1
        self.token = "new"
        return self.token


@pytest.fixture
async def client(api):
    async with Skylight(TokenAuth("t0k3n"), base_url=api.url) as skylight:
        yield skylight


async def test_request_headers(client, api):
    api.queue({"data": []})
    await client.get_colors()

    headers = api.last.headers
    assert api.last.path == "/api/colors"
    assert headers["Authorization"] == "Bearer t0k3n"
    assert headers["Skylight-Api-Version"] == API_VERSION
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Accept"] == "application/json"


async def test_get_chores_builds_query(client, api):
    api.queue(
        {
            "data": [
                {
                    "type": "chore",
                    "id": "1-2025-08-25",
                    "attributes": {"summary": "Recycling", "start": "2025-08-25"},
                }
            ]
        }
    )
    chores = await client.get_chores(
        FRAME,
        after="2025-08-25",
        before="2025-08-29",
        include_late=True,
        linked_to_profile=True,
    )
    assert [c.summary for c in chores] == ["Recycling"]
    assert api.last.path == f"/api/frames/{FRAME}/chores"
    assert api.last.query == {
        "after": "2025-08-25",
        "before": "2025-08-29",
        "include_late": "true",
        "filter": "linked_to_profile",
    }


async def test_get_chores_omits_unset_query_params(client, api):
    api.queue({"data": []})
    await client.get_chores(FRAME)
    assert api.last.query == {}


async def test_create_chore_sends_flat_body(client, api):
    api.queue({"data": {"type": "chore", "id": "9", "attributes": {"summary": "Dishes"}}})
    chore = await client.create_chore(FRAME, "Dishes", 77, start="2025-08-29", start_time="10:00")
    assert chore.id == "9"
    assert api.last.method == "POST"
    assert api.last.path == f"/api/frames/{FRAME}/chores"
    # Writes are flat form params, not JSON:API documents, and the singular
    # endpoint takes `category_id` (the plural form is rejected).
    assert api.last.body == {
        "summary": "Dishes",
        "category_id": 77,
        "start": "2025-08-29",
        "start_time": "10:00",
    }


async def test_create_chores_uses_plural_category_ids(client, api):
    api.queue({"data": {"type": "chore", "id": "9", "attributes": {}}})
    await client.create_chores(
        FRAME, "Dishes", [77, 78], start="2025-08-29", recurrence_set="RRULE:FREQ=DAILY"
    )
    assert api.last.path == f"/api/frames/{FRAME}/chores/create_multiple"
    assert api.last.body == {
        "summary": "Dishes",
        "category_ids": ["77", "78"],
        "start": "2025-08-29",
        "recurrence_set": ["RRULE:FREQ=DAILY"],
    }


async def test_create_nudge_requires_body_and_deliver_at(client, api):
    api.queue({"data": {"type": "nudge", "id": "9760", "attributes": {"body": "Brush teeth"}}})
    nudge = await client.create_nudge(FRAME, "Brush teeth", "2026-08-08T09:00:00", [77])
    assert nudge.body == "Brush teeth"
    assert api.last.body == {
        "body": "Brush teeth",
        "deliver_at": "2026-08-08T09:00:00",
        "category_ids": ["77"],
    }


async def test_create_album_uses_title(client, api):
    api.queue({"data": {"type": "album", "id": "1", "attributes": {}}})
    await client.create_album(FRAME, "Vacation")
    assert api.last.body == {"title": "Vacation"}


async def test_get_list_resolves_items_and_sections(client, api):
    api.queue(
        {
            "data": {
                "type": "list",
                "id": "3",
                "attributes": {"label": "Grocery List", "kind": "shopping"},
                "relationships": {"list_items": {"data": [{"type": "list_item", "id": "7"}]}},
            },
            "meta": {"sections": [{"name": "Produce"}]},
            "included": [{"type": "list_item", "id": "7", "attributes": {"label": "Milk"}}],
        }
    )
    grocery = await client.get_list(FRAME, 3)
    assert grocery.label == "Grocery List"
    assert grocery.list_item_ids == ["7"]
    assert [i.label for i in grocery.items] == ["Milk"]
    assert grocery.sections == [{"name": "Produce"}]


async def test_get_messages_is_the_photo_feed(client, api):
    api.queue(
        {
            "data": [
                {
                    "type": "message",
                    "id": "1824704026",
                    "attributes": {
                        "asset_type": "photo",
                        "asset_url": "https://cdn.example/photo.jpg?Expires=1786752000",
                        "thumbnail_url": "https://cdn.example/thumb.jpg?Expires=1786752000",
                        "caption": "Beach day",
                        "created_at": "2026-08-08T21:43:58.947Z",
                        "from_email": "gran@example.com",
                    },
                }
            ],
            "meta": {"current_page": 1, "num_pages": 6},
        }
    )
    messages = await client.get_messages(FRAME, page=2)

    assert [m.caption for m in messages] == ["Beach day"]
    assert messages[0].asset_type == "photo"
    assert messages[0].created_at.year == 2026
    # The page size is fixed at 30; only `page` does anything.
    assert api.last.query == {"page": "2"}


async def test_get_meal_recipes_resolves_its_category(client, api):
    api.queue(
        {
            "data": [
                {
                    "type": "meal_recipe",
                    "id": "67098089",
                    # The name is `summary`; there is no title field.
                    "attributes": {
                        "summary": "Milk & Cereal",
                        "description": "Ingredients:\n- Cereal\n- Milk\n",
                        "draft": False,
                    },
                    "relationships": {
                        "meal_category": {"data": {"type": "meal_category", "id": "9354302"}}
                    },
                }
            ],
            "included": [
                {
                    "type": "meal_category",
                    "id": "9354302",
                    "attributes": {"label": "Breakfast"},
                }
            ],
        }
    )
    recipes = await client.get_meal_recipes(FRAME)

    assert [r.summary for r in recipes] == ["Milk & Cereal"]
    assert recipes[0].meal_category_id == "9354302"
    assert api.last.query["include"] == "meal_category"


async def test_create_meal_recipe_requires_a_category(client, api):
    api.queue({"data": {"type": "meal_recipe", "id": "1", "attributes": {"summary": "Tacos"}}})
    recipe = await client.create_meal_recipe(
        FRAME, "Tacos", 9354304, description="Ingredients:\n- Tortillas\n"
    )

    assert recipe.summary == "Tacos"
    # Omitting meal_category_id is a bare 422 naming no field, so it is a
    # positional argument rather than something to forget in **fields.
    assert api.last.body == {
        "summary": "Tacos",
        "meal_category_id": "9354304",
        "description": "Ingredients:\n- Tortillas\n",
    }


async def test_get_meal_categories(client, api):
    api.queue(
        {
            "data": [
                {"type": "meal_category", "id": "1", "attributes": {"label": "Breakfast"}},
                {"type": "meal_category", "id": "3", "attributes": {"label": "Dinner"}},
            ]
        }
    )
    assert [c.label for c in await client.get_meal_categories(FRAME)] == ["Breakfast", "Dinner"]


async def test_complete_chore_uses_complete_not_completed(client, api):
    api.queue({"data": {"type": "chore", "id": "9", "attributes": {"status": "pending"}}})
    await client.complete_chore(FRAME, 9, instance_date="2025-08-25")

    assert api.last.method == "PUT"
    assert api.last.path == f"/api/frames/{FRAME}/chores/9/completions"
    # The API rejects "completed", and rejects category_id on this endpoint.
    assert api.last.body == {"status": "complete", "instance_date": "2025-08-25"}


async def test_uncomplete_chore_omits_instance_date_when_unset(client, api):
    api.queue({"data": {"type": "chore", "id": "9", "attributes": {}}})
    await client.uncomplete_chore(FRAME, 9)
    assert api.last.body == {"status": "pending"}


async def test_move_chore_requires_before_or_after(client, api):
    api.queue({})
    await client.move_chore(FRAME, 9, after=10)
    assert api.last.path == f"/api/frames/{FRAME}/chores/9/move"
    assert api.last.body == {"position": {"after": "10"}}

    with pytest.raises(ValueError, match="exactly one"):
        await client.move_chore(FRAME, 9)
    with pytest.raises(ValueError, match="exactly one"):
        await client.move_chore(FRAME, 9, before=1, after=2)


async def test_delete_chore_passes_apply_to(client, api):
    api.queue(None, status=204)
    await client.delete_chore(FRAME, 9, apply_to="all")

    assert api.last.method == "DELETE"
    assert api.last.body == {"apply_to": "all"}


async def test_delete_chore_omits_apply_to_for_one_time_chores(client, api):
    api.queue(None, status=204)
    await client.delete_chore(FRAME, 9)
    # A one-time chore is rejected outright if apply_to is present.
    assert api.last.body is None


async def test_create_chores_and_rewards_return_collections(client, api):
    api.queue(
        {
            "data": [
                {"type": "chore", "id": "1", "attributes": {"summary": "Dishes"}},
                {"type": "chore", "id": "2", "attributes": {"summary": "Dishes"}},
            ]
        }
    )
    chores = await client.create_chores(FRAME, "Dishes", [77, 78])
    assert [c.id for c in chores] == ["1", "2"]

    api.queue({"data": [{"type": "reward", "id": "5", "attributes": {"name": "Ice cream"}}]})
    rewards = await client.create_rewards(FRAME, "Ice cream", 5, [77])
    assert [r.name for r in rewards] == ["Ice cream"]


async def test_calendar_events_join_include_list(client, api):
    api.queue({"data": []})
    await client.get_calendar_events(
        FRAME,
        "2025-09-01",
        "2025-09-30",
        timezone="America/Los_Angeles",
        include=["categories", "calendar_account"],
    )
    assert api.last.query == {
        "date_min": "2025-09-01",
        "date_max": "2025-09-30",
        "timezone": "America/Los_Angeles",
        "include": "categories,calendar_account",
    }


async def test_304_returns_empty_list(client, api):
    api.queue(None, status=304)
    assert await client.get_categories(FRAME) == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, NotFoundError), (429, RateLimitError), (500, ApiError)],
)
async def test_error_statuses(client, api, status, expected):
    api.queue({"errors": ["boom"]}, status=status)
    with pytest.raises(expected) as excinfo:
        await client.get_user()
    assert excinfo.value.status == status
    assert excinfo.value.errors == ["boom"]
    assert "boom" in str(excinfo.value)


async def test_401_triggers_one_refresh_then_succeeds(api):
    auth = RotatingAuth()
    api.queue({"errors": ["Invalid token"]}, status=401)
    api.queue({"user": {"id": 1, "email": "me@example.com"}})

    async with Skylight(auth, base_url=api.url) as client:
        user = await client.get_user()

    assert auth.refreshes == 1
    assert user.email == "me@example.com"
    assert api.requests[0].headers["Authorization"] == "Bearer old"
    assert api.requests[1].headers["Authorization"] == "Bearer new"


async def test_401_twice_raises_not_authorized(api):
    api.queue({"errors": ["Invalid token"]}, status=401)
    api.queue({"errors": ["Invalid token"]}, status=401)

    async with Skylight(RotatingAuth(), base_url=api.url) as client:
        with pytest.raises(NotAuthorizedError, match="Invalid token"):
            await client.get_user()


async def test_401_with_static_token_reports_auth_failure(client, api):
    api.queue({"errors": ["Invalid token"]}, status=401)
    with pytest.raises(AuthenticationError):
        await client.get_user()


async def test_supplied_session_is_not_closed(api):
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with Skylight(TokenAuth("t"), session=session, base_url=api.url) as client:
            api.queue({"data": []})
            await client.get_frames()
        assert not session.closed


# --- request plumbing --------------------------------------------------------


async def test_datetime_and_sequence_query_params(client, api):
    api.queue({"data": []})
    await client.get_rewards(
        FRAME, redeemed_at_min=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    )
    assert api.last.query == {"redeemed_at_min": "2026-08-07T12:00:00+00:00"}


async def test_closed_supplied_session_is_reported(api):
    session = aiohttp.ClientSession()
    await session.close()
    client = Skylight(TokenAuth("t"), session=session, base_url=api.url)
    with pytest.raises(SkylightError, match="closed"):
        await client.get_colors()


async def test_empty_and_non_json_bodies(client, api):
    api.queue(None, status=200)
    assert await client.get_household_config(FRAME) is None

    api.queue_raw("<html>not json</html>", status=200, content_type="text/html")
    assert await client.get_household_config(FRAME) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"errors": ["first", "second"]}, "first; second"),
        ({"errors": [{"detail": "detailed"}]}, "detailed"),
        ({"errors": [{"title": "titled"}]}, "titled"),
        ({"error_description": "described"}, "described"),
        ({"error": "short"}, "short"),
        ({"message": "messaged"}, "messaged"),
        ({"errors": {"status": ["is not included in the list"]}}, None),
    ],
)
async def test_error_message_extraction(client, api, body, expected):
    api.queue(body, status=422)
    with pytest.raises(ApiError) as excinfo:
        await client.get_user()
    if expected is not None:
        assert expected in str(excinfo.value)
    assert excinfo.value.status == 422
    assert excinfo.value.url.endswith("/api/user")


async def test_non_dict_error_body(client, api):
    api.queue(["unexpected"], status=500)
    with pytest.raises(ApiError) as excinfo:
        await client.get_user()
    assert excinfo.value.errors == []


# --- device settings ---------------------------------------------------------


async def test_update_device_sends_a_flat_body(client, api):
    api.queue({"data": {"type": "device", "id": "9", "attributes": {"brightness": 180}}})
    device = await client.update_device(FRAME, 9, brightness=180, nightlight=True)

    assert device.brightness == 180
    assert api.last.method == "PUT"
    assert api.last.path == f"/api/frames/{FRAME}/devices/9"
    assert api.last.body == {"brightness": 180, "nightlight": True}


async def test_rename_device_goes_through_update_device(client, api):
    api.queue({"data": {"type": "device", "id": "9", "attributes": {"name": "Bedroom"}}})
    device = await client.rename_device(FRAME, 9, "Bedroom")

    assert device.name == "Bedroom"
    assert api.last.body == {"name": "Bedroom"}


async def test_nightlight_colors_are_enumerated():
    from pyskylight.models import NightlightColor

    # The set accepted by a live display; white, warm and purple are refused.
    assert NightlightColor.ALL == ("off", "red", "orange", "yellow", "green", "blue", "pink")
    assert NightlightColor.OFF in NightlightColor.ALL


async def test_requests_are_logged_without_secrets(client, api, caplog):
    """A debug log has to name the endpoint, and nothing that should stay private.

    Without this the only trace of a failed request is its status, which is what
    a downstream integration ends up reporting to its users.
    """
    caplog.set_level(logging.DEBUG, logger="pyskylight.client")
    api.queue({"data": []})

    await client.get_categories(FRAME)

    logged = [record.getMessage() for record in caplog.records]
    assert any(f"GET /api/frames/{FRAME}/categories -> 200" in line for line in logged)
    # The bearer token travels in a header on every request.
    assert not any("t0k3n" in line for line in logged)


async def test_a_failing_request_is_logged_with_its_status(client, api, caplog):
    """The status is logged before the error is raised, so both are in the log."""
    caplog.set_level(logging.DEBUG, logger="pyskylight.client")
    api.queue({"errors": ["boom"]}, status=500)

    with pytest.raises(ApiError) as err:
        await client.get_categories(FRAME)

    assert err.value.url.endswith(f"/api/frames/{FRAME}/categories")
    assert any("-> 500" in record.getMessage() for record in caplog.records)
