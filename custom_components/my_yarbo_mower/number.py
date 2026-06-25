"""Number entities for My Yarbo Mower."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import APP_NAME, DOMAIN
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


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
