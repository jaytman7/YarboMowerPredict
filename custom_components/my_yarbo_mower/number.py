"""Number entities for My Yarbo Mower."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    APP_NAME,
    AUTO_MAX_WETNESS_DEFAULT,
    AUTO_MIN_BATTERY_DEFAULT,
    AUTO_MIN_FAVORABILITY_DEFAULT,
    AUTO_START_GRACE_MINUTES_DEFAULT,
    AUTO_WAKE_INTERVAL_MINUTES_DEFAULT,
    AUTO_WAKE_LEAD_MINUTES_DEFAULT,
    DOMAIN,
)
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


@dataclass(frozen=True)
class AutoNumberDef:
    """Automatic sequence number definition."""

    key: str
    name: str
    icon: str
    store_name: str
    default: float
    minimum: float
    maximum: float
    step: float
    unit: str


AUTO_NUMBERS = [
    AutoNumberDef(
        "auto_min_battery",
        "Auto Min Battery",
        "mdi:battery-check",
        "auto_min_battery",
        AUTO_MIN_BATTERY_DEFAULT,
        20,
        100,
        5,
        "%",
    ),
    AutoNumberDef(
        "auto_min_favorability",
        "Auto Min Favorability",
        "mdi:grass",
        "auto_min_favorability",
        AUTO_MIN_FAVORABILITY_DEFAULT,
        0,
        100,
        5,
        "%",
    ),
    AutoNumberDef(
        "auto_max_wetness",
        "Auto Max Wetness",
        "mdi:water-percent",
        "auto_max_wetness",
        AUTO_MAX_WETNESS_DEFAULT,
        0,
        100,
        5,
        "%",
    ),
    AutoNumberDef(
        "auto_start_grace",
        "Auto Start Grace",
        "mdi:clock-end",
        "auto_start_grace_minutes",
        AUTO_START_GRACE_MINUTES_DEFAULT,
        5,
        120,
        5,
        "min",
    ),
    AutoNumberDef(
        "auto_wake_lead",
        "Auto Wake Lead",
        "mdi:alarm",
        "auto_wake_lead_minutes",
        AUTO_WAKE_LEAD_MINUTES_DEFAULT,
        5,
        180,
        5,
        "min",
    ),
    AutoNumberDef(
        "auto_wake_interval",
        "Auto Wake Interval",
        "mdi:timer-sync-outline",
        "auto_wake_interval_minutes",
        AUTO_WAKE_INTERVAL_MINUTES_DEFAULT,
        5,
        60,
        5,
        "min",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for device in coordinator.devices:
        entities.append(MyYarboBladeHeight(coordinator, device))
        entities.append(MyYarboBladeSpeed(coordinator, device))
        entities.append(MyYarboStartPercent(coordinator, device))
        entities.extend(
            MyYarboAutoNumber(coordinator, device, number_def)
            for number_def in AUTO_NUMBERS
        )
        entities.append(
            MyYarboBlackoutHours(
                coordinator,
                device,
                "after_sunrise_blackout",
                "After Sunrise Blackout",
                "mdi:weather-sunset-up",
                coordinator.morning_blackout_hours,
            )
        )
        entities.append(
            MyYarboBlackoutHours(
                coordinator,
                device,
                "before_sunset_blackout",
                "Before Sunset Blackout",
                "mdi:weather-sunset-down",
                coordinator.evening_blackout_hours,
            )
        )
    async_add_entities(entities)


class MyYarboBladeHeight(MyYarboEntity, NumberEntity):
    """Blade height control."""

    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 20
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "mm"

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "blade_height")
        self._attr_name = f"{APP_NAME} Blade Height"
        self._optimistic: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._optimistic if self._optimistic is not None else self.float_field("MowerState.blade_height")

    async def async_set_native_value(self, value: float) -> None:
        self._optimistic = value
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_blade_height(self._device.sn, int(value))
        except Exception as err:
            raise HomeAssistantError(f"Failed to set blade height: {err}") from err


class MyYarboBladeSpeed(MyYarboEntity, NumberEntity):
    """Blade speed control."""

    _attr_icon = "mdi:fan"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "blade_speed")
        self._attr_name = f"{APP_NAME} Blade Speed"
        self._optimistic: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._optimistic if self._optimistic is not None else self.float_field("MowerState.blade_speed")

    async def async_set_native_value(self, value: float) -> None:
        self._optimistic = value
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_blade_speed(self._device.sn, int(value))
        except Exception as err:
            raise HomeAssistantError(f"Failed to set blade speed: {err}") from err


class MyYarboStartPercent(MyYarboEntity, NumberEntity, RestoreEntity):
    """Local plan start percent."""

    _attr_icon = "mdi:percent"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "plan_start_percent")
        self._attr_name = f"{APP_NAME} Plan Start Percent"
        self._value = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state and state.state not in ("unknown", "unavailable"):
            try:
                self._value = int(float(state.state))
            except (TypeError, ValueError):
                self._value = 0
        self.coordinator.plan_start_percent[self._device.sn] = self._value

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(0, min(100, int(value)))
        self.coordinator.plan_start_percent[self._device.sn] = self._value
        self.async_write_ha_state()


class MyYarboAutoNumber(MyYarboEntity, NumberEntity, RestoreEntity):
    """Restored automatic sequence threshold or timing setting."""

    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: MyYarboCoordinator,
        device,
        number_def: AutoNumberDef,
    ) -> None:
        super().__init__(coordinator, device, number_def.key)
        self._number_def = number_def
        self._attr_name = f"{APP_NAME} {number_def.name}"
        self._attr_icon = number_def.icon
        self._attr_native_min_value = number_def.minimum
        self._attr_native_max_value = number_def.maximum
        self._attr_native_step = number_def.step
        self._attr_native_unit_of_measurement = number_def.unit
        self._value = number_def.default

    async def async_added_to_hass(self) -> None:
        """Restore the previous value."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state and state.state not in ("unknown", "unavailable"):
            try:
                self._value = float(state.state)
            except (TypeError, ValueError):
                self._value = self._number_def.default
        self._value = self._clamp(self._value)
        self._store()[self._device.sn] = self._value

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = self._clamp(float(value))
        self._store()[self._device.sn] = self._value
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.data or {})

    def _store(self) -> dict[str, float]:
        return getattr(self.coordinator, self._number_def.store_name)

    def _clamp(self, value: float) -> float:
        return max(
            self._number_def.minimum,
            min(self._number_def.maximum, round(value, 2)),
        )


class MyYarboBlackoutHours(MyYarboEntity, NumberEntity, RestoreEntity):
    """Local mowing blackout window setting."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 8
    _attr_native_step = 0.25
    _attr_native_unit_of_measurement = "h"

    def __init__(
        self,
        coordinator: MyYarboCoordinator,
        device,
        key: str,
        name: str,
        icon: str,
        store: dict[str, float],
    ) -> None:
        super().__init__(coordinator, device, key)
        self._attr_name = f"{APP_NAME} {name}"
        self._attr_icon = icon
        self._store = store
        self._value = 3.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state and state.state not in ("unknown", "unavailable"):
            try:
                self._value = float(state.state)
            except (TypeError, ValueError):
                self._value = 3.0
        self._value = max(0.0, min(8.0, self._value))
        self._store[self._device.sn] = self._value

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = max(0.0, min(8.0, round(float(value), 2)))
        self._store[self._device.sn] = self._value
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.data or {})
