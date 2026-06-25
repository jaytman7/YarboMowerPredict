"""My Yarbo Mower integration."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_DEVICE_SERIAL,
    CONF_OVERWRITE,
    DOMAIN,
    PLATFORMS,
    SERVICE_GENERATE_DASHBOARD,
)
from .coordinator import MyYarboCoordinator
from .dashboard import async_generate_dashboard, async_generate_dashboard_if_missing

_LOGGER = logging.getLogger(__name__)

GENERATE_DASHBOARD_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_DEVICE_SERIAL): cv.string,
        vol.Optional(CONF_OVERWRITE, default=True): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up My Yarbo Mower from a config entry."""
    coordinator = MyYarboCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    entry.async_create_background_task(
        hass,
        _async_generate_initial_dashboard(hass, coordinator),
        name=f"{DOMAIN}_generate_initial_dashboard",
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: MyYarboCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_GENERATE_DASHBOARD):
        return

    async def _async_handle_generate_dashboard(call) -> None:
        serial = call.data.get(CONF_DEVICE_SERIAL)
        overwrite = call.data[CONF_OVERWRITE]
        coordinator, serial = _coordinator_for_serial(hass, serial)
        result = await async_generate_dashboard(
            hass,
            coordinator,
            serial,
            overwrite=overwrite,
        )
        if result.written:
            _LOGGER.info("Generated My Yarbo dashboard at %s", result.path)
        else:
            _LOGGER.info("My Yarbo dashboard already exists at %s", result.path)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_DASHBOARD,
        _async_handle_generate_dashboard,
        schema=GENERATE_DASHBOARD_SCHEMA,
    )


def _coordinator_for_serial(
    hass: HomeAssistant,
    serial: str | None,
) -> tuple[MyYarboCoordinator, str]:
    """Return the coordinator and serial for a service call."""
    coordinators = [
        value
        for value in hass.data.get(DOMAIN, {}).values()
        if isinstance(value, MyYarboCoordinator)
    ]
    devices = [
        (coordinator, device.sn)
        for coordinator in coordinators
        for device in coordinator.devices
    ]

    if serial is not None:
        for coordinator, device_serial in devices:
            if device_serial == serial:
                return coordinator, device_serial
        raise HomeAssistantError(f"Unknown Yarbo device serial: {serial}")

    if len(devices) == 1:
        return devices[0]

    if not devices:
        raise HomeAssistantError("No My Yarbo devices are loaded")
    raise HomeAssistantError(
        "device_serial is required because multiple My Yarbo devices are loaded"
    )


async def _async_generate_initial_dashboard(
    hass: HomeAssistant,
    coordinator: MyYarboCoordinator,
) -> None:
    """Generate the dashboard for one-device installs when no file exists."""
    await asyncio.sleep(1)
    try:
        result = await async_generate_dashboard_if_missing(hass, coordinator)
    except HomeAssistantError as err:
        _LOGGER.debug("Initial My Yarbo dashboard generation skipped: %s", err)
        return
    if result is not None and result.written:
        _LOGGER.info("Generated initial My Yarbo dashboard at %s", result.path)
