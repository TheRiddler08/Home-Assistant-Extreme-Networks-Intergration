"""OPNsense routers through the REST API: parsing, polling, the config flow and the HTTP client."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.aerohive_local import opnsense as opn

from .conftest import HAS_HA, load_fixture


def fx(name: str) -> Any:
    return json.loads(load_fixture(f"opnsense/{name}"))


RESPONSES = {
    opn.SYSTEM_INFORMATION: "system_information.json",
    opn.SYSTEM_RESOURCES: "system_resources.json",
    opn.SYSTEM_TEMPERATURE: "system_temperature.json",
    opn.ACTIVITY: "activity.json",
    opn.INTERFACES: "interfaces_info.json",
    opn.GATEWAYS: "gateways.json",
    opn.ARP: "arp.json",
    "kea/leases4/search": "kea_leases.json",
}


def test_parsers() -> None:
    info = opn.parse_system_information(fx("system_information.json"))
    assert (info.hostname, info.version, info.firmware) == ("opnsense", "OPNsense 25.7.3-amd64", "25.7.3")
    assert opn.parse_memory(fx("system_resources.json")) == (25165824, 2097152)
    assert opn.parse_temperature(fx("system_temperature.json")) == 44.0
    activity = opn.parse_activity(fx("activity.json"))
    assert activity.cpu_percent == 3.6 and activity.uptime_seconds == ((3 * 24 + 4) * 60 + 12) * 60 + 33 and activity.load == [0.21, 0.18, 0.15]
    ports = opn.parse_interfaces(fx("interfaces_info.json"))
    assert list(ports) == ["igc0", "igc1", "igc2", "igc3"]  # physical only: no VLAN, no loopback
    assert ports["igc0"].wan and ports["igc0"].speed == 1000 and ports["igc0"].label == "WAN" and ports["igc0"].rx_bytes == 912345678
    assert ports["igc1"].speed == 2500 and ports["igc1"].duplex == "full" and ports["igc1"].ipv4 == "192.168.1.1/24" and not ports["igc1"].wan
    assert ports["igc2"].up is False and ports["igc2"].label is None
    networks = opn.assigned_networks(fx("interfaces_info.json"))
    assert {"name": "IoT", "device": "igc1.30", "address": "10.15.19.1/24", "vlan": 30} in networks
    gateways = opn.parse_gateways(fx("gateways.json"))
    assert [(g.name, g.online, g.delay) for g in gateways] == [("WAN_DHCP", True, "4.2 ms"), ("STARLINK_DHCP", False, "~")]
    arp = opn.parse_arp(fx("arp.json"))
    assert [n.mac for n in arp] == ["02:11:22:33:44:01", "b8:27:eb:12:34:56", "02:ee:00:00:00:01"]  # the router's own (permanent) entry is skipped
    assert arp[0].hostname == "pixel-8.home.arpa" and arp[1].hostname is None
    assert opn.parse_leases(fx("kea_leases.json"))["b8:27:eb:12:34:56"] == ("10.15.19.20", "pihole")
    assert opn.parse_media("Ethernet 10Gbase-SR <full-duplex>") == (10000, "full")
    assert opn.parse_interfaces({"rows": "nonsense"}) == {} and opn.parse_gateways(None) == []


if HAS_HA:
    from homeassistant.config_entries import SOURCE_USER
    from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.exceptions import HomeAssistantError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aerohive_local.const import CONF_API_SECRET, CONF_DEVICE_TYPE, DOMAIN
    from custom_components.aerohive_local.snapshot import network_snapshot

    from .test_init import fake_client  # noqa: F401

needs_ha = pytest.mark.skipif(not HAS_HA, reason="needs pytest-homeassistant-custom-component")

ENTRY_DATA = {
    "device_type": "opnsense", "host": "192.168.1.1", "port": 443, "api_key": "key", "api_secret": "s3cret-api", "verify_ssl": False,
}  # fmt: skip


class FakeRouter:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.missing: set[str] = set()
        self.refuse = False

    async def request(self, client: opn.OPNsenseClient, method: str, path: str, payload: Any = None, **kwargs: Any) -> Any:
        self.requests.append((method, path))
        if self.refuse:
            raise opn.OPNsenseAuthError("refused")
        if path in self.missing or path not in RESPONSES and path != opn.REBOOT:
            raise opn.OPNsenseError(f"HTTP 404 for {path}")
        return {"status": "ok"} if path == opn.REBOOT else fx(RESPONSES[path])


@pytest.fixture
def router():
    fake = FakeRouter()

    async def request(self, method, path, payload=None, **kwargs):
        return await fake.request(self, method, path, payload, **kwargs)

    with patch.object(opn.OPNsenseClient, "request", request):
        yield fake


@pytest.fixture
async def opn_entry(hass: HomeAssistant, router):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="02:5a:00:00:01:01", title="opnsense", minor_version=3, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@needs_ha
async def test_opnsense_router_is_read(hass: HomeAssistant, router, opn_entry: MockConfigEntry) -> None:
    coordinator = opn_entry.runtime_data
    data = coordinator.data
    assert data.cli == "opnsense" and data.hostname == "opnsense" and data.version.firmware == "25.7.3"
    assert data.cpu.cpu_total == 3.6 and data.environment.temperature_c == 44.0
    assert data.mac == "02:5a:00:00:01:01" and data.mgt.ip_address == "192.168.1.1"
    assert data.ports() == ["igc0", "igc1", "igc2", "igc3"] and data.interfaces["igc0"].extra["role"] == "WAN"
    snap = network_snapshot(hass)
    device = next(d for d in snap["devices"] if d["entry_id"] == opn_entry.entry_id)
    assert device["kind"] == "router" and device["features"]["save"] is False and device["features"]["port_control"] is False
    assert [g["name"] for g in device["gateways"]] == ["WAN_DHCP", "STARLINK_DHCP"]
    assert {"kind": "gateway", "text": "Gateway STARLINK_DHCP is down"} in device["problems"]
    assert any(n["vlan"] == 30 for n in device["networks"])
    # Clients behind the router: names and IPs from ARP and DHCP leases; the WAN gateway isn't one.
    clients = snap["clients"]
    assert clients["02:11:22:33:44:01"]["ip"] == "192.168.1.101" and clients["02:11:22:33:44:01"]["name"] == "pixel-8.home.arpa"
    assert clients["b8:27:eb:12:34:56"]["hostname"] == "pihole" and "02:ee:00:00:00:01" not in clients
    # The SSH-only features say so.
    with pytest.raises(HomeAssistantError, match="OPNsense"):
        await coordinator.async_set_port_enabled("igc2", False)
    with pytest.raises(HomeAssistantError, match="run_show_command isn't supported on OPNsense"):
        await coordinator.async_run_show("show version")
    # Reboot goes through the API.
    await coordinator.async_reboot()
    assert ("POST", opn.REBOOT) in router.requests


@needs_ha
async def test_opnsense_optional_endpoints_and_auth(hass: HomeAssistant, router) -> None:
    router.missing = {opn.ACTIVITY, opn.SYSTEM_TEMPERATURE, "kea/leases4/search"}
    entry = MockConfigEntry(domain=DOMAIN, unique_id="x", title="opnsense", minor_version=3, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    data = entry.runtime_data.data
    assert data.cpu is None and data.version.uptime_seconds is None and data.hostname == "opnsense"
    assert ("POST", "dnsmasq/leases/search") in router.requests  # tried the next DHCP server
    router.refuse = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


@needs_ha
async def test_opnsense_as_setup_router(hass: HomeAssistant, router, opn_entry: MockConfigEntry) -> None:
    from custom_components.aerohive_local import opnsense_setup
    from custom_components.aerohive_local.plan import get_plan

    manager = get_plan(hass)
    device = next(d for d in manager.devices() if d["entry_id"] == opn_entry.entry_id)
    assert device["kind"] == "opnsense" and device["router_capable"] and device["role"] == "none"
    plan = {
        "networks": [{"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.19.1/24"}],
        "devices": {"router": opn_entry.entry_id, "router_trunks": ["igc2"]},
    }
    assert await manager.async_save_plan(plan, {}) == []
    preview = manager.preview()["devices"][0]
    assert preview["role"] == "router" and preview["errors"] == []
    assert preview["commands"][0] == "VLAN vlan0.30 on igc2 (tag 30)"
    assert any("no undo" in w for w in preview["warnings"])

    seen = {}

    async def fake_apply(client, plan, trunks):
        seen.update(client=client, trunks=trunks, vlans=[n["vlan"] for n in plan["networks"]])
        return opnsense_setup.Result(done=["VLAN vlan0.30"], warnings=["No DHCP server"])

    with patch.object(opnsense_setup, "async_apply", fake_apply):
        results = await manager.async_apply()
    assert results[0]["ok"] and results[0]["done"] == ["VLAN vlan0.30", "note: No DHCP server"]
    assert seen["trunks"] == ["igc2"] and seen["vlans"] == [30] and isinstance(seen["client"], opn.OPNsenseClient)

    async def failing(client, plan, trunks):
        raise opnsense_setup.SetupError("VLAN 30: the router refused it")

    with patch.object(opnsense_setup, "async_apply", failing):
        results = await manager.async_apply()
    assert not results[0]["ok"] and "refused it. What this apply added was removed again" in results[0]["error"]

    # The onboarding portal needs OpenWrt.
    plan["onboarding"] = {"enabled": True}
    await manager.async_save_plan(plan, {})
    assert any("OpenWrt router" in e for e in manager.preview()["devices"][0]["errors"])


@needs_ha
async def test_opnsense_config_flow(hass: HomeAssistant, router) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert "opnsense" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "opnsense"})
    assert result["step_id"] == "opnsense"
    router.refuse = True
    form = {CONF_HOST: "192.168.1.1", CONF_API_KEY: "key", CONF_API_SECRET: "bad", CONF_VERIFY_SSL: False}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["errors"] == {"base": "invalid_auth"}
    router.refuse = False
    with patch("custom_components.aerohive_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**form, CONF_API_SECRET: "s3cret-api"})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "opnsense"
    assert result["data"][CONF_DEVICE_TYPE] == "opnsense" and result["data"][CONF_API_SECRET] == "s3cret-api"
    assert result["result"].unique_id == "02:5a:00:00:01:01"


@needs_ha
async def test_http_client(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = opn.OPNsenseClient(async_get_clientsession(hass), "192.168.1.1", 443, "key", "secret", verify_ssl=False)
    aioclient_mock.get("https://192.168.1.1/api/diagnostics/system/system_information", json=fx("system_information.json"))
    aioclient_mock.post("https://192.168.1.1/api/core/system/reboot", json={"status": "ok"})
    aioclient_mock.get("https://192.168.1.1/api/routes/gateway/status", status=401)
    aioclient_mock.get("https://192.168.1.1/api/diagnostics/interface/get_arp", status=500)
    assert (await client.get(opn.SYSTEM_INFORMATION))["name"] == "opnsense.home.arpa"
    await client.reboot()
    assert aioclient_mock.mock_calls[1][0] == "POST"
    with pytest.raises(opn.OPNsenseAuthError):
        await client.get(opn.GATEWAYS)
    with pytest.raises(opn.OPNsenseError, match="HTTP 500"):
        await client.get(opn.ARP)
    with pytest.raises(opn.OPNsenseError):
        await client.run("show version")
    port = opn.OPNsenseClient(async_get_clientsession(hass), "fw", 8443, "k", "s")
    assert port._base == "https://fw:8443/api"
