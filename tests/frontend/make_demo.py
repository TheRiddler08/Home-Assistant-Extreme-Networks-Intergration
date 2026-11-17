"""Build tests/frontend/demo.json (synthetic) from a real snapshot made by the HA tests.

    AEROHIVE_SNAPSHOT_OUT=/tmp/snap.json pytest tests/test_exos_and_backups.py -k websocket
    python3 tests/frontend/make_demo.py /tmp/snap.json
"""

import copy
import json
import random
import sys
from pathlib import Path

random.seed(7)
snap = json.load(open(sys.argv[1]))
switch = next(d for d in snap["devices"] if d["kind"] == "switch")
ap = next(d for d in snap["devices"] if d["kind"] == "ap")
template = switch["ports"][0]

names = {3: "printer", 4: "camera-drive", 7: "nas", 9: "tv-lounge", 12: "camera-yard", 15: "office-pc", 21: "ap-upstairs", 49: "uplink-core"}
ports = []
for n in range(1, 53):
    port = copy.deepcopy(template)
    up = n in (1, 2, 3, 4, 7, 9, 12, 15, 16, 21, 22, 30, 31, 49) or (n < 48 and random.random() < 0.15)
    port.update(
        name=str(n), label=names.get(n), up=up, enabled=n not in (5, 40), present=n <= 50,
        speed=(1000 if n in (49, 7, 15, 21, 1) else random.choice([100, 1000, 1000, 10])) if up else None,
        duplex="Full" if up else None, uplink=n == 49, errors=3 if n == 31 else 0,
        rx_kbps=round(random.uniform(5, 40000), 1) if up else 0.0, tx_kbps=round(random.uniform(5, 9000), 1) if up else 0.0,
        rx_bytes=random.randint(10**6, 10**11) if up else 0, tx_bytes=random.randint(10**6, 10**10) if up else 0,
        neighbor=None, clients=[],
    )
    if n <= 48:
        delivering = n in (2, 4, 12, 21, 22)
        port["poe"] = {"enabled": n != 6, "delivering": delivering, "power_w": round(random.uniform(3.5, 15.4), 1) if delivering else 0.0,
                       "status": "delivering" if delivering else ("disabled" if n == 6 else "searching"), "class": "class3" if delivering else None}
    else:
        port["poe"] = None
    if up and n != 49:
        port["clients"] = [{"mac": "02:00:00:%02x:%02x:%02x" % (n, i, random.randint(0, 255)), "vlan": 30 if n in (4, 12) else 1} for i in range(1 if n != 22 else 3)]
    ports.append(port)
ports[1]["neighbor"] = {"name": "AH-7c0680", "port": "eth0", "address": "192.168.9.73", "ap": True, "network": False}
ports[20]["neighbor"] = {"name": "ap-upstairs", "port": "eth0", "address": "192.168.9.74", "ap": True, "network": False}
ports[48]["neighbor"] = {"name": "core-switch", "port": "1:24", "address": "192.168.8.2", "ap": False, "network": True}
ports[48]["clients"] = [{"mac": "02:00:00:aa:00:%02x" % i, "vlan": 1} for i in range(12)]
switch["ports"] = ports
switch["poe"] = {"budget_w": 370.0, "used_w": round(sum(p["poe"]["power_w"] for p in ports if p["poe"]), 1), "remaining_w": None}
switch["wired_clients"] = sum(len(p["clients"]) for p in ports if not p["uplink"])
switch["uptime"] = 3 * 86400 + 5 * 3600
switch["config"]["unsaved"] = True
switch["problems"] = [{"kind": "unsaved", "text": "Unsaved configuration changes"}]

sr = copy.deepcopy(switch)
sr.update(entry_id="SR2208P", device_id="dev-sr", name="AH-Switch", host="192.168.9.66", ip="192.168.9.66", cli="switch",
          model="SR2208P", firmware="1.0.1.28", serial="SR00000000001", mac="08:ea:44:00:00:01", vlans=[], temperature=None,
          temperature_status=None, fans=None, fan_rpm={}, power_supplies={}, problems=[], features={"port_names": False, "poe": True, "save": True, "reboot_save_prompt": False})
sr["config"] = {**switch["config"], "unsaved": None, "image_booted": None, "config_booted": None,
                "changed_after_reboot": True, "change_summary": {"added": 2, "removed": 5, "sample": ["- interface 1/0/3", "-  shutdown", "- vlan 30", "+ interface 1/0/3", "+  no shutdown"]}}
sr["problems"] = [{"kind": "changed_after_reboot", "text": "Configuration changed after a reboot"}]
sr_ports = []
for n in range(1, 11):
    port = copy.deepcopy(ports[n - 1])
    port["name"] = f"1/0/{n}"
    port["label"] = None
    port["uplink"] = n == 10
    port["present"] = True
    if n > 8:
        port["poe"] = None
    sr_ports.append(port)
sr["ports"] = sr_ports
sr["poe"] = {"budget_w": 125.0, "used_w": round(sum(p["poe"]["power_w"] for p in sr_ports if p["poe"]), 1), "remaining_w": None}
sr["wired_clients"] = sum(len(p["clients"]) for p in sr_ports if not p["uplink"])

ap2 = copy.deepcopy(ap)
ap2.update(entry_id="AP2", device_id="dev-ap2", name="ap-upstairs", host="192.168.9.74", ip="192.168.9.74", online=False, problems=[])
hostnames = ["Pixel-8", "iPad", "MacBook-Air", None, "Galaxy-Watch", "living-room-speaker"]
ap["stations"] = [
    {"mac": "02:11:22:33:44:%02x" % i, "ip": f"192.168.1.{60 + i}", "hostname": hostnames[i % len(hostnames)],
     "ssid": ["AP460C", "Guest Wifi"][i % 2], "radio": ["wifi0", "wifi1"][i % 2], "signal": -40 - i * 6,
     "tx_rate": f"{[866, 400, 144, 72][i % 4]}M", "rx_rate": "65M", "connected": "00:10:23"}
    for i in range(7)
]
both = [r["name"] for r in ap["radios"]]
ap["ssids"] = [
    {"name": "AP460C", "broadcasting": True, "hidden": False, "clients": 4, "radios": both, "max_clients": 100, "security": "wpa2-aes-psk"},
    {"name": "Guest Wifi", "broadcasting": True, "hidden": True, "clients": 3, "radios": both, "max_clients": 100, "security": "wpa2-aes-psk"},
    {"name": "IoT", "broadcasting": True, "hidden": False, "clients": 0, "radios": both[:1], "max_clients": 100, "security": "wpa2-aes-psk"},
]
for radio in ap["radios"]:
    radio["ssids"] = ["AP460C", "Guest Wifi"]
snap["devices"] = [switch, sr, ap, ap2]
snap["wifi_timers"] = []
out = Path(__file__).with_name("demo.json")
out.write_text(json.dumps(snap, indent=1) + "\n")
print("wrote", out)
