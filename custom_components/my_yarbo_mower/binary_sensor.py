"""Binary sensors for My Yarbo Mower."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import APP_NAME, DOMAIN
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


@dataclass(frozen=True)
class BinaryDef:
    """Binary sensor definition."""

    key: str
    name: str
    icon: str | None = None
    device_class: BinarySensorDeviceClass | None = None


BINARY_SENSORS = [
    BinaryDef("online", "Online", None, BinarySensorDeviceClass.CONNECTIVITY),
    BinaryDef("charging", "Charging", "mdi:battery-charging"),
    BinaryDef("paused", "Paused", "mdi:pause-circle-outline"),
    BinaryDef("obstacle", "Obstacle", "mdi:alert-outline", BinarySensorDeviceClass.PROBLEM),
    BinaryDef("stuck", "Stuck", "mdi:map-marker-alert-outline", BinarySensorDeviceClass.PROBLEM),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        MyYarboBinarySensor(coordinator, device, sensor_def)
        for device in coordinator.devices
        for sensor_def in BINARY_SENSORS
    )


class MyYarboBinarySensor(MyYarboEntity, BinarySensorEntity):
    """Direct binary sensor."""

    def __init__(
        self, coordinator: MyYarboCoordinator, device, sensor_def: BinaryDef
    ) -> None:
        super().__init__(coordinator, device, sensor_def.key)
        self._sensor_def = sensor_def
        self._attr_name = f"{APP_NAME} {sensor_def.name}"
        self._attr_icon = sensor_def.icon
        self._attr_device_class = sensor_def.device_class

    @property
    def is_on(self) -> bool | None:
        """Return binary state."""
        key = self._sensor_def.key
        if key == "online":
            return self.online
        if key == "charging":
            status = self.int_field("BatteryMSG.status")
            return None if status is None else status > 1
        if key == "paused":
            paused = self.int_field("StateMSG.planning_paused")
            return None if paused is None else paused != 0
        if key == "obstacle":
            obstacle = self.int_field("StateMSG.obstacle")
            return None if obstacle is None else obstacle != 0
        if key == "stuck":
            stuck = self.int_field("StateMSG.stuck")
            return None if stuck is None else stuck != 0
        return None
