"""Buttons for My Yarbo Mower."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import APP_NAME, DOMAIN
from .coordinator import MyYarboCoordinator
from .dashboard import async_generate_dashboard
from .entity import MyYarboEntity


@dataclass(frozen=True)
class ButtonDef:
    """Button definition."""

    key: str
    name: str
    icon: str


BUTTONS = [
    ButtonDef("start", "Start Plan", "mdi:play"),
    ButtonDef("pause", "Pause", "mdi:pause"),
    ButtonDef("resume", "Resume", "mdi:play-pause"),
    ButtonDef("stop", "Stop", "mdi:stop"),
    ButtonDef("dock", "Dock", "mdi:home-import-outline"),
    ButtonDef("wake", "Wake", "mdi:power"),
    ButtonDef("add_sequence_plan", "Add Sequence Plan", "mdi:playlist-plus"),
    ButtonDef("remove_sequence_plan", "Remove Sequence Plan", "mdi:playlist-minus"),
    ButtonDef("clear_sequence", "Clear Sequence", "mdi:playlist-remove"),
    ButtonDef("refresh", "Refresh", "mdi:refresh"),
    ButtonDef("refresh_plans", "Refresh Plans", "mdi:clipboard-list"),
    ButtonDef("generate_dashboard", "Generate Dashboard", "mdi:view-dashboard-edit"),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        MyYarboButton(coordinator, device, button_def)
        for device in coordinator.devices
        for button_def in BUTTONS
    )


class MyYarboButton(MyYarboEntity, ButtonEntity):
    """Direct command button."""

    def __init__(
        self, coordinator: MyYarboCoordinator, device, button_def: ButtonDef
    ) -> None:
        super().__init__(coordinator, device, button_def.key)
        self._button_def = button_def
        self._attr_name = f"{APP_NAME} {button_def.name}"
        self._attr_icon = button_def.icon

    async def async_press(self) -> None:
        """Press the button."""
        key = self._button_def.key
        try:
            if key == "start":
                await self.coordinator.async_start_plan(self._device.sn)
            elif key in ("pause", "resume", "stop", "dock"):
                await self.coordinator.async_core_command(self._device.sn, key)
            elif key == "wake":
                await self.coordinator.async_set_working_state(self._device.sn, 1)
            elif key == "add_sequence_plan":
                self.coordinator.add_sequence_plan(self._device.sn)
            elif key == "remove_sequence_plan":
                self.coordinator.remove_sequence_plan(self._device.sn)
            elif key == "clear_sequence":
                self.coordinator.clear_sequence(self._device.sn)
            elif key == "refresh":
                await self.coordinator.async_refresh_all(self._device.sn)
            elif key == "refresh_plans":
                await self.coordinator.async_refresh_plans(
                    self._device.sn, self._device.type_id
                )
            elif key == "generate_dashboard":
                await async_generate_dashboard(
                    self.hass,
                    self.coordinator,
                    self._device.sn,
                    overwrite=True,
                )
        except Exception as err:
            raise HomeAssistantError(f"Yarbo button failed: {err}") from err
