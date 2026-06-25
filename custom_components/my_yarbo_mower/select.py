"""Select entities for My Yarbo Mower."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import APP_NAME, DOMAIN
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for device in coordinator.devices:
        entities.append(MyYarboPlanSelect(coordinator, device))
        entities.append(MyYarboSequencePlanSelect(coordinator, device))
        entities.append(MyYarboPowerSelect(coordinator, device))
    async_add_entities(entities)


class MyYarboPlanSelect(MyYarboEntity, SelectEntity):
    """Plan selector backed by this integration's plan cache."""

    _attr_icon = "mdi:clipboard-list"

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "plan")
        self._attr_name = f"{APP_NAME} Plan"
        self._plan_id_by_name: dict[str, int | None] = {}

    @property
    def options(self) -> list[str]:
        """Return available plan names."""
        self._plan_id_by_name = {
            plan_name: self.coordinator.plan_id_by_name(self._device.sn, plan_name)
            for plan_name in self.coordinator.plan_names(self._device.sn)
        }
        return list(self._plan_id_by_name)

    @property
    def current_option(self) -> str | None:
        """Return selected plan."""
        current = self.coordinator.selected_plan_name.get(self._device.sn)
        return current if current in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Select a plan locally."""
        self.coordinator.selected_plan[self._device.sn] = self.coordinator.plan_id_by_name(
            self._device.sn, option
        )
        self.coordinator.selected_plan_name[self._device.sn] = option
        if self.coordinator.sequence_picker.get(self._device.sn) is None:
            self.coordinator.sequence_picker[self._device.sn] = option
        self.coordinator.async_set_updated_data(self.coordinator.data or {})
        self.async_write_ha_state()


class MyYarboSequencePlanSelect(MyYarboEntity, SelectEntity):
    """Selector for plan sequence editing."""

    _attr_icon = "mdi:playlist-edit"

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "sequence_plan")
        self._attr_name = f"{APP_NAME} Sequence Plan"

    @property
    def options(self) -> list[str]:
        """Return plans that can be added to the sequence."""
        return self.coordinator.plan_names(self._device.sn)

    @property
    def current_option(self) -> str | None:
        """Return the plan selected for queue editing."""
        current = self.coordinator.sequence_picker.get(self._device.sn)
        return current if current in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Select the plan used by add/remove sequence buttons."""
        self.coordinator.set_sequence_picker(self._device.sn, option)
        self.async_write_ha_state()


class MyYarboPowerSelect(MyYarboEntity, SelectEntity):
    """Working/standby command select."""

    _attr_icon = "mdi:power"
    _attr_options = ["working", "standby"]

    def __init__(self, coordinator: MyYarboCoordinator, device) -> None:
        super().__init__(coordinator, device, "power_state")
        self._attr_name = f"{APP_NAME} Power State"
        self._optimistic: str | None = None

    @property
    def current_option(self) -> str | None:
        """Return current working state."""
        if self._optimistic is not None:
            return self._optimistic
        state = self.int_field("HeartBeatMSG.working_state")
        if state is None:
            state = self.int_field("StateMSG.working_state")
        if state == 1:
            return "working"
        if state == 0:
            return "standby"
        return None

    async def async_select_option(self, option: str) -> None:
        """Set working state."""
        self._optimistic = option
        self.async_write_ha_state()
        await self.coordinator.async_set_working_state(
            self._device.sn, 1 if option == "working" else 0
        )
