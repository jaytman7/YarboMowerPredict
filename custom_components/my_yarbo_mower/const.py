"""Constants for My Yarbo Mower."""

from __future__ import annotations

DOMAIN = "my_yarbo_mower"
DASHBOARD_FILENAME = "yarbo_mower_app-dashboard.yaml"
SERVICE_GENERATE_DASHBOARD = "generate_dashboard"
CONF_DEVICE_SERIAL = "device_serial"
CONF_OVERWRITE = "overwrite"
PLATFORMS = [
    "binary_sensor",
    "button",
    "lawn_mower",
    "number",
    "select",
    "sensor",
    "switch",
]

CONF_SELECTED_DEVICES = "selected_devices"
DATA_ACCESS_TOKEN = "access_token"
DATA_REFRESH_TOKEN = "refresh_token"

MANUFACTURER = "Yarbo"
APP_NAME = "My Yarbo"

AUTO_MIN_BATTERY_DEFAULT = 70.0
AUTO_MIN_FAVORABILITY_DEFAULT = 70.0
AUTO_MAX_WETNESS_DEFAULT = 45.0
AUTO_START_GRACE_MINUTES_DEFAULT = 30.0
AUTO_WAKE_LEAD_MINUTES_DEFAULT = 45.0
AUTO_WAKE_INTERVAL_MINUTES_DEFAULT = 10.0
CHARGING_FULL_NOISE_BATTERY_PERCENT = 95.0

MOWER_HEAD_TYPES = {3, 5}
RTK_READY = {4, 5}
ACTIVE_PLANNING_STATES = {1, 2, 3, 11, 12}
RETURNING_STATES = {1, 2, 3, 99}
CHARGING_RECHARGE_STATE = 4
COMPLETED_PLANNING_STATE = 5
UNKNOWN_PLAN = "None"

PLANNING_STATUS_MAP = {
    0: "Idle",
    1: "Mowing",
    2: "Calculating route",
    3: "Driving to area",
    5: "Completed",
    11: "Waypoint navigation",
    12: "Waypoint complete",
    -2: "Create plan history failed",
    -10: "Plan not found",
    -11: "Failed to read plan",
    -12: "Failed to calculate route",
    -20: "Outside mapped area",
    -21: "Area data error",
    -22: "Route data error",
    -23: "In no-go zone",
    -24: "Low battery",
    -26: "Module position failure",
    -30: "Location data error",
    -31: "Docking station error",
    -40: "Obstacle mark failed",
    -42: "Out of boundary",
    -43: "Unable to navigate obstacle",
    -44: "Exceeded boundary",
    -47: "Out of boundary >1.5m",
    -88: "In no-go zone",
    -92: "Out of boundary",
}

RECHARGING_STATUS_MAP = {
    0: "Idle",
    1: "Returning on path",
    2: "Returning in area",
    3: "Repositioning",
    4: "Charging",
    99: "Verifying",
    -2: "Server error",
    -3: "Direction uninitialized",
    -4: "Dock uninitialized",
    -5: "Recharge failed",
    -6: "Failed to park",
    -8: "Docking connection failed",
    -9: "Stuck",
    -20: "Outside mapped area",
}

RTK_STATUS_MAP = {
    4: "Strong",
    5: "Medium",
}

HEAD_TYPE_MAP = {
    3: "Mower",
    5: "Mower Pro",
}
