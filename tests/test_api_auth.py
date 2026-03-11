"""Integration-style tests for MinolApiClient.authenticate().

All HTTP traffic is mocked via unittest.mock — no real network calls are made.

The 5-step Azure B2C / SAML flow under test:
  1. GET B2C_ENTRY_URL  → follows redirects to B2C login page (HTML with $Config)
  2. Parse $Config (CSRF token, transId, policy)
  3. POST SelfAsserted → must return {"status": "200"}
  4. GET confirmed     → triggers SAML redirect back to Minol (sets MYSAPSSO2)
  5. Verify MYSAPSSO2 cookie is present

aiohttp session methods (session.get / session.post) are used as **async context
managers**, i.e. ``async with session.get(...) as resp``.  The MagicMock helper
``_make_resp`` builds a suitable context-manager mock without relying on
AsyncMock.side_effect (which creates unawaited coroutines).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

from custom_components.minol_energy.api import (
    MinolApiClient,
    MinolAuthError,
    MinolConnectionError,
)
from custom_components.minol_energy.const import B2C_ENTRY_URL

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

B2C_PAGE_URL = (
    "https://minolauth.b2clogin.com"
    "/minolauth.onmicrosoft.com/B2C_1A_SIGNIN"
    "/oauth2/v2.0/authorize"
)
B2C_BASE = (
    "https://minolauth.b2clogin.com"
    "/minolauth.onmicrosoft.com/B2C_1A_SIGNIN"
)
CSRF = "test-csrf"
TRANS_ID = "StateProperties=ABCDEF"
POLICY = "B2C_1A_SIGNIN"

SELF_ASSERTED_URL = f"{B2C_BASE}/SelfAsserted?tx={TRANS_ID}&p={POLICY}"
CONFIRMED_URL = (
    f"{B2C_BASE}/api/{TRANS_ID}/confirmed"
    f"?csrf_token={CSRF}&tx={TRANS_ID}&p={POLICY}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b2c_html(csrf: str = CSRF, trans_id: str = TRANS_ID, policy: str = POLICY) -> str:
    """Return a minimal B2C login page that authenticate() can parse."""
    config = json.dumps({"csrf": csrf, "transId": trans_id, "policy": policy})
    return f"<html><script>$Config={config};\n</script></html>"


def _make_resp(text: str, url: str = B2C_PAGE_URL, status: int = 200) -> MagicMock:
    """Return a MagicMock that behaves like an aiohttp response inside ``async with``.

    aiohttp's ``session.get(...)`` / ``session.post(...)`` return objects that
    implement the async context-manager protocol.  We need:
      - ``__aenter__`` to return an awaitable that yields *resp*
      - ``__aexit__`` to return an awaitable that returns False
      - ``resp.url`` (str)
      - ``resp.text()`` → awaitable returning *text*
      - ``resp.status`` (int)
    """
    resp = MagicMock()
    resp.url = url
    resp.status = status

    async def _text():
        return text

    resp.text = _text

    cm = MagicMock()

    async def _aenter(_self):
        return resp

    async def _aexit(_self, *args):
        return False

    cm.__aenter__ = _aenter
    cm.__aexit__ = _aexit
    return cm


def _make_mock_session(
    get_responses: list[MagicMock],
    post_responses: list[MagicMock] | None = None,
    cookie_keys: list[str] | None = None,
) -> MagicMock:
    """Build a minimal mock aiohttp.ClientSession.

    ``session.get(...)`` and ``session.post(...)`` are non-async callables
    (like the real aiohttp) that return async context managers.
    ``session.cookie_jar`` is an iterable of MagicMocks with a ``.key`` attribute.
    """
    session = MagicMock()

    get_iter = iter(get_responses)

    def _get(url, **kwargs):
        return next(get_iter)

    session.get = _get

    if post_responses is not None:
        post_iter = iter(post_responses)

        def _post(url, **kwargs):
            return next(post_iter)

        session.post = _post

    if cookie_keys is not None:
        cookies = []
        for key in cookie_keys:
            c = MagicMock()
            c.key = key
            cookies.append(c)
        session.cookie_jar = cookies

    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAuthenticate:
    """Tests for MinolApiClient.authenticate()."""

    async def test_success(self):
        """Full happy-path: all 5 steps succeed → authenticate() returns True."""
        client = MinolApiClient(username="user@example.com", password="secret")

        session = _make_mock_session(
            get_responses=[
                _make_resp(_b2c_html(), url=B2C_PAGE_URL),       # step 1
                _make_resp("", url=CONFIRMED_URL),                 # step 4
            ],
            post_responses=[
                _make_resp('{"status":"200"}'),                    # step 3
            ],
            cookie_keys=["MYSAPSSO2"],
        )

        with patch.object(client, "_ensure_session", return_value=session):
            result = await client.authenticate()

        assert result is True

    async def test_invalid_credentials(self):
        """SelfAsserted returns status≠'200' → MinolAuthError is raised."""
        client = MinolApiClient(username="user@example.com", password="wrong")

        session = _make_mock_session(
            get_responses=[_make_resp(_b2c_html(), url=B2C_PAGE_URL)],
            post_responses=[
                _make_resp(
                    '{"status":"400","message":"Incorrect username or password."}'
                )
            ],
        )

        with patch.object(client, "_ensure_session", return_value=session):
            with pytest.raises(MinolAuthError, match="Authentication failed"):
                await client.authenticate()

    async def test_no_b2c_config_on_page(self):
        """B2C page without $Config → MinolAuthError ('$Config not found')."""
        client = MinolApiClient(username="user@example.com", password="secret")

        session = _make_mock_session(
            get_responses=[_make_resp("<html>No config here</html>", url=B2C_PAGE_URL)]
        )

        with patch.object(client, "_ensure_session", return_value=session):
            with pytest.raises(MinolAuthError, match=r"\$Config not found"):
                await client.authenticate()

    async def test_incomplete_b2c_settings(self):
        """$Config present but missing transId and policy → MinolAuthError."""
        client = MinolApiClient(username="user@example.com", password="secret")

        incomplete_html = (
            '<html><script>$Config={"csrf":"tok"};\n</script></html>'
        )
        session = _make_mock_session(
            get_responses=[_make_resp(incomplete_html, url=B2C_PAGE_URL)]
        )

        with patch.object(client, "_ensure_session", return_value=session):
            with pytest.raises(MinolAuthError, match="Incomplete B2C settings"):
                await client.authenticate()

    async def test_no_sso_cookie(self):
        """All steps succeed but MYSAPSSO2 absent in cookie jar → MinolAuthError."""
        client = MinolApiClient(username="user@example.com", password="secret")

        session = _make_mock_session(
            get_responses=[
                _make_resp(_b2c_html(), url=B2C_PAGE_URL),
                _make_resp("", url=CONFIRMED_URL),
            ],
            post_responses=[_make_resp('{"status":"200"}')],
            cookie_keys=[],  # deliberately empty — no MYSAPSSO2
        )

        with patch.object(client, "_ensure_session", return_value=session):
            with pytest.raises(MinolAuthError, match="no MYSAPSSO2 cookie"):
                await client.authenticate()

    async def test_connection_error_on_initial_get(self):
        """aiohttp.ClientError on step 1 GET → MinolConnectionError."""
        client = MinolApiClient(username="user@example.com", password="secret")

        session = MagicMock()

        def _get_raises(url, **kwargs):
            raise aiohttp.ClientConnectionError("Connection refused")

        session.get = _get_raises

        with patch.object(client, "_ensure_session", return_value=session):
            with pytest.raises(MinolConnectionError, match="Cannot reach Minol portal"):
                await client.authenticate()

    async def test_self_asserted_invalid_json_treated_as_failure(self):
        """Non-JSON body from SelfAsserted is treated as auth failure."""
        client = MinolApiClient(username="user@example.com", password="secret")

        session = _make_mock_session(
            get_responses=[_make_resp(_b2c_html(), url=B2C_PAGE_URL)],
            post_responses=[_make_resp("not-json-at-all")],
        )

        with patch.object(client, "_ensure_session", return_value=session):
            with pytest.raises(MinolAuthError, match="Authentication failed"):
                await client.authenticate()
