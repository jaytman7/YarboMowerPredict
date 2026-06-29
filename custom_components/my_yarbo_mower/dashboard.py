"""Dashboard generation for My Yarbo Mower."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import DASHBOARD_FILENAME, DOMAIN

TEMPLATE_FILENAME = "dashboard_template.yaml"
PLACEHOLDER_PATTERN = re.compile(r"@@[A-Z0-9_]+@@")


@dataclass(frozen=True)
class DashboardResult:
    """Result from a dashboard generation request."""

    path: str
    device_serial: str
    written: bool


ENTITY_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "ADD_SEQUENCE_PLAN_BUTTON": ("button", "add_sequence_plan"),
    "AFTER_SUNRISE_BLACKOUT_NUMBER": ("number", "after_sunrise_blackout"),
    "AUTO_MAX_WETNESS_NUMBER": ("number", "auto_max_wetness"),
    "AUTO_MIN_BATTERY_NUMBER": ("number", "auto_min_battery"),
    "AUTO_MIN_FAVORABILITY_NUMBER": ("number", "auto_min_favorability"),
    "AUTO_SEQUENCE_READY_BINARY_SENSOR": ("binary_sensor", "sequence_auto_ready"),
    "AUTO_SEQUENCE_START_SWITCH": ("switch", "auto_sequence_start"),
    "AUTO_START_GRACE_NUMBER": ("number", "auto_start_grace"),
    "AUTO_WAKE_CHECKS_SWITCH": ("switch", "auto_wake_checks"),
    "AUTO_WAKE_INTERVAL_NUMBER": ("number", "auto_wake_interval"),
    "AUTO_WAKE_LEAD_NUMBER": ("number", "auto_wake_lead"),
    "BATTERY_SENSOR": ("sensor", "battery"),
    "BEFORE_SUNSET_BLACKOUT_NUMBER": ("number", "before_sunset_blackout"),
    "BEST_MOW_START_SENSOR": ("sensor", "best_mow_start"),
    "BLADE_HEIGHT_NUMBER": ("number", "blade_height"),
    "BLADE_SPEED_NUMBER": ("number", "blade_speed"),
    "CHARGING_BINARY_SENSOR": ("binary_sensor", "charging"),
    "CHARGING_POWER_SENSOR": ("sensor", "charging_power"),
    "CLEAR_SEQUENCE_BUTTON": ("button", "clear_sequence"),
    "DOCK_BUTTON": ("button", "dock"),
    "ERROR_CODE_SENSOR": ("sensor", "error_code"),
    "GENERATE_DASHBOARD_BUTTON": ("button", "generate_dashboard"),
    "GPS_SATELLITES_SENSOR": ("sensor", "gps_satellites"),
    "GRASS_WETNESS_SENSOR": ("sensor", "grass_wetness"),
    "HEAD_TYPE_SENSOR": ("sensor", "head_type"),
    "MOWER_ENTITY": ("lawn_mower", "mower"),
    "MOWING_CONDITIONS_SENSOR": ("sensor", "mowing_conditions"),
    "NEXT_RUN_PLAN_SENSOR": ("sensor", "next_run_plan"),
    "NEXT_SEQUENCE_PLAN_BUTTON": ("button", "next_sequence_plan"),
    "OBSTACLE_BINARY_SENSOR": ("binary_sensor", "obstacle"),
    "ONLINE_BINARY_SENSOR": ("binary_sensor", "online"),
    "PAUSE_BUTTON": ("button", "pause"),
    "PAUSED_BINARY_SENSOR": ("binary_sensor", "paused"),
    "PLAN_SELECT": ("select", "plan"),
    "PLAN_SEQUENCE_SENSOR": ("sensor", "plan_sequence"),
    "PLAN_START_PERCENT_NUMBER": ("number", "plan_start_percent"),
    "PLAN_STATUS_SENSOR": ("sensor", "plan_status"),
    "POWER_STATE_SELECT": ("select", "power_state"),
    "PREVIOUS_COMPLETED_PLAN_SENSOR": ("sensor", "previous_completed_plan"),
    "RAIN_SENSOR": ("sensor", "rain_sensor"),
    "RECHARGE_STATUS_SENSOR": ("sensor", "recharge_status"),
    "REFRESH_BUTTON": ("button", "refresh"),
    "REFRESH_PLANS_BUTTON": ("button", "refresh_plans"),
    "REMOVE_SEQUENCE_PLAN_BUTTON": ("button", "remove_sequence_plan"),
    "RESUME_BUTTON": ("button", "resume"),
    "RTK_SIGNAL_SENSOR": ("sensor", "rtk_signal"),
    "SEQUENCE_PLAN_SELECT": ("select", "sequence_plan"),
    "START_PLAN_BUTTON": ("button", "start"),
    "STOP_BUTTON": ("button", "stop"),
    "STUCK_BINARY_SENSOR": ("binary_sensor", "stuck"),
    "WAKE_BUTTON": ("button", "wake"),
    "WARM_WEATHER_GRASS_SWITCH": ("switch", "warm_weather_grass"),
    "WEATHER_SOURCE_SELECT": ("select", "weather_source"),
    "WEATHER_WINDOW_SENSOR": ("sensor", "weather_window"),
}


async def async_generate_dashboard(
    hass: HomeAssistant,
    coordinator: Any,
    device_serial: str,
    *,
    overwrite: bool = True,
) -> DashboardResult:
    """Generate the YAML dashboard with this HA instance's entity IDs."""
    if coordinator.device_by_sn(device_serial) is None:
        raise HomeAssistantError(f"Unknown Yarbo device serial: {device_serial}")

    target = Path(hass.config.path(DASHBOARD_FILENAME))
    if target.exists() and not overwrite:
        return DashboardResult(str(target), device_serial, False)

    replacements = _resolve_entities(hass, device_serial)
    template = _template_path().read_text(encoding="utf-8")
    dashboard_yaml = _render_template(template, replacements)

    await hass.async_add_executor_job(
        target.write_text,
        dashboard_yaml,
        "utf-8",
    )
    return DashboardResult(str(target), device_serial, True)


async def async_generate_dashboard_if_missing(
    hass: HomeAssistant,
    coordinator: Any,
) -> DashboardResult | None:
    """Generate the dashboard once for simple one-device installs."""
    target = Path(hass.config.path(DASHBOARD_FILENAME))
    if target.exists() or len(coordinator.devices) != 1:
        return None
    return await async_generate_dashboard(
        hass,
        coordinator,
        coordinator.devices[0].sn,
        overwrite=False,
    )


def _resolve_entities(hass: HomeAssistant, device_serial: str) -> dict[str, str]:
    """Resolve dashboard placeholders through Home Assistant's entity registry."""
    registry = er.async_get(hass)
    replacements: dict[str, str] = {}
    missing: list[str] = []

    for placeholder, (platform, key) in ENTITY_PLACEHOLDERS.items():
        entity_id = registry.async_get_entity_id(
            platform,
            DOMAIN,
            f"{device_serial}_{key}",
        )
        if entity_id is None:
            missing.append(f"{platform}.{key}")
            continue
        replacements[placeholder] = entity_id

    if missing:
        raise HomeAssistantError(
            "Cannot generate dashboard because these My Yarbo entities are missing: "
            + ", ".join(sorted(missing))
        )
    return replacements


def _render_template(template: str, replacements: dict[str, str]) -> str:
    """Render a dashboard template using resolved entity IDs."""
    rendered = template
    for placeholder, entity_id in replacements.items():
        rendered = rendered.replace(f"@@{placeholder}@@", entity_id)

    unresolved = sorted(set(PLACEHOLDER_PATTERN.findall(rendered)))
    if unresolved:
        raise HomeAssistantError(
            "Cannot generate dashboard because placeholders were not resolved: "
            + ", ".join(unresolved)
        )
    return rendered


def _template_path() -> Path:
    """Return the packaged dashboard template path."""
    return Path(__file__).with_name(TEMPLATE_FILENAME)
