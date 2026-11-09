"""SSID-on-radio switches for Aerohive Local."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AerohiveConfigEntry, AerohiveCoordinator, AerohiveData
from .entity import AerohiveEntity, DebouncedSetter, async_add_dynamic_entities, radio_label

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AerohiveConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data

    def factory(data: AerohiveData) -> Iterable[tuple[str, Callable[[], Entity]]]:
        for radio in data.radio_names():
            for ssid in data.ssids:
                yield (
                    f"ssid_{radio}_{ssid}",
                    lambda radio=radio, ssid=ssid: AerohiveSsidSwitch(coordinator, radio, ssid),
                )

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class AerohiveSsidSwitch(AerohiveEntity, SwitchEntity):
    """On = 'interface wifiN ssid X', off = 'no interface wifiN ssid X'."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: AerohiveCoordinator, radio: str, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_{radio}_{ssid}")
        self._radio = radio
        self._ssid = ssid
        self._attr_name = f"{ssid} on {radio_label(coordinator.data, radio)}"
        self._pending: bool | None = None
        self._setter: DebouncedSetter[bool] = DebouncedSetter(self._apply)

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and self._ssid in data.ssids and self._radio in data.interfaces

    @property
    def is_on(self) -> bool:
        if self._pending is not None:
            return self._pending
        return self.coordinator.data.ssid_on_radio(self._ssid, self._radio)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"radio": self._radio, "ssid": self._ssid}

    async def _apply(self, enable: bool) -> None:
        try:
            if self.coordinator.data.ssid_on_radio(self._ssid, self._radio) != enable:
                await self.coordinator.async_set_ssid_on_radio(self._radio, self._ssid, enable)
        finally:
            self._pending = None
            self.async_write_ha_state()

    async def _request(self, enable: bool) -> None:
        self._pending = enable  # optimistic while debouncing
        self.async_write_ha_state()
        await self._setter.async_request(enable)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._request(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._request(False)
