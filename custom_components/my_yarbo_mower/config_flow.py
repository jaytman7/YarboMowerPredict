"""Config flow for My Yarbo Mower."""

from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from yarbo_robot_sdk import AuthenticationError, YarboClient, YarboSDKError

from .const import CONF_SELECTED_DEVICES, DATA_ACCESS_TOKEN, DATA_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)


class MyYarboConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for My Yarbo Mower."""

    VERSION = 1
    MINOR_VERSION = 1

    _email: str | None = None
    _password: str | None = None
    _token: str | None = None
    _refresh_token: str | None = None
    _devices: list[Any] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return MyYarboOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for Yarbo account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            try:
                token, refresh_token, devices = await self._async_login_and_devices(
                    email, password
                )
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except YarboSDKError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(email.lower())
                self._abort_if_unique_id_configured()
                self._email = email
                self._password = password
                self._token = token
                self._refresh_token = refresh_token
                self._devices = devices
                return await self.async_step_select_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_select_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which Yarbo devices this standalone app should control."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_DEVICES, [])
            if not selected:
                errors["base"] = "no_devices_selected"
            else:
                return self.async_create_entry(
                    title=self._email,
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        DATA_ACCESS_TOKEN: self._token,
                        DATA_REFRESH_TOKEN: self._refresh_token,
                    },
                    options={CONF_SELECTED_DEVICES: selected},
                )

        options = {
            device.sn: f"{device.name} ({device.model}) - {device.sn}"
            for device in self._devices
        }
        return self.async_show_form(
            step_id="select_devices",
            data_schema=vol.Schema(
                {vol.Required(CONF_SELECTED_DEVICES, default=[]): cv.multi_select(options)}
            ),
            errors=errors,
        )

    async def _async_login_and_devices(self, email: str, password: str):
        def _work():
            api_url = os.environ.get("YARBO_API_BASE_URL")
            client = YarboClient(api_base_url=api_url) if api_url else YarboClient()
            try:
                client.login(email, password)
                return client.token, client.refresh_token, client.get_devices()
            finally:
                client.close()

        return await self.hass.async_add_executor_job(_work)


class MyYarboOptionsFlow(OptionsFlow):
    """Options flow for selected devices."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update selected devices."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SELECTED_DEVICES,
                        default=self.config_entry.options.get(CONF_SELECTED_DEVICES, []),
                    ): cv.multi_select(
                        {
                            sn: sn
                            for sn in self.config_entry.options.get(
                                CONF_SELECTED_DEVICES, []
                            )
                        }
                    )
                }
            ),
        )
