"""Coordinator for My Yarbo Mower."""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from yarbo_robot_sdk import (
    AuthenticationError,
    TokenExpiredError,
    YarboClient,
    YarboSDKError,
)
from yarbo_robot_sdk.device_helpers import extract_field

from .const import (
    ACTIVE_PLANNING_STATES,
    CONF_SELECTED_DEVICES,
    DATA_ACCESS_TOKEN,
    DATA_REFRESH_TOKEN,
    DOMAIN,
    COMPLETED_PLANNING_STATE,
    UNKNOWN_PLAN,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT_SECONDS = 90
HEARTBEAT_CHECK_INTERVAL = timedelta(seconds=5)
SEQUENCE_STORE_VERSION = 1
SEQUENCE_STORE_DELAY = 2
GROWTH_UPDATE_INTERVAL = timedelta(minutes=30)
GROWTH_MAX_CATCHUP_HOURS = 24.0
GROWTH_MAX_DAILY_INCHES = 0.16
GROWTH_OPTIMAL_TEMP_F = 68.0
GROWTH_TEMP_SPREAD_F = 18.0
WEATHER_ENTITY = "weather.forecast_home"
SUN_ENTITY = "sun.sun"
WET_WEATHER = {"rainy", "pouring", "lightning-rainy", "snowy-rainy", "snowy", "hail"}
BAD_GROWTH_WEATHER = {"lightning", "exceptional", "hail", "snowy", "snowy-rainy"}


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> bool:
    """Merge source into target without dropping existing nested values."""
    changed = False
    for key, value in source.items():
        if key in ("__online__", "HeartBeatMSG"):
            continue
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if target[key].get(nested_key) != nested_value:
                    target[key][nested_key] = nested_value
                    changed = True
        elif target.get(key) != value:
            target[key] = value
            changed = True
    return changed


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MyYarboCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Owns the direct SDK connection for the standalone app."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        self.data = {}
        self._client: YarboClient | None = None
        self.devices: list[Any] = []
        self.plan_data: dict[str, list[dict[str, Any]]] = {}
        self.selected_plan: dict[str, int | None] = {}
        self.selected_plan_name: dict[str, str | None] = {}
        self.plan_start_percent: dict[str, int] = {}
        self.morning_blackout_hours: dict[str, float] = {}
        self.evening_blackout_hours: dict[str, float] = {}
        self.plan_sequence: dict[str, list[str]] = {}
        self.sequence_index: dict[str, int] = {}
        self.sequence_picker: dict[str, str | None] = {}
        self.active_plan_name: dict[str, str | None] = {}
        self.active_sequence_plan: dict[str, bool] = {}
        self.previous_completed_plan: dict[str, str | None] = {}
        self.plan_growth_inches: dict[str, dict[str, float]] = {}
        self.plan_growth_started_at: dict[str, dict[str, str]] = {}
        self.plan_last_mowed_at: dict[str, dict[str, str]] = {}
        self._last_growth_update: dict[str, str] = {}
        self._last_planning_state: dict[str, int | None] = {}
        self._sequence_store = Store(
            hass, SEQUENCE_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_sequence"
        )
        self._last_heartbeat: dict[str, float] = {}
        self._unsub_heartbeat_check: CALLBACK_TYPE | None = None
        self._unsub_growth_update: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Create SDK client, authenticate, subscribe, and fetch initial data."""
        await self._async_restore_sequence()
        api_url = os.environ.get("YARBO_API_BASE_URL")

        def _create_client() -> YarboClient:
            return YarboClient(api_base_url=api_url) if api_url else YarboClient()

        client = await self.hass.async_add_executor_job(_create_client)
        self._client = client

        email = self.entry.data[CONF_EMAIL]
        token = self.entry.data.get(DATA_ACCESS_TOKEN)
        refresh_token = self.entry.data.get(DATA_REFRESH_TOKEN)

        try:
            if token and refresh_token:
                await self.hass.async_add_executor_job(
                    client.restore_session, email, token, refresh_token
                )
            else:
                await self.hass.async_add_executor_job(
                    client.login, email, self.entry.data[CONF_PASSWORD]
                )
        except (AuthenticationError, TokenExpiredError) as err:
            raise ConfigEntryAuthFailed from err

        await self._async_refresh_stored_tokens()

        try:
            all_devices = await self.hass.async_add_executor_job(client.get_devices)
        except (TokenExpiredError, AuthenticationError) as err:
            raise ConfigEntryAuthFailed from err
        except YarboSDKError as err:
            raise ConfigEntryAuthFailed from err

        selected = set(self.entry.options.get(CONF_SELECTED_DEVICES, []))
        self.devices = [device for device in all_devices if not selected or device.sn in selected]

        try:
            await self.hass.async_add_executor_job(client.mqtt_connect)
            for device in self.devices:
                await self.hass.async_add_executor_job(
                    client.subscribe_device_message,
                    device.sn,
                    device.type_id,
                    self._on_device_status,
                )
                await self.hass.async_add_executor_job(
                    client.subscribe_heart_beat,
                    device.sn,
                    device.type_id,
                    self._on_heart_beat,
                )
                try:
                    await self.hass.async_add_executor_job(
                        client.subscribe_data_feedback,
                        device.sn,
                        device.type_id,
                        None,
                    )
                except YarboSDKError as err:
                    _LOGGER.debug("data_feedback subscription failed for %s: %s", device.sn, err)
        except YarboSDKError as err:
            _LOGGER.warning("Yarbo MQTT setup failed: %s", err)

        self._unsub_heartbeat_check = async_track_time_interval(
            self.hass, self._async_check_heartbeats, HEARTBEAT_CHECK_INTERVAL
        )
        self._unsub_growth_update = async_track_time_interval(
            self.hass, self._async_update_plan_growth, GROWTH_UPDATE_INTERVAL
        )

        self.entry.async_create_background_task(
            self.hass,
            self._async_initial_fetch(),
            name=f"{DOMAIN}_initial_fetch",
        )
        self.async_set_updated_data(self.data)

    async def async_shutdown(self) -> None:
        """Shut down SDK resources."""
        if self._unsub_heartbeat_check:
            self._unsub_heartbeat_check()
            self._unsub_heartbeat_check = None
        if self._unsub_growth_update:
            self._unsub_growth_update()
            self._unsub_growth_update = None
        if self._client is not None:
            await self.hass.async_add_executor_job(self._client.close)
            self._client = None

    def bound_device(self, sn: str):
        """Return an SDK bound device for a serial number."""
        if self._client is None:
            return None
        device = self.device_by_sn(sn)
        if device is None:
            return None
        return self._client.device(device, data=(self.data or {}).get(sn))

    def device_by_sn(self, sn: str):
        """Find a selected SDK device by serial number."""
        return next((device for device in self.devices if device.sn == sn), None)

    async def async_refresh_all(self, sn: str) -> None:
        """Refresh plan list and device snapshot."""
        device = self.device_by_sn(sn)
        if device is None:
            return
        await self.async_refresh_device_msg(sn, device.type_id)
        await self.async_refresh_plans(sn, device.type_id)

    async def async_refresh_device_msg(self, sn: str, type_id: str) -> None:
        """Refresh a full DeviceMSG snapshot."""
        if self._client is None:
            return
        try:
            bound = self.bound_device(sn)
            if bound is not None:
                result = await self.hass.async_add_executor_job(
                    bound.core.get_device_msg, 20.0
                )
            else:
                result = await self.hass.async_add_executor_job(
                    self._client.get_device_msg, sn, type_id, 20.0
                )
            msg_data = result.get("data", {})
            self.data.setdefault(sn, {})
            _deep_merge(self.data[sn], msg_data)
            self._track_plan_transition(sn)
            self.async_set_updated_data(self.data)
        except TimeoutError:
            _LOGGER.warning("DeviceMSG request timed out for %s", sn)
        except Exception as err:
            _LOGGER.warning("DeviceMSG request failed for %s: %s", sn, err)

    async def async_refresh_plans(self, sn: str, type_id: str) -> None:
        """Refresh the robot's plan list."""
        if self._client is None:
            return
        try:
            bound = self.bound_device(sn)
            if bound is not None:
                result = await self.hass.async_add_executor_job(bound.core.read_all_plan)
            else:
                result = await self.hass.async_add_executor_job(
                    self._client.read_all_plan, sn, type_id
                )
            self.plan_data[sn] = result.get("data", {}).get("data", [])
            self._ensure_sequence_picker(sn)
            self._ensure_plan_growth_entries(sn)
            self._persist_sequence()
            self.async_set_updated_data(self.data)
        except TimeoutError:
            _LOGGER.warning("Plan request timed out for %s", sn)
        except Exception as err:
            _LOGGER.warning("Plan request failed for %s: %s", sn, err)

    async def async_set_working_state(self, sn: str, state: int) -> None:
        """Set Yarbo working state: 1 working, 0 standby."""
        device = self.device_by_sn(sn)
        if device is None or self._client is None:
            return
        bound = self.bound_device(sn)
        if bound is not None:
            await self.hass.async_add_executor_job(bound.core.set_working_state, state)
            return
        await self.hass.async_add_executor_job(
            self._client.mqtt_publish_command,
            sn,
            device.type_id,
            "set_working_state",
            {"state": state, "source": "smart_home"},
        )

    async def async_start_plan(self, sn: str) -> None:
        """Start the next queued plan, or the selected plan when no queue exists."""
        device = self.device_by_sn(sn)
        sequence_plan = self.next_sequence_plan(sn)
        plan_name = sequence_plan or self.selected_plan_name.get(sn)
        plan_id = (
            self.plan_id_by_name(sn, sequence_plan)
            if sequence_plan is not None
            else self.selected_plan.get(sn)
        )
        if plan_id is None and device is not None and self._client is not None:
            await self.async_refresh_plans(sn, device.type_id)
            plan_id = (
                self.plan_id_by_name(sn, sequence_plan)
                if sequence_plan is not None
                else self.selected_plan.get(sn)
            )
        if device is None or self._client is None or plan_id is None:
            raise ValueError("No Yarbo plan selected")
        if plan_name is None:
            plan_name = self.plan_name_by_id(sn, plan_id)
        start_percent = max(0, min(100, int(self.plan_start_percent.get(sn, 0))))
        bound = self.bound_device(sn)
        if bound is not None:
            await self.hass.async_add_executor_job(
                bound.core.start_plan, plan_id, start_percent
            )
            self._mark_plan_started(sn, plan_name, sequence_plan is not None)
            return
        payload = {"id": plan_id}
        if start_percent > 0:
            payload["percent"] = start_percent
        await self.hass.async_add_executor_job(
            self._client.mqtt_publish_command,
            sn,
            device.type_id,
            "start_plan",
            payload,
        )
        self._mark_plan_started(sn, plan_name, sequence_plan is not None)

    async def async_core_command(self, sn: str, command: str) -> None:
        """Run a simple core command."""
        bound = self.bound_device(sn)
        if bound is None:
            raise ValueError("Yarbo device is unavailable")
        if command == "pause":
            await self.hass.async_add_executor_job(bound.core.pause)
        elif command == "resume":
            await self.hass.async_add_executor_job(bound.core.resume)
        elif command == "stop":
            await self.hass.async_add_executor_job(bound.core.stop)
        elif command == "dock":
            await self.hass.async_add_executor_job(bound.core.wireless_charging_cmd, 0)
            await self.hass.async_add_executor_job(bound.core.return_to_charge)
        else:
            raise ValueError(f"Unsupported command: {command}")

    async def async_set_blade_height(self, sn: str, height: int) -> None:
        """Set mower blade height."""
        bound = self.bound_device(sn)
        if bound is None:
            raise ValueError("Yarbo device is unavailable")
        await self.hass.async_add_executor_job(bound.mower.set_blade_height, height)

    async def async_set_blade_speed(self, sn: str, speed: int) -> None:
        """Set mower blade speed."""
        bound = self.bound_device(sn)
        if bound is None:
            raise ValueError("Yarbo device is unavailable")
        await self.hass.async_add_executor_job(bound.mower.set_blade_speed, speed)

    async def _async_initial_fetch(self) -> None:
        for device in self.devices:
            await self.async_refresh_device_msg(device.sn, device.type_id)
            await self.async_refresh_plans(device.sn, device.type_id)

    async def _async_restore_sequence(self) -> None:
        """Restore the locally managed plan sequence."""
        stored = await self._sequence_store.async_load()
        if not isinstance(stored, dict):
            return

        sequences = stored.get("plan_sequence") or {}
        if isinstance(sequences, dict):
            self.plan_sequence = {
                str(sn): [str(plan) for plan in plans if plan]
                for sn, plans in sequences.items()
                if isinstance(plans, list)
            }

        indexes = stored.get("sequence_index") or {}
        if isinstance(indexes, dict):
            self.sequence_index = {
                str(sn): max(0, int(index))
                for sn, index in indexes.items()
                if _as_int(index) is not None
            }

        pickers = stored.get("sequence_picker") or {}
        if isinstance(pickers, dict):
            self.sequence_picker = {
                str(sn): str(plan) if plan else None for sn, plan in pickers.items()
            }

        previous = stored.get("previous_completed_plan") or {}
        if isinstance(previous, dict):
            self.previous_completed_plan = {
                str(sn): str(plan) if plan else None for sn, plan in previous.items()
            }

        growth = stored.get("plan_growth_inches") or {}
        if isinstance(growth, dict):
            self.plan_growth_inches = {
                str(sn): {
                    str(plan): max(0.0, float(value))
                    for plan, value in plans.items()
                    if _as_float(value) is not None
                }
                for sn, plans in growth.items()
                if isinstance(plans, dict)
            }

        started = stored.get("plan_growth_started_at") or {}
        if isinstance(started, dict):
            self.plan_growth_started_at = {
                str(sn): {
                    str(plan): str(value)
                    for plan, value in plans.items()
                    if self._parse_datetime(value) is not None
                }
                for sn, plans in started.items()
                if isinstance(plans, dict)
            }

        last_mowed = stored.get("plan_last_mowed_at") or {}
        if isinstance(last_mowed, dict):
            self.plan_last_mowed_at = {
                str(sn): {
                    str(plan): str(value)
                    for plan, value in plans.items()
                    if self._parse_datetime(value) is not None
                }
                for sn, plans in last_mowed.items()
                if isinstance(plans, dict)
            }

        last_growth_update = stored.get("last_growth_update") or {}
        if isinstance(last_growth_update, dict):
            self._last_growth_update = {
                str(sn): str(value)
                for sn, value in last_growth_update.items()
                if self._parse_datetime(value) is not None
            }

    def _sequence_store_data(self) -> dict[str, Any]:
        return {
            "plan_sequence": self.plan_sequence,
            "sequence_index": self.sequence_index,
            "sequence_picker": self.sequence_picker,
            "previous_completed_plan": self.previous_completed_plan,
            "plan_growth_inches": self.plan_growth_inches,
            "plan_growth_started_at": self.plan_growth_started_at,
            "plan_last_mowed_at": self.plan_last_mowed_at,
            "last_growth_update": self._last_growth_update,
        }

    def _persist_sequence(self) -> None:
        self._sequence_store.async_delay_save(
            self._sequence_store_data, SEQUENCE_STORE_DELAY
        )

    def plan_names(self, sn: str) -> list[str]:
        """Return locally cached Yarbo plan names."""
        names: list[str] = []
        for plan in self.plan_data.get(sn, []):
            name = plan.get("name")
            plan_id = plan.get("id")
            if name is None or plan_id is None:
                continue
            plan_name = str(name)
            if plan_name not in names:
                names.append(plan_name)
        return names

    def plan_id_by_name(self, sn: str, plan_name: str | None) -> int | None:
        """Return a plan id for a cached Yarbo plan name."""
        if plan_name is None:
            return None
        for plan in self.plan_data.get(sn, []):
            if str(plan.get("name")) != plan_name:
                continue
            try:
                return int(plan["id"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def plan_name_by_id(self, sn: str, plan_id: int | None) -> str | None:
        """Return a plan name for a cached Yarbo plan id."""
        if plan_id is None:
            return None
        for plan in self.plan_data.get(sn, []):
            try:
                candidate_id = int(plan["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if candidate_id == plan_id and plan.get("name") is not None:
                return str(plan["name"])
        return None

    def next_sequence_plan(self, sn: str) -> str | None:
        """Return the queued plan that will run next."""
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return None
        return sequence[self._sequence_index_for(sn)]

    def next_run_plan(self, sn: str) -> str | None:
        """Return the plan used by the next start command."""
        return self.next_sequence_plan(sn) or self.selected_plan_name.get(sn)

    def plan_growth_details(
        self, sn: str, plan_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return display-ready growth details for plans."""
        names = plan_names if plan_names is not None else self.plan_names(sn)
        return [
            self._plan_growth_detail(sn, plan_name, index + 1)
            for index, plan_name in enumerate(names)
        ]

    def set_sequence_picker(self, sn: str, plan_name: str) -> None:
        """Select a plan name for queue editing."""
        if plan_name not in self.plan_names(sn):
            raise ValueError(f"Unknown Yarbo plan: {plan_name}")
        self.sequence_picker[sn] = plan_name
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    def add_sequence_plan(self, sn: str) -> str:
        """Append the selected queue-editing plan."""
        plan_name = self._selected_sequence_plan(sn)
        if plan_name is None:
            raise ValueError("No Yarbo plan selected for the sequence")
        self.plan_sequence.setdefault(sn, []).append(plan_name)
        self.sequence_index[sn] = self._sequence_index_for(sn)
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})
        return plan_name

    def remove_sequence_plan(self, sn: str) -> str | None:
        """Remove the selected plan from the sequence, falling back to the last item."""
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return None

        plan_name = self.sequence_picker.get(sn)
        remove_index = len(sequence) - 1
        if plan_name in sequence:
            remove_index = len(sequence) - 1 - sequence[::-1].index(plan_name)

        current_index = self._sequence_index_for(sn)
        removed = sequence.pop(remove_index)
        if not sequence:
            self.sequence_index[sn] = 0
        elif remove_index < current_index:
            self.sequence_index[sn] = (current_index - 1) % len(sequence)
        else:
            self.sequence_index[sn] = current_index % len(sequence)

        self._persist_sequence()
        self.async_set_updated_data(self.data or {})
        return removed

    def clear_sequence(self, sn: str) -> None:
        """Clear the local plan sequence."""
        self.plan_sequence[sn] = []
        self.sequence_index[sn] = 0
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    async def _async_refresh_stored_tokens(self) -> None:
        """Persist refreshed SDK tokens on the config entry."""
        if self._client is None:
            return
        token = getattr(self._client, "token", None)
        refresh_token = getattr(self._client, "refresh_token", None)
        if not token or not refresh_token:
            return
        if (
            token == self.entry.data.get(DATA_ACCESS_TOKEN)
            and refresh_token == self.entry.data.get(DATA_REFRESH_TOKEN)
        ):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                DATA_ACCESS_TOKEN: token,
                DATA_REFRESH_TOKEN: refresh_token,
            },
        )

    def _on_device_status(self, topic: str, data: dict[str, Any]) -> None:
        parts = topic.split("/")
        if len(parts) < 2:
            return
        sn = parts[1]
        self.data.setdefault(sn, {})
        if _deep_merge(self.data[sn], data):
            self._track_plan_transition(sn)
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, self.data)

    def _on_heart_beat(self, topic: str, data: dict[str, Any]) -> None:
        parts = topic.split("/")
        if len(parts) < 2:
            return
        sn = parts[1]
        self._last_heartbeat[sn] = time.monotonic()
        self.data.setdefault(sn, {})
        was_online = self.data[sn].get("__online__")
        prev = self.data[sn].get("HeartBeatMSG")
        self.data[sn]["__online__"] = True
        self.data[sn]["HeartBeatMSG"] = data
        if was_online is not True or prev != data:
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, self.data)

    async def _async_check_heartbeats(self, _now=None) -> None:
        now = time.monotonic()
        changed = False
        for device in self.devices:
            last = self._last_heartbeat.get(device.sn)
            if last is None or now - last > HEARTBEAT_TIMEOUT_SECONDS:
                self.data.setdefault(device.sn, {})
                if self.data[device.sn].get("__online__") is not False:
                    self.data[device.sn]["__online__"] = False
                    changed = True
        if changed:
            self.async_set_updated_data(self.data)

    async def _async_update_plan_growth(self, _now=None) -> None:
        """Accumulate estimated grass growth for each known plan."""
        now = dt_util.now()
        growth_rate = self._growth_rate_inches_per_day()
        changed = False

        for device in self.devices:
            sn = device.sn
            self._ensure_plan_growth_entries(sn, now)

            last_update = self._parse_datetime(self._last_growth_update.get(sn))
            self._last_growth_update[sn] = now.isoformat()
            if last_update is None:
                changed = True
                continue

            elapsed_hours = (now - last_update).total_seconds() / 3600
            if elapsed_hours <= 0:
                continue

            elapsed_hours = min(elapsed_hours, GROWTH_MAX_CATCHUP_HOURS)
            increment = growth_rate * (elapsed_hours / 24)
            if increment <= 0:
                changed = True
                continue

            plan_growth = self.plan_growth_inches.setdefault(sn, {})
            for plan_name in self._known_growth_plan_names(sn):
                plan_growth[plan_name] = round(
                    max(0.0, plan_growth.get(plan_name, 0.0) + increment), 3
                )
                changed = True

        if changed:
            self._persist_sequence()
            self.async_set_updated_data(self.data or {})

    def _ensure_sequence_picker(self, sn: str) -> None:
        if self.sequence_picker.get(sn) in self.plan_names(sn):
            return
        selected = self.selected_plan_name.get(sn)
        if selected in self.plan_names(sn):
            self.sequence_picker[sn] = selected
            return
        plans = self.plan_names(sn)
        self.sequence_picker[sn] = plans[0] if plans else None

    def _selected_sequence_plan(self, sn: str) -> str | None:
        self._ensure_sequence_picker(sn)
        plan_name = self.sequence_picker.get(sn)
        return plan_name if plan_name in self.plan_names(sn) else None

    def _known_growth_plan_names(self, sn: str) -> list[str]:
        names: list[str] = []
        for plan_name in (
            self.plan_names(sn)
            + self.plan_sequence.get(sn, [])
            + list(self.plan_growth_inches.get(sn, {}))
        ):
            if plan_name not in names:
                names.append(plan_name)
        return names

    def _ensure_plan_growth_entries(
        self, sn: str, now: datetime | None = None
    ) -> None:
        now = now or dt_util.now()
        now_iso = now.isoformat()
        plan_growth = self.plan_growth_inches.setdefault(sn, {})
        plan_started = self.plan_growth_started_at.setdefault(sn, {})
        for plan_name in self._known_growth_plan_names(sn):
            plan_growth.setdefault(plan_name, 0.0)
            plan_started.setdefault(plan_name, now_iso)

    def _reset_plan_growth(self, sn: str, plan_name: str) -> None:
        now_iso = dt_util.now().isoformat()
        self.plan_growth_inches.setdefault(sn, {})[plan_name] = 0.0
        self.plan_growth_started_at.setdefault(sn, {})[plan_name] = now_iso
        self.plan_last_mowed_at.setdefault(sn, {})[plan_name] = now_iso

    def _plan_growth_detail(
        self, sn: str, plan_name: str, position: int | None = None
    ) -> dict[str, Any]:
        started_at = self.plan_growth_started_at.get(sn, {}).get(plan_name)
        last_mowed_at = self.plan_last_mowed_at.get(sn, {}).get(plan_name)
        started = self._parse_datetime(started_at)
        growth_days = None
        if started is not None:
            growth_days = round(
                max(0.0, (dt_util.now() - started).total_seconds() / 86400), 1
            )
        return {
            "position": position,
            "name": plan_name,
            "growth_since_last_mow_in": round(
                self.plan_growth_inches.get(sn, {}).get(plan_name, 0.0), 2
            ),
            "growth_days": growth_days,
            "growth_started_at": started_at,
            "last_mowed_at": last_mowed_at,
        }

    def _sequence_index_for(self, sn: str) -> int:
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return 0
        index = self.sequence_index.get(sn, 0)
        return max(0, index) % len(sequence)

    def _planning_state(self, sn: str) -> int | None:
        return _as_int(extract_field((self.data or {}).get(sn, {}), "StateMSG.on_going_planning"))

    def _track_plan_transition(self, sn: str) -> None:
        state = self._planning_state(sn)
        if state is None:
            return
        previous = self._last_planning_state.get(sn)
        self._last_planning_state[sn] = state
        if state != COMPLETED_PLANNING_STATE or previous == COMPLETED_PLANNING_STATE:
            return
        if previous not in ACTIVE_PLANNING_STATES and not self.active_plan_name.get(sn):
            return
        self.hass.loop.call_soon_threadsafe(self._mark_plan_completed, sn)

    def _mark_plan_started(
        self, sn: str, plan_name: str | None, sequence_start: bool
    ) -> None:
        self.active_plan_name[sn] = plan_name or UNKNOWN_PLAN
        self.active_sequence_plan[sn] = sequence_start
        self._last_planning_state[sn] = self._planning_state(sn)
        self.async_set_updated_data(self.data or {})

    def _mark_plan_completed(self, sn: str) -> None:
        plan_name = self.active_plan_name.pop(sn, None)
        if not plan_name:
            return

        self.previous_completed_plan[sn] = plan_name
        if plan_name != UNKNOWN_PLAN:
            self._reset_plan_growth(sn, plan_name)
        if self.active_sequence_plan.pop(sn, False):
            sequence = self.plan_sequence.get(sn, [])
            if sequence:
                current_index = self._sequence_index_for(sn)
                if sequence[current_index] == plan_name:
                    self.sequence_index[sn] = (current_index + 1) % len(sequence)
                elif plan_name in sequence:
                    self.sequence_index[sn] = (sequence.index(plan_name) + 1) % len(sequence)
                else:
                    self.sequence_index[sn] = current_index

        self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    def growth_weather_metrics(self) -> dict[str, Any]:
        """Return the current grass-growth model inputs and output."""
        metrics = self._growth_weather_inputs()
        metrics["growth_rate_inches_per_day"] = self._growth_rate_inches_per_day(
            metrics
        )
        metrics["growth_model"] = "cool-season temperature potential"
        metrics["max_daily_growth_inches"] = GROWTH_MAX_DAILY_INCHES
        return metrics

    def _growth_rate_inches_per_day(
        self, metrics: dict[str, Any] | None = None
    ) -> float:
        metrics = metrics or self._growth_weather_inputs()
        temp_f = metrics["temperature_f"]
        humidity = metrics["humidity"]
        cloud_coverage = metrics["cloud_coverage"]
        condition = metrics["condition"]
        sun_state = metrics["sun_state"]

        if temp_f is None:
            temp_factor = 0.35
        else:
            temp_factor = math.exp(
                -0.5 * ((temp_f - GROWTH_OPTIMAL_TEMP_F) / GROWTH_TEMP_SPREAD_F) ** 2
            )
            if temp_f < 38 or temp_f > 100:
                temp_factor = 0.0

        moisture_factor = 1.0
        if humidity is not None:
            if humidity < 25:
                moisture_factor = 0.55
            elif humidity < 40:
                moisture_factor = 0.75
            elif humidity > 96:
                moisture_factor = 0.85

        sunlight_factor = 0.9
        if cloud_coverage is not None:
            if cloud_coverage >= 90:
                sunlight_factor = 0.65
            elif cloud_coverage >= 70:
                sunlight_factor = 0.8
            elif cloud_coverage <= 30:
                sunlight_factor = 1.0
        if sun_state == "below_horizon":
            sunlight_factor *= 0.75

        weather_factor = 0.65 if condition in BAD_GROWTH_WEATHER else 1.0
        return round(
            GROWTH_MAX_DAILY_INCHES
            * temp_factor
            * moisture_factor
            * sunlight_factor
            * weather_factor,
            4,
        )

    def _growth_weather_inputs(self) -> dict[str, Any]:
        weather_entity = self._weather_entity_id()
        weather = self.hass.states.get(weather_entity)
        sun = self.hass.states.get(SUN_ENTITY)
        attrs = weather.attributes if weather is not None else {}

        return {
            "weather_entity": weather_entity,
            "condition": weather.state if weather is not None else None,
            "temperature_f": self._temperature_f(attrs.get("temperature")),
            "humidity": self._float_value(attrs.get("humidity")),
            "cloud_coverage": self._float_value(attrs.get("cloud_coverage")),
            "sun_state": sun.state if sun is not None else None,
        }

    def _weather_entity_id(self) -> str:
        if self.hass.states.get(WEATHER_ENTITY) is not None:
            return WEATHER_ENTITY
        weather_states = self.hass.states.async_all("weather")
        if weather_states:
            return weather_states[0].entity_id
        return WEATHER_ENTITY

    def _temperature_f(self, value: Any) -> float | None:
        raw = self._float_value(value)
        if raw is None:
            return None
        unit = str(self.hass.config.units.temperature_unit)
        if unit in {"°C", "C"}:
            return round(raw * 9 / 5 + 32, 1)
        return round(raw, 1)

    def _float_value(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        parsed = dt_util.parse_datetime(str(value))
        if parsed is None:
            return None
        return dt_util.as_local(parsed)
