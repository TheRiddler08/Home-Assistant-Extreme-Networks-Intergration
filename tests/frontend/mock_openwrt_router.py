"""A pretend OpenWrt router over SSH (ash prompt, ubus JSON, UCI), for trying the integration.

    python3 tests/frontend/mock_openwrt_router.py --port 2223

Add the integration with host 127.0.0.1, port 2223 and any user name and
password. Answers come from the synthetic fixtures in tests/fixtures. Wi-Fi
changes made with uci (enable/disable, hide, key, add, delete) are applied
to an in-memory configuration, so they show up on the next poll.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
from pathlib import Path

import asyncssh

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PROMPT = "root@OpenWrt:~# "


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class Router:
    def __init__(self) -> None:
        self.sections: dict[str, dict[str, str]] = {}
        self.order: list[str] = []
        for line in fixture("synthetic_openwrt_uci_show_wireless.txt").splitlines():
            m = re.match(r"^wireless\.([^.=]+)(?:\.([^=]+))?=(.*)$", line)
            if not m:
                continue
            sec, opt, val = m.group(1), m.group(2), shlex.split(m.group(3))[0] if m.group(3) else ""
            if opt is None:
                self.sections[sec] = {".type": val}
                self.order.append(sec)
            else:
                self.sections[sec][opt] = val
        self.counter = 90000

    # ---- rendering ----
    def uci_show(self) -> str:
        lines = []
        for sec in self.order:
            opts = self.sections[sec]
            lines.append(f"wireless.{sec}={opts['.type']}")
            lines += [f"wireless.{sec}.{k}={shlex.quote(v)}" for k, v in opts.items() if k not in (".type", "key")]
        return "\n".join(lines)

    def ifnames(self) -> dict[str, tuple[str, str]]:
        """section -> (radio, ifname) for enabled AP sections."""
        result, counts = {}, {}
        for sec in self.order:
            opts = self.sections[sec]
            if opts.get(".type") == "wifi-iface" and opts.get("disabled") != "1":
                radio = opts.get("device", "radio0")
                n = counts.get(radio, 0)
                counts[radio] = n + 1
                result[sec] = (radio, f"phy{radio[-1]}-ap{n}")
        return result

    def wireless_status(self) -> str:
        status = {}
        for sec in self.order:
            if self.sections[sec].get(".type") == "wifi-device":
                status[sec] = {"up": True, "interfaces": []}
        for sec, (radio, ifname) in self.ifnames().items():
            status.setdefault(radio, {"up": True, "interfaces": []})["interfaces"].append(
                {"section": sec, "ifname": ifname, "config": {"ssid": self.sections[sec].get("ssid"), "mode": "ap"}}
            )
        return json.dumps(status)

    # ---- commands ----
    def run(self, line: str) -> str:
        line = line.strip()
        static = {
            "ubus call system board": "synthetic_openwrt_system_board.json",
            "ubus call system info": "synthetic_openwrt_system_info.json",
            "ubus call network.device status": "synthetic_openwrt_network_device_status.json",
            "ubus call network.interface dump": "synthetic_openwrt_network_interface_dump.json",
            "cat /tmp/dhcp.leases": "synthetic_openwrt_dhcp_leases.txt",
            "cat /proc/net/arp": "synthetic_openwrt_proc_net_arp.txt",
        }
        if line in static:
            return fixture(static[line])
        if line == "head -n 1 /proc/stat":
            self.counter += 37
            return f"cpu  {self.counter} 0 {self.counter // 3} {self.counter * 4} 0 0 0 0 0 0"
        if line == "cat /sys/class/thermal/thermal_zone0/temp":
            return "51200"
        if line == "uci changes":
            return ""
        if line.startswith("uci -X show wireless"):
            return self.uci_show()
        if line == "ubus call network.wireless status":
            return self.wireless_status()
        if line.startswith("uci export"):
            return fixture("synthetic_openwrt_uci_export.txt")
        if m := re.match(r"^ubus call iwinfo (info|assoclist) '\{\"device\":\"([^\"]+)\"\}'$", line):
            kind, dev = m.groups()
            path = FIXTURES / f"synthetic_openwrt_iwinfo_{kind}_{dev}.json"
            if path.exists():
                return path.read_text()
            if kind == "info":
                return fixture("synthetic_openwrt_iwinfo_info_phy0-ap0.json" if dev.startswith("phy0") else "synthetic_openwrt_iwinfo_info_phy1-ap0.json")
            return '{"results": []}'
        if "/tmp/hn_backup" in line or "uci -q changes" in line or "uci revert" in line or "init.d/network reload" in line:
            # Network setup: backup, rollback timer, commit, reload, confirm.
            return "HN_CONFIRMED" if "echo HN_CONFIRMED" in line else ""
        if "uci set network." in line or "uci -q show network" in line or "uci set wireless.hn_" in line:
            return self.apply_setup(line)
        if "uci commit wireless" in line or line.startswith("ubus call hostapd"):
            return self.apply(line)
        return f"-ash: {line.split()[0] if line else ''}: not found"

    def apply_setup(self, line: str) -> str:
        """Network-setup commands: replace the hn_* Wi-Fi sections (network/firewall/dhcp are accepted)."""
        body = line.split(" || echo ")[0]
        for part in body.split(" && "):
            part = part.strip()
            if "uci -q show wireless" in part:
                for sec in [s for s in self.sections if s.startswith("hn_")]:
                    self.sections.pop(sec)
                    self.order.remove(sec)
            elif m := re.match(r"^uci set wireless\.(hn_\w+)=(wifi-iface)$", part):
                self.sections[m.group(1)] = {".type": "wifi-iface"}
                self.order.append(m.group(1))
            elif m := re.match(r"^uci set wireless\.(hn_\w+)\.(\w+)=(.*)$", part):
                if m.group(1) in self.sections:
                    self.sections[m.group(1)][m.group(2)] = shlex.split(m.group(3))[0] if m.group(3) else ""
        return ""

    def apply(self, line: str) -> str:
        body = line.split(" || echo ")[0]
        current = None
        copy = re.search(r"for o in ([\w ]+); do v=\$\(uci -q get wireless\.(\w+)\.\$o\)", body)
        for part in body.split(" && "):
            part = part.strip()
            if part.startswith("s=$(uci add wireless wifi-iface)"):
                self.counter += 1
                current = f"cfg{self.counter:06x}"
                self.sections[current] = {".type": "wifi-iface"}
                self.order.append(current)
                continue
            if part.startswith("for o in") and copy and current:
                src = self.sections.get(copy.group(2), {})
                for opt in copy.group(1).split():
                    if opt in src:
                        self.sections[current][opt] = src[opt]
                continue
            if m := re.match(r"^uci set wireless\.([\w$]+)\.(\w+)=(.*)$", part):
                sec = current if m.group(1) == "$s" else m.group(1)
                value = shlex.split(m.group(3))[0] if m.group(3) else ""
                if sec in self.sections:
                    self.sections[sec][m.group(2)] = value
            elif m := re.match(r"^uci delete wireless\.(\w+)$", part):
                self.sections.pop(m.group(1), None)
                if m.group(1) in self.order:
                    self.order.remove(m.group(1))
        return ""


ROUTER = Router()


class Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return True


async def read_line(process: asyncssh.SSHServerProcess) -> str | None:
    line = ""
    while True:
        char = await process.stdin.read(1)
        if not char:
            return None
        if char in "\r\n":
            process.stdout.write("\r\n")
            return line
        process.stdout.write(char)
        line += char


async def handle(process: asyncssh.SSHServerProcess) -> None:
    process.stdout.write("\r\nBusyBox v1.36.1 built-in shell (ash)\r\n\r\n OpenWrt 23.05.3, r23809-234f1a2efa (pretend)\r\n\r\n" + PROMPT)
    try:
        while (line := await read_line(process)) is not None:
            if line.strip() == "reboot":
                break
            out = ROUTER.run(line)
            if out:
                process.stdout.write(out.rstrip("\n").replace("\n", "\r\n") + "\r\n")
            process.stdout.write(PROMPT)
    except (asyncssh.BreakReceived, asyncssh.TerminalSizeChanged, BrokenPipeError, ConnectionError):
        pass
    process.exit(0)


async def main(port: int) -> None:
    key = asyncssh.generate_private_key("ssh-ed25519")
    await asyncssh.create_server(Server, "127.0.0.1", port, server_host_keys=[key], process_factory=handle, line_editor=False)
    print(f"Pretend OpenWrt router listening on 127.0.0.1:{port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2223)
    asyncio.run(main(parser.parse_args().port))
