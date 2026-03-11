"""The Minol Energy integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import MinolApiClient
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import MinolDataCoordinator, _get_update_interval

PLATFORMS: list[str] = ["sensor"]

type MinolConfigEntry = ConfigEntry[MinolDataCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MinolConfigEntry) -> bool:
    """Set up Minol Energy from a config entry."""

    def _on_tokens_refreshed(access_token: str, refresh_token: str | None) -> None:
        """Persist refreshed tokens back to the config entry."""
        _LOGGER.debug("Persisting refreshed tokens to config entry")
        new_data = {**entry.data, CONF_ACCESS_TOKEN: access_token, "token_expires_in": 3600}
        if refresh_token:
            new_data[CONF_REFRESH_TOKEN] = refresh_token
        hass.config_entries.async_update_entry(entry, data=new_data)

    client = MinolApiClient(
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        on_tokens_refreshed=_on_tokens_refreshed,
    )
    if expires_in := entry.data.get("token_expires_in"):
        client.set_token_expiry(int(expires_in))

    coordinator = MinolDataCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: MinolConfigEntry
) -> None:
    """Handle options update — adjust polling interval and reload sensors."""
    coordinator: MinolDataCoordinator = entry.runtime_data
    coordinator.update_interval = _get_update_interval(entry)
    await coordinator.async_request_refresh()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MinolConfigEntry) -> bool:
    """Unload a Minol Energy config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: MinolDataCoordinator = entry.runtime_data
        await coordinator.client.close()

    return unload_ok
