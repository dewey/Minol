"""DataUpdateCoordinator for Minol Energy."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import MinolApiClient, MinolAuthError, MinolConnectionError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _get_update_interval(entry: ConfigEntry) -> timedelta:
    """Return the update interval from options (minutes) or the default."""
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL // 60)
    return timedelta(minutes=minutes)


class MinolDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Shared data fetcher for the Minol mobile app API."""

    def __init__(
        self, hass: HomeAssistant, client: MinolApiClient, entry: ConfigEntry
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=_get_update_interval(entry),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        _LOGGER.debug("Starting data refresh for entry %s", self.entry.entry_id)
        try:
            data = await self.client.get_all_data()
            _LOGGER.debug(
                "Data refresh complete: billingUnit=%s residentialUnit=%s"
                " availablePeriods=%d latestPeriod=%s",
                data.get("billing_unit_id"),
                data.get("residential_unit_id"),
                len(data.get("available_periods", [])),
                data.get("latest_consumption", {}).get("period"),
            )
            return data
        except MinolAuthError as err:
            _LOGGER.warning(
                "Authentication error during data refresh – triggering reauth: %s", err
            )
            raise ConfigEntryAuthFailed(
                f"Authentication failed: {err}"
            ) from err
        except MinolConnectionError as err:
            _LOGGER.error("Connection error during data refresh: %s", err)
            raise UpdateFailed(f"Connection error: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error during data refresh")
            raise UpdateFailed(f"Unexpected error: {err}") from err
