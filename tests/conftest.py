"""Shared fixtures: a recording stand-in for the Skylight API."""

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


@dataclass
class _Raw:
    """A response body that is not JSON."""

    text: str
    content_type: str


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass
class FakeApi:
    """Records the requests it receives and replays queued responses."""

    url: str = ""
    requests: list[RecordedRequest] = field(default_factory=list)
    responses: deque = field(default_factory=deque)

    def queue(self, payload: Any = None, status: int = 200) -> None:
        self.responses.append((status, payload))

    def queue_raw(self, text: str, status: int = 200, content_type: str = "text/plain") -> None:
        """Queue a non-JSON response body."""
        self.responses.append((status, _Raw(text, content_type)))

    @property
    def last(self) -> RecordedRequest:
        return self.requests[-1]

    async def _handle(self, request: web.Request) -> web.Response:
        body: Any = None
        if request.can_read_body:
            try:
                body = await request.json()
            except ValueError:
                body = await request.text()
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                query=dict(request.query),
                headers=dict(request.headers),
                body=body,
            )
        )
        status, payload = self.responses.popleft() if self.responses else (200, {})
        if payload is None:
            return web.Response(status=status)
        if isinstance(payload, _Raw):
            return web.Response(text=payload.text, status=status, content_type=payload.content_type)
        return web.json_response(payload, status=status)


@pytest.fixture
async def api():
    fake = FakeApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake._handle)
    server = TestServer(app, host="localhost")
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    try:
        yield fake
    finally:
        await server.close()
