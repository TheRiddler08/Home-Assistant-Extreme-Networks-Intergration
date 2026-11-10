"""Tests for CLI command builders and input validation."""

from __future__ import annotations

import pytest

from custom_components.aerohive_local import commands as c


def test_ssid_bind() -> None:
    assert c.ssid_bind("wifi0", "AP460C", True) == "interface wifi0 ssid AP460C"
    assert c.ssid_bind("Wifi1", "AP460C", False) == "no interface wifi1 ssid AP460C"
    assert c.ssid_bind("wifi0", "Guest Wifi", True) == 'interface wifi0 ssid "Guest Wifi"'


@pytest.mark.parametrize("radio", ["eth0", "wifi0.1", "wifi0; reboot", ""])
def test_ssid_bind_rejects_bad_radio(radio: str) -> None:
    with pytest.raises(c.InvalidArgument):
        c.ssid_bind(radio, "X", True)


@pytest.mark.parametrize("ssid", ["", "a\nreboot", 'a"b', "x\r"])
def test_ssid_bind_rejects_bad_ssid(ssid: str) -> None:
    with pytest.raises(c.InvalidArgument):
        c.ssid_bind("wifi0", ssid, True)


def test_max_client() -> None:
    assert c.ssid_max_client("AP460C", 50) == "ssid AP460C max-client 50"
    for bad in (0, 256, True):
        with pytest.raises(c.InvalidArgument):
            c.ssid_max_client("AP460C", bad)


def test_psk() -> None:
    assert (
        c.ssid_psk("AP460C", "supersecret")
        == "security-object AP460C security protocol-suite wpa2-aes-psk ascii-key supersecret"
    )
    assert c.ssid_psk("AP460C", "pass with space").endswith('ascii-key "pass with space"')
    for bad in ("short", "x" * 64, 'has"quote1', "new\nline12", "tab\tchar12"):
        with pytest.raises(c.InvalidArgument):
            c.ssid_psk("AP460C", bad)


def test_disconnect_commands() -> None:
    assert c.disconnect_commands("E8:B1:FC:5A:1B:2C") == [
        "clear auth station mac e8b1:fc5a:1b2c",
        "clear auth station e8b1:fc5a:1b2c",
    ]
    with pytest.raises(c.InvalidArgument):
        c.disconnect_commands("not-a-mac")


@pytest.mark.parametrize("cmd", ["show station", "show interface wifi0", "show", "SHOW version", "show running-config | include ssid"])
def test_show_allowed(cmd: str) -> None:
    assert c.validate_show_command(cmd) == cmd


@pytest.mark.parametrize("cmd", ["reboot", "save config", "show station\nreboot", "no show", "show;reboot", "showx"])
def test_show_rejected(cmd: str) -> None:
    with pytest.raises(c.InvalidArgument):
        c.validate_show_command(cmd)


def test_redact() -> None:
    assert c.redact("security-object X security protocol-suite wpa2-aes-psk ascii-key s3cr3t!!") == (
        "security-object X security protocol-suite wpa2-aes-psk ascii-key ***"
    )
    assert c.redact('ascii-key "a b c d e"') == "ascii-key ***"
    assert c.redact("admin password foo") == "admin password ***"
    assert c.redact("show ssid") == "show ssid"


def test_port_commands() -> None:
    assert c.port_enable("Eth1/3", False) == "interface eth1/3 shutdown"
    assert c.port_enable("eth1/3", True) == "no interface eth1/3 shutdown"
    assert c.port_poe("eth1/4", False) == "interface eth1/4 pse shutdown"
    assert c.port_poe("eth1/4", True) == "no interface eth1/4 pse shutdown"
    assert c.port_enable("eth2", True) == "no interface eth2 shutdown"
    for bad in ("wifi0", "eth1/1; reboot", "", "eth1/1\nreboot"):
        with pytest.raises(c.InvalidArgument):
            c.port_poe(bad, True)


def test_ssid_lifecycle_commands() -> None:
    assert c.create_ssid("Guest Wifi", ["wifi0", "wifi1"], "password123") == [
        'security-object "Guest Wifi"',
        'security-object "Guest Wifi" security protocol-suite wpa2-aes-psk ascii-key password123',
        'ssid "Guest Wifi"',
        'ssid "Guest Wifi" security-object "Guest Wifi"',
        'interface wifi0 ssid "Guest Wifi"',
        'interface wifi1 ssid "Guest Wifi"',
    ]
    assert c.create_ssid("Open", ["wifi0"], None) == [
        "security-object Open", "ssid Open", "ssid Open security-object Open", "interface wifi0 ssid Open",
    ]
    assert c.delete_ssid("Guest", ["wifi0"], "Guest") == [
        "no interface wifi0 ssid Guest", "no ssid Guest", "no security-object Guest",
    ]
    assert c.delete_ssid("Guest", [], "shared-sec") == ["no ssid Guest"]
    assert c.ssid_hide("AP460C", True) == "ssid AP460C hide-ssid"
    assert c.ssid_hide("AP460C", False) == "no ssid AP460C hide-ssid"
    for bad in ("", "x" * 33, 'a"b', " lead"):
        with pytest.raises(c.InvalidArgument):
            c.create_ssid(bad, ["wifi0"], None)
    with pytest.raises(c.InvalidArgument):
        c.create_ssid("Ok", ["wifi0"], "short")
