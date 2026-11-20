"""JSON snapshots of every device, for the Network panel and dashboard cards.

A snapshot holds only what the UI shows. Like the entity states, it contains
no passwords or Wi-Fi keys.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import commands
from .const import DOMAIN
from .entity import radio_label
from .parsers import radio_band

if TYPE_CHECKING:
    from .coordinator import AerohiveCoordinator, AerohiveData

SNAPSHOT_VERSION = 1


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _speed_mbps(value: str | None) -> int | None:
    """'1000' / '100M' / '1G' / '10 Gbps' -> Mbit/s."""
    if not value:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([GM]?)", str(value), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    return int(number * 1000) if match.group(2).upper() == "G" else int(number)


def device_kind(data: AerohiveData) -> str:
    if data.cli == "openwrt":
        return "router"
    if data.is_switch:
        return "switch"
    if (data.version.platform or "").upper().startswith("BR"):
        return "router"
    if data.is_wireless:
        return "ap"
    if data.ports():
        return "switch"
    # Connected, but the replies weren't recognised: don't pretend it's an access point.
    return "unknown"


def _ports(coordinator: AerohiveCoordinator, data: AerohiveData) -> list[dict[str, Any]]:
    uplinks = data.uplink_ports()
    clients: dict[str, list[dict[str, Any]]] = {}
    for entry in data.fdb:
        if entry.port:
            clients.setdefault(entry.port, []).append({"mac": entry.mac, "vlan": entry.vlan})
    result = []
    for name in data.ports():
        iface = data.interfaces[name]
        detail = data.details.get(name)
        rate = data.throughput.get(name)
        poe = data.poe_ports.get(name)
        lldp = data.lldp.get(name)
        link_state = iface.extra.get("link_state")
        speed = _speed_mbps(iface.speed) or (detail.speed_mbps if detail else None)
        result.append(
            {
                "name": name,
                "label": iface.extra.get("display_string"),
                "media": iface.extra.get("media"),
                "module": iface.extra.get("module"),
                "up": iface.is_up,
                "enabled": data.port_enabled(name),
                "present": link_state != "NP",
                "speed": speed if iface.is_up else None,
                "duplex": iface.duplex or (detail.duplex if detail else None),
                "vlan": iface.vlan,
                "uplink": name in uplinks,
                "rx_kbps": _round(rate.rx_kbps) if rate else None,
                "tx_kbps": _round(rate.tx_kbps) if rate else None,
                "rx_bytes": detail.rx_bytes if detail else None,
                "tx_bytes": detail.tx_bytes if detail else None,
                "errors": ((detail.rx_errors or 0) + (detail.tx_errors or 0)) if detail else None,
                "poe": None
                if poe is None
                else {
                    "enabled": data.poe_enabled(name),
                    "delivering": poe.delivering,
                    "power_w": _round(poe.power_w),
                    "status": poe.status,
                    "class": poe.poe_class,
                },
                "neighbor": None
                if lldp is None
                else {
                    "name": lldp.system_name,
                    "port": lldp.port_description or lldp.port_id,
                    "address": lldp.management_address,
                    "ap": lldp.is_access_point,
                    "network": lldp.is_network_device,
                },
                "clients": clients.get(name, []),
                "speed_setting": None if (setting := coordinator.port_speed_setting(name)) is None else {"speed": setting[0], "duplex": setting[1]},
                "speed_options": coordinator.port_speed_options(name),
            }
        )
    return result


def _radios(data: AerohiveData) -> list[dict[str, Any]]:
    result = []
    for radio in data.radio_names():
        iface = data.interfaces.get(radio)
        detail = data.details.get(radio)
        rate = data.throughput.get(radio)
        result.append(
            {
                "name": radio,
                "label": radio_label(data, radio),
                "band": radio_band(iface.radio_profile if iface else None, iface.channel if iface else None),
                "up": data.radio_up(radio),
                "channel": iface.channel if iface else None,
                "width": iface.channel_width_mhz if iface else None,
                "tx_power": detail.tx_power if detail else None,
                "eirp": detail.eirp_power if detail else None,
                "noise": detail.noise_floor if detail else None,
                "utilization": detail.total_utilization if detail else None,
                "clients": data.clients_on_radio(radio),
                "ssids": sorted(data.ssids_on_radio(radio)),
                "rx_kbps": _round(rate.rx_kbps) if rate else None,
                "tx_kbps": _round(rate.tx_kbps) if rate else None,
            }
        )
    return result


def _stations(data: AerohiveData) -> list[dict[str, Any]]:
    return [
        {
            "mac": sta.mac,
            "ip": sta.ip,
            "hostname": sta.hostname,
            "ssid": data.station_ssid(sta),
            "radio": sta.radio,
            "signal": sta.signal,
            "tx_rate": sta.tx_rate,
            "rx_rate": sta.rx_rate,
            "connected": sta.assoc_time,
            "snr": sta.snr,
            "vlan": sta.vlan,
            "security": sta.auth_mode,
            "channel": sta.channel,
        }
        for sta in data.stations.values()
    ]


def _problems(coordinator: AerohiveCoordinator, data: AerohiveData) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    if data.backup and data.backup.changed_after_reboot:
        problems.append({"kind": "changed_after_reboot", "text": "Configuration changed after a reboot"})
    for text in data.exos.boot_problems if data.exos else []:
        problems.append({"kind": "boot", "text": f"Boot problem: {text}"})
    if data.config_unsaved:
        problems.append({"kind": "unsaved", "text": "Unsaved configuration changes"})
    if data.save_error:
        problems.append({"kind": "save_failed", "text": "Saving a change on the device failed (retrying)"})
    for text in data.environment.problems if data.environment else []:
        problems.append({"kind": "hardware", "text": text[0].upper() + text[1:]})
    return problems


def device_snapshot(hass: HomeAssistant, coordinator: AerohiveCoordinator) -> dict[str, Any]:
    entry = coordinator.config_entry
    data = coordinator.data
    registry = dr.async_get(hass)
    device = next(iter(dr.async_entries_for_config_entry(registry, entry.entry_id)), None)
    base: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "device_id": device.id if device else None,
        "name": (device.name_by_user or device.name) if device else entry.title,
        "host": coordinator.client.host,
        "online": coordinator.last_update_success and data is not None,
    }
    if data is None:
        return {**base, "kind": "unknown"}
    version = data.version
    env = data.environment
    poe = data.poe
    backup = data.backup
    snapshot = {
        **base,
        "kind": device_kind(data),
        "cli": data.cli,
        "model": version.platform or (data.hardware.product if data.hardware else None),
        "firmware": version.version,
        "serial": (data.hardware.serial if data.hardware else None) or version.serial,
        "mac": data.mac,
        "ip": data.mgt.ip_address if data.mgt else None,
        "uptime": version.uptime_seconds,
        "boot_time": data.boot_time.isoformat() if data.boot_time else None,
        "cpu": _round(data.cpu.cpu_total) if data.cpu else None,
        "memory": _round(data.memory.memory_used_percent) if data.memory else None,
        "temperature": env.temperature_c if env else None,
        "temperature_status": env.temperature_status if env else None,
        "fans": env.fans_state if env else None,
        "fan_rpm": env.fan_rpm if env else {},
        "power_supplies": env.power_supplies if env else {},
        "poe": None
        if poe is None
        else {"budget_w": _round(poe.budget_w), "used_w": _round(poe.used_w), "remaining_w": _round(poe.remaining_w)},
        "ports": _ports(coordinator, data) if data.has_switch_ports or device_kind(data) == "switch" else [],
        "radios": _radios(data) if data.is_wireless else [],
        "ssids": [
            {
                "name": name,
                "broadcasting": data.ssid_broadcasting(name),
                "hidden": data.ssid_hidden(name),
                "clients": data.clients_on_ssid(name),
                "radios": [r for r in data.radio_names() if data.ssid_on_radio(name, r)],
                "max_clients": data.ssids[name].max_client,
                "security": data.config.ssid_security(name) if data.config else None,
                "configurable": data.ssid_configurable(name),
            }
            for name in sorted(data.ssids)
        ]
        if data.is_wireless
        else [],
        "stations": _stations(data),
        "neighbors": data.neighbors,
        # Wired devices known without a port (an OpenWrt router's LAN neighbours).
        "lan_clients": sorted({e.mac for e in data.fdb if not e.port and e.mac not in data.stations}),
        "wired_clients": len(data.wired_client_macs()),
        "vlans": [
            {"name": v.name, "vid": v.vid, "ip": v.ip_address, "prefix": v.prefix, "active": v.active_ports, "total": v.total_ports}
            for v in data.vlans.values()
        ],
        "config": {
            "unsaved": data.config_unsaved,
            "changed_after_reboot": bool(backup and backup.changed_after_reboot),
            "change_summary": backup.change_summary if backup else None,
            "boot_problems": data.exos.boot_problems if data.exos else [],
            "image_booted": data.exos.image_booted if data.exos else None,
            "config_booted": data.exos.config_booted if data.exos else None,
            "last_backup": backup.last_backup.isoformat() if backup and backup.last_backup else None,
            "backups": backup.count if backup else 0,
        },
        "hardware": {
            "console": data.panel.console,
            "mgmt": None
            if data.panel.mgmt_link is None and data.panel.mgmt_ip is None
            else {"link": data.panel.mgmt_link, "ip": data.panel.mgmt_ip},
            "usb": data.panel.usb,
            "psus": [{"name": n, "state": st} for n, st in (env.power_supplies if env else {}).items()],
        },
        "problems": _problems(coordinator, data),
        "features": {
            "port_names": data.cli == commands.DIALECT_EXOS,
            "port_control": data.cli != "openwrt",
            "port_speed": data.cli != "openwrt",
            "restore": data.cli != "openwrt",
            "poe": bool(data.poe_ports),
            "save": True,
            "reboot_save_prompt": data.cli == commands.DIALECT_EXOS,
        },
    }
    return snapshot


def network_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    from .coordinator import AerohiveCoordinator  # noqa: PLC0415 - avoid an import cycle

    devices = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = getattr(entry, "runtime_data", None)
        if isinstance(coordinator, AerohiveCoordinator):
            devices.append(device_snapshot(hass, coordinator))
    devices.sort(key=lambda d: ({"switch": 0, "router": 1, "ap": 2}.get(d.get("kind"), 3), (d["name"] or "").lower()))
    from .wifi import DATA_TIMERS  # noqa: PLC0415

    timers = hass.data.get(DATA_TIMERS)
    from . import clients as clients_mod  # noqa: PLC0415
    from .plan import DATA_PLAN  # noqa: PLC0415

    manager = hass.data.get(DATA_PLAN)
    directory = clients_mod.directory(hass, devices, manager.plan if manager else None, manager.requests if manager else None)
    for device in devices:
        device.pop("neighbors", None)  # folded into the client directory
    return {
        "version": SNAPSHOT_VERSION,
        "generated": datetime.now(UTC).isoformat(),
        "devices": devices,
        "wifi_timers": timers.as_list() if timers else [],
        "clients": directory,
    }
