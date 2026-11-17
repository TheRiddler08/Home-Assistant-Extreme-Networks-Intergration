"""Aerohive / Extreme Networks local SSH integration."""

from __future__ import annotations

import asyncio

import logging
from typing import Any

import asyncssh
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .client import AerohiveClient
from .const import (
    ATTR_COMMAND,
    ATTR_DEVICE_ID,
    ATTR_MAC,
    ATTR_PASSWORD,
    ATTR_PORT,
    ATTR_RADIOS,
    ATTR_REDACT,
    ATTR_SECURITY_OBJECT,
    ATTR_SSID,
    CONF_ENTRY_TYPE,
    CONF_SSH_KEY,
    DEFAULT_PORT,
    DOMAIN,
    ENTRY_TYPE_OVERVIEW,
    SERVICE_CREATE_SSID,
    SERVICE_DELETE_SSID,
    SERVICE_DISCONNECT_CLIENT,
    SERVICE_POWER_CYCLE_POE,
    SERVICE_RUN_SHOW_COMMAND,
    SERVICE_SET_SSID_PASSWORD,
)
from . import websocket_api
from .coordinator import AerohiveConfigEntry, AerohiveCoordinator
from .frontend import async_register_frontend
from .overview import OverviewCoordinator, is_overview

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
SET_PORT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_PORT): cv.string,
        vol.Required("enabled"): cv.boolean,
        vol.Optional("force", default=False): cv.boolean,
    }
)
DEVICE_ONLY_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
SET_POE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_PORT): cv.string,
        vol.Required("enabled"): cv.boolean,
    }
)
PORT_DESCRIPTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_PORT): cv.string,
        vol.Optional("description", default=""): vol.Any(None, cv.string),
    }
)
REBOOT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("save_changes"): cv.boolean,
        vol.Required("confirm"): cv.boolean,
    }
)
RESTORE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional("file"): cv.string,
        vol.Optional("save", default=False): cv.boolean,
        vol.Required("confirm"): cv.boolean,
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
            if (
                entry is not None
                and entry.domain == DOMAIN
                and entry.state is ConfigEntryState.LOADED
                and isinstance(entry.runtime_data, AerohiveCoordinator)
            ):
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

    async def set_port(call: ServiceCall) -> None:
        await coordinator_for(call).async_set_port_enabled(
            call.data[ATTR_PORT].strip().lower(), call.data["enabled"], call.data["force"]
        )

    hass.services.async_register(DOMAIN, "set_port", set_port, SET_PORT_SCHEMA)

    async def set_poe(call: ServiceCall) -> None:
        await coordinator_for(call).async_set_poe(call.data[ATTR_PORT].strip().lower(), call.data["enabled"])

    async def set_port_description(call: ServiceCall) -> None:
        await coordinator_for(call).async_set_port_description(
            call.data[ATTR_PORT].strip(), call.data.get("description") or ""
        )

    async def reboot(call: ServiceCall) -> None:
        if not call.data["confirm"]:
            raise ServiceValidationError("Set confirm: true to reboot the device")
        await coordinator_for(call).async_reboot(call.data.get("save_changes"))

    async def refresh(call: ServiceCall) -> None:
        if call.data.get(ATTR_DEVICE_ID):
            await coordinator_for(call).async_refresh_now()
            return
        coordinators = [
            e.runtime_data
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED and isinstance(e.runtime_data, AerohiveCoordinator)
        ]
        results = await asyncio.gather(*(c.async_refresh_now() for c in coordinators), return_exceptions=True)
        failed = [str(r) for r in results if isinstance(r, Exception)]
        if failed:
            raise HomeAssistantError("; ".join(failed))

    hass.services.async_register(
        DOMAIN, "refresh", refresh, vol.Schema({vol.Optional(ATTR_DEVICE_ID): cv.string})
    )
    hass.services.async_register(DOMAIN, "set_poe", set_poe, SET_POE_SCHEMA)
    hass.services.async_register(DOMAIN, "set_port_description", set_port_description, PORT_DESCRIPTION_SCHEMA)
    hass.services.async_register(DOMAIN, "reboot", reboot, REBOOT_SCHEMA)

    async def backup_config(call: ServiceCall) -> ServiceResponse:
        return await coordinator_for(call).async_backup_config()

    async def list_backups(call: ServiceCall) -> ServiceResponse:
        return {"backups": await coordinator_for(call).async_list_backups()}

    async def save_config(call: ServiceCall) -> None:
        await coordinator_for(call).async_save_config()

    async def restore_config(call: ServiceCall) -> ServiceResponse:
        if not call.data["confirm"]:
            raise ServiceValidationError("Set confirm: true to restore a configuration backup")
        return await coordinator_for(call).async_restore_config(call.data.get("file"), call.data["save"])

    hass.services.async_register(
        DOMAIN, "backup_config", backup_config, DEVICE_ONLY_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "list_backups", list_backups, DEVICE_ONLY_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(DOMAIN, "save_config", save_config, DEVICE_ONLY_SCHEMA)

    async def accept_config(call: ServiceCall) -> ServiceResponse:
        return await coordinator_for(call).async_accept_config()

    hass.services.async_register(
        DOMAIN, "accept_config", accept_config, DEVICE_ONLY_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, "restore_config", restore_config, RESTORE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )

    websocket_api.async_register(hass)
    await async_register_frontend(hass)
    return True


OVERVIEW_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, entry: AerohiveConfigEntry) -> bool:
    if is_overview(entry):
        overview = OverviewCoordinator(hass, entry)
        await overview.async_config_entry_first_refresh()
        entry.runtime_data = overview  # type: ignore[assignment]
        await hass.config_entries.async_forward_entry_setups(entry, OVERVIEW_PLATFORMS)
        return True
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
    # Keep the Network panel live: every update (and unload) pushes a new snapshot.
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: async_dispatcher_send(hass, websocket_api.SIGNAL_SNAPSHOT))
    )
    entry.async_on_unload(lambda: async_dispatcher_send(hass, websocket_api.SIGNAL_SNAPSHOT))
    async_dispatcher_send(hass, websocket_api.SIGNAL_SNAPSHOT)
    _ensure_overview_entry(hass)
    return True


def _ensure_overview_entry(hass: HomeAssistant) -> None:
    """Create the 'Aerohive Network' overview entry once (it can be disabled, not re-added)."""
    overview_entries = [e for e in hass.config_entries.async_entries(DOMAIN, include_ignore=True) if is_overview(e)]
    for overview in overview_entries:
        # A device just finished loading: let the overview pick it up now, not in 15 s.
        if overview.state is ConfigEntryState.LOADED:
            hass.async_create_task(overview.runtime_data.async_request_refresh())
    if overview_entries:
        return
    if hass.data.setdefault(f"{DOMAIN}_overview_requested", False):
        return
    hass.data[f"{DOMAIN}_overview_requested"] = True
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data={CONF_ENTRY_TYPE: ENTRY_TYPE_OVERVIEW}
        )
    )


def _enable_integration_disabled_entities(hass: HomeAssistant, entry: AerohiveConfigEntry) -> None:
    """Re-enable entities this integration disabled by default (not user-disabled ones)."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)


# Entities created disabled by earlier versions that are now enabled by default.
_NOW_ENABLED_PREFIXES = ("psk_", "port_")


# Entities of the old on-screen SSID manager (replaced by the Wi-Fi manager in
# the integration's Configure dialog in 1.3).
_REMOVED_KEYS = ("new_ssid_name", "new_ssid_password", "create_ssid", "delete_ssid", "ssid_delete_target")


async def async_migrate_entry(hass: HomeAssistant, entry: AerohiveConfigEntry) -> bool:
    """1.1 -> 1.2: enable the Wi-Fi key text and port switches created disabled before.
    1.2 -> 1.3: remove the old SSID manager entities.
    """
    registry = er.async_get(hass)
    base = "overview" if is_overview(entry) else (entry.unique_id or entry.entry_id)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    if entry.version == 1 and entry.minor_version < 2 and not is_overview(entry):
        for reg_entry in entries:
            key = (reg_entry.unique_id or "").removeprefix(f"{base}_")
            if reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION and key.startswith(
                _NOW_ENABLED_PREFIXES
            ):
                registry.async_update_entity(reg_entry.entity_id, disabled_by=None)
    if entry.version == 1 and entry.minor_version < 3:
        for reg_entry in entries:
            if (reg_entry.unique_id or "").removeprefix(f"{base}_") in _REMOVED_KEYS:
                registry.async_remove(reg_entry.entity_id)
        hass.config_entries.async_update_entry(entry, minor_version=3)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: AerohiveConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: AerohiveConfigEntry) -> None:
    """Remove this device's repair issues (backups are kept on disk)."""
    for kind in ("config_changed_after_reboot", "boot_config_problem"):
        ir.async_delete_issue(hass, DOMAIN, f"{kind}_{entry.entry_id}")


async def async_unload_entry(hass: HomeAssistant, entry: AerohiveConfigEntry) -> bool:
    if is_overview(entry):
        return await hass.config_entries.async_unload_platforms(entry, OVERVIEW_PLATFORMS)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = getattr(entry, "runtime_data", None)
    if unloaded and coordinator is not None:
        try:
            await coordinator.async_flush_save()
        except Exception:  # noqa: BLE001 - unloading must not fail
            _LOGGER.warning("Could not save %s before unloading", coordinator.client.host)
        await coordinator.client.close()
    return unloaded

