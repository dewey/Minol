"""Live integration tests for the Minol mobile app API (Mulesoft backend).

These tests require a valid OAuth2 access token obtained via the Minol app's
Azure B2C login flow.  Use ``scripts/get_token.py`` to obtain tokens
interactively, then export them as environment variables:

    export MINOL_ACCESS_TOKEN="eyJ..."
    export MINOL_REFRESH_TOKEN="0.A..."
    uv run pytest tests/test_api_live.py -v -s

Tests are automatically skipped in CI (``CI=true``) and when the token
env var is absent.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from custom_components.minol_energy.api import MinolApiClient
from custom_components.minol_energy.const import SERVICE_COLD_WATER, SERVICE_HEATING, SERVICE_HOT_WATER

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

_ACCESS_TOKEN = os.environ.get("MINOL_ACCESS_TOKEN")
_REFRESH_TOKEN = os.environ.get("MINOL_REFRESH_TOKEN")
_IN_CI = os.environ.get("CI") == "true"

pytestmark = pytest.mark.skipif(
    _IN_CI or not _ACCESS_TOKEN,
    reason=(
        "Live tests require MINOL_ACCESS_TOKEN env var and must not run in CI. "
        "Run scripts/get_token.py to obtain a token."
    ),
)


def _make_client() -> MinolApiClient:
    return MinolApiClient(
        access_token=_ACCESS_TOKEN,
        refresh_token=_REFRESH_TOKEN,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_profiles_returns_list() -> None:
    """get_profiles() returns a non-empty list with the expected profile keys."""
    client = _make_client()
    try:
        profiles = await client.get_profiles()
        assert isinstance(profiles, list), f"Expected list, got {type(profiles)}"
        assert len(profiles) > 0, "Expected at least one profile"
        profile = profiles[0]
        print(f"\nProfile: {profile}")
        assert "billingUnit" in profile, "Profile missing billingUnit"
        assert "residentialUnitReference" in profile, "Profile missing residentialUnitReference"
        assert "userID" in profile, "Profile missing userID"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_available_periods() -> None:
    """get_available_periods() returns a list of period dicts."""
    client = _make_client()
    try:
        profiles = await client.get_profiles()
        profile = profiles[0]
        bu = profile["billingUnit"]
        ru = profile["residentialUnitReference"]["residentialUnitID"]

        periods = await client.get_available_periods(bu, ru)
        print(f"\nAvailable periods ({len(periods)}): {periods[:3]}")
        assert isinstance(periods, list)
        if periods:
            assert "period" in periods[0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_masterdata() -> None:
    """get_masterdata() returns billing period information."""
    client = _make_client()
    try:
        profiles = await client.get_profiles()
        profile = profiles[0]
        bu = profile["billingUnit"]
        ru = profile["residentialUnitReference"]["residentialUnitID"]

        master = await client.get_masterdata(bu, ru)
        print(f"\nMasterdata keys: {list(master.keys()) if master else 'None'}")
        assert master is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_consumptions() -> None:
    """get_consumptions() returns a list of period consumption objects."""
    client = _make_client()
    try:
        profiles = await client.get_profiles()
        profile = profiles[0]
        bu = profile["billingUnit"]
        ru = profile["residentialUnitReference"]["residentialUnitID"]

        now = datetime.now(timezone.utc)
        startdate = now.replace(day=1, month=1).strftime("%Y-%m-%d")
        enddate = now.strftime("%Y-%m-%d")

        consumptions = await client.get_consumptions(bu, ru, startdate, enddate)
        print(f"\nConsumption periods: {len(consumptions)}")
        if consumptions:
            period = consumptions[-1]
            print(f"Latest period: {period.get('period')}")
            print(f"Services: {[c.get('service') for c in period.get('consumptions', [])]}")
            for cons in period.get("consumptions", []):
                print(
                    f"  Service {cons['service']}: "
                    f"energy={cons.get('energyValue')} {cons.get('energyUnit')}, "
                    f"raw={cons.get('serviceValue')} {cons.get('serviceUnit')}, "
                    f"co2={cons.get('co2kg')} kg"
                )
        assert isinstance(consumptions, list)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_all_data_has_expected_keys() -> None:
    """get_all_data() returns a dict with all keys the sensors rely on."""
    client = _make_client()
    try:
        data = await client.get_all_data()
        print(f"\nData keys: {list(data.keys())}")

        for key in (
            "profile",
            "billing_unit_id",
            "residential_unit_id",
            "masterdata",
            "latest_consumption",
            "available_periods",
        ):
            assert key in data, f"Missing key: {key}"

        print(f"Billing unit: {data['billing_unit_id']}")
        print(f"Residential unit: {data['residential_unit_id']}")
        print(f"Available periods: {len(data['available_periods'])}")
        latest = data["latest_consumption"]
        print(f"Latest period: {latest.get('period')} status={latest.get('statusOverall')}")

        for svc_code, svc_name in [
            (SERVICE_HEATING, "Heating"),
            (SERVICE_HOT_WATER, "Hot Water"),
            (SERVICE_COLD_WATER, "Cold Water"),
        ]:
            val = MinolApiClient.get_service_value(latest, svc_code)
            print(f"  {svc_name}: {val}")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_token_refresh() -> None:
    """Verify the token refresh callback fires when tokens are refreshed."""
    if not _REFRESH_TOKEN:
        pytest.skip("MINOL_REFRESH_TOKEN not set")

    refreshed: list[tuple[str, str | None]] = []

    client = MinolApiClient(
        access_token=_ACCESS_TOKEN,
        refresh_token=_REFRESH_TOKEN,
        on_tokens_refreshed=lambda a, r: refreshed.append((a, r)),
    )
    try:
        # Force a "token expired" scenario by corrupting the access token
        client._access_token = "invalid.token.here"
        # _request will get a 401, then try to refresh using the valid refresh token
        profiles = await client.get_profiles()
        if profiles:
            print(f"\nToken refresh worked: {len(profiles)} profile(s) returned")
            print(f"Refresh callback fired: {len(refreshed)} time(s)")
            assert len(refreshed) > 0, "Token refresh callback was never called"
        else:
            # If the API returned empty after refresh, something else is wrong
            print("No profiles returned after token refresh (refresh may have failed)")
    finally:
        await client.close()
