"""API client for the Minol mobile app backend (Mulesoft Experience API).

Authentication flow (Azure B2C OAuth2 → Bearer token):
  1. The config flow obtains tokens via Authorization Code + PKCE (browser login).
  2. The access_token and refresh_token are stored in the config entry.
  3. The access_token is attached as ``Authorization: Bearer <token>`` on every
     API request, together with the shared Mulesoft client_id / client_secret.
  4. When the access_token expires, the client refreshes silently using the
     refresh_token.  On failure it raises MinolAuthError so the coordinator
     can trigger HA's re-authentication flow.

Data flow:
  1. GET ``/profiles`` → user profile containing billingUnit + residentialUnitID.
  2. GET ``/billingUnit/{bu}/residentialUnit/{ru}/masterdata`` → billing periods.
  3. GET ``/billingUnit/{bu}/residentialUnit/{ru}/consumptions/availableData``
     → list of available consumption periods.
  4. GET ``/billingUnit/{bu}/residentialUnit/{ru}/consumptions?startdate=…&enddate=…``
     → consumption values per service (heating / hot water / cold water).

Reverse-engineered from MinolApp v2.11.14 APK (com.minol.minolapp).

Enable debug logging in configuration.yaml to see full request/response details:
  logger:
    logs:
      custom_components.minol_energy: debug
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    API_CLIENT_ID,
    API_CLIENT_SECRET,
    APP_VERSION,
    B2C_CLIENT_ID,
    B2C_CLIENT_SECRET,
    B2C_REDIRECT_URI,
    B2C_SCOPES,
    B2C_TOKEN_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MinolAuthError(Exception):
    """Raised when the token is expired and refresh fails – triggers reauth."""


class MinolConnectionError(Exception):
    """Raised when the Minol API cannot be reached."""


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class MinolApiClient:
    """Async client for the Minol mobile app backend (Mulesoft API).

    Takes OAuth2 tokens obtained via the config flow (Authorization Code + PKCE).
    Silently refreshes the access_token using the refresh_token when needed.

    Usage::

        client = MinolApiClient(
            access_token="eyJ...",
            refresh_token="0.A...",
            on_tokens_refreshed=lambda a, r: save_tokens(a, r),
        )
        data = await client.get_all_data()
        await client.close()
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str | None = None,
        on_tokens_refreshed: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._on_tokens_refreshed = on_tokens_refreshed
        self._session: aiohttp.ClientSession | None = None
        # Proactive refresh: track when the access token expires
        self._token_expiry: datetime | None = None

    def set_token_expiry(self, expires_in: int) -> None:
        """Record when the current access token expires (seconds from now)."""
        self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        _LOGGER.debug(
            "Access token expires at %s (in %d s)",
            self._token_expiry.isoformat(),
            expires_in,
        )

    def _is_token_expired(self) -> bool:
        """Return True if the access token has expired or will within 60 s."""
        if self._token_expiry is None:
            return False
        return datetime.now(timezone.utc) >= self._token_expiry - timedelta(seconds=60)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Token refresh (silent)
    # ------------------------------------------------------------------

    async def _refresh_access_token(self) -> bool:
        """Silently refresh the access token using the stored refresh token."""
        if not self._refresh_token:
            _LOGGER.warning(
                "Token refresh requested but no refresh_token is stored."
                " Re-authentication will be required."
            )
            return False

        _LOGGER.debug("Attempting silent token refresh via B2C: %s", B2C_TOKEN_URL)
        session = self._ensure_session()
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": B2C_CLIENT_ID,
            "client_secret": B2C_CLIENT_SECRET,
            "redirect_uri": B2C_REDIRECT_URI,
            "scope": B2C_SCOPES,
        }

        try:
            async with session.post(
                B2C_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    try:
                        err = json.loads(body)
                        desc = err.get("error_description") or err.get("error") or body
                    except Exception:
                        desc = body
                    _LOGGER.warning(
                        "Token refresh failed (HTTP %s): %s", resp.status, desc[:300]
                    )
                    return False

                token_data = json.loads(body)
                new_token = token_data.get("access_token")
                if not new_token:
                    _LOGGER.warning(
                        "Token refresh response did not contain access_token: %s",
                        body[:200],
                    )
                    return False

                self._access_token = new_token
                self._refresh_token = token_data.get(
                    "refresh_token", self._refresh_token
                )
                expires_in = token_data.get("expires_in", 3600)
                self.set_token_expiry(int(expires_in))

                _LOGGER.debug(
                    "Token refreshed successfully (expires_in=%s s)", expires_in
                )
                if self._on_tokens_refreshed:
                    self._on_tokens_refreshed(self._access_token, self._refresh_token)
                return True

        except aiohttp.ClientError as err:
            _LOGGER.warning("Network error during token refresh: %s", err)
            return False
        except Exception as err:
            _LOGGER.warning("Unexpected error during token refresh: %s", err)
            return False

    # ------------------------------------------------------------------
    # Low-level API helpers
    # ------------------------------------------------------------------

    def _api_headers(self) -> dict[str, str]:
        """Build the headers required by every Mulesoft API request."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "client_id": API_CLIENT_ID,
            "client_secret": API_CLIENT_SECRET,
            "correlationId": str(uuid.uuid4()),
            "appVersion": APP_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{API_BASE_URL}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        payload: Any = None,
    ) -> Any:
        session = self._ensure_session()
        url = self._url(path)

        # Proactive refresh before the request if we know the token has expired
        if self._is_token_expired():
            _LOGGER.debug("Access token near expiry, proactively refreshing")
            if not await self._refresh_access_token():
                raise MinolAuthError(
                    "Proactive token refresh failed – re-authentication required"
                )

        for attempt in range(2):
            try:
                kwargs: dict[str, Any] = {
                    "headers": self._api_headers(),
                    "allow_redirects": True,
                }
                if params:
                    kwargs["params"] = params
                if payload is not None:
                    kwargs["json"] = payload

                _LOGGER.debug("→ %s %s params=%s", method, url, params)
                async with session.request(method, url, **kwargs) as resp:
                    _LOGGER.debug("← %s %s HTTP %s", method, url, resp.status)

                    if resp.status in (401, 403) and attempt == 0:
                        _LOGGER.warning(
                            "Received HTTP %s from Minol API – attempting token refresh",
                            resp.status,
                        )
                        if not await self._refresh_access_token():
                            raise MinolAuthError(
                                "Token expired and silent refresh failed"
                                " – re-authentication required"
                            )
                        continue

                    if resp.status != 200:
                        body = await resp.text()
                        _LOGGER.error(
                            "Minol API %s %s returned HTTP %s: %s",
                            method,
                            url,
                            resp.status,
                            body[:400],
                        )
                        return None

                    text = await resp.text()
                    if not text.strip():
                        _LOGGER.debug("Empty response body for %s %s", method, url)
                        return None

                    result = json.loads(text)
                    _LOGGER.debug(
                        "Parsed response for %s %s: %s keys / %s items",
                        method,
                        url,
                        len(result) if isinstance(result, dict) else "—",
                        len(result) if isinstance(result, list) else "—",
                    )
                    return result

            except aiohttp.ClientError as err:
                if attempt == 0:
                    _LOGGER.debug("Network error on attempt 1 (%s), retrying", err)
                    continue
                raise MinolConnectionError(
                    f"Cannot reach Minol API at {url}: {err}"
                ) from err

        return None

    async def _get(self, path: str, **params: str) -> Any:
        return await self._request("GET", path, params=params or None)

    # ------------------------------------------------------------------
    # Public data endpoints
    # ------------------------------------------------------------------

    async def get_profiles(self) -> list[dict[str, Any]]:
        """Return all user profiles for the authenticated account.

        Response shape::

            {
              "meta": {"total": 1, "cursor": ""},
              "data": [
                {
                  "userID": "000000000535",
                  "eMail": "user@example.com",
                  "billingUnit": "0607986",
                  "residentialUnitReference": {
                    "residentialUnitID": "000002",
                    "floor": "0000001",
                    "position": "Mitte"
                  },
                  "moveInDate": "2019-11-01",
                  ...
                }
              ]
            }
        """
        result = await self._get("/profiles")
        if isinstance(result, dict):
            return result.get("data", [])
        return []

    async def get_masterdata(
        self,
        billing_unit_id: str,
        residential_unit_id: str,
        startdate: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch master data (billing periods, forecast weights) for a residence."""
        if startdate is None:
            startdate = datetime.now(timezone.utc).replace(
                year=datetime.now(timezone.utc).year - 2
            ).strftime("%Y-%m-%d")

        path = (
            f"/billingUnit/{billing_unit_id}"
            f"/residentialUnit/{residential_unit_id}"
            "/masterdata"
        )
        return await self._get(path, startdate=startdate)

    async def get_available_periods(
        self,
        billing_unit_id: str,
        residential_unit_id: str,
    ) -> list[dict[str, Any]]:
        """Return the list of available consumption periods."""
        path = (
            f"/billingUnit/{billing_unit_id}"
            f"/residentialUnit/{residential_unit_id}"
            "/consumptions/availableData"
        )
        result = await self._get(path)
        if isinstance(result, dict):
            return result.get("periods", [])
        return []

    async def get_consumptions(
        self,
        billing_unit_id: str,
        residential_unit_id: str,
        startdate: str,
        enddate: str,
    ) -> list[dict[str, Any]]:
        """Fetch consumption data for a date range (YYYY-MM-DD strings)."""
        path = (
            f"/billingUnit/{billing_unit_id}"
            f"/residentialUnit/{residential_unit_id}"
            "/consumptions"
        )
        result = await self._get(path, startdate=startdate, enddate=enddate)
        if isinstance(result, list):
            return result
        return []

    # ------------------------------------------------------------------
    # Aggregate data method used by the coordinator
    # ------------------------------------------------------------------

    async def get_all_data(self) -> dict[str, Any]:
        """Collect all data needed by the integration sensors.

        Returns a dict with keys:
          - ``profile``: the primary user profile dict
          - ``billing_unit_id``: str
          - ``residential_unit_id``: str
          - ``masterdata``: masterdata response dict
          - ``latest_consumption``: the most recent consumption period dict
          - ``available_periods``: list of available period dicts
        """
        profiles = await self.get_profiles()
        if not profiles:
            raise MinolAuthError(
                "No profiles returned – cannot fetch consumption data"
            )

        profile = profiles[0]
        billing_unit_id = profile.get("billingUnit", "")
        residential_unit_id = (
            profile.get("residentialUnitReference", {}).get("residentialUnitID", "")
        )

        if not billing_unit_id or not residential_unit_id:
            raise MinolAuthError("Profile missing billingUnit or residentialUnitID")

        _LOGGER.debug(
            "Fetching data for billingUnit=%s residentialUnitID=%s",
            billing_unit_id,
            residential_unit_id,
        )

        masterdata = await self.get_masterdata(billing_unit_id, residential_unit_id) or {}
        available_periods = await self.get_available_periods(
            billing_unit_id, residential_unit_id
        )

        now = datetime.now(timezone.utc)
        start_month = now.replace(day=1)
        if start_month.month <= 3:
            startdate = start_month.replace(
                year=start_month.year - 1,
                month=start_month.month + 9,
            )
        else:
            startdate = start_month.replace(month=start_month.month - 3)

        consumptions = await self.get_consumptions(
            billing_unit_id,
            residential_unit_id,
            startdate=startdate.strftime("%Y-%m-%d"),
            enddate=now.strftime("%Y-%m-%d"),
        )

        _LOGGER.debug(
            "Fetched %d available periods, %d consumption periods",
            len(available_periods),
            len(consumptions),
        )

        latest = {}
        for period in reversed(consumptions):
            if period.get("statusOverall") == "UVI_AVAILABLE" and period.get(
                "consumptions"
            ):
                latest = period
                break
        if not latest and consumptions:
            latest = consumptions[-1]

        if latest:
            _LOGGER.debug(
                "Latest consumption period: %s (status=%s, services=%s)",
                latest.get("period"),
                latest.get("statusOverall"),
                [c.get("service") for c in latest.get("consumptions", [])],
            )
        else:
            _LOGGER.warning("No consumption periods found in the last 3 months")

        return {
            "profile": profile,
            "billing_unit_id": billing_unit_id,
            "residential_unit_id": residential_unit_id,
            "masterdata": masterdata,
            "latest_consumption": latest,
            "available_periods": available_periods,
        }

    # ------------------------------------------------------------------
    # Convenience helpers for sensors
    # ------------------------------------------------------------------

    @staticmethod
    def get_service_value(
        consumption_period: dict[str, Any],
        service_code: str,
        field: str = "energyValue",
    ) -> float | None:
        """Extract a single value from a consumption period.

        ``service_code`` is one of ``SERVICE_HEATING`` / ``SERVICE_HOT_WATER``
        / ``SERVICE_COLD_WATER`` (``"100"`` / ``"200"`` / ``"300"``).
        ``field`` defaults to ``"energyValue"`` (kWh); use ``"serviceValue"``
        for the raw meter unit (EH or m³).
        """
        for cons in consumption_period.get("consumptions", []):
            if cons.get("service") == service_code:
                val = cons.get(field)
                if val is not None:
                    return float(val)
        return None
