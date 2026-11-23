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
        self.sets: list[tuple[str, Any]] = []

    async def request(self, client: opn.OPNsenseClient, method: str, path: str, payload: Any = None, **kwargs: Any) -> Any:
        self.requests.append((method, path))
        if self.refuse:
            raise opn.OPNsenseAuthError("refused")
        if path.startswith("interfaces/assignment/get_item/"):
            return {"interface": {"media": {"": {"value": "Default", "selected": int(path.endswith("lan"))},
                                            "1000baseT\tfull-duplex": {"value": "1000baseT full-duplex", "selected": int(path.endswith("wan"))},
                                            "100baseTX\thalf-duplex": {"value": "100baseTX half-duplex", "selected": 0},
                                            "2500Base-T": {"value": "2500Base-T", "selected": 0}, "autoselect": {"value": "autoselect", "selected": 0}}}}  # fmt: skip
        if path.startswith("interfaces/assignment/set_item/"):
            self.sets.append((path, payload))
            return {"result": "saved"}
        if path == "interfaces/assignment/reconfigure":
            return {"status": "ok"}
        if path in self.missing or path not in RESPONSES and path != opn.REBOOT:
            raise opn.OPNsenseError(f"HTTP 404 for {path}")
        return {"status": "ok"} if path == opn.REBOOT else fx(RESPONSES[path])


CONFIG_XML = """<?xml version="1.0"?>
<opnsense>
  <system><hostname>opnsense</hostname><domain>home.example</domain>
    <user><name>root</name><password>$2y$10$abcdefghijklmnopqrstuv</password><apikeys><item><key>k3y</key><secret>$6$s3cr3t</secret></item></apikeys></user>
  </system>
  <cert><descr>Web GUI</descr><crt>LS0tLS1CRUdJTi</crt><prv>LS0tLS1QUklWQVRF</prv></cert>
  <wireguard><server><privkey>wg-private</privkey><pubkey>wg-public</pubkey></server></wireguard>
  <revision><time>1700000000</time></revision>
</opnsense>
"""


@pytest.fixture
def router():
    fake = FakeRouter()
    fake.config = CONFIG_XML

    async def request(self, method, path, payload=None, **kwargs):
        return await fake.request(self, method, path, payload, **kwargs)

    async def config_text(self):
        return opn.redact_config(fake.config)

    with patch.object(opn.OPNsenseClient, "request", request), patch.object(opn.OPNsenseClient, "config_text", config_text):
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
    assert device["kind"] == "router" and device["features"]["save"] is False and device["features"]["port_control"] is True
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

    async def fake_apply(client, plan, trunks, **kwargs):
        seen.update(client=client, trunks=trunks, vlans=[n["vlan"] for n in plan["networks"]])
        return opnsense_setup.Result(done=["VLAN vlan0.30"], warnings=["No DHCP server"])

    with patch.object(opnsense_setup, "async_apply", fake_apply):
        results = await manager.async_apply()
    assert results[0]["ok"] and results[0]["done"] == ["VLAN vlan0.30", "note: No DHCP server"]
    assert seen["trunks"] == ["igc2"] and seen["vlans"] == [30] and isinstance(seen["client"], opn.OPNsenseClient)

    async def failing(client, plan, trunks, **kwargs):
        raise opnsense_setup.SetupError("VLAN 30: the router refused it")

    with patch.object(opnsense_setup, "async_apply", failing):
        results = await manager.async_apply()
    assert not results[0]["ok"] and "refused it. What this apply added was removed again" in results[0]["error"]



@needs_ha
async def test_opnsense_onboarding_portal_served_by_ha(hass: HomeAssistant, router, opn_entry: MockConfigEntry, socket_enabled) -> None:
    import aiohttp

    from custom_components.aerohive_local import opnsense_setup, portal
    from custom_components.aerohive_local.plan import get_plan

    manager = get_plan(hass)
    plan = {
        "networks": [{"id": "ob", "name": "Onboarding", "vlan": 80, "subnet": "10.80.0.1/24"}, {"id": "home", "name": "Home", "vlan": 50, "subnet": "10.50.0.1/24"}],
        "wifi": [{"id": "homeob", "ssid": "homeOB", "network": "ob", "security": "wpa2"}, {"id": "home", "ssid": "home", "network": "home", "security": "ppsk"}],
        "onboarding": {"enabled": True, "wifi": "homeob", "offer": ["home"]},
        "devices": {"router": opn_entry.entry_id},
    }
    assert await manager.async_save_plan(plan, {"wifi:homeob": "onboard-me-1"}) == []
    preview = manager.preview()["devices"][0]
    assert preview["errors"] == [] and any("http://10.80.0.1:8099/" in c for c in preview["commands"])
    seen = {}

    async def fake_apply(client, plan, trunks, portal_target=None):
        seen["target"] = portal_target
        return opnsense_setup.Result(done=["ok"])

    with (
        patch.object(opnsense_setup, "async_apply", fake_apply),
        patch.object(type(manager), "portal_target_ip", return_value="192.168.1.20"),
        patch.object(portal, "PORT", 18099),
    ):
        results = await manager.async_apply()
    assert results[0]["ok"], results
    assert seen["target"] == ("ob", "192.168.1.20") and manager.portal_hosted()

    opn_entry.runtime_data.data.neighbors["02:11:22:33:44:99"] = {"ip": "127.0.0.1", "hostname": None}
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:  # cookies from an IP address, like a browser
        async with session.get("http://127.0.0.1:18099/") as response:
            assert response.status == 200 and "Name for this device" in await response.text()
        async with session.post("http://127.0.0.1:18099/cgi-bin/portal", data={"name": "TV <b>", "kind": "nope", "wants": "home"}) as response:
            assert response.status == 200 and "updates by itself" in await response.text()
        item = manager.requests["021122334499"]
        assert (item["name"], item["kind"], item["wants"], item["status"]) == ("TV b", "something_else", "home", "pending")
        await manager.async_approve("021122334499", wifi="home")
        async with session.get("http://127.0.0.1:18099/") as response:
            text = await response.text()
        key = manager.secret(f"key:{item['key_id']}")
        assert key and key in text and "homeOB" not in text
    async with aiohttp.ClientSession() as other:  # another browser on the same device: no key
        async with other.get("http://127.0.0.1:18099/") as response:
            assert key not in await response.text()

    # Portal turned off: the server stops.
    plan["onboarding"]["enabled"] = False
    await manager.async_save_plan(plan, {})
    with patch.object(opnsense_setup, "async_apply", fake_apply):
        await manager.async_apply()
    assert seen["target"] is None and manager._portal_server is None


@needs_ha
async def test_opnsense_router_extras(hass: HomeAssistant, router, opn_entry: MockConfigEntry) -> None:
    from custom_components.aerohive_local import router_extras
    from custom_components.aerohive_local.plan import get_plan

    manager = get_plan(hass)
    await manager.async_save_plan({"networks": [], "devices": {"router": opn_entry.entry_id}}, {})
    seen = []

    async def fake_sync(client, plan, now):
        seen.append((client, [b["mac"] for b in plan["blocked"]]))
        return ["1 device(s) blocked"]

    with patch.object(router_extras, "sync_opnsense", fake_sync):
        assert await manager.async_block("AA:BB:CC:DD:EE:02", None, "Console") == ["1 device(s) blocked"]
    assert isinstance(seen[0][0], opn.OPNsenseClient) and seen[0][1] == ["aa:bb:cc:dd:ee:02"]

    async def refusing(client, plan, now):
        raise router_extras.ExtrasError("block rule: the router refused it")

    with patch.object(router_extras, "sync_opnsense", refusing), pytest.raises(HomeAssistantError, match="refused it"):
        await manager.async_unblock("aa:bb:cc:dd:ee:02")
    assert manager.plan["blocked"][0]["mac"] == "aa:bb:cc:dd:ee:02"  # unchanged when the router refused


def test_media_options() -> None:
    payload = {"interface": {"media": {"": {"selected": 0}, "1000baseT\tfull-duplex": {"selected": 1}, "10Gbase-SR": {"selected": 0},
                                       "100baseTX\thalf-duplex": {"selected": 0}, "autoselect": {"selected": 0}}}}  # fmt: skip
    current, choices = opn.parse_media_options(payload)
    assert current == "1000baseT\tfull-duplex"
    assert choices == {("1000", "full"): "1000baseT\tfull-duplex", ("10000", "full"): "10Gbase-SR", ("100", "half"): "100baseTX\thalf-duplex"}
    assert opn.media_setting(current, choices) == ("1000", "full") and opn.media_setting("", choices) == ("auto", "auto")
    assert opn.parse_media_options(None) == ("", {})


@needs_ha
async def test_opnsense_port_control(hass: HomeAssistant, router, opn_entry: MockConfigEntry) -> None:
    coordinator = opn_entry.runtime_data
    assert coordinator.port_speed_options("igc0") == ["auto", "100", "1000", "2500"]
    assert coordinator.port_speed_setting("igc0") == ("1000", "full") and coordinator.port_speed_setting("igc1") == ("auto", "auto")
    assert coordinator.port_speed_options("igc2") == []  # not assigned
    device = next(d for d in network_snapshot(hass)["devices"] if d["entry_id"] == opn_entry.entry_id)
    assert device["features"]["port_control"] and device["features"]["port_speed"]

    # The port Home Assistant uses can't be turned off; WAN needs force; an unassigned port can't.
    coordinator._opn_ports["igc1"].ipv4 = "192.168.1.1/24"
    with pytest.raises(HomeAssistantError, match="would cut it off"):
        await coordinator.async_set_port_enabled("igc1", False, force=True)
    with pytest.raises(HomeAssistantError, match="WAN"):
        await coordinator.async_set_port_enabled("igc0", False)
    with pytest.raises(HomeAssistantError, match="isn't assigned"):
        await coordinator.async_set_port_enabled("igc2", False)
    await coordinator.async_set_port_enabled("igc0", False, force=True)
    assert router.sets[-1] == ("interfaces/assignment/set_item/wan", {"interface": {"enable": "0"}})

    await coordinator.async_set_port_speed("igc1", "100", "half")
    assert router.sets[-1] == ("interfaces/assignment/set_item/lan", {"interface": {"media": "100baseTX\thalf-duplex"}})
    await coordinator.async_set_port_speed("igc1", "auto")
    assert router.sets[-1][1] == {"interface": {"media": ""}}
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_port_speed("igc1", "10")


def test_redact_config() -> None:
    text = opn.redact_config(CONFIG_XML)
    for secret in ("$2y$10$", "k3y", "s3cr3t", "LS0tLS1QUklWQVRF", "wg-private"):
        assert secret not in text
    assert "<password>***</password>" in text and "<pubkey>wg-public</pubkey>" in text and "<crt>LS0tLS1CRUdJTi</crt>" in text
    assert "<hostname>opnsense</hostname>" in text


@needs_ha
async def test_opnsense_backups(hass: HomeAssistant, router, opn_entry: MockConfigEntry) -> None:
    from custom_components.aerohive_local import backup as backup_mod

    coordinator = opn_entry.runtime_data
    files = await hass.async_add_executor_job(backup_mod.list_backups, coordinator.backup_folder)
    assert len(files) == 1
    saved = await hass.async_add_executor_job(backup_mod.read_backup, files[0].path)
    assert "<hostname>opnsense</hostname>" in saved and "$2y$10$" not in saved and "wg-private" not in saved
    device = next(d for d in network_snapshot(hass)["devices"] if d["entry_id"] == opn_entry.entry_id)
    assert device["features"]["backup"] and not device["features"]["restore"]
    # A change made on the router (no reboot) is backed up on the next read.
    router.config = CONFIG_XML.replace("<hostname>opnsense</hostname>", "<hostname>gw</hostname>")
    result = await coordinator.async_backup_config()
    assert result["file"].endswith(".cfg")
    files = await hass.async_add_executor_job(backup_mod.list_backups, coordinator.backup_folder)
    assert len(files) == 2


@needs_ha
async def test_router_that_failed_setup_is_listed_with_why(hass: HomeAssistant, router) -> None:
    from custom_components.aerohive_local.plan import get_plan

    router.missing.add(opn.SYSTEM_INFORMATION)  # unreachable as an OPNsense router
    entry = MockConfigEntry(domain=DOMAIN, unique_id="02:5a:00:00:01:02", title="gw", minor_version=3, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value != "loaded"
    from custom_components.aerohive_local import plan as plan_mod

    if plan_mod.DATA_PLAN not in hass.data:
        await plan_mod.async_setup_plan(hass)
    device = next(d for d in get_plan(hass).devices() if d["entry_id"] == entry.entry_id)
    assert device["unavailable"] and not device["router_capable"] and device["kind"] == "opnsense"
    assert "Settings → Devices & services" in device["note"]


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
