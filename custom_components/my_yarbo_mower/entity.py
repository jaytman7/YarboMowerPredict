"""Shared entity helpers for My Yarbo Mower."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from yarbo_robot_sdk.device_helpers import extract_field

from .const import APP_NAME, DOMAIN, MANUFACTURER
from .coordinator import MyYarboCoordinator


def as_int(value: Any) -> int | None:
    """Convert a value to int if possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    """Convert a value to float if possible."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MyYarboEntity(CoordinatorEntity[MyYarboCoordinator]):
    """Base entity for a selected Yarbo device."""

    _attr_has_entity_name = False

    def __init__(self, coordinator: MyYarboCoordinator, device, key: str) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.sn}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device metadata."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.sn)},
            name=f"{APP_NAME} {self._device.sn}",
            manufacturer=MANUFACTURER,
            model=getattr(self._device, "model", None),
            serial_number=self._device.sn,
        )

    @property
    def data(self) -> dict[str, Any]:
        """Return this device's current coordinator data."""
        return (self.coordinator.data or {}).get(self._device.sn, {})

    @property
    def online(self) -> bool:
        """Return whether this device is heartbeat-online."""
        return self.data.get("__online__") is True

    def field(self, path: str) -> Any:
        """Extract a nested SDK field from this device's data."""
        return extract_field(self.data, path)

    def int_field(self, path: str) -> int | None:
        """Extract a nested SDK field as int."""
        return as_int(self.field(path))

    def float_field(self, path: str) -> float | None:
        """Extract a nested SDK field as float."""
        return as_float(self.field(path))
