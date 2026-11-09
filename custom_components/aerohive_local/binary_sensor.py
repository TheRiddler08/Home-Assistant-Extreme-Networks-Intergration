"""Binary sensors (interface link state) for Aerohive Local."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, async_add_dynamic_entities, extra_attrs

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for name, iface in data.interfaces.items():
            if iface.parent is not None:
                continue  # sub-interfaces (wifi0.1) are covered by SSID switches
            yield f"iface_{name}", lambda name=name: AerohiveInterfaceBinarySensor(coordinator, name)

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class AerohiveInterfaceBinarySensor(AerohiveEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AerohiveCoordinator, name: str) -> None:
        super().__init__(coordinator, f"iface_{name}")
        self._iface = name
        self._attr_name = f"{name.capitalize()} link"

    @property
    def available(self) -> bool:
        return super().available and self._iface in self.coordinator.data.interfaces

    @property
    def is_on(self) -> bool | None:
        iface = self.coordinator.data.interfaces.get(self._iface)
        if iface is None:
            return None
        if iface.is_radio and iface.channel_raw and iface.channel_raw.lower() == "down":
            return False
        return iface.is_up

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        iface = self.coordinator.data.interfaces.get(self._iface)
        if iface is None:
            return {}
        return extra_attrs(
            mac=iface.mac,
            mode=iface.mode,
            state=iface.state,
            vlan=iface.vlan,
            radio_profile=iface.radio_profile,
            hive=iface.hive,
            channel=iface.channel_raw,
            channel_width=iface.channel_width,
        )
