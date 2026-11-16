"""Diagnostics for Aerohive Local.

The file is meant to be attached to public bug reports, so every identifier
is redacted: credentials, host, MAC addresses, serial numbers, IP addresses,
hostnames and LLDP neighbour identifiers, wherever they appear (also inside
the raw "key: value" fields the device reports). Configuration snippets are
left out; only their size is reported.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_SSH_KEY, CONF_TRACKED_CLIENTS
from .coordinator import AerohiveConfigEntry
from .overview import is_overview
from .parsers import to_dict

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_HOST, CONF_SSH_KEY, CONF_TRACKED_CLIENTS}

# A key is sensitive if one of its "_"-separated parts is one of these
# ("wifi0_mac_addr", "system_mac", "serial_number"; not "machine_type").
_SENSITIVE_PARTS = {"mac", "serial", "password", "secret", "passphrase"}
_SENSITIVE_KEYS = {
    "ip", "ip_addr", "ip_address", "ipv4_addr", "ipv4_address", "gateway", "default_gateway",
    "management_address", "address", "chassis_id", "hostname", "host", "sysname", "sys_name",
    "system_name", "syslocation", "syscontact", "sys_location", "sys_contact", "line",
}  # fmt: skip


def _is_sensitive(key: str) -> bool:
    key = key.lower()
    return key in _SENSITIVE_KEYS or bool(_SENSITIVE_PARTS & set(key.split("_")))


def redact(obj: Any) -> Any:
    """Recursively replace sensitive values (dict keys decide)."""
    if isinstance(obj, dict):
        return {
            key: (REDACTED if isinstance(key, str) and _is_sensitive(key) and value not in (None, "") else redact(value))
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AerohiveConfigEntry
) -> dict[str, Any]:
    if is_overview(entry):
        overview = entry.runtime_data.data
        return {"overview": True, "devices": [{"online": d.online, "cli": d.data.cli} for d in overview.devices]}
    coordinator = entry.runtime_data
    data = coordinator.data
    payload: dict[str, Any] | None = None
    if data is not None:
        payload = {
            "cli": data.cli,
            "version": to_dict(data.version),
            "interfaces": to_dict(data.interfaces),
            "ssids": to_dict(data.ssids),
            "details": to_dict(data.details),
            "throughput": to_dict(data.throughput),
            "cpu": to_dict(data.cpu) if data.cpu else None,
            "memory": to_dict(data.memory) if data.memory else None,
            "hardware": to_dict(data.hardware) if data.hardware else None,
            "boot_params": to_dict(data.boot_params) if data.boot_params else None,
            "exos": to_dict(data.exos) if data.exos else None,
            "environment": to_dict(data.environment) if data.environment else None,
            "poe": to_dict(data.poe) if data.poe else None,
            "poe_ports": to_dict(data.poe_ports),
            "config_unsaved": data.config_unsaved,
            "backup": None
            if data.backup is None
            else {
                "last_backup": data.backup.last_backup.isoformat() if data.backup.last_backup else None,
                "count": data.backup.count,
                "changed_after_reboot": data.backup.changed_after_reboot,
                "lines_added": (data.backup.change_summary or {}).get("added"),
                "lines_removed": (data.backup.change_summary or {}).get("removed"),
            },
            "lldp_neighbors": len(data.lldp),
            "mac_table_entries": len(data.fdb),
            "station_count": len(data.stations),
            "stations": [to_dict(s) for s in data.stations.values()],
        }
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "client": {
            "cli": coordinator.client.dialect,
            "paging_disabled": coordinator.client.paging_disabled,
            "unsupported_commands": sorted(coordinator._unsupported),  # noqa: SLF001
        },
        "data": redact(payload),
    }
