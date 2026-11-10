"""Buttons for Aerohive Local: reboot and PoE power cycle."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, async_add_dynamic_entities, port_label
from .overview import async_setup_platform_entities, is_overview

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if is_overview(entry):
        await async_setup_platform_entities("button", hass, entry, async_add_entities)
        return
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        yield "reboot", lambda: AerohiveRebootButton(coordinator)

        for port in data.poe_ports:
            yield f"poe_cycle_{port}", lambda port=port: AerohivePoeCycleButton(coordinator, port)

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class AerohiveRebootButton(AerohiveEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "reboot"

    def __init__(self, coordinator: AerohiveCoordinator) -> None:
        super().__init__(coordinator, "reboot")

    async def async_press(self) -> None:
        await self.coordinator.async_reboot()


class AerohivePoeCycleButton(AerohiveEntity, ButtonEntity):
    """Turn PoE off for 5 s and back on, e.g. to reboot a hung AP or camera."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AerohiveCoordinator, port: str) -> None:
        super().__init__(coordinator, f"poe_cycle_{port}")
        self._port = port
        self._attr_name = f"{port_label(port)} PoE power cycle"

    @property
    def available(self) -> bool:
        return super().available and self._port in self.coordinator.data.poe_ports

    async def async_press(self) -> None:
        await self.coordinator.async_power_cycle_poe(self._port)
