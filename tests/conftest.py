"""Pytest configuration and shared fixtures for Minol tests.

Home Assistant is NOT installed in the test environment.  We stub out every
homeassistant.* module that the integration package's __init__.py, coordinator.py
and config_flow.py import so Python can resolve them without HA being present.
The stubs are registered in sys.modules *before* any custom_components import.
"""

from __future__ import annotations

import sys
import types


def _stub(name: str, **attrs) -> types.ModuleType:
    """Return a simple stub module registered under *name* in sys.modules."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# Minimal Home Assistant stubs
# ---------------------------------------------------------------------------

# homeassistant (top-level)
_stub("homeassistant")

# homeassistant.core
_stub("homeassistant.core", HomeAssistant=object)

# homeassistant.const
_stub(
    "homeassistant.const",
    CONF_USERNAME="username",
    CONF_PASSWORD="password",
    Platform=types.SimpleNamespace(SENSOR="sensor"),
)

# homeassistant.config_entries
class _FakeConfigEntry:
    pass

class _FakeConfigFlow:
    pass

class _FakeOptionsFlow:
    pass

class _FakeConfigFlowResult:
    pass

_stub(
    "homeassistant.config_entries",
    ConfigEntry=_FakeConfigEntry,
    ConfigFlow=_FakeConfigFlow,
    OptionsFlow=_FakeOptionsFlow,
    ConfigFlowResult=_FakeConfigFlowResult,
)

# homeassistant.helpers (and sub-modules used by coordinator)
_stub("homeassistant.helpers")
_stub("homeassistant.helpers.update_coordinator",
      DataUpdateCoordinator=type(
          "DataUpdateCoordinator",
          (),
          {"__class_getitem__": classmethod(lambda cls, item: cls)},
      ),
      UpdateFailed=Exception)
_stub("homeassistant.helpers.entity",
      Entity=object)
_stub("homeassistant.helpers.entity_platform",
      AddEntitiesCallback=object)
_stub("homeassistant.helpers.device_registry",
      DeviceEntryType=types.SimpleNamespace(SERVICE="service"),
      DeviceInfo=dict)

# homeassistant.components (sensor constants)
_stub("homeassistant.components")
_stub("homeassistant.components.sensor",
      SensorDeviceClass=types.SimpleNamespace(
          ENERGY="energy", VOLUME="volume", MONETARY="monetary"
      ),
      SensorEntity=object,
      SensorStateClass=types.SimpleNamespace(
          TOTAL="total", TOTAL_INCREASING="total_increasing", MEASUREMENT="measurement"
      ))

# homeassistant.exceptions
_stub("homeassistant.exceptions",
      ConfigEntryAuthFailed=Exception,
      ConfigEntryNotReady=Exception)

# voluptuous (used by config_flow)
import unittest.mock as _mock
sys.modules.setdefault("voluptuous", _mock.MagicMock())

# ---------------------------------------------------------------------------
# Now it is safe to import from custom_components
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from custom_components.minol_energy.api import MinolApiClient  # noqa: E402


@pytest.fixture
def client() -> MinolApiClient:
    """Return a fresh MinolApiClient with dummy credentials."""
    return MinolApiClient(username="user@example.com", password="secret")


@pytest.fixture
def b2c_html() -> str:
    """Return a minimal B2C login page HTML with a valid $Config block."""
    config = (
        '{"csrf":"test-csrf-token",'
        '"transId":"StateProperties=ABC123",'
        '"policy":"B2C_1A_SIGNIN"}'
    )
    return f"<html><body><script>$Config={config};\n</script></body></html>"
