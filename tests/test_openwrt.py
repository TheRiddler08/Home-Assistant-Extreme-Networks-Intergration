"""OpenWrt parsers and command builders (no Home Assistant needed). Fixtures are synthetic."""

from __future__ import annotations

import pytest

from custom_components.aerohive_local import openwrt as ow
from custom_components.aerohive_local.commands import InvalidArgument, redact
from custom_components.aerohive_local.parsers import PROMPT_RE, prompt_host

from .conftest import load_fixture


def test_prompt_and_board() -> None:
    m = PROMPT_RE.search("banner\nroot@OpenWrt:~# ")
    assert prompt_host(m) == "OpenWrt" and m.group("lhost")
    board = ow.parse_board(load_fixture("synthetic_openwrt_system_board.json"))
    assert (board.hostname, board.model, board.version) == ("OpenWrt", "Example AX3000 Router", "23.05.3")
    assert board.description == "OpenWrt 23.05.3 r23809-234f1a2efa"
    info = ow.parse_system_info(load_fixture("synthetic_openwrt_system_info.json"))
    assert info.uptime == 93784 and info.memory_total == 515874816 and info.memory_available == 343932928
    assert info.load1 == 0.2


def test_cpu_and_temperature() -> None:
    a = ow.parse_cpu_stat("cpu  100 0 100 800 0 0 0 0 0 0")
    b = ow.parse_cpu_stat("cpu  150 0 150 900 0 0 0 0 0 0")
    assert ow.cpu_percent(a, b) == 50.0 and ow.cpu_percent(None, b) is None
    assert ow.parse_temperature("48500\n") == 48.5 and ow.parse_temperature("cat: can't open") is None


def test_ports_dsa_and_wan() -> None:
    ports = ow.parse_ports(
        load_fixture("synthetic_openwrt_network_device_status.json"),
        load_fixture("synthetic_openwrt_network_interface_dump.json"),
    )
    # DSA switch ports, not the eth0 conduit, bridge, Wi-Fi or loopback.
    assert sorted(ports) == ["lan1", "lan2", "lan3", "lan4", "wan"]
    assert ports["lan1"].carrier and ports["lan1"].speed == 1000 and ports["lan1"].duplex == "Full"
    assert ports["lan2"].carrier is False and ports["lan2"].speed is None
    assert ports["lan4"].up is False
    assert ports["wan"].wan and not ports["lan1"].wan
    assert ports["lan1"].rx_bytes == 123456789
    assert ow.bridge_mac(load_fixture("synthetic_openwrt_network_device_status.json")) == "02:5a:00:00:00:01"


def test_ports_plain_ethernet() -> None:
    """x86 and older targets: the ethN devices are the ports; the WAN is whatever 'wan' uses."""
    devices = '{"eth0": {"type": "Network device", "up": true, "carrier": true, "speed": "1000F"},' \
              ' "eth1": {"type": "Network device", "up": true, "carrier": true, "speed": "10000F"},' \
              ' "br-lan": {"type": "bridge", "up": true}}'
    interfaces = '{"interface": [{"interface": "wan", "l3_device": "eth1"}, {"interface": "lan", "l3_device": "br-lan"}]}'
    ports = ow.parse_ports(devices, interfaces)
    assert sorted(ports) == ["eth0", "eth1"] and ports["eth1"].wan and ports["eth1"].speed == 10000


def test_wireless_config_status_and_clients() -> None:
    radios, ifaces = ow.parse_wireless_config(load_fixture("synthetic_openwrt_uci_show_wireless.txt"))
    assert {r: (v.band, v.channel, v.htmode) for r, v in radios.items()} == {
        "radio0": ("2g", "1", "HE20"), "radio1": ("5g", "36", "HE80"),
    }  # fmt: skip
    by_section = {i.section: i for i in ifaces}
    assert by_section["cfg073579"].hidden and by_section["cfg073579"].maxassoc == 20
    assert by_section["cfg083579"].disabled and by_section["default_radio0"].encryption == "sae-mixed"
    status = ow.parse_wireless_status(load_fixture("synthetic_openwrt_network_wireless_status.json"))
    assert status["radio1"].up and status["radio1"].ifnames == {"default_radio1": "phy1-ap0", "cfg073579": "phy1-ap1"}
    info = ow.parse_iwinfo_info(load_fixture("synthetic_openwrt_iwinfo_info_phy1-ap0.json"))
    assert (info.channel, info.txpower, info.noise, info.width) == (36, 23, -95, 80)
    clients = ow.parse_assoclist(load_fixture("synthetic_openwrt_iwinfo_assoclist_phy1-ap0.json"))
    assert [c.mac for c in clients] == ["02:11:22:33:44:02", "02:11:22:33:44:03"]
    assert clients[0].tx_rate_kbps == 1201000 and ow.rate_text(clients[0].tx_rate_kbps) == "1201M"
    leases = ow.parse_dhcp_leases(load_fixture("synthetic_openwrt_dhcp_leases.txt"))
    assert leases["02:11:22:33:44:01"] == ("192.168.1.101", "Pixel-8") and leases["02:aa:bb:cc:dd:01"] == ("192.168.1.50", "nas")
    assert ow.parse_dhcp_leases("1 02:00:00:00:00:09 192.168.1.9 * *")["02:00:00:00:00:09"] == ("192.168.1.9", None)
    arp = ow.parse_arp(load_fixture("synthetic_openwrt_proc_net_arp.txt"))
    assert ("192.168.1.50", "02:aa:bb:cc:dd:01", "br-lan") in arp and len(arp) == 3  # incomplete entry skipped


def test_secret_options_never_parsed() -> None:
    text = "wireless.cfg1=wifi-iface\nwireless.cfg1.ssid='X'\nwireless.cfg1.key='topsecret'\nwireless.cfg1.sae_password='x'\n"
    assert ow.parse_uci_show(text) == {"cfg1": {".type": "wifi-iface", "ssid": "X"}}
    # The commands filter them on the router too, and backups mask them.
    assert "grep -vE" in ow.UCI_WIRELESS and "key" in ow.UCI_WIRELESS
    assert "sed -E" in ow.CONFIG_EXPORT


def test_wifi_change_commands() -> None:
    line = ow.apply_line([ow.set_disabled("cfg083579", False), *ow.set_key("default_radio0", "new pass 123", "sae-mixed")])
    assert line.startswith("uci set wireless.cfg083579.disabled=0 && uci set wireless.default_radio0.key='new pass 123'")
    assert line.endswith(f"uci commit wireless && wifi reload || echo '{ow.FAILED_MARK}'")
    assert "topsecret" not in redact(ow.uci_set("x", "key", "topsecret")) and "***" in redact(ow.uci_set("x", "key", "topsecret"))
    # An open network gets WPA2 when a key is set.
    assert ow.set_key("cfg1", "abcdefgh", "none")[-1] == "uci set wireless.cfg1.encryption=psk2"
    new = ow.new_iface("radio1", "Kids' Wi-Fi", "bedtime-2026", True, "lan")
    assert "uci set wireless.$s.ssid='Kids'\"'\"' Wi-Fi'" in new and "hidden=1" in new and "encryption=psk2" in new
    assert "key=" not in ow.new_iface("radio0", "Open", None, False, "lan")
    assert ow.copy_iface("default_radio0", "radio1").startswith("s=$(uci add wireless wifi-iface) && uci set wireless.$s.device=radio1")
    assert "del_client" in ow.disconnect_client("phy0-ap0", "02:11:22:33:44:01")


@pytest.mark.parametrize(
    ("func", "args"),
    [
        (ow.set_disabled, ("cfg1; reboot", True)),
        (ow.copy_iface, ("default_radio0", "radio1 && reboot")),
        (ow.new_iface, ("radio0", "X", "short", False, "lan")),
        (ow.new_iface, ("radio0", "X", None, False, "lan; reboot")),
        (ow.iwinfo_info, ("phy0-ap0'; reboot #",)),
        (ow.disconnect_client, ("phy0-ap0", "not-a-mac")),
    ],
)
def test_injection_and_bad_input_refused(func, args) -> None:
    with pytest.raises(InvalidArgument):
        func(*args)


@pytest.mark.parametrize("garbage", ["", "x", "{", "[]", "null", '{"interface": 5}', "a" * 5000])
def test_parsers_never_raise(garbage: str) -> None:
    ow.parse_board(garbage)
    ow.parse_system_info(garbage)
    ow.parse_ports(garbage, garbage)
    ow.parse_interfaces(garbage)
    ow.parse_wireless_config(garbage)
    ow.parse_wireless_status(garbage)
    ow.parse_iwinfo_info(garbage)
    ow.parse_assoclist(garbage)
    ow.parse_dhcp_leases(garbage)
    ow.parse_arp(garbage)
    ow.parse_cpu_stat(garbage)


def test_usb_devices_and_sfp_ports() -> None:
    usb = ow.parse_usb_devices("1d6b:0002 xHCI Host Controller hub\n0bda:8153 USB 10/100/1000 LAN\n12d1:1506 HUAWEI Mobile\n")
    assert usb == [{"id": "0bda:8153", "name": "USB 10/100/1000 LAN"}, {"id": "12d1:1506", "name": "HUAWEI Mobile"}]
    devices = '{"lan1": {"type": "Network device", "up": true, "carrier": true}, "sfp1": {"type": "Network device", "up": true, "carrier": false}}'
    assert sorted(ow.parse_ports(devices)) == ["lan1", "sfp1"]
