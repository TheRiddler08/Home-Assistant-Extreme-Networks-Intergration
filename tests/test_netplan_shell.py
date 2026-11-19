"""The OpenWrt shell commands of the network setup, run in a real POSIX shell.

A small stand-in for ``uci`` (written below) keeps sections in a JSON file;
paths are moved into a temporary folder. This checks the shell side: the
cleanup loop and its sed expression, quoting of names and keys, the
backup/restore scripts and the rollback timer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.aerohive_local import netplan as n
from custom_components.aerohive_local.coordinator import _chunks

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

FAKE_UCI = r'''
import json, sys
path = sys.argv[1]
args = [a for a in sys.argv[2:] if a != "-q"]
store = json.load(open(path))
def save():
    json.dump(store, open(path, "w"))
cmd = args[0]
if cmd == "show":
    for name, sec in store.get(args[1], {}).items():
        print(f"{args[1]}.{name}={sec['.type']}")
        for opt, val in sec.items():
            if opt != ".type":
                print(f"{args[1]}.{name}.{opt}='{' '.join(val) if isinstance(val, list) else val}'")
elif cmd in ("set", "add_list"):
    key, _, value = args[1].partition("=")
    parts = key.split(".")
    cfg = store.setdefault(parts[0], {})
    if len(parts) == 2:
        cfg[parts[1]] = {".type": value}
    elif parts[1] not in cfg:
        print(f"uci: Invalid argument"); sys.exit(1)
    elif cmd == "add_list":
        cfg[parts[1]].setdefault(parts[2], []).append(value)
    else:
        cfg[parts[1]][parts[2]] = value
    save()
elif cmd == "delete":
    cfg, name = args[1].split(".")[:2]
    store.get(cfg, {}).pop(name, None)
    save()
elif cmd in ("commit", "revert", "changes"):
    open(path + ".log", "a").write(" ".join(args) + "\n")
'''


@pytest.fixture
def router(tmp_path: Path):
    root = tmp_path
    bin_dir = root / "bin"
    bin_dir.mkdir()
    store = root / "uci.json"
    store.write_text(json.dumps({
        "network": {"lan": {".type": "interface", "proto": "static"}, "hn_old": {".type": "interface"}},
        "wireless": {"default_radio0": {".type": "wifi-iface"}, "hn_w_gone_radio0": {".type": "wifi-iface"}},
        "dhcp": {}, "firewall": {"hn_zone99": {".type": "zone"}, "lan_zone": {".type": "zone"}},
    }))
    (bin_dir / "uci_impl.py").write_text(FAKE_UCI)
    (bin_dir / "uci").write_text(f"#!/bin/sh\nexec {sys.executable} {bin_dir / 'uci_impl.py'} {store} \"$@\"\n")
    for tool in ("wifi", "logger"):
        (bin_dir / tool).write_text(f"#!/bin/sh\necho {tool} \"$@\" >> {root / 'calls.log'}\n")
    (root / "etc" / "config").mkdir(parents=True)
    (root / "initd").mkdir()
    for svc in ("network", "firewall", "dnsmasq"):
        (root / "initd" / svc).write_text(f"#!/bin/sh\necho {svc} \"$@\" >> {root / 'calls.log'}\n")
    for f in list(bin_dir.iterdir()) + list((root / "initd").iterdir()):
        f.chmod(0o755)
    for cfg in n.OPENWRT_CONFIGS:
        (root / "etc" / "config" / cfg).write_text(f"original {cfg}\n")

    def sh(command: str) -> subprocess.CompletedProcess:
        command = (
            command.replace("/etc/config", str(root / "etc" / "config"))
            .replace(n.BACKUP_DIR, str(root / "backup"))
            .replace("/etc/init.d/", str(root / "initd") + "/")
        )
        return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=30,
                              env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, check=False)

    sh.root = root  # type: ignore[attr-defined]
    sh.store = store  # type: ignore[attr-defined]
    return sh


def test_cleanup_and_quoting(router) -> None:
    plan = n.normalize({
        "networks": [{"id": "guest", "name": "Guest", "vlan": 40, "subnet": "10.99.99.1/24", "reach": []}],
        "wifi": [{"id": "guest", "ssid": "Guest Wifi's", "network": "guest", "security": "wpa2", "bands": ["2.4 GHz"]}],
        "devices": {"router": "r", "router_trunks": ["br-lan"]},
    })
    key = 'p@ss $HOME "q" `x`'
    out = n.openwrt_lines(plan, lambda ref: key, is_router=True, radios={"radio0": "2.4 GHz"}, now=datetime.now(UTC))
    for chunk in _chunks(out.commands):
        result = router(f"{chunk} || echo 'ERROR: OpenWrt did not accept the change'")
        assert "ERROR" not in result.stdout, (result.stdout, result.stderr)
    store = json.loads(router.store.read_text())
    assert "hn_old" not in store["network"] and "lan" in store["network"]  # only hn_* sections replaced
    assert "hn_w_gone_radio0" not in store["wireless"] and "default_radio0" in store["wireless"]
    assert "hn_zone99" not in store["firewall"] and "lan_zone" in store["firewall"]
    assert store["wireless"]["hn_w_guest_radio0"]["ssid"] == "Guest Wifi's"
    assert store["wireless"]["hn_w_guest_radio0"]["key"] == key  # nothing expanded by the shell
    assert store["network"]["hn_br40"]["ports"] == ["br-lan.40"]
    assert store["firewall"]["hn_fwd40_wan"]["dest"] == "wan"


def test_backup_restore_and_rollback_timer(router) -> None:
    config = router.root / "etc" / "config"
    result = router(f"( {n.openwrt_prepare(1)} ) || echo FAILED")
    assert "FAILED" not in result.stdout, result.stderr
    assert (router.root / "backup" / "wireless").read_text() == "original wireless\n"
    assert "logger -t aerohive_local 'network setup rolled back'" in (router.root / "backup" / "restore.sh").read_text()

    # Applied but never confirmed: the timer puts the old files back and reloads.
    (config / "network").write_text("changed\n")
    result = router(f"{n.openwrt_commit()} || echo FAILED")
    assert "FAILED" not in result.stdout, result.stderr
    assert "commit wireless" in Path(f"{router.store}.log").read_text()
    deadline = time.monotonic() + 10
    while (config / "network").read_text() != "original network\n" and time.monotonic() < deadline:
        time.sleep(0.2)
    assert (config / "network").read_text() == "original network\n"
    time.sleep(0.3)
    calls = (router.root / "calls.log").read_text()
    assert "network reload" in calls and "wifi reload" in calls and "rolled back" in calls

    # Confirmed in time: nothing is put back.
    router(f"( {n.openwrt_prepare(2)} )")
    (config / "network").write_text("changed again\n")
    router(n.openwrt_commit())
    assert "HN_CONFIRMED" in router(n.OPENWRT_CONFIRM).stdout
    time.sleep(2.6)
    assert (config / "network").read_text() == "changed again\n"
