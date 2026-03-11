"""API client for the Minol tenant portal."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import (
    B2C_ENTRY_URL,
    BASE_URL,
    EMDATA_REST,
    NUDATA_REST,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


class MinolAuthError(Exception):
    """Raised when authentication with the Minol portal fails."""


class MinolConnectionError(Exception):
    """Raised when the Minol portal cannot be reached."""


def _extract_b2c_settings(html: str) -> dict[str, Any]:
    """Extract the Azure B2C page settings JSON from the login page HTML.

    B2C login pages embed a JSON config object in a script tag, typically as
    ``$Config={...}`` or ``var SETTINGS = {...}``.
    """
    for pattern in (
        r"\$Config\s*=\s*(\{[^<]+?\})\s*[;\n]",
        r"var\s+SETTINGS\s*=\s*(\{[^<]+?\})\s*[;\n]",
    ):
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                continue
    return {}


def _b2c_base_url(url: str) -> str:
    """Return ``scheme://host/tenant/policy`` from a full B2C URL.

    Example::

        https://minolauth.b2clogin.com/minolauth.onmicrosoft.com/B2C_1A_XYZ/api/...
        → https://minolauth.b2clogin.com/minolauth.onmicrosoft.com/B2C_1A_XYZ
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return f"{parsed.scheme}://{parsed.netloc}/{parts[0]}/{parts[1]}"
    return f"{parsed.scheme}://{parsed.netloc}"


class MinolApiClient:
    """Async client for the Minol eMonitoring portal.

    Authentication flow (Azure B2C / SAML):
      1. GET ``/?redirect2=true`` → follows redirects to Azure B2C login page.
      2. Parse ``$Config`` settings: CSRF token, transId, policy.
      3. POST credentials to the B2C SelfAsserted endpoint.
      4. GET the confirmed endpoint → triggers SAML redirect back to Minol.
      5. Verify ``MYSAPSSO2`` cookie was issued.

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
        """Login via Azure B2C / SAML.  Returns True on success.

        Flow:
          1. GET ``B2C_ENTRY_URL`` (``/?redirect2=true``) → follows redirects
             to the Azure AD B2C login page at ``minolauth.b2clogin.com``.
          2. Parse ``$Config`` JSON embedded in the page to obtain the CSRF
             token, transaction-ID and policy name.
          3. POST credentials to the B2C *SelfAsserted* endpoint.
          4. GET the *confirmed* endpoint → B2C redirects to the Minol SAML
             ACS, which issues the ``MYSAPSSO2`` session cookie.
          5. Verify ``MYSAPSSO2`` is present in the cookie jar.
        """
        session = self._ensure_session()

        try:
            # 1. Reach the B2C login page.
            async with session.get(B2C_ENTRY_URL, allow_redirects=True) as resp:
                b2c_page_url = str(resp.url)
                html = await resp.text()

            _LOGGER.debug("Reached B2C login page: %s", b2c_page_url)

            # 2. Parse page settings.
            settings = _extract_b2c_settings(html)
            if not settings:
                raise MinolAuthError(
                    "Could not parse Azure B2C login page – $Config not found"
                )

            csrf = settings.get("csrf", "")
            trans_id = settings.get("transId", "")
            policy = settings.get("policy", "")
            base = _b2c_base_url(b2c_page_url)

            if not (csrf and trans_id and policy):
                raise MinolAuthError(
                    "Incomplete B2C settings: "
                    f"csrf={bool(csrf)}, transId={bool(trans_id)}, "
                    f"policy={bool(policy)}"
                )

            # 3. POST credentials to SelfAsserted endpoint.
            self_asserted_url = f"{base}/SelfAsserted?tx={trans_id}&p={policy}"
            async with session.post(
                self_asserted_url,
                data={
                    "request_type": "RESPONSE",
                    "signInName": self._username,
                    "password": self._password,
                },
                headers={
                    "X-CSRF-TOKEN": csrf,
                    "Referer": b2c_page_url,
                },
                allow_redirects=False,
            ) as resp:
                body = await resp.text()
                try:
                    result: dict[str, Any] = json.loads(body)
                except json.JSONDecodeError:
                    result = {}

                if result.get("status") != "200":
                    msg = result.get("message", "Invalid username or password")
                    raise MinolAuthError(f"Authentication failed: {msg}")

            # 4. Confirm → triggers SAML redirect back to Minol ACS.
            confirmed_url = (
                f"{base}/api/{trans_id}/confirmed"
                f"?csrf_token={csrf}&tx={trans_id}&p={policy}"
            )
            async with session.get(confirmed_url, allow_redirects=True):
                pass

            # 5. Verify MYSAPSSO2 cookie.
            cookie_names = {c.key for c in session.cookie_jar}
            if "MYSAPSSO2" not in cookie_names:
                raise MinolAuthError(
                    "Authentication failed – no MYSAPSSO2 cookie received"
                )

        except aiohttp.ClientError as err:
            raise MinolConnectionError(
                f"Cannot reach Minol portal: {err}"
            ) from err

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
        """Collect all data needed by the integration sensors.

        Returns a dict with ``tenants``, ``layer_info``, ``dashboard``,
        and ``rooms`` (per-meter / per-room data).
        """
        tenants = await self.get_user_tenants()
        user_num = tenants[0]["userNumber"] if tenants else None
        tenant_info = tenants[0] if tenants else {}

        layer_info = await self.get_layer_info(user_num)
        dashboard = await self.get_dashboard(user_num)

        # Fetch per-room data for every RAUM view available.
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
