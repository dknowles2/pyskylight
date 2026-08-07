"""Tests for the JSON:API decoder's edge cases.

The upstream schema is observed rather than specified, so the decoder's job is
to be unsurprising when a field is a type nobody expected. These tests pin that
behaviour down: coerce when possible, pass through when not, never raise.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import pytest

from pylight.jsonapi import ApiObject, Document, alias, relationship, relationships


@dataclass(frozen=True, kw_only=True)
class Nested(ApiObject):
    """A resource embedded in another resource's attributes."""

    label: str | None = None


@dataclass(frozen=True, kw_only=True)
class Sample(ApiObject):
    """Exercises every branch of the decoder."""

    text: str | None = None
    count: int | None = None
    ratio: float | None = None
    flag: bool | None = None
    when: datetime | None = None
    day: date | None = None
    tags: list[str] = field(default_factory=list)
    blob: dict[str, Any] = field(default_factory=dict)
    anything: Any = None
    renamed: str | None = field(default=None, metadata=alias("original_name"))
    child: Nested | None = None
    parent_id: str | None = field(default=None, metadata=relationship("parent"))
    sibling_ids: list[str] = field(default_factory=list, metadata=relationships("siblings"))


def build(**attributes: Any) -> Sample:
    return Sample.from_resource({"type": "sample", "id": "1", "attributes": attributes})


def test_scalar_coercion():
    sample = build(text=7, count="42", ratio="1.5", flag="true")
    assert sample.text == "7"
    assert sample.count == 42
    assert sample.ratio == 1.5
    assert sample.flag is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), ("", False), (0, False)],
)
def test_bool_strings(value, expected):
    assert build(flag=value).flag is expected


def test_unparseable_numbers_become_none():
    sample = build(count="not a number", ratio="nope")
    assert sample.count is None
    assert sample.ratio is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-07T14:30:00Z", datetime(2026, 8, 7, 14, 30, tzinfo=timezone.utc)),
        ("2026-08-07T14:30:00+00:00", datetime(2026, 8, 7, 14, 30, tzinfo=timezone.utc)),
        ("garbage", None),
        ("", None),
        (12345, None),
    ],
)
def test_datetime_parsing(value, expected):
    assert build(when=value).when == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-07", date(2026, 8, 7)),
        ("2026-08-07T14:30:00Z", date(2026, 8, 7)),  # truncated to the date
        ("garbage", None),
        ("", None),
        (99, None),
    ],
)
def test_date_parsing(value, expected):
    assert build(day=value).day == expected


def test_datetime_passthrough_for_real_objects():
    moment = datetime(2026, 8, 7, 14, 30, tzinfo=timezone.utc)
    assert build(when=moment).when is moment
    assert build(day=moment).day == date(2026, 8, 7)
    assert build(day=date(2026, 8, 7)).day == date(2026, 8, 7)


def test_lists_and_dicts():
    sample = build(tags=["a", 2], blob={"k": "v"}, anything={"whatever": [1, 2]})
    assert sample.tags == ["a", "2"]
    assert sample.blob == {"k": "v"}
    assert sample.anything == {"whatever": [1, 2]}


def test_scalar_where_a_list_was_expected_passes_through():
    # Better a surprising value the caller can inspect than a failed request.
    assert build(tags="not-a-list").tags == "not-a-list"


def test_alias_and_nested_resource():
    sample = build(
        original_name="aliased",
        child={"type": "nested", "id": "9", "attributes": {"label": "inner"}},
    )
    assert sample.renamed == "aliased"
    assert sample.child == Nested(id="9", label="inner")


def test_relationships():
    resource = {
        "type": "sample",
        "id": "1",
        "attributes": {},
        "relationships": {
            "parent": {"data": {"type": "sample", "id": "77"}},
            "siblings": {"data": [{"type": "sample", "id": "2"}, {"type": "sample", "id": "3"}]},
        },
    }
    sample = Sample.from_resource(resource)
    assert sample.parent_id == "77"
    assert sample.sibling_ids == ["2", "3"]


def test_empty_and_null_relationships():
    sample = Sample.from_resource(
        {
            "type": "sample",
            "id": "1",
            "relationships": {"parent": {"data": None}, "siblings": {"data": None}},
        }
    )
    assert sample.parent_id is None
    assert sample.sibling_ids == []


def test_document_accessors():
    document = Document(
        {
            "data": [{"type": "a", "id": "1"}],
            "included": [{"type": "b", "id": "2"}, "junk"],
            "meta": {"page": 1},
        }
    )
    assert document.data_list == [{"type": "a", "id": "1"}]
    assert document.included == [{"type": "b", "id": "2"}]
    assert document.meta == {"page": 1}
    assert document.find_included("b", 2) == {"type": "b", "id": "2"}


def test_document_tolerates_junk():
    for payload in (None, [], "nonsense", 42):
        document = Document(payload)
        assert document.data is None
        assert document.data_list == []
        assert document.included == []
        assert document.meta == {}


def test_document_with_scalar_meta():
    assert Document({"meta": "not-an-object"}).meta == {}


def test_from_document_accepts_single_or_list():
    assert len(Sample.from_document({"data": {"type": "sample", "id": "1"}})) == 1
    assert len(Sample.from_document({"data": [{"type": "sample", "id": "1"}] * 3})) == 3
    assert Sample.from_document({"data": None}) == []


def test_one_from_document_requires_a_resource():
    with pytest.raises(ValueError, match="no primary resource"):
        Sample.one_from_document({"data": []})
    with pytest.raises(ValueError, match="no primary resource"):
        Sample.one_from_document({})


def test_attributes_property_survives_missing_keys():
    assert Sample.from_resource({"type": "sample", "id": "1"}).attributes == {}
    assert Sample.from_resource({"type": "sample", "id": "1", "attributes": None}).attributes == {}


def test_raw_is_excluded_from_equality():
    # Two resources with the same decoded fields compare equal even if the
    # server sent extra keys in one of them.
    a = build(text="x")
    b = Sample.from_resource(
        {"type": "sample", "id": "1", "attributes": {"text": "x", "extra": "ignored"}}
    )
    assert a == b
    assert b.attributes["extra"] == "ignored"
