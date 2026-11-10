"""Network overview: one 'Aerohive Network' device combining every AP and switch.

Created automatically (as its own config entry) once a device is set up. It
sends nothing to the devices by itself: it aggregates the data the device
coordinators already polled, and fans control actions out to them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EntityCategory, UnitOfDataRate, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_OVERVIEW, MANUFACTURER
from .coordinator import AerohiveCoordinator, AerohiveData, SsidDraft
from .parsers import radio_band
from .ssid_manager import manager_buttons, manager_selects, manager_texts

_LOGGER = logging.getLogger(__name__)

OVERVIEW_UNIQUE_ID = "overview"
OVERVIEW_INTERVAL = timedelta(seconds=15)
KBPS = UnitOfDataRate.KILOBITS_PER_SECOND


def is_overview(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_OVERVIEW


@dataclass
class DeviceView:
    title: str
    coordinator: AerohiveCoordinator
    online: bool

    @property
    def data(self) -> AerohiveData:
        return self.coordinator.data

    @property
    def is_ap(self) -> bool:
        return bool(self.data.radio_names())

    @property
    def is_switch(self) -> bool:
        return self.data.is_switch


@dataclass
class OverviewData:
    devices: list[DeviceView] = field(default_factory=list)

    @property
    def online(self) -> list[DeviceView]:
        return [d for d in self.devices if d.online]

    @property
    def aps(self) -> list[DeviceView]:
        return [d for d in self.online if d.is_ap]

    @property
    def switches(self) -> list[DeviceView]:
        return [d for d in self.online if d.is_switch]

    def ssid_names(self) -> list[str]:
        return sorted({s for ap in self.aps for s in ap.data.ssids})

    def aps_with_ssid(self, ssid: str) -> list[DeviceView]:
        return [ap for ap in self.aps if ssid in ap.data.ssids]

    def clients(self) -> list[dict[str, Any]]:
        rows = []
        for ap in self.aps:
            data = ap.data
            for sta in data.stations.values():
                iface = data.interfaces.get(sta.radio or "")
                rows.append(
                    {k: v for k, v in {
                        "mac": sta.mac, "ip": sta.ip, "hostname": sta.hostname, "ap": ap.title,
                        "ssid": data.station_ssid(sta), "signal": sta.signal, "snr": sta.snr,
                        "band": radio_band(iface.radio_profile if iface else None,
                                           sta.channel or (iface.channel if iface else None)),
                        "tx_rate": sta.tx_rate, "connected": sta.assoc_time,
                    }.items() if v is not None}  # fmt: skip
                )
        return sorted(rows, key=lambda r: (r.get("ap", ""), r.get("ssid") or "", r["mac"]))

    def total(self, devices: list[DeviceView], fn: Callable[[AerohiveData], float | None]) -> float | None:
        values = [v for d in devices if (v := fn(d.data)) is not None]
        return round(sum(values), 2) if values else None


class OverviewCoordinator(DataUpdateCoordinator[OverviewData]):
    """Aggregates the device coordinators every 15 s (in memory, no device I/O)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} overview", update_interval=OVERVIEW_INTERVAL)
        self.ssid_draft = SsidDraft()

    async def _async_update_data(self) -> OverviewData:
        devices = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if is_overview(entry) or entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator = getattr(entry, "runtime_data", None)
            if not isinstance(coordinator, AerohiveCoordinator) or coordinator.data is None:
                continue
            devices.append(DeviceView(entry.title, coordinator, coordinator.last_update_success))
        return OverviewData(devices=devices)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, OVERVIEW_UNIQUE_ID)},
            name="Aerohive Network",
            manufacturer=MANUFACTURER,
            model="Network overview",
            entry_type=DeviceEntryType.SERVICE,
        )

    def ssid_names(self) -> list[str]:
        return self.data.ssid_names() if self.data else []

    async def _fan_out(self, targets: list[DeviceView], action: str, call: Callable[[AerohiveCoordinator], Any]) -> None:
        """Run an action on several devices; report every failure at the end."""
        if not targets:
            raise HomeAssistantError(f"No online access point to {action}")
        failures = []
        for device in targets:
            try:
                await call(device.coordinator)
            except HomeAssistantError as err:
                failures.append(f"{device.title}: {err}")
        await self.async_refresh()
        if failures:
            raise HomeAssistantError(f"Could not {action} on: " + "; ".join(failures))

    async def async_create_ssid(self, ssid: str, password: str | None, radios: list[str] | None) -> None:
        targets = [ap for ap in self.data.aps if ssid not in ap.data.ssids]
        if not targets:
            raise HomeAssistantError(f"SSID {ssid} already exists on every online access point")
        await self._fan_out(targets, f"create SSID {ssid}", lambda c: c.async_create_ssid(ssid, password, radios))

    async def async_delete_ssid(self, ssid: str) -> None:
        await self._fan_out(self.data.aps_with_ssid(ssid), f"delete SSID {ssid}", lambda c: c.async_delete_ssid(ssid))

    async def async_set_ssid_password(self, ssid: str, password: str) -> None:
        await self._fan_out(
            self.data.aps_with_ssid(ssid), f"change the key of {ssid}", lambda c: c.async_set_ssid_password(ssid, password)
        )

    async def async_set_ssid_enabled(self, ssid: str, enable: bool) -> None:
        await self._fan_out(
            self.data.aps_with_ssid(ssid),
            f"turn {ssid} {'on' if enable else 'off'}",
            lambda c: c.async_set_ssid_all_radios(ssid, enable),
        )


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------


class OverviewEntity(CoordinatorEntity[OverviewCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OverviewCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{OVERVIEW_UNIQUE_ID}_{key}"
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info


@dataclass(frozen=True)
class OverviewSensorSpec:
    key: str
    name: str
    value_fn: Callable[[OverviewData], Any]
    attrs_fn: Callable[[OverviewData], dict[str, Any]] | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    icon: str | None = None
    suggested_unit: str | None = None
    exists_fn: Callable[[OverviewData], bool] = lambda _: True


def _rate(key: str, name: str, devices: Callable[[OverviewData], list[DeviceView]], attr: str, wired: bool) -> OverviewSensorSpec:
    def value(o: OverviewData) -> float | None:
        def one(d: AerohiveData) -> float | None:
            tp = d.wired_throughput() if wired else d.wireless_throughput()
            return getattr(tp, attr)

        return o.total(devices(o), one)

    return OverviewSensorSpec(
        key, name, value, unit=KBPS, device_class=SensorDeviceClass.DATA_RATE,
        suggested_unit=UnitOfDataRate.MEGABITS_PER_SECOND,
    )  # fmt: skip


def _device_rows(o: OverviewData) -> list[dict[str, Any]]:
    return [
        {"name": d.title, "model": d.data.version.platform, "online": d.online,
         "type": "switch" if d.is_switch else "ap" if d.is_ap else "router",
         "ip": d.data.mgt.ip_address if d.data.mgt else d.coordinator.client.host,
         "clients": len(d.data.stations) if d.is_ap else len(d.data.wired_client_macs())}
        for d in o.devices
    ]  # fmt: skip


def _ssid_rows(o: OverviewData) -> list[dict[str, Any]]:
    rows = []
    for ssid in o.ssid_names():
        aps = o.aps_with_ssid(ssid)
        rows.append(
            {
                "ssid": ssid,
                "access_points": [ap.title for ap in aps],
                "broadcasting_on": [ap.title for ap in aps if ap.data.ssid_broadcasting(ssid)],
                "clients": sum(ap.data.clients_on_ssid(ssid) for ap in aps),
                "hidden": any(ap.data.ssid_hidden(ssid) for ap in aps),
                "security": next(
                    (sec for ap in aps if ap.data.config and (sec := ap.data.config.ssid_security(ssid))), None
                ),
            }
        )
    return rows


def _band_clients(band: str) -> Callable[[OverviewData], int]:
    return lambda o: sum(1 for c in o.clients() if c.get("band") == band)


SENSORS: tuple[OverviewSensorSpec, ...] = (
    OverviewSensorSpec("devices_online", "Devices online", lambda o: len(o.online),
                       attrs_fn=lambda o: {"total": len(o.devices), "devices": _device_rows(o)}, icon="mdi:lan"),
    OverviewSensorSpec("aps_online", "Access points online", lambda o: len(o.aps), icon="mdi:access-point"),
    OverviewSensorSpec("switches_online", "Switches online", lambda o: len(o.switches), icon="mdi:switch"),
    OverviewSensorSpec("wireless_clients", "Wireless clients", lambda o: sum(len(ap.data.stations) for ap in o.aps),
                       attrs_fn=lambda o: {"clients": o.clients()}, icon="mdi:account-multiple"),
    OverviewSensorSpec("clients_24ghz", "Clients 2.4 GHz", _band_clients("2.4 GHz"), icon="mdi:wifi"),
    OverviewSensorSpec("clients_5ghz", "Clients 5 GHz", _band_clients("5 GHz"), icon="mdi:wifi"),
    OverviewSensorSpec("wired_clients", "Wired clients", lambda o: len({m for s in o.switches for m in s.data.wired_client_macs()}),
                       icon="mdi:lan-connect", exists_fn=lambda o: bool(o.switches)),
    OverviewSensorSpec("ssids", "SSIDs", lambda o: len(o.ssid_names()),
                       attrs_fn=lambda o: {"ssids": _ssid_rows(o)}, icon="mdi:wifi-settings"),
    OverviewSensorSpec("ssids_broadcasting", "SSIDs broadcasting",
                       lambda o: sum(1 for s in o.ssid_names() if any(ap.data.ssid_broadcasting(s) for ap in o.aps_with_ssid(s))),
                       icon="mdi:wifi"),
    _rate("wireless_rx_throughput", "Wireless RX throughput", lambda o: o.aps, "rx_kbps", False),
    _rate("wireless_tx_throughput", "Wireless TX throughput", lambda o: o.aps, "tx_kbps", False),
    _rate("wireless_total_throughput", "Wireless total throughput", lambda o: o.aps, "total_kbps", False),
    _rate("wired_rx_throughput", "Wired RX throughput", lambda o: o.switches, "rx_kbps", True),
    _rate("wired_tx_throughput", "Wired TX throughput", lambda o: o.switches, "tx_kbps", True),
    OverviewSensorSpec("poe_power_used", "PoE power used",
                       lambda o: o.total(o.switches, lambda d: d.poe.used_w if d.poe else None),
                       unit=UnitOfPower.WATT, device_class=SensorDeviceClass.POWER),
    OverviewSensorSpec("poe_power_budget", "PoE power budget",
                       lambda o: o.total(o.switches, lambda d: d.poe.budget_w if d.poe else None),
                       unit=UnitOfPower.WATT, device_class=SensorDeviceClass.POWER),
    OverviewSensorSpec("ports_up", "Switch ports up",
                       lambda o: sum(1 for s in o.switches for p in s.data.ports() if s.data.interfaces[p].is_up),
                       icon="mdi:ethernet", exists_fn=lambda o: bool(o.switches)),
)  # fmt: skip


class OverviewSensor(OverviewEntity, SensorEntity):
    _unrecorded_attributes = frozenset({"clients", "devices", "ssids"})

    def __init__(self, coordinator: OverviewCoordinator, spec: OverviewSensorSpec) -> None:
        super().__init__(coordinator, spec.key, spec.name)
        self._spec = spec
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_icon = spec.icon
        if spec.suggested_unit:
            self._attr_suggested_unit_of_measurement = spec.suggested_unit
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> Any:
        return self._spec.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._spec.attrs_fn(self.coordinator.data) if self._spec.attrs_fn else None


class OverviewSsidClientsSensor(OverviewEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-multiple"

    def __init__(self, coordinator: OverviewCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_clients_{ssid}", f"{ssid} clients (all APs)")
        self._ssid = ssid

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssid_names()

    @property
    def native_value(self) -> int:
        return sum(ap.data.clients_on_ssid(self._ssid) for ap in self.coordinator.data.aps_with_ssid(self._ssid))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        aps = self.coordinator.data.aps_with_ssid(self._ssid)
        return {
            "per_access_point": {ap.title: ap.data.clients_on_ssid(self._ssid) for ap in aps},
            "broadcasting_on": [ap.title for ap in aps if ap.data.ssid_broadcasting(self._ssid)],
        }


class OverviewAllOnlineBinarySensor(OverviewEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: OverviewCoordinator) -> None:
        super().__init__(coordinator, "all_online", "All devices online")

    @property
    def is_on(self) -> bool:
        devices = self.coordinator.data.devices
        return bool(devices) and all(d.online for d in devices)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"offline": [d.title for d in self.coordinator.data.devices if not d.online]}


class OverviewSsidSwitch(OverviewEntity, SwitchEntity):
    """Broadcast an SSID on every radio of every AP that has it (on) or none (off)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:wifi-star"

    def __init__(self, coordinator: OverviewCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_all_{ssid}", f"{ssid} on all APs")
        self._ssid = ssid

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssid_names()

    @property
    def is_on(self) -> bool:
        aps = self.coordinator.data.aps_with_ssid(self._ssid)
        return bool(aps) and all(ap.data.ssid_enabled(self._ssid) for ap in aps)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_ssid_enabled(self._ssid, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_ssid_enabled(self._ssid, False)


class OverviewSsidKeyText(OverviewEntity, TextEntity):
    """Change the Wi-Fi key of an SSID on every AP that has it (write-only)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.PASSWORD
    _attr_native_min = 8
    _attr_native_max = 63
    _attr_pattern = r"^[\x20\x21\x23-\x7e]{8,63}$"
    _attr_icon = "mdi:form-textbox-password"

    def __init__(self, coordinator: OverviewCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"psk_{ssid}", f"{ssid} Wi-Fi key (all APs)")
        self._ssid = ssid

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssid_names()

    @property
    def native_value(self) -> str | None:
        return None

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_set_ssid_password(self._ssid, value)


# --------------------------------------------------------------------------
# Platform setup helpers (called from each platform module)
# --------------------------------------------------------------------------


def _dynamic(
    coordinator: OverviewCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[OverviewData], Iterable[tuple[str, Callable[[], Entity]]]],
) -> None:
    known: set[str] = set()

    @callback
    def _check() -> None:
        if coordinator.data is None:
            return
        new = []
        for key, build in factory(coordinator.data):
            if key not in known:
                known.add(key)
                new.append(build())
        if new:
            async_add_entities(new)

    _check()
    entry.async_on_unload(coordinator.async_add_listener(_check))


def _coordinator(entry: ConfigEntry) -> OverviewCoordinator:
    return entry.runtime_data


async def async_setup_platform_entities(platform: str, hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = _coordinator(entry)
    info = coordinator.device_info
    base = OVERVIEW_UNIQUE_ID
    if platform == "sensor":

        def sensors(o: OverviewData) -> Iterable[tuple[str, Callable[[], Entity]]]:
            for spec in SENSORS:
                if spec.exists_fn(o):
                    yield spec.key, lambda spec=spec: OverviewSensor(coordinator, spec)
            for ssid in o.ssid_names():
                yield f"ssid_clients_{ssid}", lambda ssid=ssid: OverviewSsidClientsSensor(coordinator, ssid)

        _dynamic(coordinator, entry, add, sensors)
    elif platform == "binary_sensor":
        add([OverviewAllOnlineBinarySensor(coordinator)])
    elif platform == "switch":
        _dynamic(coordinator, entry, add, lambda o: (
            (f"ssid_all_{s}", lambda s=s: OverviewSsidSwitch(coordinator, s)) for s in o.ssid_names()
        ))  # fmt: skip
    elif platform == "text":
        add(manager_texts(coordinator, base, info, " (all APs)"))
        _dynamic(coordinator, entry, add, lambda o: (
            (f"psk_{s}", lambda s=s: OverviewSsidKeyText(coordinator, s)) for s in o.ssid_names()
        ))  # fmt: skip
    elif platform == "button":
        add(manager_buttons(coordinator, base, info, " (all APs)"))
    elif platform == "select":
        add(manager_selects(coordinator, base, info, " (all APs)"))
