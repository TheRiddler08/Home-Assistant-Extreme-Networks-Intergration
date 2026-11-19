"""DataUpdateCoordinator for one Aerohive device."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import discovery_flow
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import backup as backup_mod
from . import commands
from .client import (
    AerohiveAuthError,
    AerohiveClient,
    AerohiveCommandError,
    AerohiveConnectionError,
    AerohiveError,
)
from .const import (
    CONF_DETAILED_STATS,
    CONF_ENABLE_ALL,
    CONF_SCAN_INTERVAL,
    DEFAULT_DETAILED_STATS,
    DEFAULT_ENABLE_ALL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    SLOW_REFRESH_SECONDS,
)
from .discovery import is_aerohive_mac
from . import netplan as netplan_mod
from . import openwrt as ow
from .parsers import (
    combo_port_count,
    media_kind,
    parse_exos_mgmt_port,
    parse_exos_port_media,
    model_ethernet_ports,
    dialect_from_version,
    BootParams,
    EnvironmentInfo,
    ExosSwitchInfo,
    FdbEntry,
    HardwareInfo,
    Interface,
    InterfaceDetail,
    LldpNeighbor,
    PoePort,
    PoeSummary,
    RunningConfigSummary,
    Ssid,
    Station,
    SystemStats,
    VersionInfo,
    find_cli_error,
    is_aerohive_hostname,
    is_unsupported_command,
    normalize_mac,
    parse_exos_cpu,
    parse_exos_fans,
    parse_exos_fdb,
    parse_exos_inline_power,
    parse_exos_inline_power_info,
    parse_exos_lldp_detail_all,
    parse_exos_lldp_neighbors,
    parse_exos_port_statistics,
    parse_exos_ports,
    parse_exos_power,
    parse_exos_show_switch,
    parse_exos_temperature,
    parse_exos_version,
    parse_exos_vlans,
    parse_fastpath_vlans,
    fan_rpms,
    ExosVlan,
    parse_key_values,
    parse_lldp_neighbors,
    parse_mac_address_table,
    parse_poe_port_detail,
    parse_running_config_summary,
    parse_show_boot_param,
    parse_show_cpu,
    parse_show_hw_info,
    parse_show_interface,
    parse_show_interface_detail,
    parse_show_memory,
    parse_show_network,
    parse_show_poe,
    parse_show_port_all,
    parse_show_ssid,
    parse_show_station,
    parse_show_version,
    parse_ssid_security_objects,
    poe_port_count,
    resolve_ssid_name,
)

_LOGGER = logging.getLogger(__name__)

type AerohiveConfigEntry = ConfigEntry[AerohiveCoordinator]

UPLINK_MAC_THRESHOLD = 16
BACKUP_ROOT = "aerohive_local_backups"
# Changes made from Home Assistant are saved on the device this long after the
# last one, in the background: a burst of changes costs one save, and controls
# stay responsive. 0 saves before the action returns.
SAVE_DELAY_SECONDS = 2.0
# Network setup on OpenWrt: undo unless confirmed within this time.
ROLLBACK_SECONDS = 90
CONFIRM_DELAY_SECONDS = 8.0
CONFIRM_RETRY_SECONDS = 5.0
BACKUP_DOCS_URL = "https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/blob/main/docs/backups.md"


def _backup_slug(entry: ConfigEntry) -> str:
    """Folder name per device: title + MAC, filesystem-safe."""
    title = "".join(c if c.isalnum() or c in "-_" else "_" for c in (entry.title or "device"))
    uid = "".join(c for c in (entry.unique_id or entry.entry_id) if c.isalnum())
    return f"{title}_{uid}"
UNSUPPORTED_RETRY_SECONDS = 1800
POE_MAX_PORTS = 24  # per-port PoE probing stops at this port number


@dataclass
class BackupState:
    """Configuration backup / change-detection status of one device."""

    folder: str
    last_backup: datetime | None = None
    last_file: str | None = None
    count: int = 0
    changed_after_reboot: bool = False
    change_summary: dict[str, Any] | None = None
    last_checked: datetime | None = None


@dataclass
class Throughput:
    """Traffic rate of one interface, derived from byte counters (kbit/s)."""

    rx_kbps: float | None = None
    tx_kbps: float | None = None

    @property
    def total_kbps(self) -> float | None:
        if self.rx_kbps is None and self.tx_kbps is None:
            return None
        return round((self.rx_kbps or 0) + (self.tx_kbps or 0), 1)


def _lldp_model(neighbor: LldpNeighbor) -> str | None:
    from .config_flow import _short_model  # noqa: PLC0415 - avoid an import cycle

    return _short_model(neighbor)


def _prefix_to_netmask(prefix: int | None) -> str | None:
    if prefix is None or not 0 <= prefix <= 32:
        return None
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)


def _sum_rates(rates: list[Throughput]) -> Throughput:
    if not rates:
        return Throughput()
    rx = [r.rx_kbps for r in rates if r.rx_kbps is not None]
    tx = [r.tx_kbps for r in rates if r.tx_kbps is not None]
    return Throughput(
        rx_kbps=round(sum(rx), 1) if rx else None,
        tx_kbps=round(sum(tx), 1) if tx else None,
    )


@dataclass
class PanelExtras:
    """Front-panel hardware besides the data ports, where the device reports it.

    None means unknown (not reported), never "not there".
    """

    console: bool | None = None
    mgmt_link: bool | None = None  # out-of-band management port
    mgmt_ip: str | None = None
    usb: list[dict[str, Any]] | None = None


@dataclass
class AerohiveData:
    """Snapshot of everything read from the device in one poll."""

    hostname: str | None = None
    version: VersionInfo = field(default_factory=VersionInfo)
    interfaces: dict[str, Interface] = field(default_factory=dict)
    ssids: dict[str, Ssid] = field(default_factory=dict)
    stations: dict[str, Station] = field(default_factory=dict)
    details: dict[str, InterfaceDetail] = field(default_factory=dict)
    throughput: dict[str, Throughput] = field(default_factory=dict)
    boot_params: BootParams | None = None
    hardware: HardwareInfo | None = None
    cpu: SystemStats | None = None
    memory: SystemStats | None = None
    boot_time: datetime | None = None
    poe: PoeSummary | None = None
    poe_ports: dict[str, PoePort] = field(default_factory=dict)
    fdb: list[FdbEntry] = field(default_factory=list)
    lldp: dict[str, LldpNeighbor] = field(default_factory=dict)
    config: RunningConfigSummary | None = None
    cli: str = commands.DIALECT_HIVEOS
    exos: ExosSwitchInfo | None = None
    environment: EnvironmentInfo | None = None
    config_unsaved: bool | None = None
    backup: BackupState | None = None
    vlans: dict[str, ExosVlan] = field(default_factory=dict)
    # States set by Home Assistant that the device accepted but hasn't been read back yet:
    # ("port", name) / ("poe", name) / ("ssid", ssid, radio) / ("hidden", ssid) -> bool.
    overrides: dict[tuple[str, ...], bool] = field(default_factory=dict)
    save_error: str | None = None
    panel: PanelExtras = field(default_factory=PanelExtras)

    @property
    def is_switch(self) -> bool:
        """SR-series or ExtremeXOS switches (or anything with eth1/1-style ports)."""
        if self.cli == commands.DIALECT_EXOS:
            return True
        platform = (self.version.platform or "").upper()
        return platform.startswith("SR") or any("/" in n for n in self.interfaces)

    @property
    def has_switch_ports(self) -> bool:
        """Switches and routers (BR100 eth1-eth4, OpenWrt) have LAN ports worth managing."""
        return self.is_switch or self.cli == ow.DIALECT_OPENWRT or (self.version.platform or "").upper().startswith("BR")

    def ports(self) -> list[str]:
        """Physical Ethernet ports, naturally sorted (eth1/2 before eth1/10)."""

        def key(name: str) -> list[int]:
            return [int(x) for x in re.findall(r"\d+", name)]

        return sorted((n for n, i in self.interfaces.items() if i.is_port), key=key)

    def port_clients(self, port: str) -> list[FdbEntry]:
        return [e for e in self.fdb if e.port == port]

    def uplink_ports(self) -> set[str]:
        """Ports leading to other switches/routers (their MACs are not 'our' clients).

        An LLDP neighbour that is a bridge/router (and not an AP) marks an
        uplink; without LLDP, a port with many learned MACs is assumed to be one.
        """
        # Ports the reader already knows lead upstream (OpenWrt's WAN port).
        uplinks: set[str] = {n for n, i in self.interfaces.items() if i.extra.get("uplink") == "yes"}
        counts: dict[str, int] = {}
        for entry in self.fdb:
            if entry.port:
                counts[entry.port] = counts.get(entry.port, 0) + 1
        for port, count in counts.items():
            neighbor = self.lldp.get(port)
            if neighbor is not None and neighbor.is_network_device:
                uplinks.add(port)
            elif count >= UPLINK_MAC_THRESHOLD and (neighbor is None or not neighbor.is_access_point):
                uplinks.add(port)
        return uplinks

    def wired_client_macs(self) -> set[str]:
        """MACs learned on access ports (uplink ports excluded)."""
        uplinks = self.uplink_ports()
        return {e.mac for e in self.fdb if e.port not in uplinks}

    def port_enabled(self, port: str) -> bool | None:
        """Administrative state of a port, best effort."""
        if ("port", port) in self.overrides:
            return self.overrides[("port", port)]
        if self.config is not None and port in self.config.shutdown_ports:
            return False
        detail = self.details.get(port)
        iface = self.interfaces.get(port)
        admin = (detail.admin_state if detail else None) or (iface.admin_state if iface else None)
        if admin:
            return not any(w in admin.lower() for w in ("disable", "down", "shut"))
        if iface is not None and iface.is_up:
            return True
        if self.config is not None:
            return True  # not listed as shut down in the running config
        return None

    def poe_enabled(self, port: str) -> bool | None:
        if ("poe", port) in self.overrides:
            return self.overrides[("poe", port)]
        if self.config is not None and port in self.config.pse_shutdown_ports:
            return False
        poe = self.poe_ports.get(port)
        return poe.enabled if poe else None

    def ssid_hidden(self, ssid: str) -> bool | None:
        if ("hidden", ssid) in self.overrides:
            return self.overrides[("hidden", ssid)]
        return None if self.config is None else ssid in self.config.hidden_ssids

    def ssid_enabled(self, ssid: str) -> bool:
        """Bound to every exposed radio."""
        radios = self.radio_names()
        return bool(radios) and all(self.ssid_on_radio(ssid, r) for r in radios)

    @property
    def is_wireless(self) -> bool:
        """An AP / branch router with Wi-Fi, even if every radio is down right now."""
        if self.is_switch:
            return False
        return (
            any(i.is_radio for i in self.interfaces.values())
            or bool(self.ssids)
            or bool(self.stations)
            or (self.version.platform or "").upper().startswith("AP")
        )

    def _sub_ssid(self, iface: Interface) -> str | None:
        """SSID of a sub-interface, matched to its full 'show ssid' name."""
        return resolve_ssid_name(iface.ssid, self.ssids) if iface.ssid else None

    def ssid_bindings(self) -> dict[str, set[str]]:
        """radio -> SSIDs bound to it.

        Sources: the 'show interface' sub-interfaces (names matched to
        'show ssid'), plus 'interface wifiN ssid X' lines from the running
        config when it has been read (covers radios that are down and long
        names the table cut short).
        """
        bindings: dict[str, set[str]] = {}
        for iface in self.interfaces.values():
            if iface.parent and iface.ssid:
                bindings.setdefault(iface.parent, set()).add(self._sub_ssid(iface) or iface.ssid)
        if self.config is not None:
            for radio, names in self.config.ssid_bindings.items():
                if radio in self.interfaces:
                    bindings.setdefault(radio, set()).update(
                        resolve_ssid_name(n, self.ssids) or n for n in names
                    )
        return bindings

    def radio_up(self, radio: str) -> bool:
        """Radio is operational: not reported down. A non-numeric channel
        (Auto, DFS scan...) still counts as up."""
        iface = self.interfaces.get(radio)
        if iface is None or iface.is_up is False:
            return False
        return (iface.channel_raw or "").lower() != "down"

    @property
    def mac(self) -> str | None:
        mgt = self.interfaces.get("mgt0")
        return (mgt.mac if mgt else None) or self.version.mac

    @property
    def radios(self) -> dict[str, InterfaceDetail]:
        return {k: v for k, v in self.details.items() if k in self.interfaces and self.interfaces[k].is_radio}

    @property
    def mgt(self) -> InterfaceDetail | None:
        return self.details.get("mgt0")

    def radio_names(self) -> list[str]:
        """Radios worth exposing: up with a channel, or carrying an SSID.

        If none qualifies (e.g. every radio is down) all radios are returned,
        so an AP is never mistaken for a device without Wi-Fi.
        """
        bound = {r for r, names in self.ssid_bindings().items() if names}
        radios = sorted(n for n, i in self.interfaces.items() if i.is_radio)
        active = [r for r in radios if r in bound or self.interfaces[r].channel is not None]
        return active or radios

    def ssid_configurable(self, ssid: str) -> bool:
        """Is this SSID one the device's configuration defines (so it can be changed)?

        Access points can list entries that aren't configured networks (one AP
        showed "Unknown"); changing those fails. Until the configuration has been
        read, or when it defines no SSIDs we could parse, every SSID counts.
        """
        if ssid not in self.ssids:
            return False
        if self.cli == ow.DIALECT_OPENWRT or self.config is None or not self.config.ssids:
            return True
        return ssid in self.config.ssids or resolve_ssid_name(ssid, self.config.ssids) in self.config.ssids

    def ssid_on_radio(self, ssid: str, radio: str) -> bool:
        if ("ssid", ssid, radio) in self.overrides:
            return self.overrides[("ssid", ssid, radio)]
        return ssid in self.ssid_bindings().get(radio, set())

    def ssids_on_radio(self, radio: str) -> list[str]:
        return sorted(self.ssid_bindings().get(radio, set()))

    def ssid_subinterfaces(self, ssid: str) -> list[Interface]:
        return [i for i in self.interfaces.values() if i.parent and i.ssid and self._sub_ssid(i) == ssid]

    def ssid_broadcasting(self, ssid: str) -> bool:
        """Bound to at least one radio that is up, and its sub-interface (if listed) isn't down."""
        subs = {i.parent: i for i in self.ssid_subinterfaces(ssid)}
        for radio, names in self.ssid_bindings().items():
            if ssid not in names or not self.radio_up(radio):
                continue
            sub = subs.get(radio)
            if sub is not None and sub.is_up is False:
                continue
            return True
        return False

    def broadcasting_ssids(self) -> list[str]:
        return sorted(s for s in self.ssids if self.ssid_broadcasting(s))

    def station_ssid(self, station: Station) -> str | None:
        if station.ssid:
            return resolve_ssid_name(station.ssid, self.ssids)
        iface = self.interfaces.get(station.interface or "")
        return self._sub_ssid(iface) if iface else None

    def clients_on_ssid(self, ssid: str) -> int:
        return sum(1 for s in self.stations.values() if self.station_ssid(s) == ssid)

    def clients_on_radio(self, radio: str) -> int:
        return sum(1 for s in self.stations.values() if s.radio == radio)

    def wireless_throughput(self) -> Throughput:
        return _sum_rates([self.throughput[r] for r in self.radio_names() if r in self.throughput])

    def wired_throughput(self) -> Throughput:
        return _sum_rates(
            [t for n, t in self.throughput.items() if n in self.interfaces and self.interfaces[n].is_port]
        )

    def ssid_throughput(self, ssid: str) -> Throughput:
        return _sum_rates(
            [self.throughput[i.name] for i in self.ssid_subinterfaces(ssid) if i.name in self.throughput]
        )


class AerohiveCoordinator(DataUpdateCoordinator[AerohiveData]):
    """Polls one device and executes control commands."""

    config_entry: AerohiveConfigEntry

    def __init__(self, hass: HomeAssistant, entry: AerohiveConfigEntry, client: AerohiveClient) -> None:
        interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.detailed_stats: bool = entry.options.get(CONF_DETAILED_STATS, DEFAULT_DETAILED_STATS)
        self.enable_all_entities: bool = entry.options.get(CONF_ENABLE_ALL, DEFAULT_ENABLE_ALL)
        self._unsupported: set[str] = set()
        self._unsupported_at: dict[str, float] = {}
        self._rejections: dict[str, int] = {}
        self._rejected_in_poll: dict[str, int] = {}
        self._poll_count = 0
        self._slow_at: float | None = None
        self._boot_params: BootParams | None = None
        self._hardware: HardwareInfo | None = None
        self._mgt: InterfaceDetail | None = None
        self._boot_time: datetime | None = None
        # Set by a delete made from Home Assistant: remove stale entities on the next poll.
        self.expect_removal = False
        self._polled = False
        self._config: RunningConfigSummary | None = None
        self._config_dirty = True
        self._lldp: dict[str, LldpNeighbor] = {}
        self._chosen: dict[str, str] = {}
        self._port_detail_ok: bool | None = None
        self._poe_single: str | bool | None = None
        self._config_read_at: float | None = None
        self._announced: set[str] = set()
        # Backups / change detection
        self.backup_folder = Path(hass.config.path(BACKUP_ROOT, _backup_slug(entry)))
        self._backup = BackupState(folder=str(self.backup_folder))
        self._config_text: str | None = None
        self._environment: EnvironmentInfo | None = None
        self._vlans: dict[str, ExosVlan] = {}
        self._port_media: dict[str, tuple[str, str | None]] = {}
        self._mgmt_link: bool | None = None
        self._usb: list[dict[str, Any]] | None = None
        # OpenWrt
        self._ow_cpu: tuple[int, int] | None = None
        self._ow_wifi: list[ow.WifiIface] = []
        self._ow_ifnames: dict[str, str] = {}  # wifi-iface section -> ifname (phy0-ap0)
        self._serial: str | None = None
        self._config_text_new = False
        self._ha_changed = False
        # Instant feedback for controls: key -> (value, monotonic time it was applied)
        self._overrides: dict[tuple[str, ...], tuple[bool, float]] = {}
        # Background save of changes made from Home Assistant
        self._save_pending = False
        self._save_generation = 0
        self._save_lock = asyncio.Lock()
        self._save_error: str | None = None
        # name -> (monotonic time, rx_bytes, tx_bytes) from the previous poll
        self._counters: dict[str, tuple[float, int | None, int | None]] = {}

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #

    async def _optional(self, command: str) -> str | None:
        """Run a command the firmware may not support.

        A command is only treated as unsupported after it was rejected on two
        consecutive polls (one garbled reply must not disable it), and it is
        tried again every UNSUPPORTED_RETRY_SECONDS in case that was wrong.
        """
        if command in self._unsupported:
            if time.monotonic() - self._unsupported_at.get(command, 0) < UNSUPPORTED_RETRY_SECONDS:
                return None
            self._unsupported.discard(command)
        output = await self.client.run(command)
        if is_unsupported_command(output):
            # Count at most one rejection per poll (an in-poll retry is not "consecutive").
            if self._rejected_in_poll.get(command) != self._poll_count:
                self._rejected_in_poll[command] = self._poll_count
                self._rejections[command] = self._rejections.get(command, 0) + 1
            _LOGGER.debug("%s: '%s' rejected (%d): %s", self.client.host, command,
                          self._rejections[command], output[:200])  # fmt: skip
            if self._rejections[command] >= 2:
                self._unsupported.add(command)
                self._unsupported_at[command] = time.monotonic()
            return None
        self._rejections.pop(command, None)
        return output

    async def _first_supported(self, group: str, candidates: tuple[str, ...]) -> str | None:
        """Run the first command of a group the firmware accepts (remembered)."""
        if group in self._chosen:
            return await self._optional(self._chosen[group])
        for command in candidates:
            out = await self._optional(command)
            if out is not None:
                self._chosen[group] = command
                return out
        return None

    @callback
    def async_update_listeners(self) -> None:
        super().async_update_listeners()
        if self._polled:  # only a real poll ends the "remove right away" window
            self._polled = False
            self.expect_removal = False

    async def _async_update_data(self) -> AerohiveData:
        started = time.monotonic()
        try:
            data = await self._fetch()
            self._polled = True
        except AerohiveAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AerohiveError as err:
            raise UpdateFailed(f"Error communicating with {self.client.host}: {err}") from err
        # A poll that started after a change shows the device's real state; keep only
        # the changes made while this poll was already running.
        self._overrides = {k: v for k, v in self._overrides.items() if v[1] >= started}
        data.overrides = {k: v[0] for k, v in self._overrides.items()}
        data.save_error = self._save_error
        if self._save_pending:
            if data.config_unsaved:
                data.config_unsaved = False  # Home Assistant is about to save its own change
            if self._save_error is not None and not self._save_lock.locked():
                self._schedule_save(delay=0)  # retry a failed save
        return data

    def _detail_targets(self, data: AerohiveData) -> list[str]:
        """Interfaces whose 'show interface <name>' detail is read each poll."""
        targets = list(data.radio_names())
        # Ports the summary didn't list are read on their own to learn their link state.
        targets += sorted(n for n, i in data.interfaces.items() if i.is_port and i.extra.get("listed") == "no")
        if self.detailed_stats:
            for name, iface in sorted(data.interfaces.items()):
                if iface.is_port and iface.is_up and name not in targets:
                    targets.append(name)
                elif iface.parent in targets and iface.ssid and iface.is_up is not False:
                    targets.append(name)
        return targets

    def _rate(self, name: str, detail: InterfaceDetail, now: float) -> Throughput | None:
        if detail.rx_bytes is None and detail.tx_bytes is None:
            return None
        prev = self._counters.get(name)
        self._counters[name] = (now, detail.rx_bytes, detail.tx_bytes)
        if prev is None:
            return Throughput()
        elapsed = now - prev[0]
        if elapsed <= 0:
            return Throughput()

        def kbps(current: int | None, previous: int | None) -> float | None:
            if current is None or previous is None or current < previous:
                return None  # unknown or counter reset/wrap
            return round((current - previous) * 8 / 1000 / elapsed, 1)

        return Throughput(kbps(detail.rx_bytes, prev[1]), kbps(detail.tx_bytes, prev[2]))

    async def _fetch(self) -> AerohiveData:
        self._poll_count += 1
        data = AerohiveData()
        if not self.client.connected:
            # The login prompt tells which CLI this is: connect before choosing how to poll.
            await self.client.connect()
        if self.client.dialect == ow.DIALECT_OPENWRT:
            return await self._fetch_openwrt(data)
        version_out = await self.client.run("show version")
        detected = dialect_from_version(version_out)
        if detected and detected != self.client.dialect:
            # The prompt wasn't recognised; trust what the device says it is.
            await self.client.async_set_dialect(detected)
        data.cli = self.client.dialect
        if self.client.dialect == commands.DIALECT_SWITCH:
            return await self._fetch_switch(data, version_out)
        if self.client.dialect == commands.DIALECT_EXOS:
            return await self._fetch_exos(data)
        data.version = parse_show_version(version_out)
        data.interfaces = await self._read_interfaces()
        usb = [
            {"name": "USB modem", "id": n, "up": i.is_up}
            for n, i in sorted(data.interfaces.items()) if n.startswith("usbnet")
        ]
        data.panel = PanelExtras(usb=usb or None)
        for name in model_ethernet_ports(data.version.platform):
            if name not in data.interfaces:
                # Not in the summary (the BR100 lists only some ports): read it on its own below.
                data.interfaces[name] = Interface(name=name, extra={"listed": "no"})
        data.ssids = await self._read_ssids(data)
        if data.radio_names():
            station_out = await self._optional("show station")
            data.stations = parse_show_station(station_out) if station_out else {}

        for name in self._detail_targets(data):
            out = await self._optional(f"show interface {name}")
            if not out:
                continue
            detail = parse_show_interface_detail(out, name)
            data.details[name] = detail
            iface = data.interfaces.get(name)
            if iface is not None and iface.state is None and detail.operational_state:
                iface.state = "Up" if re.match(r"\s*(up|u\b|link up|connected)", detail.operational_state, re.I) else "Down"
            if iface is not None:
                iface.speed = iface.speed or detail.speed
                iface.duplex = iface.duplex or detail.duplex
            rate = self._rate(name, detail, time.monotonic())
            if rate is not None:
                data.throughput[name] = rate

        if self.detailed_stats:
            out = await self._optional("show cpu")
            data.cpu = parse_show_cpu(out) if out else None
            out = await self._optional("show memory")
            data.memory = parse_show_memory(out) if out else None

        if data.is_switch or (data.has_switch_ports and self.detailed_stats):
            if out := await self._first_supported("poe", commands.POE_STATUS_COMMANDS):
                data.poe, data.poe_ports = parse_show_poe(out)
                if not data.poe_ports and data.poe.budget_w is None:
                    data.poe = None
        if data.has_switch_ports:
            if out := await self._first_supported("fdb", commands.MAC_TABLE_COMMANDS):
                data.fdb = parse_mac_address_table(out)

        now = time.monotonic()
        if self._config_dirty:
            self._config_dirty = False
            if out := await self._optional("show running-config"):
                self._config = parse_running_config_summary(out)
                self._config_text, self._config_text_new = out, True
        if self._slow_at is None or now - self._slow_at > SLOW_REFRESH_SECONDS:
            self._slow_at = now
            self._config_dirty = True  # re-read on the next poll
            if out := await self._first_supported("lldp", commands.LLDP_COMMANDS):
                self._lldp = parse_lldp_neighbors(out)
                self._announce_neighbors()
            if out := await self._optional("show boot-param"):
                self._boot_params = parse_show_boot_param(out)
            if out := await self._optional("show hw-info"):
                self._hardware = parse_show_hw_info(out)
            if out := await self._optional("show interface mgt0"):
                self._mgt = parse_show_interface_detail(out, "mgt0")
        data.boot_params = self._boot_params
        data.hardware = self._hardware
        data.config = self._config
        data.lldp = self._lldp
        if self._mgt is not None and "mgt0" not in data.details:
            data.details["mgt0"] = self._mgt
        data.hostname = self.client.hostname

        self._update_boot_time(data)
        await self._process_backup(data)
        return data

    def _update_boot_time(self, data: AerohiveData) -> None:
        if data.version.uptime_seconds is not None:
            boot = dt_util.utcnow() - timedelta(seconds=data.version.uptime_seconds)
            # Keep the timestamp stable across polls (uptime has coarse
            # resolution); only move it on an actual reboot.
            if self._boot_time is None or abs((boot - self._boot_time).total_seconds()) > 120:
                self._boot_time = boot.replace(microsecond=0)
        data.boot_time = self._boot_time

    async def _read_interfaces(self) -> dict[str, Interface]:
        """'show interface', retried once if the reply looks incomplete.

        A reply without mgt0, or without the radios the previous poll had, is
        a glitch (cut-off output): publishing it would make the AP look like
        it has no Wi-Fi. If the retry is no better, the poll fails and the
        last good data is kept.
        """
        previous = self.data.interfaces if self.data else {}
        had_radios = any(i.is_radio for i in previous.values())
        interfaces: dict[str, Interface] = {}
        for attempt in (1, 2):
            interfaces = parse_show_interface(await self.client.run("show interface"))
            has_radios = any(i.is_radio for i in interfaces.values())
            if interfaces and "mgt0" in interfaces and (has_radios or not had_radios):
                return interfaces
            _LOGGER.debug("%s: incomplete 'show interface' reply (attempt %d)", self.client.host, attempt)
        if not interfaces:
            raise AerohiveConnectionError("'show interface' returned no interfaces")
        if had_radios:
            raise AerohiveConnectionError("'show interface' reply is missing the radios")
        return interfaces

    async def _read_ssids(self, data: AerohiveData) -> dict[str, Ssid]:
        """'show ssid', retried once if SSIDs are bound but none were parsed."""
        bound = any(i.parent and i.ssid for i in data.interfaces.values())
        ssids: dict[str, Ssid] = {}
        for _attempt in (1, 2):
            out = await self._optional("show ssid")
            ssids = parse_show_ssid(out) if out else {}
            if ssids or not bound:
                return ssids
        if self.data is not None and self.data.ssids:
            _LOGGER.debug("%s: 'show ssid' gave nothing usable, keeping the last list", self.client.host)
            return dict(self.data.ssids)
        return ssids

    async def _fetch_switch(self, data: AerohiveData, version_out: str) -> AerohiveData:
        """Poll an SR-series switch (FASTPATH-style CLI)."""
        data.version = parse_show_version(version_out)
        data.hostname = self.client.hostname
        if out := await self._optional(commands.SW_SHOW_PORTS):
            data.interfaces = parse_show_port_all(out)

        if self.detailed_stats and self._port_detail_ok is not False:
            for name, iface in data.interfaces.items():
                if not (iface.is_port and iface.is_up):
                    continue
                out = await self.client.run(commands.SW_PORT_DETAIL.format(port=name))
                if is_unsupported_command(out):
                    self._port_detail_ok = False
                    break
                self._port_detail_ok = True
                detail = parse_show_interface_detail(out, name)
                data.details[name] = detail
                rate = self._rate(name, detail, time.monotonic())
                if rate is not None:
                    data.throughput[name] = rate

        if out := await self._optional(commands.SW_SHOW_POE):
            summary, ports = parse_show_poe(out)
            data.poe = summary if (summary.budget_w is not None or summary.used_w is not None) else None
            data.poe_ports = ports
        if not data.poe_ports and (out := await self._first_supported("poe_ports", commands.SW_POE_PORT_COMMANDS)):
            data.poe_ports = parse_show_poe(out)[1]
        if not data.poe_ports and data.poe is not None and self._poe_single is not False:
            data.poe_ports = await self._poe_ports_one_by_one(data)
        if out := await self._optional(commands.SW_MAC_TABLE):
            data.fdb = parse_mac_address_table(out)

        now = time.monotonic()
        if self._config_dirty:
            self._config_dirty = False
            if out := await self._optional("show running-config"):
                self._config = parse_running_config_summary(out)
                self._config_read_at = time.monotonic()
                self._config_text, self._config_text_new = out, True
        if self._slow_at is None or now - self._slow_at > SLOW_REFRESH_SECONDS:
            self._slow_at = now
            self._config_dirty = True
            if out := await self._optional(commands.SW_SHOW_NETWORK):
                self._mgt = parse_show_network(out)
            out = await self._optional(commands.SW_SHOW_VLAN) or await self._optional(commands.SW_SHOW_VLAN_FALLBACK)
            if out:
                self._vlans = parse_fastpath_vlans(out) or self._vlans
            if out := await self._optional(commands.SW_LLDP):
                neighbors = parse_lldp_neighbors(out)
                # The summary has no management address/capabilities: read the detail.
                for port in list(neighbors):
                    detail_out = await self._optional(commands.SW_LLDP_DETAIL.format(port=port))
                    if detail_out and (detail := parse_lldp_neighbors(detail_out).get(port)):
                        neighbors[port] = detail
                self._lldp = neighbors
                self._announce_neighbors()
        if self._mgt is not None:
            data.details["mgt0"] = self._mgt
        data.config = self._config
        data.lldp = self._lldp
        data.vlans = self._vlans
        description = data.version.raw.get("system_description") or data.version.raw.get("machine_type")
        if combos := combo_port_count(description):
            front = [p for p in data.ports() if not p.startswith("0/")]
            for port in front[-combos:]:
                data.interfaces[port].extra["media"] = "combo"
        data.panel = PanelExtras(console=True if self._config is not None and self._config.console else None)
        if self._config is not None and self._config.uptime_seconds is not None and self._config_read_at:
            # Uptime from the running-config header, aged by the time since it was read.
            data.version.uptime_seconds = self._config.uptime_seconds + int(now - self._config_read_at)
        self._update_boot_time(data)
        await self._process_backup(data)
        return data

    async def _poe_ports_one_by_one(self, data: AerohiveData) -> dict[str, PoePort]:
        """Read PoE port by port when no 'all ports' form is accepted.

        SR2208P 1.0.1.28: 'show poe port info 1/0/N' (a one-row table).
        Only the PoE-capable ports are probed (count from the model, e.g.
        "8 GE POE+ ports").
        """
        result: dict[str, PoePort] = {}
        if isinstance(self._poe_single, str):
            templates = [self._poe_single]
        else:
            templates = list(commands.SW_POE_PORT_SINGLE)
        limit = poe_port_count(data.version) or POE_MAX_PORTS
        ports = [p for p in data.ports() if p.split("/")[-1].isdigit() and int(p.split("/")[-1]) <= limit]
        for port in ports:
            while templates:
                template = templates[0]
                out = await self.client.run(template.format(port=port))
                if is_unsupported_command(out):
                    if isinstance(self._poe_single, str):
                        break  # this port has no PoE; keep the working template
                    templates.pop(0)
                    continue
                entry = parse_show_poe(out)[1].get(port) or parse_poe_port_detail(out, port)
                if entry is not None:
                    # Per-port counters (Overload, Short, Power Denied...) ride along as extras.
                    entry.extra.update({k: v for k, v in parse_key_values(out).items() if "counter" in k})
                    result[port] = entry
                    self._poe_single = template
                    templates = [template]
                break
            if not templates:
                break
        if not result and not templates:
            self._poe_single = False  # no per-port PoE command on this firmware
        return result

    # ------------------------------------------------------------------ #
    # OpenWrt
    # ------------------------------------------------------------------ #

    def _config_command(self) -> str:
        if self.client.dialect == ow.DIALECT_OPENWRT:
            return ow.CONFIG_EXPORT
        return commands.SHOW_CONFIG_COMMANDS.get(self.client.dialect, "show running-config")

    @property
    def is_openwrt(self) -> bool:
        return self.client.dialect == ow.DIALECT_OPENWRT

    async def _fetch_openwrt(self, data: AerohiveData) -> AerohiveData:
        """Poll an OpenWrt router or access point (ubus JSON + UCI over SSH)."""
        run = self.client.run
        data.cli = ow.DIALECT_OPENWRT
        board = ow.parse_board(await run(ow.BOARD))
        if board.model is None and board.version is None and board.hostname is None:
            raise AerohiveConnectionError("'ubus call system board' gave no usable answer")
        info = ow.parse_system_info(await run(ow.SYSTEM_INFO))
        data.hostname = board.hostname or self.client.hostname
        data.version = VersionInfo(
            version=board.description or (f"OpenWrt {board.version}" if board.version else None),
            firmware=board.version,
            build=board.revision,
            os_name="OpenWrt",
            platform=board.model or board.board_name,
            uptime_seconds=info.uptime,
        )
        devices_out = await run(ow.NETWORK_DEVICES)
        interfaces_out = await run(ow.NETWORK_INTERFACES)
        data.version.mac = normalize_mac(ow.bridge_mac(devices_out))
        now = time.monotonic()
        for name, port in ow.parse_ports(devices_out, interfaces_out).items():
            extra = {"port": "yes"}
            if port.wan:
                extra.update(uplink="yes", role="WAN")
            data.interfaces[name] = Interface(
                name=name,
                mac=normalize_mac(port.mac),
                state="Up" if port.carrier else "Down",
                speed=str(port.speed) if port.speed else None,
                duplex=port.duplex,
                admin_state="Enable" if port.up is not False else "Disable",
                extra=extra,
            )
            detail = InterfaceDetail(
                name=name, rx_bytes=port.rx_bytes, tx_bytes=port.tx_bytes, rx_packets=port.rx_packets,
                tx_packets=port.tx_packets, rx_errors=port.rx_errors, tx_errors=port.tx_errors,
                speed=str(port.speed) if port.speed else None, duplex=port.duplex,
            )  # fmt: skip
            data.details[name] = detail
            if (rate := self._rate(name, detail, now)) is not None:
                data.throughput[name] = rate
        # Management address: the one Home Assistant connects to, else the LAN's.
        ip_ifaces = [i for i in ow.parse_interfaces(interfaces_out) if i.name != "loopback"]
        chosen = next(((i, a) for i in ip_ifaces for a in i.addresses if a[0] == self.client.host), None)
        chosen = chosen or next(((i, i.addresses[0]) for i in ip_ifaces if i.name == "lan" and i.addresses), None)
        if chosen:
            data.details["mgt0"] = InterfaceDetail(
                name=chosen[0].name, ip_address=chosen[1][0], netmask=_prefix_to_netmask(chosen[1][1])
            )
        stat = ow.parse_cpu_stat(await run(ow.CPU_STAT))
        data.cpu = SystemStats(cpu_total=ow.cpu_percent(self._ow_cpu, stat))
        self._ow_cpu = stat or self._ow_cpu
        if info.memory_total:
            data.memory = SystemStats(
                memory_total_kb=info.memory_total // 1024,
                memory_free_kb=(info.memory_available or 0) // 1024,
            )
        await self._fetch_openwrt_wifi(data)
        # Wired clients: neighbours on the LAN side that aren't Wi-Fi clients.
        wan_devices = {i.device for i in ip_ifaces if i.name.startswith("wan")}
        for _ip, mac, device in ow.parse_arp(await run(ow.ARP)):
            if device not in wan_devices and mac not in data.stations:
                data.fdb.append(FdbEntry(mac=mac, port=None))
        # Staged but uncommitted UCI changes (e.g. LuCI without "Save & Apply"): like ExtremeXOS's "*".
        changes = await run(ow.UCI_CHANGES)
        data.config_unsaved = bool(changes.strip()) and not ow.load_json(changes)

        now = time.monotonic()
        if self._config_dirty:
            self._config_dirty = False
            out = await run(ow.CONFIG_EXPORT, timeout=60)
            if "package " in out:
                self._config_text, self._config_text_new = out, True
        if self._slow_at is None or now - self._slow_at > SLOW_REFRESH_SECONDS:
            self._slow_at = now
            self._config_dirty = True
            self._environment = EnvironmentInfo(temperature_c=ow.parse_temperature(await run(ow.TEMPERATURE)))
            self._usb = ow.parse_usb_devices(await run(ow.USB_DEVICES))
        data.panel = PanelExtras(usb=self._usb or None)
        for name, iface in data.interfaces.items():
            if iface.is_port and name.startswith("sfp"):
                iface.extra["media"] = "sfp"
        data.environment = self._environment
        self._update_boot_time(data)
        await self._process_backup(data)
        return data

    async def _fetch_openwrt_wifi(self, data: AerohiveData) -> None:
        run = self.client.run
        radios, wifaces = ow.parse_wireless_config(await run(ow.UCI_WIRELESS))
        status = ow.parse_wireless_status(await run(ow.WIRELESS_STATUS))
        self._ow_wifi = wifaces
        self._ow_ifnames = {sec: ifn for st in status.values() for sec, ifn in st.ifnames.items()}
        if not radios:
            return
        leases = ow.parse_dhcp_leases(await run(ow.DHCP_LEASES))
        for name, radio in sorted(radios.items()):
            st = status.get(name)
            channel = int(radio.channel) if radio.channel and radio.channel.isdigit() else None
            width = re.search(r"(\d+)$", radio.htmode or "")
            data.interfaces[name] = Interface(
                name=name,
                mode="access",
                state="Up" if st and st.up and not radio.disabled else "Down",
                channel=channel,
                channel_raw=radio.channel,
                channel_width=width.group(1) if width else None,
                radio_profile={"2g": "ng", "5g": "na", "6g": "ax6"}.get(radio.band or ""),
            )
        config = RunningConfigSummary()
        for wif in wifaces:
            if wif.mode != "ap" or not wif.ssid or wif.device not in radios:
                continue
            data.ssids.setdefault(wif.ssid, Ssid(name=wif.ssid, max_client=wif.maxassoc))
            config.ssid_security_objects[wif.ssid] = wif.ssid
            config.security_suites[wif.ssid] = wif.encryption or "none"
            if wif.hidden:
                config.hidden_ssids.add(wif.ssid)
            if wif.disabled:
                continue
            config.ssid_bindings.setdefault(wif.device, set()).add(wif.ssid)
            ifname = self._ow_ifnames.get(wif.section)
            if not ifname:
                continue  # configured but not running (radio off)
            sub = Interface(name=f"{wif.device}.{ifname}", ssid=wif.ssid, state="Up", mode="access")
            data.interfaces[sub.name] = sub
            iw = ow.parse_iwinfo_info(await run(ow.iwinfo_info(ifname)))
            radio_iface = data.interfaces[wif.device]
            if iw.channel:
                radio_iface.channel = iw.channel
            if iw.width:
                radio_iface.channel_width = str(iw.width)
            if wif.device not in data.details:
                data.details[wif.device] = InterfaceDetail(
                    name=wif.device, channel=iw.channel, tx_power=iw.txpower, noise_floor=iw.noise,
                    channel_width=str(iw.width) if iw.width else None,
                )  # fmt: skip
            for client in ow.parse_assoclist(await run(ow.iwinfo_assoclist(ifname))):
                ip, hostname = leases.get(client.mac, (None, None))
                seconds = client.connected_seconds
                data.stations[client.mac] = Station(
                    mac=client.mac, ip=ip, hostname=hostname, interface=sub.name, ssid=wif.ssid,
                    channel=iw.channel, signal=client.signal, tx_rate=ow.rate_text(client.tx_rate_kbps),
                    rx_rate=ow.rate_text(client.rx_rate_kbps),
                    assoc_time=f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds else None,
                    extra={"ifname": ifname},
                )  # fmt: skip
        data.config = config

    def _require_configurable(self, ssid: str) -> None:
        data = self.data
        if data is None or ssid not in data.ssids:
            raise HomeAssistantError(f"SSID {ssid} does not exist on {self._device_name()}")
        if not data.ssid_configurable(ssid):
            raise HomeAssistantError(
                f"{ssid} is listed by {self._device_name()} but isn't in its configuration, so it can't be "
                "changed (it's not a network you set up; access points show such entries as e.g. 'Unknown')."
            )

    def wifi_home_radios(self, ssid: str) -> list[str] | None:
        """Radios a network is configured for even while it's off (OpenWrt keeps disabled sections).

        Turning it back on uses these, so a 2.4 GHz-only network doesn't spread to 5 GHz.
        None where the device keeps no such record (HiveOS removes the binding).
        """
        if not self.is_openwrt or self.data is None:
            return None
        radios = self.data.radio_names()
        return sorted({w.device for w in self._ow_sections(ssid) if w.device in radios}) or None

    def _ow_sections(self, ssid: str) -> list[ow.WifiIface]:
        return [w for w in self._ow_wifi if w.mode == "ap" and w.ssid == ssid]

    async def _ow_update_ssid(
        self,
        ssid: str,
        *,
        password: str | None = None,
        radios: list[str] | None = None,
        hidden: bool | None = None,
        max_clients: int | None = None,
    ) -> bool:
        sections = self._ow_sections(ssid)
        if not sections:
            raise HomeAssistantError(f"SSID {ssid} does not exist")
        cmds: list[str] = []
        state: dict[tuple[str, ...], bool] = {}
        if radios is not None:
            for radio in self.data.radio_names():
                want = radio in radios
                here = [w for w in sections if w.device == radio]
                if want and not here:
                    cmds.append(self._validated(ow.copy_iface, sections[0].section, radio))
                for wif in here:
                    if wif.disabled == want:  # disabled but wanted, or enabled but not wanted
                        cmds.append(self._validated(ow.set_disabled, wif.section, not want))
                if want != self.data.ssid_on_radio(ssid, radio):
                    state[("ssid", ssid, radio)] = want
        if hidden is not None and hidden != any(w.hidden for w in sections):
            cmds += [self._validated(ow.uci_set, w.section, "hidden", "1" if hidden else "0") for w in sections]
            state[("hidden", ssid)] = hidden
        if max_clients is not None and max_clients != sections[0].maxassoc:
            cmds += [self._validated(ow.uci_set, w.section, "maxassoc", str(int(max_clients))) for w in sections]
        if password:
            for wif in sections:
                cmds += self._validated(ow.set_key, wif.section, password, wif.encryption)
        if not cmds:
            return False
        _LOGGER.info("%s: updating Wi-Fi network %s (%d change(s))", self.client.host, ssid, len(cmds))
        await self._apply([ow.apply_line(cmds)], secret=password or None, overrides=state)
        return True

    async def _ow_create_ssid(self, ssid: str, password: str | None, radios: list[str] | None, hidden: bool) -> None:
        if ssid in self.data.ssids:
            raise HomeAssistantError(f"SSID {ssid} already exists")
        targets = radios if radios is not None else self.data.radio_names()
        if not targets:
            raise HomeAssistantError("No radio to broadcast the network on")
        network = next((w.network for w in self._ow_wifi if w.mode == "ap" and w.network), None) or "lan"
        cmds = [self._validated(ow.new_iface, radio, ssid, password, hidden, network.split()[0]) for radio in targets]
        _LOGGER.info("%s: creating Wi-Fi network %s on %s", self.client.host, ssid, ", ".join(targets))
        await self._apply([ow.apply_line(cmds)], secret=password or None)
        await self.async_request_refresh()

    async def _ow_delete_ssid(self, ssid: str) -> None:
        sections = self._ow_sections(ssid)
        if not sections:
            raise HomeAssistantError(f"SSID {ssid} does not exist")
        _LOGGER.info("%s: deleting Wi-Fi network %s", self.client.host, ssid)
        self.expect_removal = True
        try:
            await self._apply([ow.apply_line([self._validated(ow.delete_iface, w.section) for w in sections])])
        except HomeAssistantError:
            self.expect_removal = False
            raise
        await self.async_request_refresh()

    @staticmethod
    def _not_on_openwrt(what: str) -> HomeAssistantError:
        return HomeAssistantError(f"{what} isn't supported on OpenWrt devices")

    async def async_apply_openwrt_plan(
        self, lines: list[str], secrets: list[str], configs: tuple[str, ...] = netplan_mod.OPENWRT_CONFIGS
    ) -> None:
        """Apply network-plan UCI commands with an automatic rollback.

        Like LuCI's "apply with rollback": the four configuration files are
        copied first and a timer on the router puts them back after
        ROLLBACK_SECONDS unless Home Assistant can still reach the router and
        confirms. Changes are staged with uci and only committed when every
        command was accepted, so a rejected command changes nothing.
        """
        host = self.client.host
        mark = ow.FAILED_MARK

        def scrub(text: str) -> str:
            text = commands.redact(text)
            for value in secrets:
                if value:
                    text = text.replace(value, "***")
            return text

        async def run(cmd: str, timeout: float | None = None) -> str:
            try:
                return await self.client.run(cmd, timeout=timeout, hide=secrets)
            except AerohiveError as err:
                raise HomeAssistantError(f"Error communicating with {host}: {scrub(str(err))}") from err

        staged = await run(netplan_mod.openwrt_staged(configs))
        if staged.strip():
            raise HomeAssistantError(
                f"{host} has changes that aren't saved yet (made in LuCI or over SSH). Save & Apply or revert them first."
            )
        out = await run(f"( {netplan_mod.openwrt_prepare(int(ROLLBACK_SECONDS), configs)} ) || echo '{mark}'")
        if mark in out:
            raise HomeAssistantError(f"{host}: could not back up the current settings; nothing was changed")
        _LOGGER.info("%s: applying network setup (%d command(s))", host, len(lines))
        for chunk in netplan_mod.chunks(lines):
            out = await run(f"{chunk} || echo '{mark}'")
            if mark in out or re.search(r"^uci: ", out, re.MULTILINE):
                await run(netplan_mod.openwrt_revert(configs))
                detail = scrub(out.replace(mark, "").strip()) or "a command was rejected"
                shown = scrub(chunk)
                shown = shown if len(shown) <= 300 else shown[:300] + "…"
                raise HomeAssistantError(f"{host} rejected the change ({detail}) in: {shown}. Nothing was changed.")
        out = await run(f"{netplan_mod.openwrt_commit(configs)} || echo '{mark}'")
        self._config_dirty = True
        self._ha_changed = True
        if mark in out:
            await run(netplan_mod.OPENWRT_RESTORE_NOW)
            raise HomeAssistantError(f"{host}: saving the settings failed; the previous settings were put back")
        await run(netplan_mod.openwrt_reload(configs))
        await asyncio.sleep(CONFIRM_DELAY_SECONDS)
        deadline = time.monotonic() + max(ROLLBACK_SECONDS - CONFIRM_DELAY_SECONDS - 15, 0)
        while True:
            try:
                out = await self.client.run(netplan_mod.OPENWRT_CONFIRM, timeout=10)
                if "HN_CONFIRMED" in out:
                    break
            except AerohiveError:
                pass
            if time.monotonic() >= deadline:
                raise HomeAssistantError(
                    f"Lost contact with {host} after applying. It goes back to the previous settings "
                    f"within {int(ROLLBACK_SECONDS)} seconds of the change."
                )
            await asyncio.sleep(CONFIRM_RETRY_SECONDS)
        _LOGGER.info("%s: network setup applied and confirmed", host)
        await self.async_request_refresh()

    async def async_run_router_lines(self, lines: list[str], secrets: list[str] | None = None) -> str:
        """Run shell lines on an OpenWrt device (joined into a few SSH lines); raise if one fails.

        Returns the combined output (used to read the onboarding portal's requests).
        """
        hide = [s for s in secrets or [] if s]

        def scrub(text: str) -> str:
            text = commands.redact(text)
            for value in hide:
                text = text.replace(value, "***")
            return text

        outputs = []
        for chunk in netplan_mod.chunks(lines):
            try:
                out = await self.client.run(f"{chunk} || echo '{ow.FAILED_MARK}'", hide=hide)
            except AerohiveError as err:
                raise HomeAssistantError(f"Error communicating with {self.client.host}: {scrub(str(err))}") from err
            if ow.FAILED_MARK in out:
                detail = scrub(out.replace(ow.FAILED_MARK, "").strip()) or "a command failed"
                raise HomeAssistantError(f"{self.client.host}: {detail}")
            outputs.append(out)
        return "\n".join(outputs)

    async def async_apply_cli_plan(self, cmds: list[str], secrets: list[str]) -> None:
        """Apply network-plan CLI commands (HiveOS, ExtremeXOS, SR switches); saved like any other change."""
        if not cmds:
            return
        _LOGGER.info("%s: applying network setup (%d command(s))", self.client.host, len(cmds))
        # SR switches: one mode-dependent sequence (vlan database / configure / interface).
        await self._apply(cmds, secret=secrets, sequence=self.is_switch_cli)
        await self.async_request_refresh()


    async def _fetch_exos(self, data: AerohiveData) -> AerohiveData:
        """Poll an ExtremeXOS switch (Summit X450e and others)."""
        data.hostname = self.client.hostname
        if out := await self._optional(commands.EXOS_SHOW_SWITCH):
            info = parse_exos_show_switch(out)
            data.exos = info
            data.version = VersionInfo(
                version=info.booted_version and f"ExtremeXOS {info.booted_version}",
                firmware=info.booted_version,
                os_name="ExtremeXOS",
                platform=info.system_type,
                uptime_seconds=info.uptime_seconds,
                mac=info.mac,
            )
        if out := await self._optional(commands.EXOS_SHOW_PORTS):
            data.interfaces = parse_exos_ports(out)
        if self.detailed_stats and (out := await self._optional(commands.EXOS_SHOW_PORT_STATS)):
            now = time.monotonic()
            for name, detail in parse_exos_port_statistics(out).items():
                iface = data.interfaces.get(name)
                if iface is not None:
                    detail.speed = iface.speed
                    detail.duplex = iface.duplex
                data.details[name] = detail
                rate = self._rate(name, detail, now)
                if rate is not None:
                    data.throughput[name] = rate
        if out := await self._optional(commands.EXOS_SHOW_POE):
            summary = parse_exos_inline_power(out)
            data.poe = summary if summary.budget_w is not None or summary.used_w is not None else None
            if out := await self._optional(commands.EXOS_SHOW_POE_INFO):
                data.poe_ports = parse_exos_inline_power_info(out)
        if out := await self._optional(commands.EXOS_SHOW_FDB):
            data.fdb = parse_exos_fdb(out)
        if self.detailed_stats:
            if out := await self._optional(commands.EXOS_SHOW_CPU):
                data.cpu = parse_exos_cpu(out)
            if out := await self._optional(commands.EXOS_SHOW_MEMORY):
                data.memory = parse_show_memory(out)

        now = time.monotonic()
        if self._config_dirty:
            self._config_dirty = False
            if out := await self._optional(commands.EXOS_SHOW_CONFIG):
                self._config_text, self._config_text_new = out, True
        if self._slow_at is None or now - self._slow_at > SLOW_REFRESH_SECONDS:
            self._slow_at = now
            self._config_dirty = True
            if out := await self._optional(commands.EXOS_SHOW_LLDP):
                neighbors = parse_exos_lldp_neighbors(out)
                # One command for every port's detail (management address, capabilities).
                detail_out = await self._optional(commands.EXOS_SHOW_LLDP_DETAIL) if neighbors else None
                for port, detail in parse_exos_lldp_detail_all(detail_out or "").items():
                    summary = neighbors.get(port)
                    if summary is not None:
                        detail.system_name = detail.system_name or summary.system_name
                        detail.chassis_id = detail.chassis_id or summary.chassis_id
                        detail.port_id = detail.port_id or summary.port_id
                    neighbors[port] = detail
                self._lldp = neighbors
                self._announce_neighbors()
            if out := await self._optional(commands.EXOS_SHOW_VERSION):
                self._serial = parse_exos_version(out)[0] or self._serial
            if out := await self._optional(commands.EXOS_SHOW_VLAN):
                self._vlans = parse_exos_vlans(out)
            if out := await self._optional(commands.EXOS_SHOW_PORT_CONFIG):
                self._port_media = parse_exos_port_media(out) or self._port_media
            if out := await self._optional(commands.EXOS_SHOW_MGMT_PORT):
                self._mgmt_link = parse_exos_mgmt_port(out)
            env = EnvironmentInfo()
            if out := await self._optional(commands.EXOS_SHOW_TEMPERATURE):
                env.temperature_c, env.temperature_status = parse_exos_temperature(out)
            if out := await self._optional(commands.EXOS_SHOW_FANS):
                env.fans_state, env.fans = parse_exos_fans(out)
                env.fan_rpm = fan_rpms(env.fans)
            if out := await self._optional(commands.EXOS_SHOW_POWER):
                env.power_supplies, env.power_usage_w = parse_exos_power(out)
            self._environment = env
        data.environment = self._environment
        data.lldp = self._lldp
        data.vlans = self._vlans
        for port, (primary, redundant) in self._port_media.items():
            if (iface := data.interfaces.get(port)) is not None:
                media, module = media_kind(primary, redundant)
                if media:
                    iface.extra["media"] = media
                if module:
                    iface.extra["module"] = module
        mgmt_vlan = next((v for v in self._vlans.values() if v.name.lower() == "mgmt"), None)
        data.panel = PanelExtras(
            console=True,  # every ExtremeXOS switch has a serial console port
            mgmt_link=self._mgmt_link if self._mgmt_link is not None
            else (bool(mgmt_vlan.active_ports) if mgmt_vlan and mgmt_vlan.active_ports is not None else None),
            mgmt_ip=mgmt_vlan.ip_address if mgmt_vlan else None,
        )
        data.version.serial = self._serial
        if mgmt := self._management_vlan(self._vlans):
            data.details["mgt0"] = InterfaceDetail(
                name=mgmt.name, ip_address=mgmt.ip_address, netmask=_prefix_to_netmask(mgmt.prefix)
            )
        data.config_unsaved = self.client.config_unsaved
        self._update_boot_time(data)
        self._update_boot_problem_issue(data)
        await self._process_backup(data)
        return data

    def _management_vlan(self, vlans: dict[str, ExosVlan]) -> ExosVlan | None:
        """The VLAN Home Assistant reaches the switch on (else Mgmt, else the first with an IP)."""
        with_ip = [v for v in vlans.values() if v.ip_address]
        host = self.client.host
        return (
            next((v for v in with_ip if v.ip_address == host), None)
            or next((v for v in with_ip if v.name.lower() == "mgmt"), None)
            or (with_ip[0] if with_ip else None)
        )

    # ------------------------------------------------------------------ #
    # Configuration backups and change detection
    # ------------------------------------------------------------------ #

    async def _process_backup(self, data: AerohiveData) -> None:
        """Back up the configuration when it changed, or flag a change after a reboot.

        - First time: back it up.
        - Same as the latest backup: nothing to do (clears a previous flag).
        - Changed while a "changed after reboot" flag is active: keep the flag,
          no backup (the known-good backup stays the latest until the user
          restores it or accepts the current configuration).
        - Changed by Home Assistant, or by someone else while the device kept
          running: back it up.
        - Changed and the device rebooted since the latest backup: that's the
          power-loss symptom (unsaved changes lost, wrong configuration
          booted). Flag it and raise a repair issue; no backup is taken.

        A changed configuration is read a second time before acting on it, so
        a cut-off reply is never saved as a backup or raised as a problem.
        """
        state = self._backup
        if self._config_text_new and self._config_text:
            self._config_text_new = False
            text = self._config_text
            try:
                latest = await self.hass.async_add_executor_job(backup_mod.latest_backup, self.backup_folder)
                latest_text = (
                    await self.hass.async_add_executor_job(backup_mod.read_backup, latest.path) if latest else None
                )
                state.last_checked = dt_util.utcnow()
                if latest is None:
                    if await self._confirm_config(text):
                        await self._write_backup(text)
                elif backup_mod.config_fingerprint(latest_text or "") == backup_mod.config_fingerprint(text):
                    if state.changed_after_reboot:
                        self._clear_change_flag()
                    state.last_backup, state.last_file = latest.created, latest.name
                elif not await self._confirm_config(text):
                    pass  # inconsistent reads: decide on the next check
                elif state.changed_after_reboot:
                    self._flag_change(latest, latest_text or "", text)
                elif self._ha_changed or not (data.boot_time and data.boot_time > latest.created):
                    await self._write_backup(text)
                else:
                    self._flag_change(latest, latest_text or "", text)
            except OSError as err:
                _LOGGER.warning("%s: configuration backup failed: %s", self.client.host, err)
            self._ha_changed = False
        if state.count == 0:
            backups = await self.hass.async_add_executor_job(backup_mod.list_backups, self.backup_folder)
            state.count = len(backups)
            if backups and state.last_backup is None:
                state.last_backup, state.last_file = backups[0].created, backups[0].name
        data.backup = BackupState(**vars(state))

    async def _confirm_config(self, text: str) -> bool:
        """Read the configuration again; True if it matches (not a cut-off reply)."""
        command = self._config_command()
        try:
            again = await self.client.run(command, timeout=180)
        except AerohiveError:
            self._config_dirty = True
            return False
        if backup_mod.config_fingerprint(again) != backup_mod.config_fingerprint(text):
            _LOGGER.debug("%s: two configuration reads differ, checking again next time", self.client.host)
            self._config_dirty = True
            return False
        return True

    def _flag_change(self, latest: backup_mod.BackupFile, latest_text: str, text: str) -> None:
        state = self._backup
        newly_flagged = not state.changed_after_reboot
        state.changed_after_reboot = True
        state.change_summary = backup_mod.diff_summary(latest_text, text)
        state.last_backup, state.last_file = latest.created, latest.name
        self._raise_change_issue(latest, state.change_summary, log=newly_flagged)

    async def _write_backup(self, text: str, *, force: bool = False) -> backup_mod.BackupFile:
        backup, written = await self.hass.async_add_executor_job(
            partial(backup_mod.write_backup, self.backup_folder, text, dt_util.utcnow(), force=force)
        )
        backups = await self.hass.async_add_executor_job(backup_mod.list_backups, self.backup_folder)
        self._backup.count = len(backups)
        self._backup.last_backup, self._backup.last_file = backup.created, backup.name
        if written:
            _LOGGER.info("%s: configuration backed up to %s", self.client.host, backup.path)
        return backup

    def _issue_id(self, kind: str) -> str:
        return f"{kind}_{self.config_entry.entry_id}"

    def _device_name(self) -> str:
        return (self.data.hostname if self.data else None) or self.config_entry.title

    def _raise_change_issue(self, latest: backup_mod.BackupFile, summary: dict[str, Any], *, log: bool = True) -> None:
        if log:
            _LOGGER.warning(
                "%s: configuration differs from the backup of %s after a reboot (%s line(s) added, %s removed)",
                self.client.host, latest.created.isoformat(), summary["added"], summary["removed"],
            )  # fmt: skip
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id("config_changed_after_reboot"),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="config_changed_after_reboot",
            translation_placeholders={
                "device": self._device_name(),
                "backup": dt_util.as_local(latest.created).strftime("%Y-%m-%d %H:%M"),
                "added": str(summary["added"]),
                "removed": str(summary["removed"]),
                "changes": "\n".join(str(line) for line in summary["sample"]) or "-",
            },
            learn_more_url=BACKUP_DOCS_URL,
        )

    def _clear_change_flag(self) -> None:
        self._backup.changed_after_reboot = False
        self._backup.change_summary = None
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id("config_changed_after_reboot"))

    def _update_boot_problem_issue(self, data: AerohiveData) -> None:
        problems = data.exos.boot_problems if data.exos else []
        issue_id = self._issue_id("boot_config_problem")
        if problems:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="boot_config_problem",
                translation_placeholders={"device": self._device_name(), "problems": "; ".join(problems)},
                learn_more_url=BACKUP_DOCS_URL,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _read_config_text(self) -> str:
        command = self._config_command()
        try:
            text = await self.client.run(command, timeout=180)
        except AerohiveError as err:
            raise HomeAssistantError(f"Cannot read the configuration of {self.client.host}: {err}") from err
        if is_unsupported_command(text) or not backup_mod.normalize_config(text):
            raise HomeAssistantError(f"{self.client.host} returned no configuration for '{command}'")
        return text

    async def async_backup_config(self) -> dict[str, Any]:
        """Back up now (no new file if nothing changed since the latest backup)."""
        if self._backup.changed_after_reboot:
            raise HomeAssistantError(
                f"The configuration of {self._device_name()} changed after a reboot. Restore the latest "
                "backup (restore_config action) or press 'Accept current configuration' first, so the "
                "known-good backup isn't replaced by accident."
            )
        text = await self._read_config_text()
        try:
            backup = await self._write_backup(text)
        except OSError as err:
            raise HomeAssistantError(f"Could not write the backup: {err}") from err
        self.async_update_listeners()
        return {"file": str(backup.path), "created": backup.created.isoformat(), "size": backup.size}

    async def async_accept_config(self) -> dict[str, Any]:
        """The current configuration is correct: save it as the new known-good backup."""
        text = await self._read_config_text()
        try:
            backup = await self._write_backup(text, force=True)
        except OSError as err:
            raise HomeAssistantError(f"Could not write the backup: {err}") from err
        self._clear_change_flag()
        await self.async_request_refresh()
        return {"file": str(backup.path), "created": backup.created.isoformat()}

    async def async_save_config(self) -> None:
        self._save_generation += 1  # a scheduled save is covered by this one
        try:
            async with self._save_lock:
                await self._save()
                self._save_pending = False
                self._save_error = None
        except AerohiveError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
        self._ha_changed = True
        self._config_dirty = True
        await self.async_request_refresh()

    async def async_refresh_now(self) -> None:
        """Read everything now, including what's normally read every 15 minutes."""
        self._slow_at = None
        self._config_dirty = True
        await self.async_refresh()
        if not self.last_update_success:
            raise HomeAssistantError(
                f"Could not read {self._device_name()} ({self.client.host}): {self.last_exception}"
            )

    async def async_list_backups(self) -> list[dict[str, Any]]:
        backups = await self.hass.async_add_executor_job(backup_mod.list_backups, self.backup_folder)
        return [{"file": b.name, "created": b.created.isoformat(), "size": b.size} for b in backups]

    async def async_restore_config(self, file: str | None = None, save: bool = False) -> dict[str, Any]:
        """Send the lines of a backup back to the device (latest backup if no file is given).

        Lines the device rejects (e.g. "already exists") are collected and
        reported; the rest are still applied. The result is only saved on the
        device when save=True.
        """
        if self.is_openwrt:
            raise HomeAssistantError(
                "Restoring isn't supported on OpenWrt: use System → Backup / Flash firmware in LuCI, or "
                "'sysupgrade -r'. Home Assistant's OpenWrt backups are a record of the configuration "
                "with Wi-Fi keys and passwords masked."
            )
        backups = await self.hass.async_add_executor_job(backup_mod.list_backups, self.backup_folder)
        if not backups:
            raise HomeAssistantError(f"No configuration backups for {self._device_name()} yet")
        chosen = backups[0] if not file else next((b for b in backups if b.name == file), None)
        if chosen is None:
            raise HomeAssistantError(f"Backup {file} not found. Available: {', '.join(b.name for b in backups[:5])}")
        text = await self.hass.async_add_executor_job(backup_mod.read_backup, chosen.path)
        lines = backup_mod.restore_lines(text, self.client.dialect)
        errors: list[str] = []
        _LOGGER.warning("%s: restoring %d configuration line(s) from %s", self.client.host, len(lines), chosen.name)
        try:
            for line in lines:
                if self.client.dialect == commands.DIALECT_SWITCH:
                    out = await self.client.run(line, keep_mode=True, retry=False, confirm=True)
                else:
                    out = await self.client.run(line, retry=False, confirm=True, timeout=60)
                if error := find_cli_error(out):
                    errors.append(f"{commands.redact(line)} -> {error}")
            if self.client.dialect == commands.DIALECT_SWITCH:
                await self.client.run_sequence([])  # back to privileged mode
            if save:
                await self._save()
        except AerohiveError as err:
            raise HomeAssistantError(f"Restore interrupted: {err}") from err
        self._ha_changed = save
        self._config_dirty = True
        await self.async_request_refresh()
        return {
            "backup": chosen.name,
            "lines_sent": len(lines),
            "errors": len(errors),
            "error_lines": errors[:25],
            "saved": save,
        }

    def _announce_neighbors(self) -> None:
        """Offer unconfigured Aerohive/Extreme neighbours seen via LLDP for setup."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        configured = {e.data.get(CONF_HOST) for e in entries} | {e.unique_id for e in entries}
        # Also by name: a device already added under another IP address isn't offered twice.
        names = {e.title.lower() for e in entries if e.title}
        names |= {
            e.runtime_data.data.hostname.lower()
            for e in entries
            if isinstance(getattr(e, "runtime_data", None), AerohiveCoordinator)
            and e.runtime_data.data is not None
            and e.runtime_data.data.hostname
        }
        for neighbor in self._lldp.values():
            host = neighbor.management_address
            mac = normalize_mac(neighbor.chassis_id)
            looks_aerohive = is_aerohive_hostname(neighbor.system_name) or is_aerohive_mac(mac)
            if not host or not looks_aerohive or host in configured or mac in configured:
                continue
            if neighbor.system_name and neighbor.system_name.lower() in names:
                continue
            if host in self._announced:
                continue
            self._announced.add(host)
            _LOGGER.debug("%s: offering LLDP neighbour %s (%s) for setup", self.client.host, neighbor.system_name, host)
            discovery_flow.async_create_flow(
                self.hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_HOST: host,
                    "mac": mac,
                    "name": neighbor.system_name or host,
                    "model": _lldp_model(neighbor),
                    "seen_on": f"{self._device_name()} port {neighbor.local_port}",
                },
            )

    # ------------------------------------------------------------------ #
    # Device info
    # ------------------------------------------------------------------ #

    @property
    def device_info(self) -> DeviceInfo:
        data = self.data or AerohiveData()
        unique = self.config_entry.unique_id or self.client.host
        hardware = data.hardware or HardwareInfo()
        hw_parts = [p for p in (
            hardware.hw_revision and f"Rev {hardware.hw_revision}",
            data.version.bootloader and f"Bootloader {data.version.bootloader}",
        ) if p]  # fmt: skip
        info = DeviceInfo(
            identifiers={(DOMAIN, unique)},
            manufacturer=MANUFACTURER,
            model=data.version.platform or hardware.product,
            name=data.hostname or self.config_entry.title,
            sw_version=data.version.version,
            hw_version=", ".join(hw_parts) or None,
            serial_number=hardware.serial or data.version.serial,
        )
        if data.mgt and data.mgt.ip_address:
            info["configuration_url"] = f"https://{data.mgt.ip_address}"
        if data.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, data.mac)}
        return info

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #

    async def _apply(
        self,
        cmds: list[str],
        *,
        secret: str | list[str] | None = None,
        sequence: bool = False,
        overrides: dict[tuple[str, ...], bool] | None = None,
    ) -> None:
        """Run config commands and check the replies, then show the new state at once.

        The configuration is saved on the device shortly afterwards in the
        background (SAVE_DELAY_SECONDS), followed by a refresh.
        """

        secrets = [secret] if isinstance(secret, str) else list(secret or [])

        def scrub(text: str) -> str:
            text = commands.redact(text)
            for value in secrets:
                if value:
                    text = text.replace(value, "***")
            return text

        try:
            if sequence:
                # Mode-dependent block (switch config mode); errors raise.
                await self.client.run_sequence(cmds)
                cmds = []
            for cmd in cmds:
                output = await self.client.run(cmd)
                error = find_cli_error(output)
                if error:
                    raise HomeAssistantError(
                        f"{self.client.host} rejected '{scrub(cmd)}': {scrub(error)}"
                    )
        except AerohiveCommandError as err:
            self._config_dirty = True
            raise HomeAssistantError(scrub(str(err))) from err
        except AerohiveError as err:
            self._config_dirty = True
            raise HomeAssistantError(
                f"Error communicating with {self.client.host}: {scrub(str(err))}"
            ) from err
        except HomeAssistantError:
            self._config_dirty = True
            raise
        self._config_dirty = True
        self._ha_changed = True
        if overrides:
            self._set_overrides(overrides)
        if SAVE_DELAY_SECONDS <= 0:
            self._save_pending = True
            await self._run_pending_save(raise_errors=True)
            return
        self._schedule_save()

    def _set_overrides(self, overrides: dict[tuple[str, ...], bool]) -> None:
        """Show a state the device accepted right away, without waiting for a poll."""
        now = time.monotonic()
        for key, value in overrides.items():
            self._overrides[key] = (value, now)
            if self.data is not None:
                self.data.overrides[key] = value
        if self.data is not None:
            self.async_update_listeners()

    def _schedule_save(self, delay: float | None = None) -> None:
        self._save_pending = True
        self._save_generation += 1
        generation = self._save_generation
        wait = SAVE_DELAY_SECONDS if delay is None else delay

        async def later() -> None:
            await asyncio.sleep(wait)
            if generation == self._save_generation:
                await self._run_pending_save()

        self.config_entry.async_create_background_task(self.hass, later(), f"{DOMAIN} save {self.client.host}")

    async def _run_pending_save(self, *, raise_errors: bool = False) -> None:
        async with self._save_lock:
            if not self._save_pending:
                return
            self._save_pending = False
            try:
                await self._save()
            except (AerohiveError, HomeAssistantError) as err:
                self._save_pending = True
                self._save_error = str(err)
                _LOGGER.warning("%s: saving the configuration failed (retried on the next poll): %s", self.client.host, err)
                if raise_errors:
                    raise HomeAssistantError(f"{self.client.host}: the change was made but saving failed: {err}") from err
                return
            self._save_error = None
        await self.async_request_refresh()

    async def async_flush_save(self) -> None:
        """Save a pending change now (before a reboot or unload)."""
        self._save_generation += 1  # cancels a scheduled save
        await self._run_pending_save()

    @property
    def is_switch_cli(self) -> bool:
        return self.client.dialect == commands.DIALECT_SWITCH

    @property
    def is_exos(self) -> bool:
        return self.client.dialect == commands.DIALECT_EXOS

    async def _save(self) -> None:
        if self.is_openwrt:
            return  # every change is committed with 'uci commit' as part of the change
        if self.is_exos:
            output = await self.client.run(commands.EXOS_SAVE, timeout=120, retry=False, confirm=True)
        elif self.is_switch_cli:
            output = await self.client.run(commands.SW_SAVE, timeout=90, retry=False, confirm=True)
        else:
            output = await self.client.run(commands.SAVE_CONFIG, timeout=90, retry=False)
        error = find_cli_error(output)
        if error:
            raise HomeAssistantError(f"{self.client.host}: 'save config' failed: {error}")

    @staticmethod
    def _validated(func, *args):  # type: ignore[no-untyped-def]
        try:
            return func(*args)
        except commands.InvalidArgument as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_ssid_on_radio(self, radio: str, ssid: str, enable: bool) -> None:
        self._require_configurable(ssid)
        if self.is_openwrt:
            bound = {r for r in self.data.radio_names() if self.data.ssid_on_radio(ssid, r)}
            await self._ow_update_ssid(ssid, radios=sorted(bound | {radio} if enable else bound - {radio}))
            return
        cmd = self._validated(commands.ssid_bind, radio, ssid, enable)
        await self._apply([cmd], overrides={("ssid", ssid, radio): enable})

    async def async_set_max_client(self, ssid: str, value: int) -> None:
        self._require_configurable(ssid)
        if self.is_openwrt:
            await self._ow_update_ssid(ssid, max_clients=int(value))
            return
        cmd = self._validated(commands.ssid_max_client, ssid, int(value))
        await self._apply([cmd])

    async def async_get_security_object(self, ssid: str) -> str:
        """Find the security-object bound to an SSID (defaults to its name)."""
        if self._config is not None and ssid in self._config.ssid_security_objects:
            return self._config.ssid_security_objects[ssid]
        try:
            config = await self.client.run("show running-config")
        except AerohiveError as err:
            raise HomeAssistantError(f"Cannot read running-config: {err}") from err
        mapping = parse_ssid_security_objects(config)
        return mapping.get(ssid, ssid)

    async def async_set_ssid_password(
        self, ssid: str, password: str, security_object: str | None = None
    ) -> None:
        self._validated(commands.validate_psk, password)
        self._require_configurable(ssid)
        if self.is_openwrt:
            await self._ow_update_ssid(ssid, password=password)
            return
        obj = security_object or await self.async_get_security_object(ssid)
        cmd = self._validated(commands.ssid_psk, obj, password)
        _LOGGER.info("%s: changing Wi-Fi key of security-object %s", self.client.host, obj)
        await self._apply([cmd], secret=password)

    def ssid_names(self) -> list[str]:
        return sorted(self.data.ssids) if self.data else []

    async def async_set_ssid_all_radios(self, ssid: str, enable: bool) -> None:
        self._require_configurable(ssid)
        if self.is_openwrt:
            on = (self.wifi_home_radios(ssid) or self.data.radio_names()) if enable else []
            await self._ow_update_ssid(ssid, radios=on)
            return
        data = self.data
        cmds = [
            self._validated(commands.ssid_bind, radio, ssid, enable)
            for radio in data.radio_names()
            if data.ssid_on_radio(ssid, radio) != enable
        ]
        if cmds:
            await self._apply(cmds)

    async def async_set_ssid_hidden(self, ssid: str, hidden: bool) -> None:
        self._require_configurable(ssid)
        if self.is_openwrt:
            await self._ow_update_ssid(ssid, hidden=hidden)
            return
        await self._apply([self._validated(commands.ssid_hide, ssid, hidden)], overrides={("hidden", ssid): hidden})

    async def async_create_ssid(
        self, ssid: str, password: str | None, radios: list[str] | None, hidden: bool = False
    ) -> None:
        """Create an SSID. radios=None: all active radios; []: create without broadcasting."""
        if self.is_openwrt:
            await self._ow_create_ssid(ssid, password, radios, hidden)
            return
        if ssid in self.data.ssids:
            raise HomeAssistantError(f"SSID {ssid} already exists")
        available = self.data.radio_names()
        if not available:
            raise HomeAssistantError(f"{self.client.host} has no Wi-Fi radios")
        targets = available if radios is None else radios
        unknown = [r for r in targets if r not in available]
        if unknown:
            raise HomeAssistantError(f"Unknown radio(s) {', '.join(unknown)}; available: {', '.join(available)}")
        cmds = self._validated(commands.create_ssid, ssid, targets, password or None)
        if hidden:
            cmds.append(self._validated(commands.ssid_hide, ssid, True))
        _LOGGER.info("%s: creating SSID %s on %s", self.client.host, ssid, ", ".join(targets) or "no radio")
        await self._apply(cmds, secret=password or None)
        await self.async_request_refresh()  # the new network shows without waiting for the save

    async def async_update_ssid(
        self,
        ssid: str,
        *,
        password: str | None = None,
        radios: list[str] | None = None,
        hidden: bool | None = None,
        max_clients: int | None = None,
    ) -> bool:
        """Apply several changes to one SSID with a single save. Returns False if nothing changed."""
        self._require_configurable(ssid)
        if self.is_openwrt:
            return await self._ow_update_ssid(
                ssid, password=password, radios=radios, hidden=hidden, max_clients=max_clients
            )
        data = self.data
        if ssid not in data.ssids:
            raise HomeAssistantError(f"SSID {ssid} does not exist")
        cmds: list[str] = []
        state: dict[tuple[str, ...], bool] = {}
        if radios is not None:
            for radio in data.radio_names():
                want, have = radio in radios, data.ssid_on_radio(ssid, radio)
                if want != have:
                    cmds.append(self._validated(commands.ssid_bind, radio, ssid, want))
                    state[("ssid", ssid, radio)] = want
        if hidden is not None and hidden != bool(data.ssid_hidden(ssid)):
            cmds.append(self._validated(commands.ssid_hide, ssid, hidden))
            state[("hidden", ssid)] = hidden
        if max_clients is not None and max_clients != data.ssids[ssid].max_client:
            cmds.append(self._validated(commands.ssid_max_client, ssid, int(max_clients)))
        if password:
            self._validated(commands.validate_psk, password)
            obj = await self.async_get_security_object(ssid)
            cmds.append(self._validated(commands.ssid_psk, obj, password))
        if not cmds:
            return False
        _LOGGER.info("%s: updating SSID %s (%d change(s))", self.client.host, ssid, len(cmds))
        await self._apply(cmds, secret=password or None, overrides=state)
        return True

    async def async_delete_ssid(self, ssid: str) -> None:
        self._require_configurable(ssid)
        if self.is_openwrt:
            await self._ow_delete_ssid(ssid)
            return
        if ssid not in self.data.ssids:
            raise HomeAssistantError(f"SSID {ssid} does not exist")
        obj = await self.async_get_security_object(ssid)
        bound = [r for r in self.data.radio_names() if self.data.ssid_on_radio(ssid, r)]
        cmds = self._validated(commands.delete_ssid, ssid, bound, obj)
        _LOGGER.info("%s: deleting SSID %s", self.client.host, ssid)
        self.expect_removal = True
        try:
            await self._apply(cmds)
        except HomeAssistantError:
            self.expect_removal = False
            raise
        await self.async_request_refresh()  # the network disappears without waiting for the save

    async def async_set_port_enabled(self, port: str, enable: bool, force: bool = False) -> None:
        if self.is_openwrt:
            raise self._not_on_openwrt("Turning ports on and off")
        if not enable and not force and port in self.data.uplink_ports():
            raise HomeAssistantError(
                f"{port} is an uplink port on {self.client.host} (LLDP neighbour is a switch/router or "
                "many devices are learned on it). Turning it off could disconnect Home Assistant from "
                "the device. Use the set_port action with force: true if you really mean it."
            )
        state = {("port", port): enable}
        if self.is_exos:
            await self._apply([self._validated(commands.exos_port_enable, port, enable)], overrides=state)
        elif self.is_switch_cli:
            await self._apply(self._validated(commands.switch_port_enable, port, enable), sequence=True, overrides=state)
        else:
            await self._apply([self._validated(commands.port_enable, port, enable)], overrides=state)

    async def async_set_poe(self, port: str, enable: bool) -> None:
        if self.is_openwrt:
            raise self._not_on_openwrt("PoE control")
        state = {("poe", port): enable}
        if self.is_exos:
            await self._apply([self._validated(commands.exos_poe, port, enable)], overrides=state)
        elif self.is_switch_cli:
            await self._apply(self._validated(commands.switch_poe, port, enable), sequence=True, overrides=state)
        else:
            await self._apply([self._validated(commands.port_poe, port, enable)], overrides=state)

    async def async_power_cycle_poe(self, port: str, off_seconds: float = 5) -> None:
        """Turn PoE off and on again (runtime only, nothing is saved)."""
        if self.is_openwrt:
            raise self._not_on_openwrt("PoE control")
        if self.is_exos:
            try:
                out = await self.client.run(self._validated(commands.exos_poe_reset, port))
                if find_cli_error(out):
                    # No "reset inline-power": disable, wait, enable.
                    for cmd, pause in ((commands.exos_poe(port, False), off_seconds), (commands.exos_poe(port, True), 0)):
                        out = await self.client.run(cmd)
                        if error := find_cli_error(out):
                            raise HomeAssistantError(f"{self.client.host} rejected '{cmd}': {error}")
                        if pause:
                            await asyncio.sleep(pause)
            except AerohiveError as err:
                raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
            await self.async_request_refresh()
            return
        if self.is_switch_cli:
            reset_block = self._validated(commands.switch_poe_reset, port)
            off_block = self._validated(commands.switch_poe, port, False)
            on_block = self._validated(commands.switch_poe, port, True)
            try:
                try:
                    await self.client.run_sequence(reset_block)
                except AerohiveCommandError:
                    # Older firmware without "poe reset": switch PoE off and on again.
                    _LOGGER.debug("%s: 'poe reset' rejected, cycling PoE off/on", self.client.host)
                    await self.client.run_sequence(off_block)
                    await asyncio.sleep(off_seconds)
                    await self.client.run_sequence(on_block)
            except AerohiveCommandError as err:
                raise HomeAssistantError(str(err)) from err
            except AerohiveError as err:
                raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
            await self.async_request_refresh()
            return
        off = self._validated(commands.port_poe, port, False)
        on = self._validated(commands.port_poe, port, True)
        try:
            output = await self.client.run(off)
            if error := find_cli_error(output):
                raise HomeAssistantError(f"{self.client.host} rejected '{off}': {error}")
            await asyncio.sleep(off_seconds)
            output = await self.client.run(on)
            if error := find_cli_error(output):
                raise HomeAssistantError(f"{self.client.host} rejected '{on}': {error}")
        except AerohiveError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
        await self.async_request_refresh()

    async def async_disconnect_client(self, mac: str) -> None:
        if self.is_openwrt:
            station = self.data.stations.get((mac or "").lower())
            if station is None:
                raise HomeAssistantError(f"{mac} isn't connected to {self._device_name()}")
            try:
                output = await self.client.run(self._validated(ow.disconnect_client, station.extra["ifname"], mac))
            except AerohiveError as err:
                raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
            if find_cli_error(output):
                raise HomeAssistantError(f"{self.client.host} could not disconnect {mac} (is hostapd's ubus interface available?)")
            await self.async_request_refresh()
            return
        candidates = self._validated(commands.disconnect_commands, mac)
        last_error = None
        try:
            for cmd in candidates:
                output = await self.client.run(cmd)
                last_error = find_cli_error(output)
                if last_error is None:
                    break
        except AerohiveError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
        if last_error:
            raise HomeAssistantError(f"{self.client.host} could not disconnect {mac}: {last_error}")
        # Runtime action, nothing to persist.
        await self.async_request_refresh()

    async def async_reboot(self, save_changes: bool | None = None) -> None:
        """Reboot the device.

        ExtremeXOS with unsaved changes: refused unless ``save_changes`` says
        whether to save them first (True) or throw them away (False), because
        losing them is exactly the power-loss problem this integration guards
        against.
        """
        if self.is_exos and self.client.config_unsaved and save_changes is None:
            raise HomeAssistantError(
                f"{self._device_name()} has unsaved configuration changes. Press Save configuration first, "
                "or use the Reboot action with save_changes set to true (save, then reboot) or false "
                "(reboot and lose them)."
            )
        await self.async_flush_save()  # Home Assistant's own changes are always kept
        try:
            if save_changes and not self.is_exos:
                await self._save()
            if self.is_exos:
                command = commands.EXOS_REBOOT
            elif self.is_openwrt:
                command = ow.REBOOT
            elif self.is_switch_cli:
                command = commands.SW_REBOOT
            else:
                command = commands.REBOOT
            _LOGGER.info("%s: rebooting (save changes: %s)", self.client.host, bool(save_changes))
            await self.client.reboot(command, save=bool(save_changes))
        except AerohiveCommandError as err:
            raise HomeAssistantError(str(err)) from err
        except AerohiveConnectionError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err

    async def async_set_port_description(self, port: str, description: str) -> None:
        """Name a switch port (ExtremeXOS display string)."""
        if not self.is_exos:
            raise HomeAssistantError("Port names can only be set on ExtremeXOS switches")
        await self._apply([self._validated(commands.exos_port_display_string, port, description)])

    async def async_run_show(self, command: str, redact: bool = True) -> str:
        if self.is_openwrt:
            raise self._not_on_openwrt("run_show_command")
        command = self._validated(commands.validate_show_command, command)
        try:
            output = await self.client.run(command, timeout=120)
        except AerohiveError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
        return commands.redact(output) if redact else output
