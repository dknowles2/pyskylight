"""Tests for the OAuth + PKCE login flow, against a stand-in auth server."""

import base64
import hashlib
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pylight.auth import PasswordAuth, TokenAuth, _csrf_token, _pkce_pair
from pylight.const import REDIRECT_URI
from pylight.exceptions import AuthenticationError

EMAIL = "me@example.com"
PASSWORD = "hunter2"
CSRF = "csrf-token-value"
AUTH_CODE = "auth-code-value"

LOGIN_PAGE = f"""<!DOCTYPE html>
<html><head>
<meta name="csrf-param" content="authenticity_token">
<meta name="csrf-token" content="{CSRF}">
</head><body><form></form></body></html>
"""


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def make_app(*, password: str = PASSWORD) -> web.Application:
    """Build a minimal stand-in for the Skylight Rails + Doorkeeper server."""
    app = web.Application()
    state: dict[str, str] = {}

    async def authorize(request: web.Request) -> web.StreamResponse:
        if request.query:
            state["query"] = urlencode(dict(request.query))
            state["state"] = request.query.get("state", "")
            state["challenge"] = request.query.get("code_challenge", "")
        if request.cookies.get("skylightcloud_session") == "authed":
            params = urlencode({"code": AUTH_CODE, "state": state["state"]})
            raise web.HTTPFound(f"{REDIRECT_URI}?{params}")
        response = web.HTTPFound(f"/auth/session/new?{state['query']}")
        response.set_cookie("skylightcloud_session", "anonymous")
        raise response

    async def login_form(request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_PAGE, content_type="text/html")

    async def submit(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        if form.get("authenticity_token") != CSRF:
            raise web.HTTPForbidden(text="invalid authenticity token")
        if form.get("email") != EMAIL or form.get("password") != password:
            # Rails re-renders the form on bad credentials.
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        response = web.HTTPFound(f"/oauth/authorize?{state['query']}")
        response.set_cookie("skylightcloud_session", "authed")
        raise response

    async def token(request: web.Request) -> web.Response:
        form = await request.post()
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") != "refresh-1":
                return web.json_response({"error": "invalid_grant"}, status=401)
            return web.json_response(
                {
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                }
            )
        if form.get("code") != AUTH_CODE:
            return web.json_response({"error": "invalid_grant"}, status=400)
        verifier = str(form.get("code_verifier", ""))
        if _challenge_for(verifier) != state["challenge"]:
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(
            {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )

    app.router.add_get("/oauth/authorize", authorize)
    app.router.add_get("/auth/session/new", login_form)
    app.router.add_post("/auth/session", submit)
    app.router.add_post("/oauth/token", token)
    return app


@pytest.fixture
async def auth_url():
    server = TestServer(make_app(), host="localhost")
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


def test_pkce_pair_matches_s256():
    verifier, challenge = _pkce_pair()
    assert _challenge_for(verifier) == challenge
    assert "=" not in verifier and "=" not in challenge


def test_csrf_token_parsing():
    assert _csrf_token(LOGIN_PAGE) == CSRF
    with pytest.raises(AuthenticationError):
        _csrf_token("<html><head></head></html>")


async def test_login_returns_token(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url) as auth:
        assert await auth.access_token() == "access-1"
        assert auth.token is not None
        assert auth.token.refresh_token == "refresh-1"
        assert auth.token.expires_at is not None
        # A second call reuses the cached token rather than logging in again.
        assert await auth.access_token() == "access-1"


async def test_refresh_uses_refresh_token(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url) as auth:
        await auth.access_token()
        assert await auth.refresh() == "access-2"


async def test_refresh_falls_back_to_login(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url) as auth:
        await auth.access_token()
        object.__setattr__(auth.token, "refresh_token", "bogus")
        assert await auth.refresh() == "access-1"


async def test_bad_password_raises(auth_url):
    async with PasswordAuth(EMAIL, "wrong", base_url=auth_url) as auth:
        with pytest.raises(AuthenticationError, match="authorization code"):
            await auth.access_token()


async def test_token_auth_cannot_refresh():
    auth = TokenAuth("abc")
    assert await auth.access_token() == "abc"
    with pytest.raises(AuthenticationError):
        await auth.refresh()
