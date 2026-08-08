"""Tests for JSON:API decoding and models."""

from datetime import date, datetime, timedelta, timezone

from pyskylight.jsonapi import Document
from pyskylight.models import (
    Category,
    Chore,
    ChoreGroups,
    ListItem,
    RewardPoint,
    SkylightList,
    Token,
    User,
)

# Shape taken from a live capture: the occurrence id is "<group>-<date>", the
# addressable chore id is `group`, and recurrence_set is a list.
CHORES_DOC = {
    "data": [
        {
            "type": "chore",
            "id": "9001-2025-08-25",
            "attributes": {
                "id": "9001-2025-08-25",
                "group": "9001",
                "series": "9001",
                "summary": "Recycling",
                "status": "pending",
                "start": "2025-08-25",
                "up_for_grabs": False,
                "recurring": True,
                "recurrence_set": ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"],
                "start_time": None,
                "completed_on": None,
                "completed_at": None,
                "unmodeled_field": "kept in .attributes",
            },
            "relationships": {"category": {"data": {"type": "category", "id": "77"}}},
        }
    ],
    "included": [
        {
            "type": "category",
            "id": "77",
            "attributes": {
                "id": 77,
                "label": "Alex",
                "color": "#A6A6BE",
                "linked_to_profile": True,
                "profile_picture_urls": {
                    "small": "https://example.invalid/s.jpg",
                    "original": "https://example.invalid/o.jpg",
                },
            },
        }
    ],
}


def test_chore_decoding():
    (chore,) = Chore.from_document(CHORES_DOC)
    assert chore.id == "9001-2025-08-25"
    assert chore.chore_id == "9001"
    assert chore.series == "9001"
    assert chore.summary == "Recycling"
    assert chore.start == date(2025, 8, 25)
    assert chore.recurring is True
    assert chore.recurrence_set == ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"]
    assert chore.start_time is None
    assert chore.category_id == "77"
    assert chore.completed is False
    assert chore.attributes["unmodeled_field"] == "kept in .attributes"


def test_unassigned_chore():
    """An "Up for Grabs" chore is flagged and owned by nobody."""
    up_for_grabs = Chore.from_resource(
        {
            "type": "chore",
            "id": "9002",
            "attributes": {"summary": "Vacuum", "up_for_grabs": True},
            "relationships": {"category": {"data": None}},
        }
    )
    assert up_for_grabs.unassigned is True
    assert up_for_grabs.category_id is None

    # The flag alone is not enough: the API ignores it unless the category goes
    # too, so a chore can carry it while still belonging to someone.
    still_owned = Chore.from_resource(
        {
            "type": "chore",
            "id": "9003",
            "attributes": {"summary": "Vacuum", "up_for_grabs": True},
            "relationships": {"category": {"data": {"type": "category", "id": "77"}}},
        }
    )
    assert still_owned.unassigned is False

    (ordinary,) = Chore.from_document(CHORES_DOC)
    assert ordinary.unassigned is False


def test_completed_chore():
    doc = {
        "data": {
            "type": "chore",
            "id": "1-2025-08-25",
            "attributes": {"status": "completed", "completed_on": "2025-08-25T09:00:00Z"},
        }
    }
    chore = Chore.one_from_document(doc)
    assert chore.completed is True
    assert chore.completed_on == date(2025, 8, 25)


def test_missing_attributes_default_to_none():
    chore = Chore.from_resource({"type": "chore", "id": "5"})
    assert chore.summary is None
    assert chore.category_id is None
    assert chore.attributes == {}


def test_category_decoding():
    document = Document(CHORES_DOC)
    (resource,) = document.included_of("category")
    category = Category.from_resource(resource)
    assert (category.id, category.label, category.color) == ("77", "Alex", "#A6A6BE")
    assert category.category_id == 77
    assert category.profile_picture_url == "https://example.invalid/o.jpg"
    assert document.find_included("category", "77") == resource
    assert document.find_included("category", "nope") is None


def test_list_relationships_and_items():
    list_doc = {
        "data": {
            "type": "list",
            "id": "3",
            "attributes": {
                "label": "Grocery List",
                "color": "#A6A6BE",
                "kind": "shopping",
                "default_grocery_list": True,
            },
            "relationships": {"list_items": {"data": [{"type": "list_item", "id": "ITEM1"}]}},
        },
        "included": [
            {
                "type": "list_item",
                "id": "ITEM1",
                "attributes": {
                    "label": "Milk",
                    "status": "completed",
                    "position": 2,
                    "created_at": "2025-08-25T09:00:00Z",
                },
            }
        ],
    }
    skylight_list = SkylightList.one_from_document(list_doc)
    assert skylight_list.label == "Grocery List"
    assert skylight_list.kind == "shopping"
    assert skylight_list.list_item_ids == ["ITEM1"]

    (item,) = [ListItem.from_resource(r) for r in Document(list_doc).included_of("list_item")]
    assert item.label == "Milk"
    assert item.completed is True
    assert item.created_at == datetime(2025, 8, 25, 9, 0, tzinfo=timezone.utc)


def test_numeric_ids_become_strings():
    assert Category.from_resource({"type": "category", "id": 42}).id == "42"


def test_unparseable_values_do_not_raise():
    chore = Chore.from_resource(
        {"type": "chore", "id": "1", "attributes": {"start": "not-a-date", "position": "x"}}
    )
    assert chore.start is None
    assert chore.position is None


def test_user_from_plain_response():
    user = User.from_response({"user": {"id": 12, "email": "me@example.com", "name": "Alex"}})
    assert (user.id, user.email, user.name) == ("12", "me@example.com", "Alex")


def test_user_from_jsonapi_response():
    user = User.from_response(
        {"data": {"type": "user", "id": "12", "attributes": {"email": "me@example.com"}}}
    )
    assert user.email == "me@example.com"


def test_token_expiry():
    now = datetime.now(timezone.utc)
    assert Token(access_token="a", expires_at=now - timedelta(seconds=1)).is_expired
    assert not Token(access_token="a", expires_at=now + timedelta(hours=1)).is_expired
    assert not Token(access_token="a").is_expired


def test_chore_groups_buckets():
    groups = ChoreGroups.from_response(
        {
            "chores": {
                "late": {"data": [{"type": "chore", "id": "1-2025-08-01"}], "included": []},
                "today": {"data": [{"type": "chore", "id": "2-2025-08-07"}]},
                "future": {"data": []},
            },
            "routines": {"today": {"data": [{"type": "chore", "id": "3-2025-08-07"}]}},
        }
    )
    assert sorted(groups.chores) == ["future", "late", "today"]
    assert [c.id for c in groups.chores["late"]] == ["1-2025-08-01"]
    assert [c.id for c in groups.routines["today"]] == ["3-2025-08-07"]
    assert len(groups.all) == 3


def test_chore_groups_tolerates_missing_keys():
    assert ChoreGroups.from_response({}).all == []
    assert ChoreGroups.from_response(None).all == []


def test_reward_points_from_plain_array():
    (point,) = RewardPoint.from_response(
        [{"category_id": 21505173, "lifetime_points_earned": 12, "current_point_balance": 4}]
    )
    assert point.category_id == 21505173
    assert point.current_point_balance == 4
    assert point.lifetime_points_earned == 12


def test_user_name_falls_back_to_profile():
    user = User.from_response(
        {"id": 12, "email": "me@example.com", "profile": {"id": 5, "name": "Alex"}}
    )
    assert user.name == "Alex"
    assert user.profile["id"] == 5


def test_calendar_event_handles_both_category_relationship_shapes():
    from pyskylight.models import CalendarEvent

    singular = CalendarEvent.from_resource(
        {
            "type": "calendar_event",
            "id": "1",
            "attributes": {"summary": "Dentist", "starts_at": "2026-08-07T14:00:00Z"},
            "relationships": {"category": {"data": {"type": "category", "id": "77"}}},
        }
    )
    assert singular.category_id == "77"
    assert singular.category_ids == []
    assert singular.starts_at == datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)

    plural = CalendarEvent.from_resource(
        {
            "type": "calendar_event",
            "id": "2",
            "attributes": {"rrule": ["RRULE:FREQ=DAILY"]},
            "relationships": {
                "categories": {"data": [{"type": "category", "id": "77"}]},
            },
        }
    )
    assert plural.category_ids == ["77"]
    assert plural.category_id is None
    assert plural.rrule == ["RRULE:FREQ=DAILY"]
