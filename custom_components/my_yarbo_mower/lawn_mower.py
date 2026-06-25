"""Lawn mower entity for My Yarbo Mower."""

from __future__ import annotations

from homeassistant.components.lawn_mower import LawnMowerEntity
from homeassistant.components.lawn_mower.const import (
    LawnMowerActivity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTIVE_PLANNING_STATES,
    APP_NAME,
    CHARGING_RECHARGE_STATE,
    COMPLETED_PLANNING_STATE,
    DOMAIN,
    MOWER_HEAD_TYPES,
    RETURNING_STATES,
    RTK_READY,
)
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up mower entities."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(MyYarboMower(coordinator, device) for device in coordinator.devices)


class MyYarboMower(MyYarboEntity, LawnMowerEntity):
    """Native HA lawn mower control for the standalone app."""

    _attr_name = f"{APP_NAME} Mower"
    _attr_icon = "mdi:robot-mower"
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "mower")

    @property
    def available(self) -> bool:
        """Return whether the mower can be controlled."""
        return super().available and self.online

    @property
    def activity(self) -> LawnMowerActivity | None:
        """Return current mower activity."""
        planning = self.int_field("StateMSG.on_going_planning")
        recharging = self.int_field("StateMSG.on_going_recharging")
        error_code = self.int_field("StateMSG.error_code")
        paused = bool(self.int_field("StateMSG.planning_paused") or 0)
        charging = self._is_charging

        if (
            (planning is not None and planning < 0)
            or (recharging is not None and recharging < 0)
            or (error_code not in (None, 0))
        ):
            return LawnMowerActivity.ERROR
        if charging or recharging == CHARGING_RECHARGE_STATE:
            return LawnMowerActivity.DOCKED
        if recharging in RETURNING_STATES:
            return LawnMowerActivity.RETURNING
        if paused:
            return LawnMowerActivity.PAUSED
        if planning in ACTIVE_PLANNING_STATES:
            return LawnMowerActivity.MOWING
        return None

    async def async_start_mowing(self) -> None:
        """Start the selected plan, or resume if paused."""
        if self.activity == LawnMowerActivity.PAUSED:
            await self._run_command("resume")
            return
        self._check_can_start()
        try:
            await self.coordinator.async_start_plan(self._device.sn)
        except Exception as err:
            raise HomeAssistantError(f"Failed to start Yarbo plan: {err}") from err

    async def async_pause(self) -> None:
        """Pause the mower."""
        await self._run_command("pause")

    async def async_dock(self) -> None:
        """Return the mower to dock."""
        self._check_can_dock()
        await self._run_command("dock")

    @property
    def _is_charging(self) -> bool:
        status = self.int_field("BatteryMSG.status")
        return status is not None and status > 1

    def _check_can_start(self) -> None:
        if not self.online:
            raise HomeAssistantError("Cannot start: Yarbo is offline")
        head_type = self.int_field("HeadMsg.head_type")
        if head_type is not None and head_type not in MOWER_HEAD_TYPES:
            raise HomeAssistantError("Cannot start: mower head is not attached")
        if (
            self.coordinator.next_sequence_plan(self._device.sn) is None
            and self.coordinator.selected_plan.get(self._device.sn) is None
        ):
            raise HomeAssistantError("Cannot start: no plan selected")
        if self.coordinator.weather_start_blocked():
            raise HomeAssistantError(self.coordinator.weather_start_block_reason())
        if self._is_charging:
            raise HomeAssistantError("Cannot start: Yarbo is charging")
        recharge_state = self.int_field("BodyMsg.rechargeState")
        if recharge_state in (1, 3):
            raise HomeAssistantError("Cannot start: Yarbo is wired charging")
        rtk = self.int_field("RTKMSG.status") or 0
        if rtk not in RTK_READY:
            raise HomeAssistantError("Cannot start: RTK/GPS signal is weak")
        planning = self.int_field("StateMSG.on_going_planning") or 0
        if planning > 0 and planning != COMPLETED_PLANNING_STATE:
            raise HomeAssistantError("Cannot start: a plan is already running")
        recharging = self.int_field("StateMSG.on_going_recharging") or 0
        if recharging > 0 and recharging != CHARGING_RECHARGE_STATE:
            raise HomeAssistantError("Cannot start: Yarbo is already returning")

    def _check_can_dock(self) -> None:
        if not self.online:
            raise HomeAssistantError("Cannot dock: Yarbo is offline")
        if self._is_charging:
            raise HomeAssistantError("Cannot dock: Yarbo is already charging")
        recharging = self.int_field("StateMSG.on_going_recharging") or 0
        if recharging > 0 and recharging != CHARGING_RECHARGE_STATE:
            raise HomeAssistantError("Cannot dock: Yarbo is already returning")

    async def _run_command(self, command: str) -> None:
        try:
            await self.coordinator.async_core_command(self._device.sn, command)
        except Exception as err:
            raise HomeAssistantError(f"Yarbo command failed: {err}") from err
