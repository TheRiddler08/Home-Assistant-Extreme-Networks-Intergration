"""Every line sent to an OpenWrt shell fits BusyBox's interactive line limit (1024 characters).

A longer line is cut off by the shell's line editor, leaves a quote open, and the shell waits
at "> " until Home Assistant times out ("Timed out waiting for prompt … (last output: '>')").
The second test replays the commands in a real interactive BusyBox ash on a terminal when
BusyBox is installed.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import sys
import time
from datetime import UTC, datetime

import pytest

from custom_components.aerohive_local import netplan as n
from custom_components.aerohive_local import openwrt as ow
from custom_components.aerohive_local import portal, router_extras

MARK = " || echo 'ERROR: OpenWrt did not accept the change'"
BUSYBOX_LIMIT = 1024
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _groups() -> dict[str, list[str]]:
    plan = n.template()
    plan["onboarding"]["enabled"] = True
    plan["keys"] = [{"id": f"k{i}", "name": f"Device {i}", "wifi": "home", "network": "iot", "mac": f"aa:bb:cc:dd:ee:{i:02x}"} for i in range(30)]
    plan = n.normalize(plan)
    plan["reservations"] = [{"mac": f"aa:bb:cc:dd:ee:{i:02x}", "ip": f"10.15.19.{i + 20}", "name": f"Kid's device {i}"} for i in range(20)]
    plan["forwards"] = [{"id": "web", "name": "Web", "protocol": "both", "port": "443", "to_ip": "10.40.33.5", "to_port": "8443", "enabled": True}]

    def secret(ref: str) -> str:
        return "Pa$$w'rd `x` \"q\" 1234"

    radios = {"radio0": "2.4 GHz", "radio1": "5 GHz"}
    look = portal.look_from(n.ONBOARDING_DEFAULTS)
    big = portal.look_from({**n.ONBOARDING_DEFAULTS, "logo": "data:image/png;base64," + "A" * 90000, "text": "It's \"great\" `x` $HOME " * 40})
    page = portal.approved_page(look=look, name="TV", ssid="home", key="Kx7m'Pq2", qr_svg="<svg>" + "<path d='M0 0h1v1H0z'/>" * 200 + "</svg>", expires_text=None)
    return {
        "router": n.openwrt_lines(plan, secret, is_router=True, radios=radios, now=NOW).commands,
        "ap": n.openwrt_lines(plan, secret, is_router=False, radios=radios, now=NOW).commands,
        "portal": portal.install_lines(address="10.8.10.1", look=look, offer=[("home", "home"), ("homeiso", "homeISO")]),
        "portal_big": portal.install_lines(address="10.8.10.1", look=big, offer=[("home", "home")]),
        "approve": portal.approve_lines("021122334455", "abcdefghijklmnop", page),
        "extras": router_extras.openwrt_sync_lines(plan, NOW),
    }


def test_every_line_fits() -> None:
    for name, lines in _groups().items():
        for chunk in n.chunks(lines):
            assert len(chunk + MARK) < BUSYBOX_LIMIT - 50, (name, len(chunk), chunk[:120])
    many = [c for i in range(20) for c in ow.set_key(f"cfg{i:03x}", f"a-long-wifi-password-{i}", "psk2")]
    for line in ow.apply_lines(many):
        assert len(line) < BUSYBOX_LIMIT - 50
        assert line.endswith(f"|| echo '{ow.FAILED_MARK}'")
    assert ow.WIFI_APPLY in ow.apply_lines(many)[-1]


@pytest.mark.skipif(shutil.which("busybox") is None or sys.platform == "win32", reason="needs BusyBox")
def test_replayed_in_interactive_busybox(tmp_path) -> None:
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # the child: an interactive shell like the one an SSH session gets
        os.chdir(tmp_path)  # paths below are made relative (shorter than the router's, never longer)
        os.environ.update(PS1="root@OpenWrt:~# ", PS2="> ", HOME=str(tmp_path), TERM="vt100")
        os.execvp("busybox", ["busybox", "ash", "-i"])

    def read(wait: float) -> str:
        out, end = b"", time.time() + wait
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
                visible = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", out).rstrip()
                if visible.endswith((b"#", b">")):
                    end = min(end, time.time() + 0.05)
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.decode(errors="replace")).replace("\r", "")

    try:
        read(1.0)
        for name, lines in _groups().items():
            for chunk in n.chunks(lines):
                command = (
                    chunk.replace("/www/", "w/").replace("/tmp/hn_portal", "p")
                    .replace("/etc/init.d/", "true ").replace("uci ", "true ").replace("wifi reload", "true")
                )  # fmt: skip
                os.write(fd, (command + MARK + "\n").encode())
                tail = read(3.0).rstrip().splitlines()[-1:] or [""]
                assert tail[0].rstrip().endswith("#"), (name, tail[0], chunk[:200])
    finally:
        os.kill(pid, 9)
        os.close(fd)
