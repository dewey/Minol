"""API client for the Minol tenant portal (SAP NetWeaver eMonitoring)."""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, parse_qs, urlunparse

import aiohttp

from .const import (
    BASE_URL,
    EMDATA_REST,
    LOGIN_ENTRY_URL,
    NUDATA_REST,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# String that only appears on an authenticated SAP portal page.
_PORTAL_AUTH_MARKER = "Liegenschaft"


class MinolAuthError(Exception):
    """Raised when authentication with the Minol portal fails."""


class MinolConnectionError(Exception):
    """Raised when the Minol portal cannot be reached."""


# ---------------------------------------------------------------------------
# B2C / SAML page helpers (module-level for testability)
# ---------------------------------------------------------------------------

class _B2CFormParser(HTMLParser):
    """Extract the first (or #localAccountForm) form and its hidden fields."""

    def __init__(self) -> None:
        super().__init__()
        self._in_form = False
        self.action: str | None = None
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if tag == "form":
            if d.get("id") == "localAccountForm" or self.action is None:
                self._in_form = True
                self.action = d.get("action") or ""
        elif tag == "input" and self._in_form:
            if d.get("type") == "hidden" and d.get("name"):
                self.fields[d["name"]] = d.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def _parse_b2c_login_form(html: str, page_url: str) -> tuple[str, dict[str, str]]:
    """Return (absolute_action_url, hidden_fields) from a B2C login page.

    Raises MinolAuthError if no parseable form is found.
    """
    parser = _B2CFormParser()
    parser.feed(html)

    if not parser.action:
        raise MinolAuthError("B2C login form not found in page")

    action = parser.action
    if not action.startswith("http"):
        action = urljoin(page_url, action)

    return action, parser.fields


def _extract_b2c_csrf(html: str) -> str:
    """Extract the CSRF token from the SETTINGS JS object on the B2C login page.

    Azure B2C embeds ``var SETTINGS = {...}`` in the page.  The ``csrf`` field
    is the token required for the confirmed endpoint – more reliable than the
    ``x-ms-cpim-csrf`` cookie (which aiohttp may not receive in all setups).
    Returns an empty string when the pattern is not found.
    """
    match = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    return match.group(1) if match else ""


def _build_confirmed_url(self_asserted_url: str, csrf_token: str) -> str:
    """Derive the B2C 'confirmed' URL from the SelfAsserted action URL.

    Azure B2C custom policies complete the auth flow when the client GETs:
      .../api/CombinedSigninAndSignup/confirmed?rememberMe=false
          &csrf_token=<SETTINGS.csrf>
          &tx=<from original query>&p=<policy name>
    """
    parsed = urlparse(self_asserted_url)
    path = re.sub(r"/SelfAsserted$", "/api/CombinedSigninAndSignup/confirmed", parsed.path)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    confirmed_qs = urlencode({
        "rememberMe": "false",
        "csrf_token": csrf_token,
        "tx": qs.get("tx", [""])[0],
        "p": qs.get("p", [""])[0],
    })
    return urlunparse(parsed._replace(path=path, query=confirmed_qs))


def _parse_saml_post_form(html: str, page_url: str) -> tuple[str, dict[str, str]]:
    """Parse the SAML auto-post form returned by the B2C confirmed endpoint.

    Azure B2C delivers the SAML Response as an HTML page containing a form
    that the browser auto-submits to the portal's SAML handler.  We parse that
    form to replicate the submission.

    Returns (absolute_action_url, form_fields_including_SAMLResponse).
    Raises MinolAuthError if no form or no SAMLResponse field is found.
    """
    parser = _B2CFormParser()
    parser.feed(html)

    if not parser.action:
        raise MinolAuthError("SAML auto-post form not found in B2C response")
    if "SAMLResponse" not in parser.fields:
        raise MinolAuthError("SAMLResponse field missing from B2C auto-post form")

    action = parser.action
    if not action.startswith("http"):
        action = urljoin(page_url, action)

    return action, parser.fields


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class MinolApiClient:
    """Async client for the Minol eMonitoring portal.

    Authentication flow (Azure B2C → SAML → SAP portal):
      1. GET the portal entry point; follow redirects to the B2C login page.
      2. Parse SETTINGS.csrf and the ``localAccountForm`` (SelfAsserted endpoint).
      3. POST credentials; B2C returns ``{"status": "200"}`` JSON on success.
      4. GET the ``confirmed`` URL → B2C returns an HTML auto-post form with
         the SAML Response.
      5. POST the SAML Response to the portal's SAML handler; follow redirects
         back to the portal landing page.
      6. Verify authentication by checking for a known string ("Liegenschaft")
         that only appears on an authenticated portal page.

    Note on MYSAPSSO2: the portal sets this cookie via JavaScript, not via an
    HTTP Set-Cookie header.  A headless HTTP client therefore never receives it
    in the cookie jar; we verify login via portal content instead.

    Data flow (for a *tenant* / Mieter user):
      1. GET ``EMData/getUserTenants`` → tenant list with ``userNumber``.
      2. POST ``EMData/getLayerInfo`` → available views & periods.
      3. POST ``EMData/readData`` (dashboard) → current consumption values.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> bool:
        """Login via Azure B2C → SAML → SAP portal. Returns True on success."""
        session = self._ensure_session()

        try:
            # 1. Hit the portal entry; follow redirects to the B2C login page.
            async with session.get(LOGIN_ENTRY_URL, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise MinolAuthError(
                        f"B2C login page returned HTTP {resp.status}"
                    )
                page_url = str(resp.url)
                html = await resp.text()

            # 2. Parse login form + extract CSRF from SETTINGS.csrf.
            action_url, form_data = _parse_b2c_login_form(html, page_url)
            csrf_token = _extract_b2c_csrf(html)

            form_data["signInName"] = self._username
            form_data["password"] = self._password
            form_data["request_type"] = "RESPONSE"

            # 3. POST credentials to the SelfAsserted endpoint.
            async with session.post(
                action_url, data=form_data, allow_redirects=True
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "json" in content_type:
                    body = await resp.json(content_type=None)
                    if str(body.get("status")) != "200":
                        raise MinolAuthError(
                            "B2C login rejected: "
                            f"{body.get('message', 'invalid credentials')}"
                        )

                    # 4. GET the confirmed URL → HTML page with SAML auto-post form.
                    #    We do NOT follow redirects so we can read the response body.
                    confirmed_url = _build_confirmed_url(action_url, csrf_token)
                    async with session.get(
                        confirmed_url, allow_redirects=False
                    ) as confirmed_resp:
                        confirmed_html = await confirmed_resp.text()

                    # 5. Parse and submit the SAML auto-post form to the portal.
                    saml_action, saml_data = _parse_saml_post_form(
                        confirmed_html, confirmed_url
                    )
                    async with session.post(
                        saml_action, data=saml_data, allow_redirects=True
                    ) as portal_resp:
                        portal_html = await portal_resp.text()

                # If not JSON, the POST was already redirected to the portal.
                else:
                    portal_html = await resp.text()

        except aiohttp.ClientError as err:
            raise MinolConnectionError(
                f"Cannot reach Minol portal: {err}"
            ) from err

        # 6. Verify we landed on an authenticated portal page.
        if _PORTAL_AUTH_MARKER not in portal_html:
            raise MinolAuthError(
                f"Authentication failed – '{_PORTAL_AUTH_MARKER}' not found in portal page"
            )

        _LOGGER.debug("Minol authentication successful")
        return True

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, **kwargs: Any) -> Any:
        """GET *url* and return parsed JSON.  Re-authenticates once on 401/403."""
        return await self._request("GET", url, **kwargs)

    async def _post_json(self, url: str, payload: Any = None, **kwargs: Any) -> Any:
        """POST *url* with a JSON body and return parsed JSON."""
        return await self._request("POST", url, payload=payload, **kwargs)

    async def _request(
        self,
        method: str,
        url: str,
        payload: Any = None,
        **kwargs: Any,
    ) -> Any:
        session = self._ensure_session()

        for attempt in range(2):
            try:
                kw: dict[str, Any] = {"allow_redirects": True, **kwargs}
                if method == "POST" and payload is not None:
                    kw["data"] = json.dumps(payload)
                    kw["headers"] = {
                        "Content-Type": "application/json; charset=utf-8",
                    }

                async with session.request(method, url, **kw) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        _LOGGER.debug("Session expired, re-authenticating")
                        await self.authenticate()
                        continue

                    if resp.status != 200:
                        _LOGGER.error(
                            "Minol %s %s returned HTTP %s",
                            method,
                            url,
                            resp.status,
                        )
                        return None

                    text = await resp.text()
                    if not text.strip():
                        return None
                    return json.loads(text)

            except aiohttp.ClientError as err:
                if attempt == 0:
                    _LOGGER.debug("Request failed (%s), re-authenticating", err)
                    await self.authenticate()
                    continue
                raise MinolConnectionError(
                    f"Cannot fetch {url}: {err}"
                ) from err

        return None

    # ------------------------------------------------------------------
    # Public data endpoints
    # ------------------------------------------------------------------

    async def get_user_tenants(self) -> list[dict[str, Any]]:
        """Return the list of tenant units for the logged-in user."""
        data = await self._get_json(f"{EMDATA_REST}/getUserTenants")
        return data if isinstance(data, list) else []

    async def get_layer_info(
        self, user_num: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch available views and periods for the NE (tenant) layer."""
        selection = {
            "userNum": user_num,
            "layer": "NE",
            "scale": "CALMONTH",
            "chartRefUnit": "ABS",
            "refObject": "PREV_YEAR",
            "consType": "HEIZUNG",
            "dashBoardKey": "PE",
            "valuesInKWH": True,
        }
        return await self._post_json(f"{EMDATA_REST}/getLayerInfo", selection)

    async def get_dashboard(
        self, user_num: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch the dashboard overview (current + previous year per type)."""
        selection = {
            "userNum": user_num,
            "layer": "NE",
            "scale": "CALMONTH",
            "chartRefUnit": "ABS",
            "refObject": "DIN_AVG",
            "consType": "HEIZUNG",
            "dashBoardKey": "PE",
            "valuesInKWH": True,
            "dlgKey": "dashboard",
        }
        return await self._post_json(f"{EMDATA_REST}/readData", selection)

    async def get_consumption_for_view(
        self,
        user_num: str | None,
        view_key: str,
        cons_type: str,
    ) -> dict[str, Any] | None:
        """Fetch detailed consumption data for a specific view / type."""
        is_overview = view_key in ("100EH", "100KWH", "200", "dashboard")
        selection = {
            "userNum": user_num,
            "layer": "NE",
            "scale": "CALMONTH",
            "chartRefUnit": "ABS",
            "refObject": "DIN_AVG" if is_overview else "UPPER_LEVEL",
            "consType": cons_type,
            "dashBoardKey": "PE",
            "valuesInKWH": True,
            "dlgKey": view_key,
        }
        return await self._post_json(f"{EMDATA_REST}/readData", selection)

    async def get_room_data(
        self,
        user_num: str | None,
        view_key: str,
        cons_type: str,
    ) -> dict[str, Any] | None:
        """Fetch per-room / per-meter data for a RAUM view."""
        selection = {
            "userNum": user_num,
            "layer": "NE",
            "scale": "CALYEAR",
            "chartRefUnit": "ABS",
            "refObject": "NOREF",
            "consType": cons_type,
            "dashBoardKey": "PE",
            "valuesInKWH": True,
            "dlgKey": view_key,
        }
        return await self._post_json(f"{EMDATA_REST}/readData", selection)

    async def get_all_data(self) -> dict[str, Any]:
        """Collect all data needed by the integration sensors."""
        tenants = await self.get_user_tenants()
        user_num = tenants[0]["userNumber"] if tenants else None
        tenant_info = tenants[0] if tenants else {}

        layer_info = await self.get_layer_info(user_num)
        dashboard = await self.get_dashboard(user_num)

        rooms: dict[str, list[dict[str, Any]]] = {}
        raum_views = {
            "100EHRAUM": "HEIZUNG",
            "200RAUM": "WARMWASSER",
            "300RAUM": "KALTWASSER",
        }
        available_keys = {
            v["key"] for v in (layer_info or {}).get("views", [])
        }
        for view_key, cons_type in raum_views.items():
            if view_key not in available_keys:
                continue
            result = await self.get_room_data(user_num, view_key, cons_type)
            if result and isinstance(result.get("table"), list):
                rooms[cons_type] = result["table"]

        return {
            "tenants": tenants,
            "tenant_info": tenant_info,
            "user_num": user_num,
            "layer_info": layer_info or {},
            "dashboard": dashboard or {},
            "rooms": rooms,
        }
