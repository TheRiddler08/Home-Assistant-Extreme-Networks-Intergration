"""Binary sensors (interface link state) for Aerohive Local."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, async_add_dynamic_entities, extra_attrs, port_label
from .overview import async_setup_platform_entities, is_overview

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if is_overview(entry):
        await async_setup_platform_entities("binary_sensor", hass, entry, async_add_entities)
        return
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for name, iface in data.interfaces.items():
            if iface.parent is not None:
                continue  # sub-interfaces (wifi0.1) are covered by SSID switches
            yield f"iface_{name}", lambda name=name: AerohiveInterfaceBinarySensor(coordinator, name)
        for ssid in data.ssids:
            yield f"ssid_broadcasting_{ssid}", lambda ssid=ssid: AerohiveSsidBroadcastingBinarySensor(coordinator, ssid)
        for desc in PROBLEM_SENSORS:
            if desc.exists_fn(data):
                yield desc.key, lambda desc=desc: AerohiveProblemBinarySensor(coordinator, desc)

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


@dataclass(frozen=True, kw_only=True)
class ProblemDescription(BinarySensorEntityDescription):
    value_fn: Callable[[AerohiveData], bool | None]
    exists_fn: Callable[[AerohiveData], bool]
    attrs_fn: Callable[[AerohiveData], dict[str, Any]] | None = None


PROBLEM_SENSORS: tuple[ProblemDescription, ...] = (
    ProblemDescription(
        key="config_changed_after_reboot", translation_key="config_changed_after_reboot",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.backup.changed_after_reboot if d.backup else None,
        exists_fn=lambda d: d.backup is not None,
        attrs_fn=lambda d: extra_attrs(
            backup=d.backup.last_file,
            lines_added=(d.backup.change_summary or {}).get("added"),
            lines_removed=(d.backup.change_summary or {}).get("removed"),
            changes=(d.backup.change_summary or {}).get("sample"),
        ) if d.backup else {},
    ),
    ProblemDescription(
        key="boot_config_problem", translation_key="boot_config_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: bool(d.exos.boot_problems) if d.exos else None,
        exists_fn=lambda d: d.exos is not None,
        attrs_fn=lambda d: {"problems": d.exos.boot_problems} if d.exos else {},
    ),
    ProblemDescription(
        key="unsaved_config", translation_key="unsaved_config",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.config_unsaved,
        exists_fn=lambda d: d.config_unsaved is not None,
    ),
)  # fmt: skip


class AerohiveProblemBinarySensor(AerohiveEntity, BinarySensorEntity):
    entity_description: ProblemDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AerohiveCoordinator, description: ProblemDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


class AerohiveInterfaceBinarySensor(AerohiveEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AerohiveCoordinator, name: str) -> None:
        super().__init__(coordinator, f"iface_{name}")
        self._iface = name
        self._attr_name = f"{port_label(name)} link"

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


class AerohiveSsidBroadcastingBinarySensor(AerohiveEntity, BinarySensorEntity):
    """On while the SSID is bound to at least one radio that is up."""

    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_broadcasting_{ssid}")
        self._ssid = ssid
        self._attr_name = f"{ssid} broadcasting"

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssids

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.ssid_broadcasting(self._ssid)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        ssid = data.ssids.get(self._ssid)
        subs = data.ssid_subinterfaces(self._ssid)
        return extra_attrs(
            radios=sorted({s.parent for s in subs if s.parent}),
            interfaces=sorted(s.name for s in subs),
            clients=data.clients_on_ssid(self._ssid),
            max_client=ssid.max_client if ssid else None,
            dtim_period=ssid.dtim_period if ssid else None,
            rts=ssid.rts if ssid else None,
            frag=ssid.frag if ssid else None,
            mac_filter=ssid.mac_filter if ssid else None,
        )
