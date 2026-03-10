"""Stub out homeassistant modules so api.py can be imported without a running HA."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Register stubs for every homeassistant namespace touched by the package
# __init__.py and its siblings.  Must happen before any test module is imported.
_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.components",
    "homeassistant.components.diagnostics",
    "homeassistant.components.sensor",
    "voluptuous",
]
for _mod in _HA_MODULES:
    sys.modules.setdefault(_mod, MagicMock())
