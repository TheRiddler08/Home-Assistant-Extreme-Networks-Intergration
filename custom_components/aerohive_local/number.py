"""Per-SSID max-client number entities for Aerohive Local."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, DebouncedSetter, async_add_dynamic_entities

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for ssid in data.ssids:
            yield f"max_client_{ssid}", lambda ssid=ssid: AerohiveMaxClientNumber(coordinator, ssid)

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class AerohiveMaxClientNumber(AerohiveEntity, NumberEntity):
    """'ssid <name> max-client <n>'."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_icon = "mdi:account-multiple-plus"

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"max_client_{ssid}")
        self._ssid = ssid
        self._attr_name = f"{ssid} max clients"
        self._pending: int | None = None
        self._setter: DebouncedSetter[int] = DebouncedSetter(self._apply)

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssids

    @property
    def native_value(self) -> int | None:
        if self._pending is not None:
            return self._pending
        ssid = self.coordinator.data.ssids.get(self._ssid)
        return ssid.max_client if ssid else None

    async def _apply(self, value: int) -> None:
        try:
            ssid = self.coordinator.data.ssids.get(self._ssid)
            if ssid is None or ssid.max_client != value:
                await self.coordinator.async_set_max_client(self._ssid, value)
        finally:
            self._pending = None
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        self._pending = int(value)
        self.async_write_ha_state()
        await self._setter.async_request(int(value))
