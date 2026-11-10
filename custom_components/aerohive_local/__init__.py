"""Aerohive / Extreme Networks local SSH integration."""

from __future__ import annotations

import logging
from typing import Any

import asyncssh
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import ConfigType

from .client import AerohiveClient
from .const import (
    ATTR_COMMAND,
    ATTR_DEVICE_ID,
    ATTR_MAC,
    ATTR_PORT,
    ATTR_RADIOS,
    ATTR_PASSWORD,
    ATTR_REDACT,
    ATTR_SECURITY_OBJECT,
    ATTR_SSID,
    CONF_SSH_KEY,
    DEFAULT_PORT,
    DOMAIN,
    SERVICE_CREATE_SSID,
    SERVICE_DELETE_SSID,
    SERVICE_DISCONNECT_CLIENT,
    SERVICE_POWER_CYCLE_POE,
    SERVICE_RUN_SHOW_COMMAND,
    SERVICE_SET_SSID_PASSWORD,
)
from .coordinator import AerohiveConfigEntry, AerohiveCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

RUN_SHOW_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Optional(ATTR_REDACT, default=True): cv.boolean,
    }
)
SET_PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SSID): cv.string,
        vol.Required(ATTR_PASSWORD): cv.string,
        vol.Optional(ATTR_SECURITY_OBJECT): cv.string,
    }
)
CREATE_SSID_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SSID): cv.string,
        vol.Optional(ATTR_PASSWORD): cv.string,
        vol.Optional(ATTR_RADIOS): vol.All(cv.ensure_list, [cv.string]),
    }
)
DELETE_SSID_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_SSID): cv.string,
    }
)
POWER_CYCLE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_PORT): cv.string,
    }
)
DISCONNECT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_MAC): cv.string,
    }
)


def load_client_key(path: str | None) -> Any | None:
    """Blocking: read an SSH private key from disk (run in executor)."""
    if not path:
        return None
    return asyncssh.read_private_key(path)


async def async_create_client(hass: HomeAssistant, data: dict[str, Any]) -> AerohiveClient:
    key = None
    if data.get(CONF_SSH_KEY):
        key = await hass.async_add_executor_job(load_client_key, data[CONF_SSH_KEY])
    return AerohiveClient(
        data[CONF_HOST],
        data.get(CONF_PORT, DEFAULT_PORT),
        data[CONF_USERNAME],
        data.get(CONF_PASSWORD) or None,
        client_key=key,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide services."""

    def coordinator_for(call: ServiceCall) -> AerohiveCoordinator:
        device = dr.async_get(hass).async_get(call.data[ATTR_DEVICE_ID])
        if device is None:
            raise ServiceValidationError(f"Unknown device {call.data[ATTR_DEVICE_ID]}")
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN and entry.state is ConfigEntryState.LOADED:
                return entry.runtime_data
        raise ServiceValidationError("Device is not a loaded Aerohive Local device")

    async def run_show(call: ServiceCall) -> ServiceResponse:
        coordinator = coordinator_for(call)
        output = await coordinator.async_run_show(call.data[ATTR_COMMAND], call.data[ATTR_REDACT])
        return {"command": call.data[ATTR_COMMAND], "output": output}

    async def set_password(call: ServiceCall) -> None:
        coordinator = coordinator_for(call)
        await coordinator.async_set_ssid_password(
            call.data[ATTR_SSID], call.data[ATTR_PASSWORD], call.data.get(ATTR_SECURITY_OBJECT)
        )

    async def disconnect(call: ServiceCall) -> None:
        coordinator = coordinator_for(call)
        await coordinator.async_disconnect_client(call.data[ATTR_MAC])

    hass.services.async_register(
        DOMAIN, SERVICE_RUN_SHOW_COMMAND, run_show, RUN_SHOW_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(DOMAIN, SERVICE_SET_SSID_PASSWORD, set_password, SET_PASSWORD_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISCONNECT_CLIENT, disconnect, DISCONNECT_SCHEMA)

    async def create_ssid(call: ServiceCall) -> None:
        coordinator = coordinator_for(call)
        radios = [r.strip().lower() for r in call.data.get(ATTR_RADIOS, []) if r.strip()]
        await coordinator.async_create_ssid(call.data[ATTR_SSID], call.data.get(ATTR_PASSWORD), radios or None)

    async def delete_ssid(call: ServiceCall) -> None:
        await coordinator_for(call).async_delete_ssid(call.data[ATTR_SSID])

    async def power_cycle_poe(call: ServiceCall) -> None:
        await coordinator_for(call).async_power_cycle_poe(call.data[ATTR_PORT].strip().lower())

    hass.services.async_register(DOMAIN, SERVICE_CREATE_SSID, create_ssid, CREATE_SSID_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_SSID, delete_ssid, DELETE_SSID_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_POWER_CYCLE_POE, power_cycle_poe, POWER_CYCLE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AerohiveConfigEntry) -> bool:
    try:
        client = await async_create_client(hass, dict(entry.data))
    except (OSError, asyncssh.KeyImportError) as err:
        raise ConfigEntryNotReady(f"Cannot load SSH key: {err}") from err
    coordinator = AerohiveCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise
    entry.runtime_data = coordinator
    if coordinator.enable_all_entities:
        _enable_integration_disabled_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _enable_integration_disabled_entities(hass: HomeAssistant, entry: AerohiveConfigEntry) -> None:
    """Re-enable entities this integration disabled by default (not user-disabled ones)."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)


async def _async_update_listener(hass: HomeAssistant, entry: AerohiveConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AerohiveConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = getattr(entry, "runtime_data", None)
    if unloaded and coordinator is not None:
        await coordinator.client.close()
    return unloaded

