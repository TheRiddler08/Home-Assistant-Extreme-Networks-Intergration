"""Parser tests. Fixtures prefixed 'ap460c_' are real device output; fixtures
prefixed 'synthetic_' are hand-written approximations awaiting real samples."""

from __future__ import annotations

import pytest

from custom_components.aerohive_local import parsers as p

from .conftest import load_fixture

# --------------------------------------------------------------------------
# show interface (real AP460C output)
# --------------------------------------------------------------------------


@pytest.fixture
def ap_ifaces() -> dict[str, p.Interface]:
    return p.parse_show_interface(load_fixture("ap460c_show_interface.txt"))


def test_interface_names(ap_ifaces: dict[str, p.Interface]) -> None:
    assert list(ap_ifaces) == ["mgt0", "eth0", "eth1", "wifi0", "wifi0.1", "wifi1", "wifi1.1", "wifi2"]


def test_interface_mgt0(ap_ifaces: dict[str, p.Interface]) -> None:
    mgt = ap_ifaces["mgt0"]
    assert mgt.mac == "bc:f3:10:7c:06:80"
    assert mgt.mode is None
    assert mgt.is_up is True
    assert mgt.vlan == "1"
    assert mgt.hive == "hive0"
    assert mgt.ssid is None
    assert mgt.channel is None


def test_interface_eth_state(ap_ifaces: dict[str, p.Interface]) -> None:
    assert ap_ifaces["eth0"].is_up is False
    assert ap_ifaces["eth0"].mode == "backhaul"
    assert ap_ifaces["eth1"].is_up is True
    assert ap_ifaces["eth1"].mac == "bc:f3:10:7c:06:81"


def test_interface_radios(ap_ifaces: dict[str, p.Interface]) -> None:
    w0 = ap_ifaces["wifi0"]
    assert w0.is_radio
    assert w0.channel == 6
    assert w0.channel_width == "20MHz"
    assert w0.channel_width_mhz == 20
    assert w0.radio_profile == "radio_axg0"
    assert w0.mode == "access"
    w1 = ap_ifaces["wifi1"]
    assert w1.channel == 36
    assert w1.radio_profile == "radio_axa0"


def test_interface_down_radio(ap_ifaces: dict[str, p.Interface]) -> None:
    w2 = ap_ifaces["wifi2"]
    assert w2.channel is None
    assert w2.channel_raw == "Down"
    assert w2.channel_width == "20MHz"


def test_interface_subinterfaces(ap_ifaces: dict[str, p.Interface]) -> None:
    sub = ap_ifaces["wifi0.1"]
    assert not sub.is_radio
    assert sub.parent == "wifi0"
    assert sub.ssid == "AP460C"
    assert sub.hive == "hive0"
    assert ap_ifaces["wifi1.1"].ssid == "AP460C"
    assert ap_ifaces["wifi1.1"].parent == "wifi1"


def test_interface_with_prompt_and_echo() -> None:
    text = "AH-7c0680#show interface\r\n" + load_fixture("ap460c_show_interface.txt").replace("\n", "\r\n") + "AH-7c0680#"
    ifaces = p.parse_show_interface(text)
    assert len(ifaces) == 8
    assert ifaces["wifi1.1"].ssid == "AP460C"


def test_interface_without_separator_falls_back() -> None:
    text = "\n".join(
        line for line in load_fixture("ap460c_show_interface.txt").splitlines() if not line.startswith("---")
    )
    ifaces = p.parse_show_interface(text)
    assert ifaces["wifi0.1"].ssid == "AP460C"
    assert ifaces["wifi0"].channel == 6
    assert ifaces["wifi2"].channel_raw == "Down"


def test_interface_br100_synthetic() -> None:
    ifaces = p.parse_show_interface(load_fixture("synthetic_br100_show_interface.txt"))
    assert {"eth0", "eth1", "eth2", "eth3", "eth4", "wifi0", "wifi0.1", "mgt0"} <= set(ifaces)
    assert ifaces["eth0"].mode == "wan"
    assert ifaces["eth2"].is_up is False
    assert ifaces["wifi0"].channel == 11
    assert ifaces["wifi0"].channel_width is None
    assert ifaces["wifi0.1"].ssid == "Home Net"  # SSID with a space


# --------------------------------------------------------------------------
# show ssid (real)
# --------------------------------------------------------------------------


def test_show_ssid() -> None:
    ssids = p.parse_show_ssid(load_fixture("ap460c_show_ssid.txt"))
    assert list(ssids) == ["AP460C"]
    ssid = ssids["AP460C"]
    assert ssid.index == 1
    assert ssid.frag == 2346
    assert ssid.rts == 2346
    assert ssid.dtim_period == 1
    assert ssid.max_client == 50
    assert ssid.mac_filter is None


def test_show_ssid_name_with_spaces() -> None:
    text = (
        "No. Name                             Frag    RTS     DTIM period Max client  Mac filter\n"
        "--- ----                             ----    ---     ----------- ---------- ----------\n"
        "1   AP460C                           2346    2346    1           50          None\n"
        "2   Guest Wifi                       2346    2346    1           20          blocklist\n"
    )
    ssids = p.parse_show_ssid(text)
    assert ssids["Guest Wifi"].max_client == 20
    assert ssids["Guest Wifi"].mac_filter == "blocklist"


def test_show_ssid_fallback_no_separator() -> None:
    text = "No. Name Frag RTS DTIM period Max client Mac filter\n1 Guest Wifi 2346 2346 1 20 None\n"
    ssids = p.parse_show_ssid(text)
    assert "Guest Wifi" in ssids or "Guest" in ssids  # tolerant either way
    assert any(s.max_client == 20 for s in ssids.values())


# --------------------------------------------------------------------------
# show version (real; uptime line completed, the sample was truncated)
# --------------------------------------------------------------------------


def test_show_version() -> None:
    info = p.parse_show_version(load_fixture("ap460c_show_version.txt"))
    assert info.version == "IQ Engine 10.5r3 build-278650"
    assert info.firmware == "10.5r3"
    assert info.build == "278650"
    assert info.os_name == "IQ Engine"
    assert info.platform == "AP460C"
    assert info.bootloader == "v0.0.4.71"
    assert info.uptime_seconds == 3 * 3600 + 12 * 60 + 45


def test_show_version_truncated_uptime() -> None:
    text = "Version: IQ Engine 10.5r3 build-278650\nPlatform:           AP460C\nUptime:             0 weeks, 0 d..."
    info = p.parse_show_version(text)
    assert info.platform == "AP460C"
    assert info.uptime_seconds == 0


def test_show_version_old_hiveos() -> None:
    text = (
        "Aerohive Networks Inc.\n"
        "Copyright (c) 2006-2014\n\n"
        "Version:              HiveOS 6.1r6a build-1234\n"
        "Build time:           Mon Oct 13 08:00:00 UTC 2014\n"
        "Platform:             BR100\n"
        "Uptime:               2 weeks, 3 days, 4 hours, 5 minutes, 6 seconds\n"
    )
    info = p.parse_show_version(text)
    assert info.firmware == "6.1r6a"
    assert info.os_name == "HiveOS"
    assert info.platform == "BR100"
    assert info.uptime_seconds == 2 * 604800 + 3 * 86400 + 4 * 3600 + 5 * 60 + 6


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0 weeks, 0 days, 0 hours, 1 minutes, 5 seconds", 65),
        ("1 week, 1 day, 1 hour, 1 minute, 1 second", 604800 + 86400 + 3600 + 61),
        ("3 days, 01:02:03", 3 * 86400 + 3723),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_uptime(value: str, expected: int | None) -> None:
    assert p.parse_uptime(value) == expected


# --------------------------------------------------------------------------
# show station (synthetic until real samples are captured)
# --------------------------------------------------------------------------


def test_show_station_synthetic() -> None:
    stations = p.parse_show_station(load_fixture("synthetic_show_station.txt"))
    assert set(stations) == {"e8:b1:fc:5a:1b:2c", "a4:c3:f0:aa:00:01", "3c:22:fb:11:22:33"}
    sta = stations["e8:b1:fc:5a:1b:2c"]
    assert sta.ip == "192.168.1.50"
    assert sta.interface == "wifi0.1"
    assert sta.radio == "wifi0"
    assert sta.ssid == "AP460C"
    assert sta.channel == 6
    assert sta.signal == -45
    assert sta.snr == 50
    assert sta.cipher == "aes ccmp"
    assert sta.authenticated is True
    assert stations["a4:c3:f0:aa:00:01"].ip is None  # 0.0.0.0 => unknown
    assert stations["a4:c3:f0:aa:00:01"].authenticated is False
    assert stations["3c:22:fb:11:22:33"].interface == "wifi1.1"
    assert stations["3c:22:fb:11:22:33"].signal == -52


def test_show_station_empty() -> None:
    text = "Chan=channel number; Pow=Power in dBm;\n\nIfname=wifi0.1, Ifindex=13, SSID=AP460C:\nMac Addr IP Addr Chan\n-------- ------- ----\n"
    assert p.parse_show_station(text) == {}
    assert p.parse_show_station("") == {}


def test_show_station_unknown_layout_fallback() -> None:
    text = (
        "Ifname=wifi1.1, SSID=Guest Wifi:\n"
        "e8b1:fc5a:1b2c  10.0.0.5  rssi -60(35) something else\n"
        "not a station line\n"
    )
    stations = p.parse_show_station(text)
    sta = stations["e8:b1:fc:5a:1b:2c"]
    assert sta.ip == "10.0.0.5"
    assert sta.signal == -60
    assert sta.ssid == "Guest Wifi"
    assert sta.interface == "wifi1.1"


# --------------------------------------------------------------------------
# show interface wifiN / boot-param (synthetic)
# --------------------------------------------------------------------------


def test_radio_detail_synthetic() -> None:
    detail = p.parse_show_interface_radio(load_fixture("synthetic_show_interface_wifi0.txt"), "Wifi0")
    assert detail.name == "wifi0"
    assert detail.channel == 6
    assert detail.channel_width == "20MHz"
    assert detail.tx_power == 17
    assert detail.eirp_power == 21
    assert detail.noise_floor == -95
    assert detail.tx_utilization == 3
    assert detail.rx_utilization == 7
    assert detail.interference_utilization == 12
    assert detail.total_utilization == 22
    assert detail.max_clients == 100
    assert detail.radio_profile == "radio_axg0"
    assert detail.raw["rx_errors"] == "0"


def test_radio_detail_garbage() -> None:
    detail = p.parse_show_interface_radio("%%% nothing useful here\n\x1b[0m", "wifi1")
    assert detail.channel is None
    assert detail.noise_floor is None


def test_boot_param_synthetic() -> None:
    bp = p.parse_show_boot_param(load_fixture("synthetic_show_boot_param.txt"))
    assert bp.region_code == "FCC"
    assert bp.country_code == "840"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("bcf3:107c:0680", "bc:f3:10:7c:06:80"),
        ("BC:F3:10:7C:06:80", "bc:f3:10:7c:06:80"),
        ("bc-f3-10-7c-06-80", "bc:f3:10:7c:06:80"),
        ("bcf3.107c.0680", "bc:f3:10:7c:06:80"),
        ("bcf3107c0680", "bc:f3:10:7c:06:80"),
        ("-", None),
        ("hive0", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_mac(value: str | None, expected: str | None) -> None:
    assert p.normalize_mac(value) == expected


def test_to_aerohive_mac() -> None:
    assert p.to_aerohive_mac("BC:F3:10:7C:06:80") == "bcf3:107c:0680"
    with pytest.raises(ValueError):
        p.to_aerohive_mac("nope")


def test_clean_output_more_and_ansi() -> None:
    raw = "line1\r\n --More-- \x08\x08\x08\x08\x08\x08\x08\x08\x08\x08          \x08\x08\x08\x08\x08\x08\x08\x08\x08\x08line2\r\n\x1b[7mline3\x1b[0m\r\n"
    assert p.clean_output(raw).split("\n")[:3] == ["line1", "line2", "line3"]
    raw2 = "line1\r\n--More--\r          \rline2\r\n"
    assert p.clean_output(raw2).split("\n")[:2] == ["line1", "line2"]


@pytest.mark.parametrize(
    ("output", "error"),
    [
        ("", None),
        ("AH-7c0680#", None),
        ("ERROR: Invalid SSID name", "ERROR: Invalid SSID name"),
        ("       ^-- unknown keyword or invalid input", "^-- unknown keyword or invalid input"),
        ("Incomplete command", "Incomplete command"),
        ("Rx errors=0; Tx errors=0", None),
    ],
)
def test_find_cli_error(output: str, error: str | None) -> None:
    assert p.find_cli_error(output) == error


def test_strip_echo_and_prompt() -> None:
    out = "show ssid\nNo. Name\n--- ----\n1   X\nAH-7c0680#"
    assert p.strip_echo_and_prompt(out, "show ssid") == "No. Name\n--- ----\n1   X"


def test_prompt_regex() -> None:
    assert p.PROMPT_RE.search("foo\nAH-7c0680#").group("host") == "AH-7c0680"
    assert p.PROMPT_RE.search("foo\nAH-7c0680# ").group("host") == "AH-7c0680"
    assert p.PROMPT_RE.search("Rx#=5 more text") is None


def test_ssid_security_objects() -> None:
    cfg = (
        "security-object AP460C security protocol-suite wpa2-aes-psk ascii-key ***\n"
        "ssid AP460C security-object AP460C\n"
        'ssid "Guest Wifi" security-object guest-sec\n'
        "interface wifi0 ssid AP460C\n"
    )
    assert p.parse_ssid_security_objects(cfg) == {"AP460C": "AP460C", "Guest Wifi": "guest-sec"}


@pytest.mark.parametrize(
    ("profile", "channel", "band"),
    [
        ("radio_axg0", 6, "2.4 GHz"),
        ("radio_axa0", 36, "5 GHz"),
        ("radio_axa0", None, "5 GHz"),
        ("radio_axg0", None, "2.4 GHz"),
        ("radio_ng0", None, "2.4 GHz"),
        ("radio_na0", None, "5 GHz"),
        ("weird", None, None),
    ],
)
def test_radio_band(profile: str, channel: int | None, band: str | None) -> None:
    assert p.radio_band(profile, channel) == band


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "\x00\x01\x02",
        "-----\n-----\n",
        "Name\n----\n\n\n",
        "% Unknown command\n",
        "Ifname=\nMac Addr\n--------\nzzzz\n",
        "a" * 10000,
    ],
)
def test_parsers_never_raise(garbage: str) -> None:
    p.parse_show_interface(garbage)
    p.parse_show_ssid(garbage)
    p.parse_show_station(garbage)
    p.parse_show_version(garbage)
    p.parse_show_interface_radio(garbage, "wifi0")
    p.parse_show_boot_param(garbage)
    p.parse_ssid_security_objects(garbage)


def test_is_unsupported_command() -> None:
    assert p.is_unsupported_command("         ^-- unknown keyword or invalid input")
    assert p.is_unsupported_command("ERROR: not supported on this platform")
    assert not p.is_unsupported_command(load_fixture("synthetic_show_interface_wifi0.txt"))
    long_ok = "Mode=access;\nChannel=6;\nNoise floor=-95;\nRx invalid frames=0;"
    assert not p.is_unsupported_command(long_ok)
    assert not p.is_unsupported_command("")
