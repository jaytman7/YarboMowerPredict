"""Switches for My Yarbo Mower."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import APP_NAME, DOMAIN
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


@dataclass(frozen=True)
class AutoSwitchDef:
    """Restored switch definition."""

    key: str
    name: str
    icon: str
    store_name: str


AUTO_SWITCHES = [
    AutoSwitchDef(
        "auto_wake_checks",
        "Auto Wake Checks",
        "mdi:alarm-check",
        "auto_wake_checks",
    ),
    AutoSwitchDef(
        "auto_sequence_start",
        "Auto Sequence Start",
        "mdi:robot-mower-outline",
        "auto_sequence_start",
    ),
    AutoSwitchDef(
        "warm_weather_grass",
        "Warm Weather Grass",
        "mdi:sprout",
        "warm_weather_grass",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        MyYarboAutoSwitch(coordinator, device, switch_def)
        for device in coordinator.devices
        for switch_def in AUTO_SWITCHES
    )


class MyYarboAutoSwitch(MyYarboEntity, SwitchEntity, RestoreEntity):
    """Restored local behavior switch."""

    def __init__(
        self,
        coordinator: MyYarboCoordinator,
        device,
        switch_def: AutoSwitchDef,
    ) -> None:
        super().__init__(coordinator, device, switch_def.key)
        self._switch_def = switch_def
        self._attr_name = f"{APP_NAME} {switch_def.name}"
        self._attr_icon = switch_def.icon
        self._value = False

    async def async_added_to_hass(self) -> None:
        """Restore the previous switch value."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state is None:
            self._value = bool(self._store().get(self._device.sn, False))
        else:
            self._value = state.state == "on"
        self._store()[self._device.sn] = self._value
        self.coordinator.persist_local_state()

    @property
    def is_on(self) -> bool:
        """Return current switch value."""
        return self._value

    async def async_turn_on(self, **kwargs) -> None:
        """Enable automatic behavior."""
        self._value = True
        self._store()[self._device.sn] = self._value
        self.coordinator.persist_local_state()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.data or {})

    async def async_turn_off(self, **kwargs) -> None:
        """Disable automatic behavior."""
        self._value = False
        self._store()[self._device.sn] = self._value
        self.coordinator.persist_local_state()
        self.async_write_ha_state()
        self.coordinator.async_set_updated_data(self.coordinator.data or {})

    def _store(self) -> dict[str, bool]:
        return getattr(self.coordinator, self._switch_def.store_name)
