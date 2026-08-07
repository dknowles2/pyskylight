"""Tests for the OAuth + PKCE login flow, against a stand-in auth server."""

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pyskylight.auth import Auth, PasswordAuth, TokenAuth, _csrf_token, _pkce_pair
from pyskylight.const import REDIRECT_URI
from pyskylight.exceptions import AuthenticationError
from pyskylight.models import Token

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


def _jar() -> aiohttp.CookieJar:
    """A cookie jar that works against a bare `localhost` test server.

    aiohttp < 3.10 refuses to store cookies for a host with no dot in it, which
    silently breaks the Rails session hand-off the login flow depends on. The
    real host has dots, so the default jar is correct in production.
    """
    return aiohttp.CookieJar(unsafe=True)


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
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        assert await auth.access_token() == "access-1"
        assert auth.token is not None
        assert auth.token.refresh_token == "refresh-1"
        assert auth.token.expires_at is not None
        # A second call reuses the cached token rather than logging in again.
        assert await auth.access_token() == "access-1"


async def test_refresh_uses_refresh_token(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        await auth.access_token()
        assert await auth.refresh() == "access-2"


async def test_refresh_falls_back_to_login(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        await auth.access_token()
        object.__setattr__(auth.token, "refresh_token", "bogus")
        assert await auth.refresh() == "access-1"


async def test_bad_password_raises(auth_url):
    async with PasswordAuth(EMAIL, "wrong", base_url=auth_url, cookie_jar=_jar()) as auth:
        with pytest.raises(AuthenticationError, match="authorization code"):
            await auth.access_token()


async def test_token_auth_cannot_refresh():
    auth = TokenAuth("abc")
    assert await auth.access_token() == "abc"
    with pytest.raises(AuthenticationError):
        await auth.refresh()


# --- token lifecycle ---------------------------------------------------------


async def test_expiring_token_is_refreshed_before_use(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        await auth.access_token()
        # Inside the refresh margin: the next read should renew rather than
        # hand back a token that is about to die mid-request.
        object.__setattr__(
            auth.token, "expires_at", datetime.now(timezone.utc) + timedelta(seconds=5)
        )
        assert await auth.access_token() == "access-2"


async def test_token_without_expiry_is_reused(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        await auth.access_token()
        object.__setattr__(auth.token, "expires_at", None)
        assert await auth.access_token() == "access-1"


async def test_revoke_clears_the_token(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        await auth.access_token()
        await auth.revoke()
        assert auth.token is None
        # Revoking again is a no-op rather than an error.
        await auth.revoke()


async def test_revoke_before_login_is_a_noop(auth_url):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, cookie_jar=_jar()) as auth:
        await auth.revoke()
        assert auth.token is None


async def test_supplied_session_is_not_closed(auth_url):
    async with aiohttp.ClientSession() as session:
        auth = PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, session=session, cookie_jar=_jar())
        await auth.access_token()
        await auth.close()
        assert not session.closed


async def test_closed_supplied_session_falls_back_to_a_fresh_login(auth_url):
    # The refresh grant needs the supplied session; login does not, since it
    # runs on its own private one. A closed session degrades to a re-login
    # rather than failing outright.
    session = aiohttp.ClientSession()
    await session.close()
    auth = PasswordAuth(EMAIL, PASSWORD, base_url=auth_url, session=session, cookie_jar=_jar())
    assert await auth.access_token() == "access-1"
    assert await auth.refresh() == "access-1"


async def test_base_auth_refresh_defaults_to_current_token():
    class Static(Auth):
        async def access_token(self) -> str:
            return "static"

    auth = Static()
    assert await auth.refresh() == "static"
    await auth.close()


def test_token_is_expired_without_expiry():
    assert Token(access_token="a").is_expired is False


# --- login flow failures -----------------------------------------------------


@pytest.fixture
async def broken_server_url(request):
    """A server that fails the login flow in a specific way."""
    app = web.Application()
    mode = request.param

    async def authorize(req):
        if mode == "no_location":
            return web.Response(status=302)  # a redirect with no Location
        if mode == "loop":
            raise web.HTTPFound("/oauth/authorize?again=1")
        if mode == "denied":
            raise web.HTTPFound(f"{REDIRECT_URI}?error=access_denied")
        if mode == "no_code":
            raise web.HTTPFound(f"{REDIRECT_URI}?state=whatever")
        if mode == "bad_state":
            raise web.HTTPFound(f"{REDIRECT_URI}?code=abc&state=not-the-state-we-sent")
        if mode == "no_csrf":
            return web.Response(text="<html><head></head></html>", content_type="text/html")
        raise AssertionError(mode)

    app.router.add_get("/oauth/authorize", authorize)
    server = TestServer(app, host="localhost")
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("broken_server_url", "message"),
    [
        ("no_location", "without a Location"),
        ("loop", "too many redirects"),
        ("denied", "access_denied"),
        ("no_code", "no authorization code"),
        ("bad_state", "state mismatch"),
        ("no_csrf", "CSRF token"),
    ],
    indirect=["broken_server_url"],
)
async def test_login_failures_are_reported(broken_server_url, message):
    async with PasswordAuth(EMAIL, PASSWORD, base_url=broken_server_url, cookie_jar=_jar()) as auth:
        with pytest.raises(AuthenticationError, match=message):
            await auth.access_token()


@pytest.fixture
async def bad_token_endpoint_url(request):
    """A server that reaches the token exchange, then fails it."""
    app = web.Application()
    mode = request.param

    async def authorize(req):
        raise web.HTTPFound(f"{REDIRECT_URI}?code={AUTH_CODE}&state={req.query.get('state', '')}")

    async def token(req):
        if mode == "error_json":
            return web.json_response(
                {"error": "invalid_grant", "error_description": "code expired"}, status=400
            )
        if mode == "not_json":
            return web.Response(text="<html>gateway error</html>", content_type="text/html")
        if mode == "no_token":
            return web.json_response({"token_type": "Bearer"})
        raise AssertionError(mode)

    app.router.add_get("/oauth/authorize", authorize)
    app.router.add_post("/oauth/token", token)
    server = TestServer(app, host="localhost")
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("bad_token_endpoint_url", "message"),
    [
        ("error_json", "code expired"),
        ("not_json", "HTTP 200"),
        ("no_token", "no access_token"),
    ],
    indirect=["bad_token_endpoint_url"],
)
async def test_token_exchange_failures_are_reported(bad_token_endpoint_url, message):
    async with PasswordAuth(
        EMAIL, PASSWORD, base_url=bad_token_endpoint_url, cookie_jar=_jar()
    ) as auth:
        with pytest.raises(AuthenticationError, match=message):
            await auth.access_token()


async def test_token_without_expires_in_has_no_expiry():
    app = web.Application()

    async def authorize(req):
        raise web.HTTPFound(f"{REDIRECT_URI}?code={AUTH_CODE}&state={req.query.get('state', '')}")

    async def token(req):
        return web.json_response({"access_token": "no-expiry"})

    app.router.add_get("/oauth/authorize", authorize)
    app.router.add_post("/oauth/token", token)
    server = TestServer(app, host="localhost")
    await server.start_server()
    try:
        url = str(server.make_url("")).rstrip("/")
        async with PasswordAuth(EMAIL, PASSWORD, base_url=url, cookie_jar=_jar()) as auth:
            assert await auth.access_token() == "no-expiry"
            assert auth.token.expires_at is None
            assert auth.token.token_type == "Bearer"
    finally:
        await server.close()
