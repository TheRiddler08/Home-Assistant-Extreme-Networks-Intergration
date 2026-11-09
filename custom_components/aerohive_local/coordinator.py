"""DataUpdateCoordinator for one Aerohive device."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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
    CONF_SCAN_INTERVAL,
    DEFAULT_DETAILED_STATS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    SLOW_REFRESH_SECONDS,
)
from .parsers import (
    BootParams,
    HardwareInfo,
    Interface,
    InterfaceDetail,
    Ssid,
    Station,
    SystemStats,
    VersionInfo,
    find_cli_error,
    is_unsupported_command,
    parse_show_boot_param,
    parse_show_cpu,
    parse_show_hw_info,
    parse_show_interface,
    parse_show_interface_detail,
    parse_show_memory,
    parse_show_ssid,
    parse_show_station,
    parse_show_version,
    parse_ssid_security_objects,
)

_LOGGER = logging.getLogger(__name__)

type AerohiveConfigEntry = ConfigEntry[AerohiveCoordinator]


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

    @property
    def mac(self) -> str | None:
        mgt = self.interfaces.get("mgt0")
        return mgt.mac if mgt else None

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
        return _sum_rates([t for n, t in self.throughput.items() if n.startswith("eth") and "." not in n])

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
        self._unsupported: set[str] = set()
        self._slow_at: float | None = None
        self._boot_params: BootParams | None = None
        self._hardware: HardwareInfo | None = None
        self._mgt: InterfaceDetail | None = None
        self._boot_time: datetime | None = None
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

    async def _async_update_data(self) -> AerohiveData:
        try:
            return await self._fetch()
        except AerohiveAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AerohiveError as err:
            raise UpdateFailed(f"Error communicating with {self.client.host}: {err}") from err

    def _detail_targets(self, data: AerohiveData) -> list[str]:
        """Interfaces whose 'show interface <name>' detail is read each poll."""
        targets = list(data.radio_names())
        if self.detailed_stats:
            for name, iface in sorted(data.interfaces.items()):
                if name.startswith("eth") and iface.parent is None and iface.is_up:
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
        data.version = parse_show_version(await self.client.run("show version"))
        data.interfaces = parse_show_interface(await self.client.run("show interface"))
        data.ssids = parse_show_ssid(await self.client.run("show ssid"))
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

        now = time.monotonic()
        if self._slow_at is None or now - self._slow_at > SLOW_REFRESH_SECONDS:
            self._slow_at = now
            if out := await self._optional("show boot-param"):
                self._boot_params = parse_show_boot_param(out)
            if out := await self._optional("show hw-info"):
                self._hardware = parse_show_hw_info(out)
            if out := await self._optional("show interface mgt0"):
                self._mgt = parse_show_interface_detail(out, "mgt0")
        data.boot_params = self._boot_params
        data.hardware = self._hardware
        if self._mgt is not None and "mgt0" not in data.details:
            data.details["mgt0"] = self._mgt
        data.hostname = self.client.hostname

        if data.version.uptime_seconds is not None:
            boot = dt_util.utcnow() - timedelta(seconds=data.version.uptime_seconds)
            # Keep the timestamp stable across polls (uptime has coarse
            # resolution); only move it on an actual reboot.
            if self._boot_time is None or abs((boot - self._boot_time).total_seconds()) > 120:
                self._boot_time = boot.replace(microsecond=0)
        data.boot_time = self._boot_time
        return data

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

    async def _apply(self, cmds: list[str], *, secret: str | None = None) -> None:
        """Run config commands, verify, save config and refresh."""

        def scrub(text: str) -> str:
            text = commands.redact(text)
            return text.replace(secret, "***") if secret else text

        try:
            for cmd in cmds:
                output = await self.client.run(cmd)
                error = find_cli_error(output)
                if error:
                    raise HomeAssistantError(
                        f"{self.client.host} rejected '{scrub(cmd)}': {scrub(error)}"
                    )
            await self._save()
        except AerohiveCommandError as err:
            raise HomeAssistantError(scrub(str(err))) from err
        except AerohiveError as err:
            raise HomeAssistantError(
                f"Error communicating with {self.client.host}: {scrub(str(err))}"
            ) from err
        await self.async_request_refresh()

    async def _save(self) -> None:
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
            await self.client.reboot(commands.REBOOT)
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
