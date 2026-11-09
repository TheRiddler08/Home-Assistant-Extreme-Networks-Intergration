"""Config and options flow for Aerohive Local."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import asyncssh
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_create_client
from .client import AerohiveAuthError, AerohiveError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SSH_KEY,
    CONF_TRACKED_CLIENTS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .parsers import normalize_mac, parse_show_interface, parse_show_version

_LOGGER = logging.getLogger(__name__)

_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        unit_of_measurement="s",
        mode=NumberSelectorMode.BOX,
    )
)


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
            vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Optional(CONF_SSH_KEY, default=defaults.get(CONF_SSH_KEY, "")): str,
            vol.Required(
                CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): _INTERVAL_SELECTOR,
        }
    )


async def _probe(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Connect, read identity, disconnect. Raises AerohiveError subclasses."""
    client = await async_create_client(hass, data)
    try:
        version = parse_show_version(await client.run("show version"))
        interfaces = parse_show_interface(await client.run("show interface"))
    finally:
        await client.close()
    mgt = interfaces.get("mgt0")
    return {
        "mac": mgt.mac if mgt else None,
        "hostname": client.hostname,
        "platform": version.platform,
    }


class AerohiveConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, data: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        try:
            return await _probe(self.hass, data)
        except AerohiveAuthError:
            errors["base"] = "invalid_auth"
        except (OSError, asyncssh.KeyImportError):
            errors[CONF_SSH_KEY] = "invalid_key"
        except AerohiveError as err:
            _LOGGER.debug("Connection test failed: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during setup")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {k: v for k, v in user_input.items() if v not in ("", None)}
            data[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            if not data.get(CONF_PASSWORD) and not data.get(CONF_SSH_KEY):
                errors["base"] = "password_or_key"
            else:
                info = await self._validate(data, errors)
                if info is not None:
                    await self.async_set_unique_id(info["mac"] or data[CONF_HOST])
                    self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
                    title = info["hostname"] or info["platform"] or data[CONF_HOST]
                    return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(user_input or {}), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **{k: v for k, v in user_input.items() if v}}
            if await self._validate(data, errors) is not None:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> AerohiveOptionsFlow:  # type: ignore[no-untyped-def]
        return AerohiveOptionsFlow()


class AerohiveOptionsFlow(OptionsFlow):
    """Scan interval and opt-in client tracking."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current_tracked: list[str] = list(self.config_entry.options.get(CONF_TRACKED_CLIENTS, []))

        if user_input is not None:
            tracked: list[str] = []
            for raw in user_input.get(CONF_TRACKED_CLIENTS, []):
                mac = normalize_mac(raw.split(" ", 1)[0])
                if mac is None:
                    errors[CONF_TRACKED_CLIENTS] = "invalid_mac"
                    break
                if mac not in tracked:
                    tracked.append(mac)
            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_TRACKED_CLIENTS: tracked,
                    }
                )

        options: list[SelectOptionDict] = []
        seen: set[str] = set()
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is not None and coordinator.data is not None:
            data = coordinator.data
            for mac, station in sorted(data.stations.items()):
                label = " ".join(
                    p for p in (mac, station.ip and f"({station.ip})", data.station_ssid(station)) if p
                )
                options.append(SelectOptionDict(value=mac, label=label))
                seen.add(mac)
        for mac in current_tracked:
            if mac not in seen:
                options.append(SelectOptionDict(value=mac, label=f"{mac} (not connected)"))

        interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=interval): _INTERVAL_SELECTOR,
                vol.Optional(CONF_TRACKED_CLIENTS, default=current_tracked): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True, custom_value=True)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
