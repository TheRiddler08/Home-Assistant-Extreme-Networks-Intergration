"""What's known about each connected device (client), from every source.

For every MAC address seen on the network (Wi-Fi clients, devices learned on
switch ports, the router's neighbours) this collects:

- a **name**: your nickname, else the name another Home Assistant integration
  gives the device, the name of its per-device key or onboarding request, its
  DHCP hostname;
- the **manufacturer** from the MAC address (offline OUI list, ``aiooui``), or
  that it's a **private (randomised) address**, as phones use;
- **IP address and hostname** from the access points, the router's DHCP leases
  and ARP tables of routers and switches;
- the **Home Assistant device** that has this MAC (model, manufacturer, area);
- a guess of the **kind of device**.

Nicknames are stored in ``.storage/aerohive_local.client_names``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.client_names"
DATA_NAMES = f"{DOMAIN}_client_names"
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# (kind, words found in the name, hostname, model or manufacturer). First match wins.
KIND_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("network", ("aerohive", "extreme networks", "ubiquiti", "unifi", "mikrotik", "openwrt", "netgear", "tp-link", "access point", "switch", "router")),
    ("tv", ("tv", "roku", "chromecast", "firetv", "fire tv", "apple tv", "appletv", "shield", "bravia", "webos", "tizen", "vizio", "hisense")),
    ("speaker", ("echo", "alexa", "sonos", "homepod", "google home", "nest mini", "nest audio", "nest hub", "speaker", "bose")),
    ("console", ("playstation", "ps4", "ps5", "xbox", "nintendo", "switch-")),
    ("printer", ("printer", "epson", "brother", "canon", "laserjet", "officejet", "deskjet")),
    ("camera", ("camera", "cam-", "-cam", "reolink", "hikvision", "dahua", "axis", "amcrest", "ring", "wyze", "doorbell")),
    ("tablet", ("ipad", "tablet", "galaxy tab", "kindle", "fire hd")),
    ("phone", ("iphone", "android", "pixel", "galaxy", "oneplus", "xiaomi", "redmi", "huawei", "oppo", "motorola", "phone")),
    ("computer", ("macbook", "imac", "mac mini", "laptop", "desktop", "thinkpad", "surface", "windows", "-pc", "pc-", "dell", "lenovo", "intel corporate")),
    ("smart home", ("espressif", "tuya", "shelly", "sonoff", "tasmota", "esphome", "philips lighting", "signify", "hue", "ikea", "nest", "ecobee", "raspberry")),
]


def is_private_mac(mac: str) -> bool:
    """A locally administered (randomised) address: the second bit of the first byte is set."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def guess_kind(*texts: str | None) -> str | None:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    for kind, words in KIND_HINTS:
        if any(word in blob for word in words):
            return kind
    return None


def vendor(mac: str) -> str | None:
    """Manufacturer from the MAC address (None when unknown or a private address)."""
    if is_private_mac(mac):
        return None
    try:
        import aiooui  # noqa: PLC0415 - loaded at setup

        return aiooui.get_vendor(mac) if aiooui.is_loaded() else None
    except Exception:  # noqa: BLE001 - a lookup must never break the snapshot
        return None


async def async_setup_clients(hass: HomeAssistant) -> None:
    """Load the manufacturer list and the nicknames."""
    try:
        import aiooui  # noqa: PLC0415

        if not aiooui.is_loaded():
            await aiooui.async_load()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Manufacturer lookup unavailable: %s", err)
    if DATA_NAMES not in hass.data:
        store: Store[dict[str, str]] = Store(hass, 1, STORAGE_KEY)
        hass.data[DATA_NAMES] = (store, dict(await store.async_load() or {}))


def nicknames(hass: HomeAssistant) -> dict[str, str]:
    return hass.data.get(DATA_NAMES, (None, {}))[1]


async def async_set_nickname(hass: HomeAssistant, mac: str, name: str | None) -> None:
    mac = (mac or "").strip().lower().replace("-", ":")
    if not _MAC_RE.match(mac):
        raise ValueError("Invalid MAC address")
    name = (name or "").strip()[:64]
    store, names = hass.data[DATA_NAMES]
    if name:
        names[mac] = name
    else:
        names.pop(mac, None)
    await store.async_save(names)


def _ha_devices(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Devices other integrations know, by MAC address."""
    registry = dr.async_get(hass)
    areas = ar.async_get(hass)
    result: dict[str, dict[str, Any]] = {}
    for device in registry.devices.values():
        macs = [value.lower() for kind, value in device.connections if kind == dr.CONNECTION_NETWORK_MAC]
        if not macs:
            continue
        area = areas.async_get_area(device.area_id) if device.area_id else None
        info = {
            "id": device.id,
            "name": device.name_by_user or device.name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "area": area.name if area else None,
        }
        for mac in macs:
            result.setdefault(mac, info)
    return result


def _trackers(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """IP address and hostname from device trackers of other integrations."""
    result: dict[str, dict[str, Any]] = {}
    for state in hass.states.async_all("device_tracker"):
        mac = str(state.attributes.get("mac") or "").lower()
        if _MAC_RE.match(mac):
            result[mac] = {"ip": state.attributes.get("ip"), "hostname": state.attributes.get("host_name")}
    return result


def directory(hass: HomeAssistant, devices: list[dict[str, Any]], plan: dict[str, Any] | None, requests: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Everything known per client MAC, for the devices in a network snapshot."""
    macs: dict[str, dict[str, Any]] = {}
    infra = {str(d.get("mac") or "").lower() for d in devices}
    for device in devices:
        for sta in device.get("stations") or []:
            entry = macs.setdefault(sta["mac"], {"ip": None, "hostname": None})
            entry["ip"] = entry["ip"] or sta.get("ip")
            entry["hostname"] = entry["hostname"] or sta.get("hostname")
        for port in device.get("ports") or []:
            if port.get("uplink"):
                continue
            for client in port.get("clients") or []:
                macs.setdefault(client["mac"], {"ip": None, "hostname": None})
        for mac in device.get("lan_clients") or []:
            macs.setdefault(mac, {"ip": None, "hostname": None})
        for mac, seen in (device.get("neighbors") or {}).items():
            entry = macs.get(mac)
            if entry is None:
                continue  # only devices actually connected somewhere get a card
            entry["ip"] = entry["ip"] or seen.get("ip")
            entry["hostname"] = entry["hostname"] or seen.get("hostname")
    # Router/switch ARP and DHCP may know devices that only show on another device's port.
    neighbors: dict[str, dict[str, Any]] = {}
    for device in devices:
        for mac, seen in (device.get("neighbors") or {}).items():
            n = neighbors.setdefault(mac, {"ip": None, "hostname": None})
            n["ip"] = n["ip"] or seen.get("ip")
            n["hostname"] = n["hostname"] or seen.get("hostname")
    names = nicknames(hass)
    ha = _ha_devices(hass)
    trackers = _trackers(hass)
    keys = {str(k.get("mac") or "").lower(): k for k in (plan or {}).get("keys", []) if k.get("mac")}
    wifi_names = {w["id"]: w["ssid"] for w in (plan or {}).get("wifi", [])}
    asked = {str(r.get("mac") or "").lower(): r for r in (requests or {}).values()}
    result: dict[str, dict[str, Any]] = {}
    for mac, seen in macs.items():
        if mac in infra:
            continue
        other = neighbors.get(mac, {})
        tracked = trackers.get(mac, {})
        ip = seen["ip"] or other.get("ip") or tracked.get("ip")
        hostname = seen["hostname"] or other.get("hostname") or tracked.get("hostname")
        device = ha.get(mac)
        key = keys.get(mac)
        request = asked.get(mac)
        maker = vendor(mac)
        candidates = [
            (names.get(mac), "nickname"),
            (device and device["name"], "home_assistant"),
            (key and key.get("name"), "key"),
            (request and request.get("name"), "onboarding"),
            (hostname if hostname and hostname != "*" else None, "hostname"),
        ]
        name, source = next(((n, s) for n, s in candidates if n), (None, None))
        result[mac] = {
            "name": name,
            "name_source": source,
            "nickname": names.get(mac),
            "hostname": hostname if hostname != "*" else None,
            "ip": ip,
            "vendor": maker,
            "private": is_private_mac(mac),
            "kind": guess_kind(name, hostname, device and device.get("model"), device and device.get("manufacturer"), maker),
            "ha_device": device,
            "key": {"name": key.get("name"), "wifi": wifi_names.get(key.get("wifi"))} if key else None,
        }
    return result
