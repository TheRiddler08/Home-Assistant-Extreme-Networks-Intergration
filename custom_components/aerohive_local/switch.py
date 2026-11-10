"""Switches for Aerohive Local: SSIDs, hidden SSIDs, switch ports and PoE."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
        radios = data.radio_names()
        for ssid in data.ssids:
            for radio in radios:
                yield (
                    f"ssid_{radio}_{ssid}",
                    lambda radio=radio, ssid=ssid: AerohiveSsidSwitch(coordinator, radio, ssid),
                )
            if len(radios) > 1:
                yield f"ssid_all_{ssid}", lambda ssid=ssid: AerohiveSsidAllRadiosSwitch(coordinator, ssid)
            if data.config is not None:
                yield f"ssid_hidden_{ssid}", lambda ssid=ssid: AerohiveSsidHiddenSwitch(coordinator, ssid)
        if data.has_switch_ports:
            for port in data.ports():
                yield f"port_{port}", lambda port=port: AerohivePortSwitch(coordinator, port)
        for port in data.poe_ports:
            yield f"poe_{port}", lambda port=port: AerohivePoeSwitch(coordinator, port)

    async_add_dynamic_entities(coordinator, entry, async_add_entities, factory)


class _DebouncedSwitch(AerohiveEntity, SwitchEntity):
    """Optimistic while debouncing; only sends a command if the state differs."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AerohiveCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._pending: bool | None = None
        self._setter: DebouncedSetter[bool] = DebouncedSetter(self._apply)

    def _device_state(self) -> bool | None:
        raise NotImplementedError

    async def _async_set(self, enable: bool) -> None:
        raise NotImplementedError

    @property
    def is_on(self) -> bool | None:
        if self._pending is not None:
            return self._pending
        return self._device_state()

    async def _apply(self, enable: bool) -> None:
        try:
            if self._device_state() != enable:
                await self._async_set(enable)
        finally:
            self._pending = None
            self.async_write_ha_state()

    async def _request(self, enable: bool) -> None:
        self._pending = enable
        self.async_write_ha_state()
        await self._setter.async_request(enable)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._request(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._request(False)


class AerohiveSsidSwitch(_DebouncedSwitch):
    """On = 'interface wifiN ssid X', off = 'no interface wifiN ssid X'."""

    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: AerohiveCoordinator, radio: str, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_{radio}_{ssid}")
        self._radio = radio
        self._ssid = ssid
        self._attr_name = f"{ssid} on {radio_label(coordinator.data, radio)}"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and self._ssid in data.ssids and self._radio in data.interfaces

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"radio": self._radio, "ssid": self._ssid}

    def _device_state(self) -> bool:
        return self.coordinator.data.ssid_on_radio(self._ssid, self._radio)

    async def _async_set(self, enable: bool) -> None:
        await self.coordinator.async_set_ssid_on_radio(self._radio, self._ssid, enable)


class AerohiveSsidAllRadiosSwitch(_DebouncedSwitch):
    """Broadcast the SSID on all radios (on) or none (off)."""

    _attr_icon = "mdi:wifi-star"

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_all_{ssid}")
        self._ssid = ssid
        self._attr_name = f"{ssid} on all radios"

    @property
    def available(self) -> bool:
        return super().available and self._ssid in self.coordinator.data.ssids

    def _device_state(self) -> bool:
        return self.coordinator.data.ssid_enabled(self._ssid)

    async def _apply(self, enable: bool) -> None:
        # Always (re)apply: "off" must unbind partially bound SSIDs too.
        try:
            await self.coordinator.async_set_ssid_all_radios(self._ssid, enable)
        finally:
            self._pending = None
            self.async_write_ha_state()


class AerohiveSsidHiddenSwitch(_DebouncedSwitch):
    """'ssid X hide-ssid': stop advertising the SSID name in beacons."""

    _attr_icon = "mdi:wifi-lock"

    def __init__(self, coordinator: AerohiveCoordinator, ssid: str) -> None:
        super().__init__(coordinator, f"ssid_hidden_{ssid}")
        self._ssid = ssid
        self._attr_name = f"{ssid} hidden"

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and self._ssid in data.ssids and data.config is not None

    def _device_state(self) -> bool | None:
        return self.coordinator.data.ssid_hidden(self._ssid)

    async def _async_set(self, enable: bool) -> None:
        await self.coordinator.async_set_ssid_hidden(self._ssid, enable)


class AerohivePortSwitch(_DebouncedSwitch):
    """Administratively enable/disable an Ethernet port.

    Disabled by default: shutting down the port Home Assistant uses to reach
    the device would lock you out until someone re-enables it on the console.
    """

    _attr_icon = "mdi:ethernet"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AerohiveCoordinator, port: str) -> None:
        super().__init__(coordinator, f"port_{port}")
        self._port = port
        self._attr_name = f"{port.capitalize()} enabled"

    @property
    def available(self) -> bool:
        return super().available and self._port in self.coordinator.data.interfaces

    def _device_state(self) -> bool | None:
        return self.coordinator.data.port_enabled(self._port)

    async def _async_set(self, enable: bool) -> None:
        await self.coordinator.async_set_port_enabled(self._port, enable)


class AerohivePoeSwitch(_DebouncedSwitch):
    """PoE on/off per port ('interface <port> pse shutdown')."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_icon = "mdi:power-plug"

    def __init__(self, coordinator: AerohiveCoordinator, port: str) -> None:
        super().__init__(coordinator, f"poe_{port}")
        self._port = port
        self._attr_name = f"{port.capitalize()} PoE"

    @property
    def available(self) -> bool:
        return super().available and self._port in self.coordinator.data.poe_ports

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        poe = self.coordinator.data.poe_ports.get(self._port)
        if poe is None:
            return {}
        return {k: v for k, v in {
            "status": poe.status, "power_w": poe.power_w, "class": poe.poe_class,
            "priority": poe.priority, "max_power_w": poe.max_power_w,
        }.items() if v is not None}  # fmt: skip

    def _device_state(self) -> bool | None:
        return self.coordinator.data.poe_enabled(self._port)

    async def _async_set(self, enable: bool) -> None:
        await self.coordinator.async_set_poe(self._port, enable)
