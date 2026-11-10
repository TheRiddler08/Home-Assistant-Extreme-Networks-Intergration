"""Diagnostics for Aerohive Local (secrets redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_SSH_KEY, CONF_TRACKED_CLIENTS
from .coordinator import AerohiveConfigEntry
from .overview import is_overview
from .parsers import to_dict

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_HOST, CONF_SSH_KEY, CONF_TRACKED_CLIENTS}
STATION_REDACT = {"mac", "ip", "hostname"}
ADDRESS_REDACT = {"ip_address", "gateway", "ip_addr", "default_gateway", "serial", "serial_number"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AerohiveConfigEntry
) -> dict[str, Any]:
    if is_overview(entry):
        overview = entry.runtime_data.data
        return {"overview": True, "devices": [{"title": d.title, "online": d.online} for d in overview.devices]}
    coordinator = entry.runtime_data
    data = coordinator.data
    stations = []
    if data is not None:
        for station in data.stations.values():
            stations.append(async_redact_data(to_dict(station), STATION_REDACT))
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "client": {
            "hostname": coordinator.client.hostname,
            "paging_disabled": coordinator.client.paging_disabled,
            "unsupported_commands": sorted(coordinator._unsupported),  # noqa: SLF001
        },
        "data": None
        if data is None
        else {
            "version": to_dict(data.version),
            "interfaces": to_dict(data.interfaces),
            "ssids": to_dict(data.ssids),
            "details": async_redact_data(to_dict(data.details), ADDRESS_REDACT),
            "throughput": to_dict(data.throughput),
            "cpu": to_dict(data.cpu) if data.cpu else None,
            "memory": to_dict(data.memory) if data.memory else None,
            "hardware": async_redact_data(to_dict(data.hardware), ADDRESS_REDACT) if data.hardware else None,
            "boot_params": to_dict(data.boot_params) if data.boot_params else None,
            "station_count": len(data.stations),
            "stations": stations,
        },
    }
