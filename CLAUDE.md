# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A [HACS](https://hacs.xyz/) custom integration for Home Assistant that reads consumption data (heating, hot water, cold water) from the Minol eMonitoring tenant portal (`webservices.minol.com`). Requires HA ≥ 2024.1. No Python package dependencies — only `aiohttp`, which ships with Home Assistant.

## Development commands

There is no local test runner or build step. Development is done by copying `custom_components/minol_energy/` into a Home Assistant config directory (or a dev container) and restarting HA.

Linting (run from repo root):
```bash
ruff check custom_components/
ruff format custom_components/
```

Type checking:
```bash
pyright custom_components/
```

## Architecture

All code lives in `custom_components/minol_energy/`. The integration follows the standard HA pattern: config entry → coordinator → sensor platform.

```
__init__.py        Entry setup/teardown. Creates MinolApiClient, calls authenticate(),
                   creates MinolDataCoordinator, forwards to PLATFORMS = [SENSOR].

coordinator.py     MinolDataCoordinator (DataUpdateCoordinator). Calls
                   client.get_all_data() on every poll, maps MinolAuthError →
                   ConfigEntryAuthFailed (triggers HA reauth flow) and
                   MinolConnectionError → UpdateFailed.

api.py             MinolApiClient — the only file that makes HTTP requests.
                   authenticate() implements the Azure B2C / SAML flow (5 steps).
                   get_all_data() calls getUserTenants, getLayerInfo, readData
                   (dashboard), and readData for each available RAUM view.
                   _request() retries once on 401/403 via re-authentication.

sensor.py          Builds sensor entities from coordinator.data. Dashboard sensors
                   are derived from the JSON structure returned by readData
                   (data1/data2/data3 arrays). Per-room sensors come from the
                   "rooms" key (table entries per RAUM view). Cost sensors are
                   created when a non-zero price is configured in options.

config_flow.py     ConfigFlow + OptionsFlow. Credentials stored under CONF_USERNAME /
                   CONF_PASSWORD. Options: scan_interval, heating/hot_water/cold_water
                   prices.

const.py           All constants. Key ones: BASE_URL, B2C_ENTRY_URL (auth entry
                   point), EMDATA_REST (REST API base path).

diagnostics.py     Async diagnostics export; redacts personal fields from coordinator
                   data before returning.
```

## Authentication

The portal uses Azure AD B2C (SAML SP-initiated). `authenticate()` in `api.py`:
1. GET `/?redirect2=true` → follows redirects to `minolauth.b2clogin.com`
2. Parses `$Config` JSON from the B2C HTML page (CSRF token, `transId`, `policy`)
3. POSTs credentials to `{base}/SelfAsserted?tx={transId}&p={policy}` (expects `{"status":"200"}`)
4. GETs `{base}/api/{transId}/confirmed` → triggers SAML redirect back to Minol ACS
5. Verifies `MYSAPSSO2` cookie is present

Helper functions `_extract_b2c_settings()` and `_b2c_base_url()` handle parsing.

## API data shape

`get_all_data()` returns:
```python
{
    "tenants": [...],          # list from getUserTenants
    "tenant_info": {...},      # tenants[0]
    "user_num": "...",         # tenants[0]["userNumber"]
    "layer_info": {...},       # from getLayerInfo (contains "views" list)
    "dashboard": {...},        # from readData with dlgKey="dashboard"
    "rooms": {                 # keyed by consType (HEIZUNG/WARMWASSER/KALTWASSER)
        "HEIZUNG": [...],      # table rows from readData for 100EHRAUM view
        ...
    },
}
```

Dashboard `readData` responses contain `data1` (yearly totals), `data2_*` (building share), `data3` (per-m² + DIN reference) arrays. Room `readData` responses contain a `table` array with per-meter entries.
