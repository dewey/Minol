"""Config flow for Minol Energy integration.

Authentication uses OAuth2 Authorization Code + PKCE against Azure B2C.
Since the Azure B2C custom policies don't support ROPC (direct password grant),
and the only registered redirect URIs are the mobile-app native scheme and the
Postman OAuth2 callback (https://oauth.pstmn.io/v1/callback), the config flow
guides the user to authenticate in their browser and paste the resulting redirect
URL back into Home Assistant.

Flow:
  1. HA generates a PKCE code_verifier/challenge and builds the authorization URL.
  2. User clicks the link, logs in to Minol in their browser.
  3. After login, the browser navigates to https://oauth.pstmn.io/v1/callback?code=…
  4. User copies the full URL from the browser address bar and pastes it into HA.
  5. HA extracts the auth code and exchanges it for access + refresh tokens.
  6. Tokens are stored in the config entry; the refresh_token is used for silent
     renewal without requiring the user to log in again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)

from .const import (
    B2C_AUTH_URL,
    B2C_CLIENT_ID,
    B2C_CLIENT_SECRET,
    B2C_REDIRECT_URI,
    B2C_SCOPES,
    B2C_TOKEN_URL,
    CONF_ACCESS_TOKEN,
    CONF_COLD_WATER_PRICE,
    CONF_HEATING_PRICE,
    CONF_HOT_WATER_PRICE,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PKCE + OAuth2 helpers
# ---------------------------------------------------------------------------


def _generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def _compute_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _build_auth_url(code_challenge: str, state: str) -> str:
    params = {
        "client_id": B2C_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": B2C_REDIRECT_URI,
        "scope": B2C_SCOPES,
        "state": state,
        "nonce": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return B2C_AUTH_URL + "?" + urlencode(params)


def _extract_code_from_url(redirect_url: str) -> str | None:
    """Extract the authorization code from the pasted redirect URL."""
    try:
        qs = parse_qs(urlparse(redirect_url.strip()).query)
        codes = qs.get("code", [])
        return codes[0] if codes else None
    except Exception:
        return None


async def _exchange_code_for_tokens(
    code: str, code_verifier: str
) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens."""
    _LOGGER.debug("Exchanging authorization code for tokens via %s", B2C_TOKEN_URL)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": B2C_REDIRECT_URI,
        "client_id": B2C_CLIENT_ID,
        "client_secret": B2C_CLIENT_SECRET,
        "code_verifier": code_verifier,
        "scope": B2C_SCOPES,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            B2C_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                try:
                    err = json.loads(body)
                    msg = err.get("error_description") or err.get("error") or body
                except Exception:
                    msg = body
                _LOGGER.error("Token exchange failed (HTTP %s): %s", resp.status, msg[:300])
                raise ValueError(
                    f"Token exchange failed (HTTP {resp.status}): {msg}"
                )
            token_data = json.loads(body)
            _LOGGER.debug(
                "Token exchange successful: expires_in=%s has_refresh=%s",
                token_data.get("expires_in"),
                bool(token_data.get("refresh_token")),
            )
            return token_data


def _get_email_from_token(token_data: dict[str, Any]) -> str | None:
    """Decode the JWT access token payload to extract the user's email."""
    try:
        access_token = token_data.get("access_token", "")
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        padding = 4 - (len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(parts[1] + "=" * padding)
        claims = json.loads(payload_bytes)
        return (
            claims.get("email")
            or (claims.get("emails") or [None])[0]
            or claims.get("preferred_username")
            or claims.get("unique_name")
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class MinolEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Minol Energy."""

    VERSION = 1

    def __init__(self) -> None:
        self._code_verifier: str = ""
        self._state: str = ""
        self._auth_url: str = ""

    @staticmethod
    def async_get_options_flow(config_entry):
        return MinolOptionsFlow(config_entry)

    def _init_pkce(self) -> None:
        """Generate PKCE verifier/challenge and build the authorization URL."""
        self._code_verifier = _generate_code_verifier()
        self._state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
        self._auth_url = _build_auth_url(
            _compute_code_challenge(self._code_verifier), self._state
        )

    async def _process_redirect_url(
        self, redirect_url: str
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Parse redirect URL, exchange code for tokens.  Returns (token_data, errors)."""
        errors: dict[str, str] = {}
        code = _extract_code_from_url(redirect_url)
        if not code:
            errors["redirect_url"] = "invalid_redirect_url"
            return None, errors
        try:
            token_data = await _exchange_code_for_tokens(code, self._code_verifier)
        except ValueError as exc:
            _LOGGER.error("Token exchange failed: %s", exc)
            errors["base"] = "invalid_auth"
            return None, errors
        except Exception:
            _LOGGER.exception("Unexpected error during token exchange")
            errors["base"] = "unknown"
            return None, errors
        return token_data, errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show OAuth2 login URL; accept pasted redirect URL."""
        errors: dict[str, str] = {}

        if not self._code_verifier:
            self._init_pkce()

        if user_input is not None:
            token_data, errors = await self._process_redirect_url(
                user_input.get("redirect_url", "")
            )
            if token_data:
                email = _get_email_from_token(token_data) or "minol_user"
                await self.async_set_unique_id(email)
                self._abort_if_unique_id_configured()
                _LOGGER.debug("Creating config entry for account: %s", email)
                return self.async_create_entry(
                    title=f"Minol ({email})",
                    data={
                        CONF_ACCESS_TOKEN: token_data.get("access_token", ""),
                        CONF_REFRESH_TOKEN: token_data.get("refresh_token"),
                        "token_expires_in": token_data.get("expires_in", 3600),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            description_placeholders={"auth_url": self._auth_url},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when tokens can no longer be refreshed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to re-authenticate via the browser."""
        errors: dict[str, str] = {}

        if not self._code_verifier:
            self._init_pkce()

        if user_input is not None:
            token_data, errors = await self._process_redirect_url(
                user_input.get("redirect_url", "")
            )
            if token_data:
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        CONF_ACCESS_TOKEN: token_data.get("access_token", ""),
                        CONF_REFRESH_TOKEN: token_data.get("refresh_token"),
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            description_placeholders={"auth_url": self._auth_url},
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class MinolOptionsFlow(OptionsFlow):
    """Handle options for Minol Energy."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL // 60),
                    ): vol.All(vol.Coerce(int), vol.Range(min=15, max=1440)),
                    vol.Optional(
                        CONF_HEATING_PRICE,
                        default=options.get(CONF_HEATING_PRICE, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                    vol.Optional(
                        CONF_HOT_WATER_PRICE,
                        default=options.get(CONF_HOT_WATER_PRICE, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                    vol.Optional(
                        CONF_COLD_WATER_PRICE,
                        default=options.get(CONF_COLD_WATER_PRICE, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                }
            ),
        )
