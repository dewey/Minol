"""Tests for MinolApiClient.authenticate() and its B2C/SAML parsing helpers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from aioresponses import aioresponses

from custom_components.minol_energy.api import (
    MinolApiClient,
    MinolAuthError,
    MinolConnectionError,
    _build_confirmed_url,
    _extract_b2c_csrf,
    _parse_b2c_login_form,
    _parse_saml_post_form,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"

ENTRY_URL = "https://webservices.minol.com/?redirect2=true"
B2C_SAML_URL = (
    "https://minolauth.b2clogin.com/minolauth.onmicrosoft.com"
    "/B2C_1A_Signup_Signin_Groups_SAML/samlp/sso/login"
    "?SAMLRequest=FAKEREQUEST&RelayState=test"
)
SELF_ASSERTED_URL = (
    "https://minolauth.b2clogin.com/minolauth.onmicrosoft.com"
    "/B2C_1A_Signup_Signin_Groups_SAML"
    "/SelfAsserted?tx=StateProperties=AAABBBCCC&p=B2C_1A_Signup_Signin_Groups_SAML"
)
SAML_HANDLER_URL = (
    "https://webservices.minol.com/minol.com~kundenportal~login~saml/"
    "?logonTargetUrl=https%3A%2F%2Fwebservices.minol.com%2F%3Fredirect2%3Dtrue"
    "&saml2idp=B2C-Minol-Tenant"
)

PORTAL_PAGE_AUTHENTICATED = (
    "<html><body>"
    "<span>Liegenschaft:</span>"
    "<div>Heizung 1234 kWh</div>"
    "</body></html>"
)
PORTAL_PAGE_UNAUTHENTICATED = "<html><body><span>Bitte anmelden</span></body></html>"

# Regex patterns for URLs with unpredictable query strings.
RE_B2C_SAML = re.compile(r"https://minolauth\.b2clogin\.com/.*/samlp/sso/login.*")
RE_SELF_ASSERTED = re.compile(r"https://minolauth\.b2clogin\.com/.*/SelfAsserted.*")
RE_CONFIRMED = re.compile(r"https://minolauth\.b2clogin\.com/.*/confirmed.*")
RE_SAML_HANDLER = re.compile(r"https://webservices\.minol\.com/minol\.com~kundenportal~login~saml/.*")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def b2c_login_html() -> str:
    return (FIXTURES / "b2c_login.html").read_text()


@pytest.fixture()
def b2c_saml_post_html() -> str:
    return (FIXTURES / "b2c_saml_post.html").read_text()


# ---------------------------------------------------------------------------
# Mock helper
# ---------------------------------------------------------------------------

def _mock_full_auth_flow(
    mock: aioresponses,
    b2c_login_html: str,
    b2c_saml_post_html: str,
    *,
    self_asserted_status_json: dict | None = None,
    portal_page_html: str = PORTAL_PAGE_AUTHENTICATED,
) -> None:
    """Register all mocked responses for a complete auth flow.

    Flow:
      1. GET ENTRY_URL → 302 to B2C SAML login page
      2. GET B2C SAML login page → 200 HTML (b2c_login.html fixture)
      3. POST SelfAsserted → 200 JSON {"status": "200"}
      4. GET confirmed → 200 HTML with SAML auto-post form (b2c_saml_post.html)
      5. POST SAML handler → 302 → 302 → 200 portal page
    """
    if self_asserted_status_json is None:
        self_asserted_status_json = {"status": "200"}

    # 1. Portal entry → redirect to B2C.
    mock.get(ENTRY_URL, status=302, headers={"Location": B2C_SAML_URL})

    # 2. B2C login page.
    mock.get(
        RE_B2C_SAML,
        status=200,
        body=b2c_login_html,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

    # 3. SelfAsserted POST → B2C JSON.
    mock.post(
        RE_SELF_ASSERTED,
        status=200,
        payload=self_asserted_status_json,
        headers={"Content-Type": "application/json"},
    )

    # 4. Confirmed GET → SAML auto-post HTML (allow_redirects=False, so 200 body).
    mock.get(
        RE_CONFIRMED,
        status=200,
        body=b2c_saml_post_html,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

    # 5. SAML handler POST → redirects → portal landing page.
    mock.post(
        RE_SAML_HANDLER,
        status=302,
        headers={"Location": ENTRY_URL},
    )
    mock.get(
        ENTRY_URL,
        status=200,
        body=portal_page_html,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# Unit tests: _parse_b2c_login_form
# ---------------------------------------------------------------------------

class TestParseB2CLoginForm:
    def test_extracts_action_and_hidden_fields(self, b2c_login_html: str) -> None:
        action, fields = _parse_b2c_login_form(b2c_login_html, "https://example.com/")

        assert action == SELF_ASSERTED_URL
        assert fields["tx"] == "StateProperties=AAABBBCCC"
        assert fields["p"] == "B2C_1A_Signup_Signin_Groups_SAML"
        assert fields["request_type"] == "RESPONSE"
        # Non-hidden inputs must NOT appear.
        assert "signInName" not in fields
        assert "password" not in fields

    def test_relative_action_is_made_absolute(self) -> None:
        html = (
            '<form id="localAccountForm" action="/tenant/SelfAsserted?tx=X&p=Y">'
            '<input type="hidden" name="tx" value="X"/>'
            "</form>"
        )
        action, _ = _parse_b2c_login_form(html, "https://login.example.com/path/page")
        assert action == "https://login.example.com/tenant/SelfAsserted?tx=X&p=Y"

    def test_falls_back_to_first_form(self) -> None:
        html = (
            '<form action="https://login.example.com/SelfAsserted?tx=X&p=Y">'
            '<input type="hidden" name="tx" value="X"/>'
            "</form>"
        )
        action, fields = _parse_b2c_login_form(html, "https://login.example.com/")
        assert "SelfAsserted" in action
        assert fields["tx"] == "X"

    def test_raises_when_no_form_found(self) -> None:
        with pytest.raises(MinolAuthError, match="form not found"):
            _parse_b2c_login_form("<html><body>No form here</body></html>", "https://x.com/")


# ---------------------------------------------------------------------------
# Unit tests: _extract_b2c_csrf
# ---------------------------------------------------------------------------

class TestExtractB2CCsrf:
    def test_extracts_csrf_from_settings(self, b2c_login_html: str) -> None:
        csrf = _extract_b2c_csrf(b2c_login_html)
        assert csrf == "FAKECSRFTOKENFROMPAGE"

    def test_returns_empty_string_when_missing(self) -> None:
        assert _extract_b2c_csrf("<html><body>no settings here</body></html>") == ""

    def test_handles_whitespace_around_colon(self) -> None:
        html = 'var SETTINGS = {"csrf" : "token123"};'
        assert _extract_b2c_csrf(html) == "token123"


# ---------------------------------------------------------------------------
# Unit tests: _build_confirmed_url
# ---------------------------------------------------------------------------

class TestBuildConfirmedUrl:
    def test_replaces_selfasserted_path(self) -> None:
        url = _build_confirmed_url(SELF_ASSERTED_URL, csrf_token="testcsrf")
        assert "/api/CombinedSigninAndSignup/confirmed" in url
        assert "SelfAsserted" not in url

    def test_carries_tx_and_p(self) -> None:
        url = _build_confirmed_url(SELF_ASSERTED_URL, csrf_token="tok")
        assert "tx=" in url
        assert "p=B2C_1A_Signup_Signin_Groups_SAML" in url

    def test_includes_csrf_token(self) -> None:
        url = _build_confirmed_url(SELF_ASSERTED_URL, csrf_token="mytoken")
        assert "csrf_token=mytoken" in url

    def test_preserves_host(self) -> None:
        url = _build_confirmed_url(SELF_ASSERTED_URL, csrf_token="x")
        assert url.startswith("https://minolauth.b2clogin.com/")


# ---------------------------------------------------------------------------
# Unit tests: _parse_saml_post_form
# ---------------------------------------------------------------------------

class TestParseSamlPostForm:
    def test_extracts_action_and_saml_fields(self, b2c_saml_post_html: str) -> None:
        action, fields = _parse_saml_post_form(b2c_saml_post_html, "https://b2c.example.com/")

        assert "minol.com~kundenportal~login~saml" in action
        assert "SAMLResponse" in fields
        assert fields["SAMLResponse"] == "FAKESAMLBASE64RESPONSEVALUE"
        assert fields["RelayState"] == "ouccparpccpc"

    def test_raises_when_no_form(self) -> None:
        with pytest.raises(MinolAuthError, match="SAML auto-post form not found"):
            _parse_saml_post_form("<html><body>error</body></html>", "https://x.com/")

    def test_raises_when_no_saml_response_field(self) -> None:
        html = (
            '<form action="https://example.com/saml">'
            '<input type="hidden" name="RelayState" value="foo"/>'
            "</form>"
        )
        with pytest.raises(MinolAuthError, match="SAMLResponse field missing"):
            _parse_saml_post_form(html, "https://x.com/")


# ---------------------------------------------------------------------------
# Integration tests: MinolApiClient.authenticate()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authenticate_success(b2c_login_html: str, b2c_saml_post_html: str) -> None:
    """Full happy path: entry → B2C form → POST → SAML → portal Liegenschaft."""
    client = MinolApiClient("user@example.com", "correct-password")
    try:
        with aioresponses() as mock:
            _mock_full_auth_flow(mock, b2c_login_html, b2c_saml_post_html)
            result = await client.authenticate()
        assert result is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_posts_to_selfasserted_and_saml_handler(
    b2c_login_html: str, b2c_saml_post_html: str
) -> None:
    """Credentials go to SelfAsserted; SAMLResponse goes to the SAML handler."""
    client = MinolApiClient("user@example.com", "s3cr3t")
    try:
        with aioresponses() as mock:
            _mock_full_auth_flow(mock, b2c_login_html, b2c_saml_post_html)
            await client.authenticate()

        post_urls = [str(url) for method, url in mock.requests if method == "POST"]
        assert any("SelfAsserted" in u for u in post_urls), (
            f"Expected POST to SelfAsserted, got: {post_urls}"
        )
        assert any("kundenportal~login~saml" in u for u in post_urls), (
            f"Expected POST to SAML handler, got: {post_urls}"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_uses_signinname_field(
    b2c_login_html: str, b2c_saml_post_html: str
) -> None:
    """Credentials must be sent as 'signInName', not 'logonIdentifier'."""
    client = MinolApiClient("user@example.com", "s3cr3t")
    try:
        with aioresponses() as mock:
            _mock_full_auth_flow(mock, b2c_login_html, b2c_saml_post_html)
            await client.authenticate()

        # Inspect the SelfAsserted POST call
        post_calls = [
            (url, calls)
            for (method, url), calls in mock.requests.items()
            if method == "POST" and "SelfAsserted" in str(url)
        ]
        assert post_calls, "No SelfAsserted POST found"
        call_kwargs = post_calls[0][1][0].kwargs
        sent_data = call_kwargs.get("data", {})
        assert "signInName" in sent_data, (
            f"Expected 'signInName' in POST data, got: {list(sent_data.keys())}"
        )
        assert "logonIdentifier" not in sent_data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_wrong_password(
    b2c_login_html: str, b2c_saml_post_html: str
) -> None:
    """B2C returns error status → MinolAuthError."""
    client = MinolApiClient("user@example.com", "wrong-password")
    try:
        with aioresponses() as mock:
            _mock_full_auth_flow(
                mock,
                b2c_login_html,
                b2c_saml_post_html,
                self_asserted_status_json={
                    "status": "400",
                    "message": "Your password is incorrect.",
                },
            )
            with pytest.raises(MinolAuthError, match="B2C login rejected"):
                await client.authenticate()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_portal_content_check_fails(
    b2c_login_html: str, b2c_saml_post_html: str
) -> None:
    """Auth flow completes but portal page lacks 'Liegenschaft' → MinolAuthError.

    This catches the case where credentials work in B2C but the SAP session
    was not properly established (e.g. wrong tenant, incomplete SAML assertion).
    """
    client = MinolApiClient("user@example.com", "correct-password")
    try:
        with aioresponses() as mock:
            _mock_full_auth_flow(
                mock,
                b2c_login_html,
                b2c_saml_post_html,
                portal_page_html=PORTAL_PAGE_UNAUTHENTICATED,
            )
            with pytest.raises(MinolAuthError, match="Liegenschaft"):
                await client.authenticate()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_portal_unreachable() -> None:
    """Network error on first GET → MinolConnectionError."""
    import aiohttp as _aiohttp

    client = MinolApiClient("user@example.com", "password")
    try:
        with aioresponses() as mock:
            mock.get(ENTRY_URL, exception=_aiohttp.ClientConnectionError("timeout"))
            with pytest.raises(MinolConnectionError, match="Cannot reach"):
                await client.authenticate()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_b2c_page_has_no_form() -> None:
    """B2C returns HTML without a parseable form → MinolAuthError."""
    client = MinolApiClient("user@example.com", "password")
    try:
        with aioresponses() as mock:
            mock.get(ENTRY_URL, status=302, headers={"Location": B2C_SAML_URL})
            mock.get(
                RE_B2C_SAML,
                status=200,
                body="<html><body>Unexpected error page</body></html>",
                headers={"Content-Type": "text/html"},
            )
            with pytest.raises(MinolAuthError, match="form not found"):
                await client.authenticate()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_login_page_http_error() -> None:
    """B2C login page returns non-200 → MinolAuthError."""
    client = MinolApiClient("user@example.com", "password")
    try:
        with aioresponses() as mock:
            mock.get(ENTRY_URL, status=302, headers={"Location": B2C_SAML_URL})
            mock.get(RE_B2C_SAML, status=503, body="Service Unavailable")
            with pytest.raises(MinolAuthError, match="HTTP 503"):
                await client.authenticate()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticate_saml_form_missing(b2c_login_html: str) -> None:
    """confirmed endpoint returns page without SAML form → MinolAuthError."""
    client = MinolApiClient("user@example.com", "password")
    try:
        with aioresponses() as mock:
            mock.get(ENTRY_URL, status=302, headers={"Location": B2C_SAML_URL})
            mock.get(
                RE_B2C_SAML,
                status=200,
                body=b2c_login_html,
                headers={"Content-Type": "text/html"},
            )
            mock.post(
                RE_SELF_ASSERTED,
                status=200,
                payload={"status": "200"},
                headers={"Content-Type": "application/json"},
            )
            mock.get(
                RE_CONFIRMED,
                status=200,
                body="<html><body>Something went wrong</body></html>",
                headers={"Content-Type": "text/html"},
            )
            with pytest.raises(MinolAuthError, match="SAML auto-post form not found"):
                await client.authenticate()
    finally:
        await client.close()
