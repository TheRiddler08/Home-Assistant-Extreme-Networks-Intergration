"""Network plan: validation and the commands generated for OpenWrt, Aerohive and ExtremeXOS."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.aerohive_local import netplan as n

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _plan(**devices) -> dict:
    plan = n.normalize(
        {
            "networks": [
                {"id": "home", "name": "Home", "vlan": 50, "subnet": "10.40.33.1/24", "reach": ["iot", "lan"]},
                {"id": "iot", "name": "IoT", "vlan": 30, "subnet": "10.15.19.1/24", "router_access": True, "switch_ports": {"sw": "5-8"}},
                {"id": "guest", "name": "Guest", "vlan": 40, "subnet": "10.99.99.1/24", "dhcp": False, "internet": True},
            ],
            "wifi": [
                {"id": "home", "ssid": "home", "network": "home", "security": "ppsk", "bands": ["2.4 GHz"]},
                {"id": "guest", "ssid": "Guest Wifi", "network": "guest", "security": "wpa2", "bands": ["2.4 GHz", "5 GHz"], "isolate": True, "max_clients": 20},
            ],
            "keys": [
                {"id": "tv", "name": "TV", "wifi": "home"},
                {"id": "cam", "name": "Camera", "wifi": "home", "network": "iot", "mac": "AA:BB:CC:DD:EE:01"},
                {"id": "old", "name": "Old phone", "wifi": "home", "expires": "2026-11-19T11:00:00+00:00"},
            ],
            "devices": {"router": "r", "router_trunks": ["eth1"], "switch_trunks": {"sw": "49-50"}, **devices},
        }
    )
    return plan


def _secret(ref: str) -> str | None:
    return {"wifi:home": "base-key-123456", "wifi:guest": "guest-pass-1", "key:tv": "tv-key-1234", "key:cam": "cam-key-1234", "key:old": "old-key-1234"}.get(ref)


def test_valid_plan_and_template() -> None:
    assert n.validate(_plan(), _secret) == []
    template = n.normalize(n.template())
    assert n.validate(template) == []
    assert [x["vlan"] for x in template["networks"]] == [10, 20, 30, 40, 50, 60, 70, 80, 90, 110]
    assert {w["ssid"] for w in template["wifi"]} == {"home", "home5", "homeISO", "homeOB", "Guest", "Google-Auth-Home"}


def test_validation_catches_mistakes() -> None:
    plan = _plan()
    plan["networks"][1]["vlan"] = 50
    plan["networks"][2]["subnet"] = "10.40.33.200/25"
    plan["networks"][0]["dhcp_limit"] = 300
    plan["networks"][0]["reach"] = ["nowhere"]
    plan["wifi"][1]["bands"] = []
    plan["keys"].append({"id": "x", "name": "X", "wifi": "guest", "network": None, "mac": "nope", "expires": None})
    errors = n.validate(plan, lambda ref: None if ref == "wifi:guest" else "long-enough-key")
    text = "\n".join(errors)
    for expected in ("VLAN 50 is also used", "overlaps", "DHCP range", "unknown network 'nowhere'", "at least one band",
                     "Guest Wifi: needs a password", "doesn't use per-device keys", "invalid MAC"):
        assert expected in text, expected
    bad = n.normalize({"networks": [{"id": "Bad Id", "vlan": 1, "subnet": "10.0.0.0/24"}]})
    assert "lowercase" in n.validate(bad)[0]


def test_openwrt_router_lines() -> None:
    out = n.openwrt_lines(_plan(), _secret, is_router=True, radios={"radio0": "2.4 GHz", "radio1": "5 GHz"}, now=NOW)
    cmds = "\n".join(out.commands)
    assert out.errors == []
    # Only sections named hn_* are replaced.
    assert out.commands[0].startswith("for s in $(uci -q show network | sed -n 's/^network\\.\\(hn_")
    for line in (
        "uci set network.hn_br50.name=br-hn50",
        "uci add_list network.hn_br50.ports=eth1.50",
        "uci set network.hn_if50.ipaddr=10.40.33.1",
        "uci set network.hn_if50.netmask=255.255.255.0",
        "uci set dhcp.hn_dhcp50.start=50",
        "uci set dhcp.hn_dhcp40.ignore=1",
        "uci set firewall.hn_zone50.input=REJECT",
        "uci set firewall.hn_zone30.input=ACCEPT",
        "uci set firewall.hn_rule50_dns.dest_port=53",
        "uci set firewall.hn_fwd50_wan.dest=wan",
        "uci set firewall.hn_fwd50_30.dest=hn30",
        "uci set firewall.hn_fwd50_lan.dest=lan",
        "uci set wireless.hn_w_guest_radio1.encryption=psk2",
        "uci set wireless.hn_w_guest_radio1.isolate=1",
        "uci set wireless.hn_w_guest_radio1.maxassoc=20",
        "uci set wireless.hn_k_tv_radio0.key=tv-key-1234",
        "uci set wireless.hn_k_cam_radio0.vid=30",
        "uci set wireless.hn_k_cam_radio0.mac=aa:bb:cc:dd:ee:01",
        "uci set wireless.hn_v_home_radio0_30.network=hn_if30",
    ):
        assert line in cmds, line
    assert "rule30" not in cmds  # IoT may reach the router: no DHCP/DNS-only rules
    assert "hn_fwd40" in cmds and "hn_fwd30_wan" in cmds
    assert "old-key" not in cmds  # expired
    assert "hn_w_home_radio1" not in cmds  # home is 2.4 GHz only
    assert {"tv-key-1234", "cam-key-1234", "base-key-123456", "guest-pass-1"} <= set(out.secrets)


def test_openwrt_access_point_lines() -> None:
    out = n.openwrt_lines(_plan(), _secret, is_router=False, radios={"radio0": "2.4 GHz"}, now=NOW)
    cmds = "\n".join(out.commands)
    assert "uci set network.hn_if50.proto=none" in cmds
    assert "firewall.hn_" not in cmds.replace("uci -q show firewall", "").replace("firewall.$s", "") and "dhcp.hn_" not in cmds
    assert out.warnings == ["Guest Wifi: this device has no 5 GHz radio"]


def test_hiveos_commands() -> None:
    plan = _plan()
    out = n.hiveos_commands(
        plan, _secret,
        radios={"wifi0": "2.4 GHz", "wifi1": "5 GHz"},
        bound={"Guest Wifi": {"wifi0"}, "Old": {"wifi1"}},
        security_objects={"Guest Wifi": "guest-obj"},
        managed=["Old"],
    )
    assert out.commands == [
        "user-profile hn40 qos-policy def-user-qos vlan-id 40 attribute 40",
        "security-object guest-obj security protocol-suite wpa2-aes-psk ascii-key guest-pass-1",
        "security-object guest-obj default-user-profile-attr 40",
        'ssid "Guest Wifi"',
        'ssid "Guest Wifi" security-object guest-obj',
        'no ssid "Guest Wifi" hide-ssid',
        'ssid "Guest Wifi" max-client 20',
        'interface wifi1 ssid "Guest Wifi"',
        "no interface wifi1 ssid Old",
        "no ssid Old",
        "no security-object Old",
    ]
    assert any("per-device keys" in w for w in out.warnings)
    assert any("client isolation" in w for w in out.warnings)
    assert out.managed == {"ssids": ["Guest Wifi"]}


def test_exos_commands() -> None:
    out = n.exos_commands(_plan(), entry_id="sw", existing={"Default": 1, "Mgmt": None, "v50": 50, "guest": 41}, managed=["stale", "v50"])
    assert out.commands == [
        "configure vlan v50 add ports 49-50 tagged",
        "create vlan iot",
        "configure vlan iot tag 30",
        "configure vlan iot add ports 49-50 tagged",
        "configure vlan Default delete ports 5-8",
        "configure vlan iot add ports 5-8 untagged",
    ]
    assert out.errors == ["Guest: VLAN guest exists on the switch with tag 41"]
    assert out.managed == {"vlans": ["v50", "iot"]}
    gone = n.exos_commands(_plan(), entry_id="sw", existing={"stale": 99}, managed=["stale"])
    assert gone.commands[-1] == "delete vlan stale"
    no_trunk = n.exos_commands(_plan(switch_trunks={}), entry_id="sw", existing={}, managed=[])
    assert no_trunk.warnings


def test_key_expiry_helpers_and_chunks() -> None:
    plan = _plan()
    assert [k["id"] for k in n.expired_keys(plan, NOW)] == ["old"]
    assert [k["id"] for k in n.active_keys(plan, NOW)] == ["tv", "cam"]
    chunks = n.chunks(["a" * 400, "b" * 400, "c" * 400, "d"])
    assert chunks == ["a" * 400 + " && " + "b" * 400, "c" * 400 + " && d"]
    assert all(len(c) <= n.SHELL_LINE_MAX for c in chunks)


def test_ipv6_on_openwrt() -> None:
    plan = n.normalize({
        "networks": [{"id": "home", "name": "Home", "vlan": 50, "subnet": "10.40.33.1/24", "ipv6": True}, {"id": "iot", "name": "IoT", "vlan": 300, "subnet": "10.15.19.1/24"}],
        "devices": {"router": "r"},
    })  # fmt: skip
    out = n.openwrt_lines(plan, _secret, is_router=True, radios={}, now=NOW)
    for line in ("uci set network.hn_if50.ip6assign=64", "uci set network.hn_if50.ip6hint=32", "uci set dhcp.hn_dhcp50.ra=server",
                 "uci set dhcp.hn_dhcp50.dhcpv6=server", "uci set firewall.hn_rule50_dhcp6.dest_port=547",
                 "uci add_list firewall.hn_rule50_icmp6.icmp_type=router-solicitation"):  # fmt: skip
        assert line in out.commands, line
    assert not any("hn_if300.ip6" in c for c in out.commands)
    assert n.validate(plan) == []
    plan["networks"][1]["ipv6"] = True
    assert any("IPv6 needs a VLAN from 1 to 255" in e for e in n.validate(plan))


def test_access_point_only_mode() -> None:
    plan = n.normalize({
        "networks": [{"id": "home", "name": "Home", "vlan": 50, "subnet": "10.40.33.1/24"}],
        "wifi": [{"id": "home", "ssid": "home", "network": "home", "security": "wpa2", "bands": ["2.4 GHz"]}],
        "devices": {"router": "opn", "ap_mode": {"netgear": {"gateway": "10.77.0.254"}}},
    })  # fmt: skip
    assert n.validate(plan, _secret) == []
    out = n.openwrt_lines(plan, _secret, is_router=False, radios={"radio0": "2.4 GHz"}, now=NOW, ap_mode=plan["devices"]["ap_mode"]["netgear"])
    for line in ("uci set network.lan.gateway=10.77.0.254", "uci add_list network.lan.dns=10.77.0.254", "uci set dhcp.lan.ignore=1",
                 "{ uci -q get network.wan >/dev/null && uci set network.wan.auto=0 || true; }"):  # fmt: skip
        assert line in out.commands, line
    assert "firewall odhcpd" in out.after[0] and any("access point only" in w for w in out.warnings)
    plain = n.openwrt_lines(plan, _secret, is_router=False, radios={"radio0": "2.4 GHz"}, now=NOW)
    assert not any("network.lan.gateway" in c for c in plain.commands) and plain.after == []
    plan["devices"]["ap_mode"]["netgear"]["gateway"] = "nope"
    plan["devices"]["ap_mode"]["opn"] = {"gateway": "10.77.0.254"}
    errors = n.validate(plan, _secret)
    assert any("main router's address" in e for e in errors) and any("router can't also" in e for e in errors)
