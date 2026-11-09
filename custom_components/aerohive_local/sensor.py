"""Sensors for Aerohive Local.

Groups:
- device: firmware, uptime, CPU, memory, addressing, totals, throughput
- radio: clients, channel, power, noise, utilisation (per wifiN)
- interface: throughput, counters, link speed and table fields (per interface)
- SSID: clients, throughput, SSID settings
- client: signal / SNR / rates for opted-in client MACs
- raw: one (disabled) sensor for every other field the CLI reports
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_RAW_SENSORS, CONF_TRACKED_CLIENTS, DEFAULT_RAW_SENSORS
from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, async_add_dynamic_entities, extra_attrs, radio_label
from .parsers import COUNTER_FIELDS, normalize_mac, parse_raw_value, parse_rate_mbps, radio_band

PARALLEL_UPDATES = 0

DIAG = EntityCategory.DIAGNOSTIC
KBPS = UnitOfDataRate.KILOBITS_PER_SECOND


def _client_list(data: AerohiveData, stations: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """Compact per-client dicts for dashboards (markdown / flex-table cards)."""
    result = []
    for sta in stations if stations is not None else data.stations.values():
        iface = data.interfaces.get(sta.radio or "")
        result.append(
            extra_attrs(
                mac=sta.mac,
                ip=sta.ip,
                hostname=sta.hostname,
                ssid=data.station_ssid(sta),
                interface=sta.interface,
                band=radio_band(iface.radio_profile if iface else None, sta.channel or (iface.channel if iface else None)),
                channel=sta.channel,
                signal=sta.signal,
                snr=sta.snr,
                tx_rate=sta.tx_rate,
                rx_rate=sta.rx_rate,
                phy_mode=sta.phy_mode,
                auth_mode=sta.auth_mode,
                vlan=sta.vlan,
                connected=sta.assoc_time,
            )
        )
    return sorted(result, key=lambda c: (c.get("ssid") or "", c.get("mac")))


# --------------------------------------------------------------------------
# Device sensors
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DeviceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AerohiveData], Any]
    exists_fn: Callable[[AerohiveData], bool] = lambda _: True
    attrs_fn: Callable[[AerohiveData], dict[str, Any]] | None = None


def _rate_desc(key: str, value_fn: Callable[[AerohiveData], Any], exists_fn: Callable[[AerohiveData], bool]) -> DeviceSensorDescription:
    return DeviceSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=KBPS,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        suggested_display_precision=2,
        value_fn=value_fn,
        exists_fn=exists_fn,
    )


def _has_wireless_rates(d: AerohiveData) -> bool:
    return any(r in d.throughput for r in d.radio_names())


def _has_wired_rates(d: AerohiveData) -> bool:
    return any(n.startswith("eth") for n in d.throughput)


DEVICE_SENSORS: tuple[DeviceSensorDescription, ...] = (
    DeviceSensorDescription(
        key="firmware", translation_key="firmware", entity_category=DIAG,
        value_fn=lambda d: d.version.version,
        attrs_fn=lambda d: extra_attrs(
            firmware=d.version.firmware, build=d.version.build, os=d.version.os_name,
            bootloader=d.version.bootloader,
        ),
    ),
    DeviceSensorDescription(
        key="platform", translation_key="platform", entity_category=DIAG,
        entity_registry_enabled_default=False, value_fn=lambda d: d.version.platform,
    ),
    DeviceSensorDescription(
        key="boot_time", translation_key="boot_time", device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=DIAG, value_fn=lambda d: d.boot_time,
    ),
    DeviceSensorDescription(
        key="uptime", translation_key="uptime", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="s", suggested_unit_of_measurement="d", suggested_display_precision=2,
        entity_category=DIAG, entity_registry_enabled_default=False,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.version.uptime_seconds,
        exists_fn=lambda d: d.version.uptime_seconds is not None,
    ),
    DeviceSensorDescription(
        key="clients", translation_key="clients", state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.stations),
        attrs_fn=lambda d: {"clients": _client_list(d)},
    ),
    DeviceSensorDescription(
        key="ssids_broadcasting", translation_key="ssids_broadcasting",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.broadcasting_ssids()),
        attrs_fn=lambda d: {
            "broadcasting": d.broadcasting_ssids(),
            "configured": sorted(d.ssids),
            "not_broadcasting": sorted(s for s in d.ssids if not d.ssid_broadcasting(s)),
        },
    ),
    DeviceSensorDescription(
        key="cpu_usage", translation_key="cpu_usage", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
        value_fn=lambda d: d.cpu.cpu_total if d.cpu else None,
        exists_fn=lambda d: bool(d.cpu and d.cpu.cpu_total is not None),
    ),
    DeviceSensorDescription(
        key="cpu_user", translation_key="cpu_user", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, entity_registry_enabled_default=False,
        entity_category=DIAG, suggested_display_precision=1,
        value_fn=lambda d: d.cpu.cpu_user if d.cpu else None,
        exists_fn=lambda d: bool(d.cpu and d.cpu.cpu_user is not None),
    ),
    DeviceSensorDescription(
        key="cpu_system", translation_key="cpu_system", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, entity_registry_enabled_default=False,
        entity_category=DIAG, suggested_display_precision=1,
        value_fn=lambda d: d.cpu.cpu_system if d.cpu else None,
        exists_fn=lambda d: bool(d.cpu and d.cpu.cpu_system is not None),
    ),
    DeviceSensorDescription(
        key="memory_usage", translation_key="memory_usage", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
        value_fn=lambda d: d.memory.memory_used_percent if d.memory else None,
        exists_fn=lambda d: bool(d.memory and d.memory.memory_used_percent is not None),
    ),
    DeviceSensorDescription(
        key="memory_free", translation_key="memory_free", device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.KILOBYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES, suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT, entity_category=DIAG,
        value_fn=lambda d: d.memory.memory_free_kb if d.memory else None,
        exists_fn=lambda d: bool(d.memory and d.memory.memory_free_kb is not None),
    ),
    DeviceSensorDescription(
        key="memory_total", translation_key="memory_total", device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.KILOBYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES, suggested_display_precision=0,
        entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=lambda d: d.memory.memory_total_kb if d.memory else None,
        exists_fn=lambda d: bool(d.memory and d.memory.memory_total_kb is not None),
    ),
    DeviceSensorDescription(
        key="ip_address", translation_key="ip_address", entity_category=DIAG,
        value_fn=lambda d: d.mgt.ip_address if d.mgt else None,
        exists_fn=lambda d: bool(d.mgt and d.mgt.ip_address),
        attrs_fn=lambda d: extra_attrs(
            netmask=d.mgt.netmask if d.mgt else None, gateway=d.mgt.gateway if d.mgt else None,
        ),
    ),
    DeviceSensorDescription(
        key="gateway", translation_key="gateway", entity_category=DIAG,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.mgt.gateway if d.mgt else None,
        exists_fn=lambda d: bool(d.mgt and d.mgt.gateway),
    ),
    DeviceSensorDescription(
        key="serial_number", translation_key="serial_number", entity_category=DIAG,
        value_fn=lambda d: (d.hardware.serial if d.hardware else None) or d.version.serial,
        exists_fn=lambda d: bool((d.hardware and d.hardware.serial) or d.version.serial),
    ),
    DeviceSensorDescription(
        key="hw_revision", translation_key="hw_revision", entity_category=DIAG,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.hardware.hw_revision if d.hardware else None,
        exists_fn=lambda d: bool(d.hardware and d.hardware.hw_revision),
    ),
    DeviceSensorDescription(
        key="region_code", translation_key="region_code", entity_category=DIAG,
        value_fn=lambda d: d.boot_params.region_code if d.boot_params else None,
        exists_fn=lambda d: bool(d.boot_params and d.boot_params.region_code),
    ),
    DeviceSensorDescription(
        key="country_code", translation_key="country_code", entity_category=DIAG,
        value_fn=lambda d: d.boot_params.country_code if d.boot_params else None,
        exists_fn=lambda d: bool(d.boot_params and d.boot_params.country_code),
    ),
    _rate_desc("wireless_rx_throughput", lambda d: d.wireless_throughput().rx_kbps, _has_wireless_rates),
    _rate_desc("wireless_tx_throughput", lambda d: d.wireless_throughput().tx_kbps, _has_wireless_rates),
    _rate_desc("wireless_total_throughput", lambda d: d.wireless_throughput().total_kbps, _has_wireless_rates),
    _rate_desc("wired_rx_throughput", lambda d: d.wired_throughput().rx_kbps, _has_wired_rates),
    _rate_desc("wired_tx_throughput", lambda d: d.wired_throughput().tx_kbps, _has_wired_rates),
)  # fmt: skip


class AerohiveDeviceSensor(AerohiveEntity, SensorEntity):
    entity_description: DeviceSensorDescription
    # The per-client list can be large; keep it out of the recorder.
    _unrecorded_attributes = frozenset({"clients"})

    def __init__(self, coordinator: AerohiveCoordinator, description: DeviceSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


# --------------------------------------------------------------------------
# Radio sensors
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RadioSensorDescription(SensorEntityDescription):
    """Sensor bound to one radio; value_fn gets (data, radio)."""

    label: str
    value_fn: Callable[[AerohiveData, str], Any]
    exists_fn: Callable[[AerohiveData, str], bool] = lambda d, r: True


def _detail(attr: str) -> Callable[[AerohiveData, str], Any]:
    def value(data: AerohiveData, name: str) -> Any:
        detail = data.details.get(name)
        return getattr(detail, attr) if detail else None

    return value


def _detail_exists(attr: str) -> Callable[[AerohiveData, str], bool]:
    return lambda d, r: _detail(attr)(d, r) is not None


def _channel(data: AerohiveData, radio: str) -> int | None:
    iface = data.interfaces.get(radio)
    if iface and iface.channel is not None:
        return iface.channel
    return _detail("channel")(data, radio)


def _width(data: AerohiveData, radio: str) -> str | None:
    iface = data.interfaces.get(radio)
    if iface and iface.channel_width:
        return iface.channel_width
    return _detail("channel_width")(data, radio)


def _radio_clients(data: AerohiveData, radio: str) -> list[dict[str, Any]]:
    return _client_list(data, [s for s in data.stations.values() if s.radio == radio])


RADIO_SENSORS: tuple[RadioSensorDescription, ...] = (
    RadioSensorDescription(
        key="clients", label="clients", state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, r: d.clients_on_radio(r),
    ),
    RadioSensorDescription(key="channel", label="channel", value_fn=_channel),
    RadioSensorDescription(key="channel_width", label="channel width", value_fn=_width),
    RadioSensorDescription(
        key="ssids", label="SSIDs",
        value_fn=lambda d, r: ", ".join(d.ssids_on_radio(r)) or "none",
    ),
    RadioSensorDescription(
        key="eirp_power", label="EIRP power", native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("eirp_power"), exists_fn=_detail_exists("eirp_power"),
    ),
    RadioSensorDescription(
        key="tx_power", label="TX power", native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("tx_power"), exists_fn=_detail_exists("tx_power"),
    ),
    RadioSensorDescription(
        key="noise_floor", label="noise floor", native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("noise_floor"), exists_fn=_detail_exists("noise_floor"),
    ),
    RadioSensorDescription(
        key="tx_utilization", label="TX utilization", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("tx_utilization"), exists_fn=_detail_exists("tx_utilization"),
    ),
    RadioSensorDescription(
        key="rx_utilization", label="RX utilization", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("rx_utilization"), exists_fn=_detail_exists("rx_utilization"),
    ),
    RadioSensorDescription(
        key="interference_utilization", label="interference utilization",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("interference_utilization"), exists_fn=_detail_exists("interference_utilization"),
    ),
    RadioSensorDescription(
        key="total_utilization", label="total utilization", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("total_utilization"), exists_fn=_detail_exists("total_utilization"),
    ),
    RadioSensorDescription(
        key="max_clients", label="max clients", entity_category=DIAG,
        value_fn=_detail("max_clients"), exists_fn=_detail_exists("max_clients"),
    ),
    RadioSensorDescription(
        key="phy_mode", label="PHY mode", entity_category=DIAG,
        value_fn=_detail("phy_mode"), exists_fn=_detail_exists("phy_mode"),
    ),
)  # fmt: skip


class AerohiveRadioSensor(AerohiveEntity, SensorEntity):
    entity_description: RadioSensorDescription
    _unrecorded_attributes = frozenset({"clients"})

    def __init__(self, coordinator: AerohiveCoordinator, radio: str, description: RadioSensorDescription) -> None:
        super().__init__(coordinator, f"{radio}_{description.key}")
        self.entity_description = description
        self._radio = radio
        self._attr_name = f"{radio_label(coordinator.data, radio)} {description.label}"

    @property
    def available(self) -> bool:
        return super().available and self._radio in self.coordinator.data.interfaces

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, self._radio)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if self.entity_description.key == "clients":
            return {"clients": _radio_clients(data, self._radio)}
        if self.entity_description.key != "channel":
            return None
        iface = data.interfaces.get(self._radio)
        detail = data.details.get(self._radio)
        return extra_attrs(
            radio_profile=iface.radio_profile if iface else None,
            mode=iface.mode if iface else None,
            phy_mode=detail.phy_mode if detail else None,
            band=radio_band(iface.radio_profile, iface.channel) if iface else None,
        )


# --------------------------------------------------------------------------
# Interface sensors (throughput, counters, link, table fields)
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class InterfaceSensorDescription(SensorEntityDescription):
    label: str
    value_fn: Callable[[AerohiveData, str], Any]
    exists_fn: Callable[[AerohiveData, str], bool]


def _tp(direction: str) -> Callable[[AerohiveData, str], Any]:
    def value(d: AerohiveData, n: str) -> Any:
        rate = d.throughput.get(n)
        return getattr(rate, f"{direction}_kbps") if rate else None

    return value


def _counter_desc(field: str) -> InterfaceSensorDescription:
    is_bytes = field.endswith("bytes")
    return InterfaceSensorDescription(
        key=field,
        label=field.replace("_", " ").replace("rx", "RX").replace("tx", "TX"),
        device_class=SensorDeviceClass.DATA_SIZE if is_bytes else None,
        native_unit_of_measurement=UnitOfInformation.BYTES if is_bytes else None,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES if is_bytes else None,
        suggested_display_precision=2 if is_bytes else None,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=DIAG,
        entity_registry_enabled_default=False,
        value_fn=_detail(field),
        exists_fn=_detail_exists(field),
    )


def _table(field: str) -> Callable[[AerohiveData, str], Any]:
    def value(d: AerohiveData, n: str) -> Any:
        iface = d.interfaces.get(n)
        return getattr(iface, field) if iface else None

    return value


INTERFACE_SENSORS: tuple[InterfaceSensorDescription, ...] = (
    InterfaceSensorDescription(
        key="rx_throughput", label="RX throughput", device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=KBPS,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, suggested_display_precision=2,
        value_fn=_tp("rx"), exists_fn=lambda d, n: n in d.throughput,
    ),
    InterfaceSensorDescription(
        key="tx_throughput", label="TX throughput", device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=KBPS,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, suggested_display_precision=2,
        value_fn=_tp("tx"), exists_fn=lambda d, n: n in d.throughput,
    ),
    *(_counter_desc(f) for f in COUNTER_FIELDS),
    InterfaceSensorDescription(
        key="link_speed", label="link speed", device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value_fn=lambda d, n: d.details[n].speed_mbps if n in d.details else None,
        exists_fn=lambda d, n: n in d.details and d.details[n].speed_mbps is not None,
    ),
    InterfaceSensorDescription(
        key="duplex", label="duplex", entity_category=DIAG,
        value_fn=_detail("duplex"), exists_fn=_detail_exists("duplex"),
    ),
    InterfaceSensorDescription(
        key="mode", label="mode", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_table("mode"), exists_fn=lambda d, n: n in d.interfaces,
    ),
    InterfaceSensorDescription(
        key="vlan", label="VLAN", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_table("vlan"), exists_fn=lambda d, n: n in d.interfaces,
    ),
    InterfaceSensorDescription(
        key="hive", label="hive", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_table("hive"), exists_fn=lambda d, n: n in d.interfaces,
    ),
    InterfaceSensorDescription(
        key="mac", label="MAC address", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_table("mac"), exists_fn=lambda d, n: n in d.interfaces and d.interfaces[n].mac is not None,
    ),
)  # fmt: skip


def interface_label(data: AerohiveData, name: str) -> str:
    iface = data.interfaces.get(name)
    if iface is not None and iface.is_radio:
        return radio_label(data, name)
    if iface is not None and iface.parent and iface.ssid:
        return f"{name} ({iface.ssid})"
    return name.capitalize()


class AerohiveInterfaceSensor(AerohiveEntity, SensorEntity):
    entity_description: InterfaceSensorDescription

    def __init__(self, coordinator: AerohiveCoordinator, name: str, description: InterfaceSensorDescription) -> None:
        super().__init__(coordinator, f"iface_{name}_{description.key}")
        self.entity_description = description
        self._iface = name
        self._attr_name = f"{interface_label(coordinator.data, name)} {description.label}"

    @property
    def available(self) -> bool:
        return super().available and (
            self._iface in self.coordinator.data.interfaces or self._iface in self.coordinator.data.details
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, self._iface)


# --------------------------------------------------------------------------
# SSID sensors
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SsidSensorDescription(SensorEntityDescription):
    label: str
    value_fn: Callable[[AerohiveData, str], Any]
    exists_fn: Callable[[AerohiveData, str], bool] = lambda d, s: True


def _ssid_field(attr: str) -> Callable[[AerohiveData, str], Any]:
    def value(d: AerohiveData, s: str) -> Any:
        ssid = d.ssids.get(s)
        return getattr(ssid, attr) if ssid else None

    return value


def _ssid_has_rates(d: AerohiveData, s: str) -> bool:
    return any(i.name in d.throughput for i in d.ssid_subinterfaces(s))


SSID_SENSORS: tuple[SsidSensorDescription, ...] = (
    SsidSensorDescription(
        key="clients", label="clients", state_class=SensorStateClass.MEASUREMENT, icon="mdi:account-multiple",
        value_fn=lambda d, s: d.clients_on_ssid(s),
    ),
    SsidSensorDescription(
        key="rx_throughput", label="RX throughput", device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=KBPS,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, suggested_display_precision=2,
        value_fn=lambda d, s: d.ssid_throughput(s).rx_kbps, exists_fn=_ssid_has_rates,
    ),
    SsidSensorDescription(
        key="tx_throughput", label="TX throughput", device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=KBPS,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, suggested_display_precision=2,
        value_fn=lambda d, s: d.ssid_throughput(s).tx_kbps, exists_fn=_ssid_has_rates,
    ),
    SsidSensorDescription(
        key="dtim_period", label="DTIM period", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_ssid_field("dtim_period"),
    ),
    SsidSensorDescription(
        key="rts", label="RTS threshold", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_ssid_field("rts"),
    ),
    SsidSensorDescription(
        key="frag", label="fragmentation threshold", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=_ssid_field("frag"),
    ),
    SsidSensorDescription(
        key="mac_filter", label="MAC filter", entity_category=DIAG, entity_registry_enabled_default=False,
        value_fn=lambda d, s: _ssid_field("mac_filter")(d, s) or "none",
    ),
)  # fmt: skip


class AerohiveSsidSensor(AerohiveEntity, SensorEntity):
    entity_description: SsidSensorDescription
    _unrecorded_attributes = frozenset({"clients"})

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str, description: SsidSensorDescription) -> None:
        # "ssid_clients_<ssid>" keeps the unique ID of earlier versions.
        key = "ssid_clients" if description.key == "clients" else f"ssid_{description.key}"
        super().__init__(coordinator, f"{key}_{ssid}")
        self.entity_description = description
        self._ssid = ssid
        self._attr_name = f"{ssid} {description.label}"

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssids

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, self._ssid)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "clients":
            return None
        data = self.coordinator.data
        ssid = data.ssids.get(self._ssid)
        stations = [s for s in data.stations.values() if data.station_ssid(s) == self._ssid]
        return extra_attrs(
            max_client=ssid.max_client if ssid else None,
            radios=[r for r in data.radio_names() if data.ssid_on_radio(self._ssid, r)],
            broadcasting=data.ssid_broadcasting(self._ssid),
            clients=_client_list(data, stations),
        )


# --------------------------------------------------------------------------
# Tracked client sensors
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ClientSensorDescription(SensorEntityDescription):
    label: str
    value_fn: Callable[[AerohiveData, Any], Any]


CLIENT_SENSORS: tuple[ClientSensorDescription, ...] = (
    ClientSensorDescription(
        key="signal", label="signal", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, s: s.signal,
    ),
    ClientSensorDescription(
        key="snr", label="SNR", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, s: s.snr,
    ),
    ClientSensorDescription(
        key="tx_rate", label="TX rate", device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, s: parse_rate_mbps(s.tx_rate),
    ),
    ClientSensorDescription(
        key="rx_rate", label="RX rate", device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, s: parse_rate_mbps(s.rx_rate),
    ),
    ClientSensorDescription(key="ssid", label="SSID", value_fn=lambda d, s: d.station_ssid(s)),
    ClientSensorDescription(key="interface", label="interface", value_fn=lambda d, s: s.interface),
    ClientSensorDescription(key="ip", label="IP address", value_fn=lambda d, s: s.ip),
    ClientSensorDescription(key="connected_time", label="connected time", value_fn=lambda d, s: s.assoc_time),
)  # fmt: skip


class AerohiveClientSensor(AerohiveEntity, SensorEntity):
    """Per-client details for MACs opted in under 'Clients to track'."""

    entity_description: ClientSensorDescription

    def __init__(self, coordinator: AerohiveCoordinator, mac: str, description: ClientSensorDescription) -> None:
        super().__init__(coordinator, f"client_{mac}_{description.key}")
        self.entity_description = description
        self._mac = mac
        self._attr_name = f"Client {mac} {description.label}"

    @property
    def available(self) -> bool:
        return super().available and self._mac in self.coordinator.data.stations

    @property
    def native_value(self) -> Any:
        station = self.coordinator.data.stations.get(self._mac)
        return self.entity_description.value_fn(self.coordinator.data, station) if station else None


# --------------------------------------------------------------------------
# Raw "every field" sensors
# --------------------------------------------------------------------------

_COUNTER_HINT = ("bytes", "packets", "frames", "errors", "dropped", "retries", "octets")
_VERSION_MAPPED = {"version", "platform", "uptime"}
# InterfaceDetail fields that have a dedicated entity (anything else gets a raw sensor).
_DETAIL_WITH_ENTITY = {
    "channel", "channel_width", "tx_power", "eirp_power", "noise_floor", "tx_utilization",
    "rx_utilization", "interference_utilization", "total_utilization", "max_clients", "phy_mode",
    "speed", "duplex", "ip_address", "gateway", *COUNTER_FIELDS,
}  # fmt: skip


def _raw_sources(data: AerohiveData) -> dict[str, tuple[str, dict[str, str], set[str]]]:
    """source id -> (label, raw dict, already-mapped keys)."""
    sources: dict[str, tuple[str, dict[str, str], set[str]]] = {
        "version": (
            "Version",
            data.version.raw,
            _VERSION_MAPPED | {k for k in data.version.raw if k.startswith("bootloader")},
        )
    }
    for name, detail in data.details.items():
        mapped = {key for attr, key in detail.mapped_fields.items() if attr in _DETAIL_WITH_ENTITY}
        sources[f"if_{name}"] = (interface_label(data, name), detail.raw, mapped)
    for key, label, obj in (
        ("cpu", "CPU", data.cpu),
        ("memory", "Memory", data.memory),
        ("hw", "Hardware", data.hardware),
        ("boot", "Boot", data.boot_params),
    ):
        if obj is not None:
            sources[key] = (label, obj.raw, set(obj.mapped_keys))
    return sources


class AerohiveRawSensor(AerohiveEntity, SensorEntity):
    """Any other field the CLI reports (disabled by default)."""

    _attr_entity_category = DIAG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AerohiveCoordinator, source: str, label: str, key: str) -> None:
        super().__init__(coordinator, f"raw_{source}_{key}")
        self._source = source
        self._key = key
        self._attr_name = f"{label} {key.replace('_', ' ')}"
        sources = _raw_sources(coordinator.data)
        value, unit = parse_raw_value(sources[source][1].get(key))
        self._numeric = isinstance(value, (int, float))
        if self._numeric:
            self._attr_native_unit_of_measurement = unit
            self._attr_state_class = (
                SensorStateClass.TOTAL_INCREASING
                if any(h in key for h in _COUNTER_HINT)
                else SensorStateClass.MEASUREMENT
            )

    def _raw(self) -> str | None:
        source = _raw_sources(self.coordinator.data).get(self._source)
        return source[1].get(self._key) if source else None

    @property
    def available(self) -> bool:
        return super().available and self._raw() is not None

    @property
    def native_value(self) -> Any:
        value, _ = parse_raw_value(self._raw())
        if self._numeric and not isinstance(value, (int, float)):
            return None
        if not self._numeric and value is not None:
            return str(value)
        return value


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    raw_enabled = entry.options.get(CONF_RAW_SENSORS, DEFAULT_RAW_SENSORS)
    tracked = sorted(
        {m for raw in entry.options.get(CONF_TRACKED_CLIENTS, []) if (m := normalize_mac(raw))}
    )

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for desc in DEVICE_SENSORS:
            if desc.exists_fn(data):
                yield desc.key, lambda desc=desc: AerohiveDeviceSensor(coordinator, desc)
        for radio in data.radio_names():
            for rdesc in RADIO_SENSORS:
                if rdesc.exists_fn(data, radio):
                    yield (
                        f"{radio}_{rdesc.key}",
                        lambda radio=radio, rdesc=rdesc: AerohiveRadioSensor(coordinator, radio, rdesc),
                    )
        for name in sorted(set(data.interfaces) | set(data.details)):
            for idesc in INTERFACE_SENSORS:
                if idesc.exists_fn(data, name):
                    yield (
                        f"iface_{name}_{idesc.key}",
                        lambda name=name, idesc=idesc: AerohiveInterfaceSensor(coordinator, name, idesc),
                    )
        for ssid in data.ssids:
            for sdesc in SSID_SENSORS:
                if sdesc.exists_fn(data, ssid):
                    yield (
                        f"ssid_{sdesc.key}_{ssid}",
                        lambda ssid=ssid, sdesc=sdesc: AerohiveSsidSensor(coordinator, ssid, sdesc),
                    )
        for mac in tracked:
            for cdesc in CLIENT_SENSORS:
                yield (
                    f"client_{mac}_{cdesc.key}",
                    lambda mac=mac, cdesc=cdesc: AerohiveClientSensor(coordinator, mac, cdesc),
                )
        if raw_enabled:
            for source, (label, raw, mapped) in _raw_sources(data).items():
                for key, value in raw.items():
                    if key in mapped or parse_raw_value(value)[0] is None:
                        continue
                    yield (
                        f"raw_{source}_{key}",
                        lambda source=source, label=label, key=key: AerohiveRawSensor(
                            coordinator, source, label, key
                        ),
                    )

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)
