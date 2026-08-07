"""Minimal JSON:API document handling and dataclass deserialization.

The Skylight API returns JSON:API-style documents::

    {
        "data": {"type": "list", "id": "1", "attributes": {...}, "relationships": {...}},
        "included": [{"type": "list_item", "id": "2", "attributes": {...}}],
        "meta": {...},
    }

This module turns those into frozen dataclasses. Every model keeps the raw
resource dict around, because the upstream schema is reverse-engineered and
documents ``additionalProperties: true`` nearly everywhere: attributes that
pylight does not know about are still reachable via ``obj.attributes``.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from types import UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

_UNION_TYPES: tuple[Any, ...] = (Union, UnionType)

__all__ = [
    "ApiObject",
    "Document",
    "ALIAS",
    "RELATIONSHIP",
    "RELATIONSHIPS",
    "alias",
    "relationship",
    "relationships",
]

#: ``field(metadata=...)`` key: read this attribute name instead of the field name.
ALIAS = "pylight.alias"
#: ``field(metadata=...)`` key: read the id of a to-one relationship.
RELATIONSHIP = "pylight.relationship"
#: ``field(metadata=...)`` key: read the ids of a to-many relationship.
RELATIONSHIPS = "pylight.relationships"

T = TypeVar("T", bound="ApiObject")


def alias(name: str) -> dict[str, str]:
    """Map a dataclass field onto a differently-named JSON attribute."""
    return {ALIAS: name}


def relationship(name: str) -> dict[str, str]:
    """Map a dataclass field onto the id of a to-one relationship."""
    return {RELATIONSHIP: name}


def relationships(name: str) -> dict[str, str]:
    """Map a dataclass field onto the ids of a to-many relationship."""
    return {RELATIONSHIPS: name}


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _decode(hint: Any, value: Any) -> Any:
    """Coerce ``value`` to ``hint``, best-effort.

    Unknown or unparseable values are passed through rather than raising: the
    upstream schema is observed, not specified, so a surprising type is a
    documentation bug and not a reason to fail the whole request.
    """
    if value is None:
        return None

    origin = get_origin(hint)
    if origin in _UNION_TYPES:
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) == 1:
            return _decode(args[0], value)
        return value
    if origin in (list, tuple, set):
        item_args = get_args(hint)
        item_hint = item_args[0] if item_args else Any
        if not isinstance(value, (list, tuple)):
            return value
        return [_decode(item_hint, item) for item in value]
    if origin is dict:
        return value

    if hint is Any or hint is None:
        return value
    if hint is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return bool(value)
    if hint is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if hint is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if hint is str:
        return value if isinstance(value, str) else str(value)
    if hint is datetime:
        return _parse_datetime(value)
    if hint is date:
        return _parse_date(value)
    if isinstance(hint, type) and issubclass(hint, ApiObject):
        return hint.from_resource(value)
    return value


def _rel_data(resource: dict[str, Any], name: str) -> Any:
    rel = (resource.get("relationships") or {}).get(name) or {}
    return rel.get("data")


@dataclasses.dataclass(frozen=True, kw_only=True)
class ApiObject:
    """Base class for JSON:API resources.

    Attributes:
        id: The JSON:API resource id. Always a string, even when the underlying
            value is numeric.
        raw: The undecoded resource dict, including any attributes pylight does
            not model.
    """

    id: str
    raw: dict[str, Any] = dataclasses.field(repr=False, compare=False, default_factory=dict)

    @property
    def attributes(self) -> dict[str, Any]:
        """The raw ``attributes`` object, including unmodeled keys."""
        return self.raw.get("attributes") or {}

    @classmethod
    def from_resource(cls: type[T], resource: dict[str, Any]) -> T:
        """Build an instance from a single JSON:API resource object."""
        attributes = resource.get("attributes") or {}
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {
            "id": str(resource.get("id", "")),
            "raw": resource,
        }
        for field in dataclasses.fields(cls):
            if field.name in ("id", "raw"):
                continue
            hint = hints.get(field.name, Any)
            if (rel := field.metadata.get(RELATIONSHIP)) is not None:
                data = _rel_data(resource, rel)
                kwargs[field.name] = str(data["id"]) if isinstance(data, dict) else None
                continue
            if (rel := field.metadata.get(RELATIONSHIPS)) is not None:
                data = _rel_data(resource, rel)
                kwargs[field.name] = (
                    [str(d["id"]) for d in data if isinstance(d, dict)]
                    if isinstance(data, list)
                    else []
                )
                continue
            key = field.metadata.get(ALIAS, field.name)
            if key in attributes:
                kwargs[field.name] = _decode(hint, attributes[key])
            elif field.default is dataclasses.MISSING and (
                field.default_factory is dataclasses.MISSING  # type: ignore[misc]
            ):
                kwargs[field.name] = None
        return cls(**kwargs)

    @classmethod
    def from_document(cls: type[T], document: Any) -> list[T]:
        """Build a list of instances from a JSON:API document's ``data``."""
        return [cls.from_resource(r) for r in Document(document).data_list]

    @classmethod
    def one_from_document(cls: type[T], document: Any) -> T:
        """Build a single instance from a JSON:API document's ``data``."""
        data = Document(document).data
        if not isinstance(data, dict):
            raise ValueError("document has no primary resource object")
        return cls.from_resource(data)


class Document:
    """A parsed JSON:API document."""

    def __init__(self, payload: Any) -> None:
        self.payload: dict[str, Any] = payload if isinstance(payload, dict) else {}

    @property
    def data(self) -> Any:
        """The primary ``data`` member (a resource, a list, or ``None``)."""
        return self.payload.get("data")

    @property
    def data_list(self) -> list[dict[str, Any]]:
        """The primary data coerced to a list of resource objects."""
        data = self.data
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    @property
    def included(self) -> list[dict[str, Any]]:
        """The ``included`` compound-document members."""
        value = self.payload.get("included")
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    @property
    def meta(self) -> dict[str, Any]:
        """The top-level ``meta`` object."""
        value = self.payload.get("meta")
        return value if isinstance(value, dict) else {}

    def included_of(self, resource_type: str) -> list[dict[str, Any]]:
        """All included resources of the given JSON:API ``type``."""
        return [r for r in self.included if r.get("type") == resource_type]

    def find_included(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        """Look up one included resource by type and id."""
        for resource in self.included:
            if resource.get("type") == resource_type and str(resource.get("id")) == str(
                resource_id
            ):
                return resource
        return None
