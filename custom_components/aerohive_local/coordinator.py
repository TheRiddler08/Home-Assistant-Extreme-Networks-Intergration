"""DataUpdateCoordinator for one Aerohive device."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
from .parsers import (
    BootParams,
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
)

_LOGGER = logging.getLogger(__name__)

type AerohiveConfigEntry = ConfigEntry[AerohiveCoordinator]

UPLINK_MAC_THRESHOLD = 16
POE_MAX_PORTS = 24  # per-port PoE probing stops at this port number


@dataclass
class SsidDraft:
    """Values typed into the 'new SSID' / 'SSID to delete' entities (in memory only)."""

    name: str = ""
    password: str = ""
    delete: str | None = None

    def clear(self) -> None:
        self.name = ""
        self.password = ""


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

    @property
    def is_switch(self) -> bool:
        """SR-series switches (or anything with eth1/1-style ports)."""
        platform = (self.version.platform or "").upper()
        return platform.startswith("SR") or any("/" in n for n in self.interfaces)

    @property
    def has_switch_ports(self) -> bool:
        """Switches and branch routers (BR100 eth1-eth4) have LAN ports worth managing."""
        return self.is_switch or (self.version.platform or "").upper().startswith("BR")

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
        uplinks: set[str] = set()
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
        if self.config is not None and port in self.config.pse_shutdown_ports:
            return False
        poe = self.poe_ports.get(port)
        return poe.enabled if poe else None

    def ssid_hidden(self, ssid: str) -> bool | None:
        return None if self.config is None else ssid in self.config.hidden_ssids

    def ssid_enabled(self, ssid: str) -> bool:
        """Bound to every exposed radio."""
        radios = self.radio_names()
        return bool(radios) and all(self.ssid_on_radio(ssid, r) for r in radios)

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
        """Radios worth exposing: up with a channel, or carrying an SSID."""
        with_ssid = {i.parent for i in self.interfaces.values() if i.parent and i.ssid}
        result = []
        for name, iface in sorted(self.interfaces.items()):
            if not iface.is_radio:
                continue
            if name in with_ssid or iface.channel is not None:
                result.append(name)
        return result

    def ssid_on_radio(self, ssid: str, radio: str) -> bool:
        return any(i.parent == radio and i.ssid == ssid for i in self.interfaces.values())

    def ssids_on_radio(self, radio: str) -> list[str]:
        return sorted({i.ssid for i in self.interfaces.values() if i.parent == radio and i.ssid})

    def ssid_subinterfaces(self, ssid: str) -> list[Interface]:
        return [i for i in self.interfaces.values() if i.parent and i.ssid == ssid]

    def ssid_broadcasting(self, ssid: str) -> bool:
        """True if the SSID is bound to at least one radio that is up and on a channel."""
        for sub in self.ssid_subinterfaces(ssid):
            radio = self.interfaces.get(sub.parent or "")
            if sub.is_up is False or radio is None or radio.is_up is False or radio.channel is None:
                continue
            return True
        return False

    def broadcasting_ssids(self) -> list[str]:
        return sorted(s for s in self.ssids if self.ssid_broadcasting(s))

    def station_ssid(self, station: Station) -> str | None:
        if station.ssid:
            return station.ssid
        iface = self.interfaces.get(station.interface or "")
        return iface.ssid if iface else None

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
        self._slow_at: float | None = None
        self._boot_params: BootParams | None = None
        self._hardware: HardwareInfo | None = None
        self._mgt: InterfaceDetail | None = None
        self._boot_time: datetime | None = None
        self.ssid_draft = SsidDraft()
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
        # name -> (monotonic time, rx_bytes, tx_bytes) from the previous poll
        self._counters: dict[str, tuple[float, int | None, int | None]] = {}

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #

    async def _optional(self, command: str) -> str | None:
        """Run a command the firmware may not support; remember failures."""
        if command in self._unsupported:
            return None
        output = await self.client.run(command)
        if is_unsupported_command(output):
            _LOGGER.debug("%s: '%s' not supported: %s", self.client.host, command, output[:200])
            self._unsupported.add(command)
            return None
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
        try:
            data = await self._fetch()
            self._polled = True
            return data
        except AerohiveAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AerohiveError as err:
            raise UpdateFailed(f"Error communicating with {self.client.host}: {err}") from err

    def _detail_targets(self, data: AerohiveData) -> list[str]:
        """Interfaces whose 'show interface <name>' detail is read each poll."""
        targets = list(data.radio_names())
        if self.detailed_stats:
            for name, iface in sorted(data.interfaces.items()):
                if iface.is_port and iface.is_up:
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
        data = AerohiveData()
        version_out = await self.client.run("show version")
        if self.client.dialect == commands.DIALECT_SWITCH:
            return await self._fetch_switch(data, version_out)
        data.version = parse_show_version(version_out)
        data.interfaces = parse_show_interface(await self.client.run("show interface"))
        ssid_out = await self._optional("show ssid")
        data.ssids = parse_show_ssid(ssid_out) if ssid_out else {}
        if data.radio_names():
            station_out = await self._optional("show station")
            data.stations = parse_show_station(station_out) if station_out else {}

        for name in self._detail_targets(data):
            out = await self._optional(f"show interface {name}")
            if not out:
                continue
            detail = parse_show_interface_detail(out, name)
            data.details[name] = detail
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
        return data

    def _update_boot_time(self, data: AerohiveData) -> None:
        if data.version.uptime_seconds is not None:
            boot = dt_util.utcnow() - timedelta(seconds=data.version.uptime_seconds)
            # Keep the timestamp stable across polls (uptime has coarse
            # resolution); only move it on an actual reboot.
            if self._boot_time is None or abs((boot - self._boot_time).total_seconds()) > 120:
                self._boot_time = boot.replace(microsecond=0)
        data.boot_time = self._boot_time

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
        if self._slow_at is None or now - self._slow_at > SLOW_REFRESH_SECONDS:
            self._slow_at = now
            self._config_dirty = True
            if out := await self._optional(commands.SW_SHOW_NETWORK):
                self._mgt = parse_show_network(out)
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
        if self._config is not None and self._config.uptime_seconds is not None and self._config_read_at:
            # Uptime from the running-config header, aged by the time since it was read.
            data.version.uptime_seconds = self._config.uptime_seconds + int(now - self._config_read_at)
        self._update_boot_time(data)
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

    def _announce_neighbors(self) -> None:
        """Offer unconfigured Aerohive/Extreme neighbours seen via LLDP for setup."""
        configured = {
            e.data.get(CONF_HOST) for e in self.hass.config_entries.async_entries(DOMAIN)
        } | {e.unique_id for e in self.hass.config_entries.async_entries(DOMAIN)}
        for neighbor in self._lldp.values():
            host = neighbor.management_address
            mac = normalize_mac(neighbor.chassis_id)
            looks_aerohive = is_aerohive_hostname(neighbor.system_name) or is_aerohive_mac(mac)
            if not host or not looks_aerohive or host in configured or mac in configured:
                continue
            if host in self._announced:
                continue
            self._announced.add(host)
            _LOGGER.debug("%s: offering LLDP neighbour %s (%s) for setup", self.client.host, neighbor.system_name, host)
            discovery_flow.async_create_flow(
                self.hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={CONF_HOST: host, "mac": mac, "name": neighbor.system_name or host},
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

    async def _apply(self, cmds: list[str], *, secret: str | None = None, sequence: bool = False) -> None:
        """Run config commands, verify, save config and refresh."""

        def scrub(text: str) -> str:
            text = commands.redact(text)
            return text.replace(secret, "***") if secret else text

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
            await self._save()
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
        await self.async_request_refresh()

    @property
    def is_switch_cli(self) -> bool:
        return self.client.dialect == commands.DIALECT_SWITCH

    async def _save(self) -> None:
        if self.is_switch_cli:
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
        cmd = self._validated(commands.ssid_bind, radio, ssid, enable)
        await self._apply([cmd])

    async def async_set_max_client(self, ssid: str, value: int) -> None:
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
        obj = security_object or await self.async_get_security_object(ssid)
        cmd = self._validated(commands.ssid_psk, obj, password)
        _LOGGER.info("%s: changing Wi-Fi key of security-object %s", self.client.host, obj)
        await self._apply([cmd], secret=password)

    def ssid_names(self) -> list[str]:
        return sorted(self.data.ssids) if self.data else []

    async def async_set_ssid_all_radios(self, ssid: str, enable: bool) -> None:
        data = self.data
        cmds = [
            self._validated(commands.ssid_bind, radio, ssid, enable)
            for radio in data.radio_names()
            if data.ssid_on_radio(ssid, radio) != enable
        ]
        if cmds:
            await self._apply(cmds)

    async def async_set_ssid_hidden(self, ssid: str, hidden: bool) -> None:
        await self._apply([self._validated(commands.ssid_hide, ssid, hidden)])

    async def async_create_ssid(self, ssid: str, password: str | None, radios: list[str] | None) -> None:
        if ssid in self.data.ssids:
            raise HomeAssistantError(f"SSID {ssid} already exists")
        available = self.data.radio_names()
        if not available:
            raise HomeAssistantError(f"{self.client.host} has no Wi-Fi radios")
        targets = radios or available
        unknown = [r for r in targets if r not in available]
        if unknown:
            raise HomeAssistantError(f"Unknown radio(s) {', '.join(unknown)}; available: {', '.join(available)}")
        cmds = self._validated(commands.create_ssid, ssid, targets, password or None)
        _LOGGER.info("%s: creating SSID %s on %s", self.client.host, ssid, ", ".join(targets))
        await self._apply(cmds, secret=password or None)

    async def async_delete_ssid(self, ssid: str) -> None:
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

    async def async_set_port_enabled(self, port: str, enable: bool, force: bool = False) -> None:
        if not enable and not force and port in self.data.uplink_ports():
            raise HomeAssistantError(
                f"{port} is an uplink port on {self.client.host} (LLDP neighbour is a switch/router or "
                "many devices are learned on it). Turning it off could disconnect Home Assistant from "
                "the device. Use the set_port action with force: true if you really mean it."
            )
        if self.is_switch_cli:
            await self._apply(self._validated(commands.switch_port_enable, port, enable), sequence=True)
        else:
            await self._apply([self._validated(commands.port_enable, port, enable)])

    async def async_set_poe(self, port: str, enable: bool) -> None:
        if self.is_switch_cli:
            await self._apply(self._validated(commands.switch_poe, port, enable), sequence=True)
        else:
            await self._apply([self._validated(commands.port_poe, port, enable)])

    async def async_power_cycle_poe(self, port: str, off_seconds: float = 5) -> None:
        """Turn PoE off and on again (runtime only, nothing is saved)."""
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

    async def async_reboot(self) -> None:
        try:
            await self.client.reboot(commands.SW_REBOOT if self.is_switch_cli else commands.REBOOT)
        except AerohiveCommandError as err:
            raise HomeAssistantError(str(err)) from err
        except AerohiveConnectionError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err

    async def async_run_show(self, command: str, redact: bool = True) -> str:
        command = self._validated(commands.validate_show_command, command)
        try:
            output = await self.client.run(command, timeout=120)
        except AerohiveError as err:
            raise HomeAssistantError(f"Error communicating with {self.client.host}: {err}") from err
        return commands.redact(output) if redact else output
