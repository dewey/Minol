"""Sensor platform for Minol Energy.

Sensors are derived from the mobile app's Mulesoft Experience API.
The API returns monthly consumption periods; each period contains
one entry per service type (100=Heating, 200=Hot Water, 300=Cold Water).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfMass, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MinolConfigEntry
from .const import (
    CONF_COLD_WATER_PRICE,
    CONF_HEATING_PRICE,
    CONF_HOT_WATER_PRICE,
    DOMAIN,
    SERVICE_COLD_WATER,
    SERVICE_HEATING,
    SERVICE_HOT_WATER,
)
from .coordinator import MinolDataCoordinator

_LOGGER = logging.getLogger(__name__)

_DEVICE_INFO_BASE = {
    "manufacturer": "Minol-ZENNER",
    "model": "MinolApp API",
    "entry_type": "service",
}


@dataclass(frozen=True)
class _ServiceMeta:
    service_code: str
    type_text: str
    icon: str
    energy_unit: str
    service_unit: str  # raw meter unit shown in attributes
    device_class: SensorDeviceClass | None


_SERVICES: list[_ServiceMeta] = [
    _ServiceMeta(
        service_code=SERVICE_HEATING,
        type_text="Heating",
        icon="mdi:radiator",
        energy_unit=UnitOfEnergy.KILO_WATT_HOUR,
        service_unit="EH",
        device_class=SensorDeviceClass.ENERGY,
    ),
    _ServiceMeta(
        service_code=SERVICE_HOT_WATER,
        type_text="Hot Water",
        icon="mdi:water-boiler",
        energy_unit=UnitOfEnergy.KILO_WATT_HOUR,
        service_unit="m³",
        device_class=SensorDeviceClass.ENERGY,
    ),
    _ServiceMeta(
        service_code=SERVICE_COLD_WATER,
        type_text="Cold Water",
        icon="mdi:water",
        energy_unit=UnitOfVolume.CUBIC_METERS,
        service_unit="m³",
        device_class=SensorDeviceClass.WATER,
    ),
]

_PRICE_CONF_KEY: dict[str, str] = {
    SERVICE_HEATING: CONF_HEATING_PRICE,
    SERVICE_HOT_WATER: CONF_HOT_WATER_PRICE,
    SERVICE_COLD_WATER: CONF_COLD_WATER_PRICE,
}


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MinolConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Minol sensors from a config entry."""
    coordinator: MinolDataCoordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        MinolTenantInfoSensor(coordinator, entry),
    ]

    latest = coordinator.data.get("latest_consumption", {})
    active_services = {
        c.get("service") for c in latest.get("consumptions", [])
    }

    for svc in _SERVICES:
        if svc.service_code not in active_services:
            continue
        # Latest month energy / volume
        entities.append(
            MinolConsumptionSensor(
                coordinator=coordinator,
                entry=entry,
                service=svc,
                field="energyValue",
                suffix="latest_month",
                name=f"{svc.type_text} Latest Month",
                unit=svc.energy_unit,
                state_class=SensorStateClass.TOTAL,
                device_class=svc.device_class,
                icon=svc.icon,
            )
        )
        # CO2 sensor
        entities.append(
            MinolConsumptionSensor(
                coordinator=coordinator,
                entry=entry,
                service=svc,
                field="co2kg",
                suffix="co2_latest_month",
                name=f"{svc.type_text} CO₂ Latest Month",
                unit=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
                device_class=SensorDeviceClass.WEIGHT,
                icon="mdi:molecule-co2",
            )
        )
        # Cost sensor (only when price configured)
        price = entry.options.get(_PRICE_CONF_KEY.get(svc.service_code, ""), 0.0)
        if price:
            entities.append(
                MinolCostSensor(
                    coordinator=coordinator,
                    entry=entry,
                    service=svc,
                    price=float(price),
                )
            )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_consumption_entry(
    data: dict[str, Any], service_code: str
) -> dict[str, Any]:
    """Return the consumption dict for a specific service in the latest period."""
    latest = data.get("latest_consumption", {})
    for cons in latest.get("consumptions", []):
        if cons.get("service") == service_code:
            return cons
    return {}


# ---------------------------------------------------------------------------
# Sensor entities
# ---------------------------------------------------------------------------


class MinolConsumptionSensor(CoordinatorEntity[MinolDataCoordinator], SensorEntity):
    """A sensor showing a single field for one service type in the latest period."""

    _attr_has_entity_name = True
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: MinolDataCoordinator,
        entry: MinolConfigEntry,
        service: _ServiceMeta,
        field: str,
        suffix: str,
        name: str,
        unit: str | None,
        state_class: SensorStateClass | None,
        device_class: SensorDeviceClass | None,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._service = service
        self._field = field

        slug = f"{service.service_code}_{suffix}".lower()
        self._attr_unique_id = f"{entry.entry_id}_{slug}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_device_info = {
            **_DEVICE_INFO_BASE,
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Minol Energy",
        }

    @property
    def native_value(self) -> float | None:
        cons = _get_consumption_entry(
            self.coordinator.data, self._service.service_code
        )
        val = cons.get(self._field)
        return float(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        latest = self.coordinator.data.get("latest_consumption", {})
        cons = _get_consumption_entry(
            self.coordinator.data, self._service.service_code
        )
        attrs: dict[str, Any] = {
            "period": latest.get("period"),
            "status": latest.get("statusOverall"),
            "estimated": cons.get("estimated"),
            "service_value": cons.get("serviceValue"),
            "service_unit": cons.get("serviceUnit") or self._service.service_unit,
            "energy_unit": cons.get("energyUnit"),
        }
        return {k: v for k, v in attrs.items() if v is not None}


class MinolTenantInfoSensor(CoordinatorEntity[MinolDataCoordinator], SensorEntity):
    """Sensor showing property / tenant info from the user profile."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:home-account"

    def __init__(
        self,
        coordinator: MinolDataCoordinator,
        entry: MinolConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tenant_info"
        self._attr_name = "Tenant Info"
        self._attr_device_info = {
            **_DEVICE_INFO_BASE,
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Minol Energy",
        }

    @property
    def native_value(self) -> str | None:
        profile = self.coordinator.data.get("profile", {})
        addr = profile.get("billingUnitAddress", {})
        street = addr.get("street", "")
        num = addr.get("houseNumber", "")
        city = addr.get("city", "")
        postal = addr.get("zip", "")
        if street:
            return f"{street} {num}, {postal} {city}".strip(", ")
        return profile.get("billingUnit")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        profile = self.coordinator.data.get("profile", {})
        ref = profile.get("residentialUnitReference", {})
        addr = profile.get("billingUnitAddress", {})
        return {
            "user_id": profile.get("userID"),
            "email": profile.get("eMail"),
            "first_name": profile.get("firstName"),
            "last_name": profile.get("lastName"),
            "billing_unit": profile.get("billingUnit"),
            "residential_unit_id": ref.get("residentialUnitID"),
            "floor": ref.get("floor"),
            "position": ref.get("position"),
            "move_in_date": profile.get("moveInDate"),
            "city": addr.get("city"),
            "country": addr.get("country"),
        }


class MinolCostSensor(CoordinatorEntity[MinolDataCoordinator], SensorEntity):
    """Estimated cost: latest month consumption × configured price per unit."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "\u20ac"
    _attr_icon = "mdi:currency-eur"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: MinolDataCoordinator,
        entry: MinolConfigEntry,
        service: _ServiceMeta,
        price: float,
    ) -> None:
        super().__init__(coordinator)
        self._service = service
        self._price = price

        slug = f"{service.service_code}_cost".lower()
        self._attr_unique_id = f"{entry.entry_id}_{slug}"
        self._attr_name = f"{service.type_text} Cost"
        self._attr_device_info = {
            **_DEVICE_INFO_BASE,
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Minol Energy",
        }

    @property
    def native_value(self) -> float | None:
        cons = _get_consumption_entry(
            self.coordinator.data, self._service.service_code
        )
        energy = cons.get("energyValue")
        if energy is None:
            return None
        return round(float(energy) * self._price, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"price_per_unit": self._price}
