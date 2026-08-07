"""Constants for the Skylight API."""

from __future__ import annotations

#: Root of the Skylight web/API host.
BASE_URL = "https://app.ourskylight.com"

#: Prefix shared by every REST endpoint.
API_PREFIX = "/api"

#: OAuth client identifier used by the Skylight mobile/web app.
CLIENT_ID = "skylight-mobile"

#: Redirect target registered for :data:`CLIENT_ID`. The host is *not* the API
#: host; the client only ever detects the redirect, it never follows it.
REDIRECT_URI = "https://ourskylight.com/welcome"

#: OAuth scope requested by the first-party app.
SCOPE = "everything"

#: Value sent in the ``Skylight-Api-Version`` header.
API_VERSION = "2026-05-01"

#: Value sent in the ``User-Agent`` header.
USER_AGENT = "SkylightMobile (web)"

#: Refresh the access token this many seconds before it actually expires.
TOKEN_EXPIRY_MARGIN = 60

#: Default total timeout, in seconds, for a single request.
DEFAULT_TIMEOUT = 30
