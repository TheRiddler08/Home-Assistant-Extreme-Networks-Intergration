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
    BOOT_PARAM_REFRESH_SECONDS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
)
from .parsers import (
    BootParams,
    Interface,
    RadioDetail,
    Ssid,
    Station,
    VersionInfo,
    find_cli_error,
    is_unsupported_command,
    parse_show_boot_param,
    parse_show_interface,
    parse_show_interface_radio,
    parse_show_ssid,
    parse_show_station,
    parse_show_version,
    parse_ssid_security_objects,
)

_LOGGER = logging.getLogger(__name__)

type AerohiveConfigEntry = ConfigEntry[AerohiveCoordinator]


@dataclass
class AerohiveData:
    """Snapshot of everything read from the device in one poll."""

    hostname: str | None = None
    version: VersionInfo = field(default_factory=VersionInfo)
    interfaces: dict[str, Interface] = field(default_factory=dict)
    ssids: dict[str, Ssid] = field(default_factory=dict)
    stations: dict[str, Station] = field(default_factory=dict)
    radios: dict[str, RadioDetail] = field(default_factory=dict)
    boot_params: BootParams | None = None
    boot_time: datetime | None = None

    @property
    def mac(self) -> str | None:
        mgt = self.interfaces.get("mgt0")
        return mgt.mac if mgt else None

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

    def station_ssid(self, station: Station) -> str | None:
        if station.ssid:
            return station.ssid
        iface = self.interfaces.get(station.interface or "")
        return iface.ssid if iface else None

    def clients_on_ssid(self, ssid: str) -> int:
        return sum(1 for s in self.stations.values() if self.station_ssid(s) == ssid)

    def clients_on_radio(self, radio: str) -> int:
        return sum(1 for s in self.stations.values() if s.radio == radio)


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
        self._unsupported: set[str] = set()
        self._boot_params: BootParams | None = None
        self._boot_params_at = 0.0
        self._boot_time: datetime | None = None

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

    async def _fetch(self) -> AerohiveData:
        data = AerohiveData()
        data.version = parse_show_version(await self.client.run("show version"))
        data.interfaces = parse_show_interface(await self.client.run("show interface"))
        data.ssids = parse_show_ssid(await self.client.run("show ssid"))
        station_out = await self._optional("show station")
        data.stations = parse_show_station(station_out) if station_out else {}
        for radio in data.radio_names():
            out = await self._optional(f"show interface {radio}")
            if out:
                data.radios[radio] = parse_show_interface_radio(out, radio)

        now = time.monotonic()
        if self._boot_params is None or now - self._boot_params_at > BOOT_PARAM_REFRESH_SECONDS:
            out = await self._optional("show boot-param")
            if out:
                self._boot_params = parse_show_boot_param(out)
            self._boot_params_at = now
        data.boot_params = self._boot_params
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
        info = DeviceInfo(
            identifiers={(DOMAIN, unique)},
            manufacturer=MANUFACTURER,
            model=data.version.platform,
            name=data.hostname or self.config_entry.title,
            sw_version=data.version.version,
            hw_version=data.version.bootloader and f"Bootloader {data.version.bootloader}",
            serial_number=data.version.serial,
            configuration_url=None,
        )
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
