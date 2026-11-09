"""Sensors for Aerohive Local."""

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
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, async_add_dynamic_entities, extra_attrs, radio_label

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DeviceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AerohiveData], Any]
    exists_fn: Callable[[AerohiveData], bool] = lambda _: True
    attrs_fn: Callable[[AerohiveData], dict[str, Any]] | None = None


DEVICE_SENSORS: tuple[DeviceSensorDescription, ...] = (
    DeviceSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.version.version,
        attrs_fn=lambda d: extra_attrs(
            firmware=d.version.firmware,
            build=d.version.build,
            os=d.version.os_name,
            bootloader=d.version.bootloader,
        ),
    ),
    DeviceSensorDescription(
        key="platform",
        translation_key="platform",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.version.platform,
    ),
    DeviceSensorDescription(
        key="boot_time",
        translation_key="boot_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.boot_time,
    ),
    DeviceSensorDescription(
        key="clients",
        translation_key="clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.stations),
    ),
    DeviceSensorDescription(
        key="region_code",
        translation_key="region_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.boot_params.region_code if d.boot_params else None,
        exists_fn=lambda d: bool(d.boot_params and d.boot_params.region_code),
    ),
    DeviceSensorDescription(
        key="country_code",
        translation_key="country_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.boot_params.country_code if d.boot_params else None,
        exists_fn=lambda d: bool(d.boot_params and d.boot_params.country_code),
    ),
)


@dataclass(frozen=True, kw_only=True)
class RadioSensorDescription(SensorEntityDescription):
    """Sensor bound to one radio; value_fn gets (data, radio)."""

    label: str
    value_fn: Callable[[AerohiveData, str], Any]
    exists_fn: Callable[[AerohiveData, str], bool] = lambda d, r: True


def _detail(attr: str) -> Callable[[AerohiveData, str], Any]:
    def value(data: AerohiveData, radio: str) -> Any:
        detail = data.radios.get(radio)
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
        key="eirp_power", label="EIRP power", native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT,
        value_fn=_detail("eirp_power"), exists_fn=_detail_exists("eirp_power"),
    ),
    RadioSensorDescription(
        key="tx_power", label="TX power", native_unit_of_measurement="dBm",
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
        key="max_clients", label="max clients", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_detail("max_clients"), exists_fn=_detail_exists("max_clients"),
    ),
)  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for desc in DEVICE_SENSORS:
            if desc.exists_fn(data):
                yield desc.key, lambda desc=desc: AerohiveDeviceSensor(coordinator, desc)
        for ssid in data.ssids:
            yield f"ssid_clients_{ssid}", lambda ssid=ssid: AerohiveSsidClientsSensor(coordinator, ssid)
        for radio in data.radio_names():
            for desc in RADIO_SENSORS:
                if desc.exists_fn(data, radio):
                    yield (
                        f"{radio}_{desc.key}",
                        lambda radio=radio, desc=desc: AerohiveRadioSensor(coordinator, radio, desc),
                    )

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class AerohiveDeviceSensor(AerohiveEntity, SensorEntity):
    entity_description: DeviceSensorDescription

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


class AerohiveSsidClientsSensor(AerohiveEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-multiple"

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_clients_{ssid}")
        self._ssid = ssid
        self._attr_name = f"{ssid} clients"

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssids

    @property
    def native_value(self) -> int:
        return self.coordinator.data.clients_on_ssid(self._ssid)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        ssid = data.ssids.get(self._ssid)
        return extra_attrs(
            max_client=ssid.max_client if ssid else None,
            radios=[r for r in data.radio_names() if data.ssid_on_radio(self._ssid, r)],
        )


class AerohiveRadioSensor(AerohiveEntity, SensorEntity):
    entity_description: RadioSensorDescription

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
        if self.entity_description.key != "channel":
            return None
        iface = self.coordinator.data.interfaces.get(self._radio)
        detail = self.coordinator.data.radios.get(self._radio)
        return extra_attrs(
            radio_profile=iface.radio_profile if iface else None,
            mode=iface.mode if iface else None,
            phy_mode=detail.phy_mode if detail else None,
        )
