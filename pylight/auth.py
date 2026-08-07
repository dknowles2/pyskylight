"""Authentication for the Skylight API.

Skylight uses OAuth 2.0 authorization code + PKCE, with credential entry handled
by a server-rendered Rails login form:

1. ``GET /oauth/authorize`` redirects to ``/auth/session/new`` and sets a
   ``skylightcloud_session`` cookie. The login page carries a Rails CSRF token
   in ``<meta name="csrf-token">``.
2. ``POST /auth/session`` submits ``authenticity_token``/``email``/``password``
   and redirects back to ``/oauth/authorize``.
3. ``/oauth/authorize`` then redirects to the app's ``redirect_uri`` with
   ``?code=...&state=...``.
4. ``POST /oauth/token`` exchanges the code (plus the PKCE ``code_verifier``)
   for an access token and refresh token.

:class:`PasswordAuth` drives that flow headlessly. :class:`TokenAuth` skips it
when a token was obtained some other way.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp

from .const import (
    BASE_URL,
    CLIENT_ID,
    DEFAULT_TIMEOUT,
    REDIRECT_URI,
    SCOPE,
    TOKEN_EXPIRY_MARGIN,
    USER_AGENT,
)
from .exceptions import AuthenticationError
from .models import Token

__all__ = ["Auth", "TokenAuth", "PasswordAuth"]

_CSRF_RE = re.compile(
    r"""<meta[^>]+name=["']csrf-token["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _pkce_pair() -> tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` pair for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _csrf_token(page: str) -> str:
    match = _CSRF_RE.search(page)
    if not match:
        raise AuthenticationError("could not find a CSRF token on the login page")
    return html.unescape(match.group(1))


class Auth(ABC):
    """Supplies (and refreshes) the bearer token used for API requests."""

    @abstractmethod
    async def access_token(self) -> str:
        """Return a currently-valid access token, logging in if needed."""

    async def refresh(self) -> str:
        """Force a new access token and return it.

        Called by the client after a ``401``. The default implementation just
        returns the existing token.
        """
        return await self.access_token()

    async def close(self) -> None:
        """Release any resources owned by this object."""
        return None


class TokenAuth(Auth):
    """Uses a token you already have — e.g. captured from app traffic.

    Args:
        access_token: The bearer token to send.
    """

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    async def access_token(self) -> str:
        """Return the token this object was constructed with."""
        return self._access_token

    async def refresh(self) -> str:
        """Always fail: a static token cannot be refreshed."""
        raise AuthenticationError(
            "the supplied access token was rejected and TokenAuth cannot refresh it"
        )


class PasswordAuth(Auth):
    """Logs in with an email and password and keeps the token fresh.

    Args:
        email: Skylight account email.
        password: Skylight account password.
        session: Optional session used for token/revoke calls. The interactive
            login always runs on a private session so the Rails login cookie
            never leaks into an application-wide cookie jar.
        base_url: Override the auth host. Useful for tests.
        timeout: Total timeout, in seconds, for each auth request.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        session: aiohttp.ClientSession | None = None,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._email = email
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._token: Token | None = None
        self._lock = asyncio.Lock()

    @property
    def token(self) -> Token | None:
        """The current token set, if a login has happened."""
        return self._token

    async def __aenter__(self) -> PasswordAuth:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the internally-created session, if any."""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        if self._owns_session:
            self._session = None

    async def access_token(self) -> str:
        """Return a valid access token, logging in or refreshing as needed."""
        async with self._lock:
            if self._token is None:
                self._token = await self._login()
            elif self._needs_refresh(self._token):
                self._token = await self._refresh_or_login(self._token)
            return self._token.access_token

    async def refresh(self) -> str:
        """Mint a new access token, by refresh grant if possible."""
        async with self._lock:
            self._token = await self._refresh_or_login(self._token)
            return self._token.access_token

    async def revoke(self) -> None:
        """Revoke the current access token and forget it (sign out)."""
        async with self._lock:
            token = self._token
            self._token = None
            if token is None:
                return
            session = await self._get_session()
            async with session.post(
                f"{self._base_url}/oauth/revoke",
                data={"token": token.access_token, "client_id": CLIENT_ID},
                headers=self._headers(),
                timeout=self._timeout,
            ) as resp:
                await resp.read()

    @staticmethod
    def _needs_refresh(token: Token) -> bool:
        if token.expires_at is None:
            return False
        margin = timedelta(seconds=TOKEN_EXPIRY_MARGIN)
        return datetime.now(token.expires_at.tzinfo) >= token.expires_at - margin

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"User-Agent": USER_AGENT}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if not self._owns_session:
                raise AuthenticationError("the supplied aiohttp session is closed")
            self._session = aiohttp.ClientSession()
        return self._session

    async def _refresh_or_login(self, token: Token | None) -> Token:
        if token is not None and token.refresh_token:
            try:
                return await self._refresh(token.refresh_token)
            except AuthenticationError:
                pass
        return await self._login()

    async def _refresh(self, refresh_token: str) -> Token:
        session = await self._get_session()
        return await self._exchange(
            session,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )

    async def _login(self) -> Token:
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)

        # A private cookie jar: the Rails session cookie is scoped to this flow.
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            authorize_params = {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "scope": SCOPE,
                "redirect_uri": REDIRECT_URI,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            page, _, code = await self._follow(
                session, "GET", f"{self._base_url}/oauth/authorize", params=authorize_params
            )
            if code is None:
                if page is None:
                    raise AuthenticationError("authorization request returned no login page")
                page, _, code = await self._follow(
                    session,
                    "POST",
                    f"{self._base_url}/auth/session",
                    data={
                        "authenticity_token": _csrf_token(page),
                        "email": self._email,
                        "password": self._password,
                    },
                    state=state,
                )
            if code is None:
                raise AuthenticationError(
                    "login did not yield an authorization code; check the email and password"
                )

            return await self._exchange(
                session,
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                },
            )

    async def _follow(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        state: str | None = None,
        max_redirects: int = 10,
    ) -> tuple[str | None, str | None, str | None]:
        """Walk redirects by hand, stopping at the OAuth redirect URI.

        Returns ``(body, final_url, code)``. ``code`` is set only when the chain
        reached :data:`~pylight.const.REDIRECT_URI`, which is never fetched: the
        authorization code is read straight out of the ``Location`` header.
        """
        for _ in range(max_redirects):
            async with session.request(
                method,
                url,
                params=params,
                data=data,
                allow_redirects=False,
                timeout=self._timeout,
            ) as resp:
                if resp.status not in _REDIRECT_STATUSES:
                    return await resp.text(), str(resp.url), None
                location = resp.headers.get("Location", "")
                if not location:
                    raise AuthenticationError(f"HTTP {resp.status} redirect without a Location")
                target = urljoin(str(resp.url), location)

            if target.startswith(REDIRECT_URI):
                query = parse_qs(urlsplit(target).query)
                if error := query.get("error"):
                    raise AuthenticationError(f"authorization denied: {error[0]}")
                code = query.get("code", [None])[0]
                returned_state = query.get("state", [None])[0]
                if code is None:
                    raise AuthenticationError("redirect carried no authorization code")
                if state is not None and returned_state != state:
                    raise AuthenticationError("OAuth state mismatch; aborting login")
                return None, target, code

            url, params, data = target, None, None
            if resp.status in (301, 302, 303):
                method = "GET"

        raise AuthenticationError("too many redirects during login")

    async def _exchange(self, session: aiohttp.ClientSession, data: dict[str, str]) -> Token:
        async with session.post(
            f"{self._base_url}/oauth/token",
            data=data,
            headers=self._headers(),
            timeout=self._timeout,
        ) as resp:
            body: Any = None
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = None
            if resp.status != 200 or not isinstance(body, dict):
                detail = ""
                if isinstance(body, dict):
                    detail = str(body.get("error_description") or body.get("error") or "")
                raise AuthenticationError(
                    f"token endpoint returned HTTP {resp.status}"
                    + (f": {detail}" if detail else "")
                )

        access_token = body.get("access_token")
        if not access_token:
            raise AuthenticationError("token endpoint returned no access_token")

        expires_at = None
        if isinstance(expires_in := body.get("expires_in"), (int, float)):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))

        return Token(
            access_token=access_token,
            refresh_token=body.get("refresh_token"),
            expires_at=expires_at,
            token_type=body.get("token_type") or "Bearer",
        )
