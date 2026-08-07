"""Tests for JSON:API decoding and models."""

from datetime import date, datetime, timedelta, timezone

from pylight.jsonapi import Document
from pylight.models import Category, Chore, ListItem, SkylightList, Token, User

# From mightybandito/Skylight examples/get-chores-redacted.json.
CHORES_DOC = {
    "data": [
        {
            "type": "chore",
            "id": "9001-2025-08-25",
            "attributes": {
                "id": 9001,
                "summary": "Recycling",
                "status": "pending",
                "start": "2025-08-25",
                "is_future": False,
                "recurring": True,
                "recurrence_set": "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU",
                "start_time": None,
                "completed_on": None,
                "unmodeled_field": "kept in .attributes",
            },
            "relationships": {"category": {"data": {"type": "category", "id": "77"}}},
        }
    ],
    "included": [
        {
            "type": "category",
            "id": "77",
            "attributes": {"label": "Alex", "color": "#A6A6BE", "linked_to_profile": True},
        }
    ],
}


def test_chore_decoding():
    (chore,) = Chore.from_document(CHORES_DOC)
    assert chore.id == "9001-2025-08-25"
    assert chore.chore_id == 9001
    assert chore.summary == "Recycling"
    assert chore.start == date(2025, 8, 25)
    assert chore.recurring is True
    assert chore.start_time is None
    assert chore.category_id == "77"
    assert chore.completed is False
    assert chore.attributes["unmodeled_field"] == "kept in .attributes"


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
