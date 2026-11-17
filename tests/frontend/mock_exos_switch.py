"""A pretend Summit X450e-48p (ExtremeXOS) over SSH, for trying the integration and its UI.

    python3 tests/frontend/mock_exos_switch.py --port 2222

Then add the integration with host 127.0.0.1, port 2222, any user name and
password. Ports can be turned on and off, PoE switched and reset, ports
renamed, and the configuration saved; the prompt shows "*" while there are
unsaved changes, like a real switch. Output follows the synthetic fixtures in
tests/fixtures (documented ExtremeXOS formats); nothing here is from a real
device.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
from pathlib import Path

import asyncssh

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
HOST = "X450e-48p"
STATIC = {
    "show switch": "synthetic_exos_show_switch.txt",
    "show version": "synthetic_exos_show_version.txt",
    "show vlan": "synthetic_exos_show_vlan.txt",
    "show fdb": "synthetic_exos_show_fdb.txt",
    "show lldp neighbors": "synthetic_exos_show_lldp_neighbors.txt",
    "show lldp neighbors detailed": "synthetic_exos_show_lldp_neighbors_detailed.txt",
    "show temperature": "synthetic_exos_show_temperature.txt",
    "show fans": "synthetic_exos_show_fans.txt",
    "show power": "synthetic_exos_show_power.txt",
    "show cpu-monitoring": "synthetic_exos_show_cpu_monitoring.txt",
    "show memory": "synthetic_exos_show_memory.txt",
}
ERROR = "\r\n                   ^\r\n% Invalid input detected at '^' marker.\r\n"


class Switch:
    def __init__(self) -> None:
        rng = random.Random(4)
        self.unsaved = False
        self.seq = 1
        self.ports: dict[int, dict] = {}
        for n in range(1, 53):
            up = n in (1, 2, 4, 7, 9, 12, 15, 21, 30, 49) or (n < 48 and rng.random() < 0.12)
            self.ports[n] = {
                "enabled": n != 5,
                "link": "NP" if n > 50 else ("A" if up else "R"),
                "speed": (1000 if n in (1, 7, 15, 49) else rng.choice([100, 1000, 10])) if up else None,
                "name": {4: "camera", 12: "camera-yard", 49: "uplink"}.get(n, ""),
                "poe": n <= 48 and n != 3,
                "draw": round(rng.uniform(3.5, 15.4), 1) if n in (2, 4, 12, 21) else 0.0,
                "tx": rng.randint(10**5, 10**8) if up else 0,
                "rx": rng.randint(10**5, 10**9) if up else 0,
            }

    def link(self, p: dict) -> str:
        return p["link"] if p["enabled"] else ("NP" if p["link"] == "NP" else "R")

    def show_ports(self) -> str:
        lines = [
            "Port Summary",
            "Port  Display              VLAN Name          Port  Link  Speed  Duplex",
            "#     String               (or # VLANs)       State State Actual Actual",
            "=" * 82,
        ]
        for n, p in self.ports.items():
            link = self.link(p)
            speed = f"{p['speed']:<6} FULL" if link == "A" else ""
            lines.append(f"{n:<5} {p['name']:<20} {'Default':<18} {'E' if p['enabled'] else 'D':<5} {link:<5} {speed}".rstrip())
        lines += ["=" * 82, "   Port State: D-Disabled, E-Enabled", "   Link State: A-Active, R-Ready, NP-Port not present, L-Loopback"]
        return "\n".join(lines)

    def show_stats(self) -> str:
        lines = ["Port Statistics", "Port      Link       Tx Pkt      Tx Byte      Rx Pkt      Rx Byte", "=" * 70]
        for n, p in self.ports.items():
            if self.link(p) == "A":
                p["tx"] += random.randint(10**5, 10**7)
                p["rx"] += random.randint(10**5, 10**8)
            lines.append(f"{n:<9} {self.link(p):<5} {p['tx'] // 900:>11} {p['tx']:>11} {p['rx'] // 900:>11} {p['rx']:>11}           0           0")
        return "\n".join(lines)

    def poe_rows(self) -> list[tuple[int, str, float]]:
        rows = []
        for n, p in self.ports.items():
            if n > 48:
                continue
            delivering = p["poe"] and p["draw"] > 0 and self.link(p) == "A"
            state = "disabled" if not p["poe"] else ("delivering" if delivering else "searching")
            rows.append((n, state, p["draw"] if delivering else 0.0))
        return rows

    def show_poe_info(self) -> str:
        lines = ["Port      State      Class     Volts   Curr   Power   Fault", "                                       (mA)   (Watts)"]
        for n, state, watts in self.poe_rows():
            cls, volts, ma = ("class3", 48.3, int(watts * 1000 / 48.3)) if watts else ("------", 0.0, 0)
            lines.append(f"{n:<9} {state:<10} {cls:<9} {volts:<7} {ma:<6} {watts:<7} None")
        return "\n".join(lines)

    def show_poe(self) -> str:
        used = round(sum(w for _, _, w in self.poe_rows()), 1)
        return (
            "\nInline Power System Information\nConfigured          : Enabled\n\n"
            "Inline Power Slot Information\nSlot  Inline-Power  Firmware   Budgeted   Measured\n"
            f"1     Operational   290        370 W      {used} W"
        )

    def show_config(self) -> str:
        base = (FIXTURES / "synthetic_exos_show_configuration.txt").read_text()
        extra = []
        for n, p in self.ports.items():
            if not p["enabled"]:
                extra.append(f"disable ports {n}")
            if p["name"]:
                extra.append(f"configure ports {n} display-string {p['name']}")
            if n <= 48 and not p["poe"]:
                extra.append(f"disable inline-power ports {n}")
        return base.rstrip() + "\n#\n# Module vlan configuration.\n#\n" + "\n".join(extra)

    def run(self, cmd: str) -> str | None:
        """Output of a command, or None when it needs a confirmation (handled by the caller)."""
        if cmd in STATIC:
            return (FIXTURES / STATIC[cmd]).read_text()
        if cmd == "show ports no-refresh":
            return self.show_ports()
        if cmd == "show ports statistics no-refresh":
            return self.show_stats()
        if cmd == "show inline-power":
            return self.show_poe()
        if cmd == "show inline-power info ports":
            return self.show_poe_info()
        if cmd == "show configuration":
            return self.show_config()
        if cmd in ("disable clipaging", ""):
            return ""
        if m := re.fullmatch(r"(enable|disable) ports (\d+)", cmd):
            self.ports[int(m.group(2))]["enabled"] = m.group(1) == "enable"
        elif m := re.fullmatch(r"(enable|disable) inline-power ports (\d+)", cmd):
            self.ports[int(m.group(2))]["poe"] = m.group(1) == "enable"
        elif m := re.fullmatch(r"reset inline-power ports (\d+)", cmd):
            return ""
        elif m := re.fullmatch(r"configure ports (\d+) display-string (\S{1,15})", cmd):
            self.ports[int(m.group(1))]["name"] = m.group(2)
        elif m := re.fullmatch(r"unconfigure ports (\d+) display-string", cmd):
            self.ports[int(m.group(1))]["name"] = ""
        else:
            return ERROR
        self.unsaved = True
        return ""


SWITCH = Switch()


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
    def prompt() -> None:
        SWITCH.seq += 1
        process.stdout.write(f"\r\n{'* ' if SWITCH.unsaved else ''}{HOST}.{SWITCH.seq} # ")

    process.stdout.write("\r\nExtremeXOS\r\nCopyright (C) 1996-2011 Extreme Networks. All rights reserved.\r\n")
    prompt()
    try:
        while (line := await read_line(process)) is not None:
            cmd = " ".join(line.split())
            if cmd == "save configuration":
                process.stdout.write("Do you want to save configuration to primary.cfg and overwrite it? (y/N) ")
                answer = await read_line(process)
                if answer and answer.lower().startswith("y"):
                    SWITCH.unsaved = False
                    process.stdout.write("Saving configuration on master ............ done!\r\nConfiguration saved to primary.cfg successfully.\r\n")
                else:
                    process.stdout.write("Save configuration cancelled.\r\n")
            elif cmd == "reboot":
                if SWITCH.unsaved:
                    process.stdout.write(
                        "Do you want to save configuration changes to primary and reboot?\r\n"
                        "(y - save and reboot, n - reboot without save, <cr> - cancel command) "
                    )
                else:
                    process.stdout.write("Are you sure you want to reboot the switch? (y/N) ")
                answer = (await read_line(process) or "").strip().lower()
                if answer in ("y", "n"):
                    if answer == "y":
                        SWITCH.unsaved = False
                    process.stdout.write("\r\nRebooting...\r\n")
                    break
            else:
                out = SWITCH.run(cmd)
                if out:
                    process.stdout.write(out.replace("\n", "\r\n"))
            prompt()
    except (asyncssh.BreakReceived, asyncssh.TerminalSizeChanged, BrokenPipeError, ConnectionError):
        pass
    process.exit(0)


async def main(port: int) -> None:
    key = asyncssh.generate_private_key("ssh-rsa", 2048)
    await asyncssh.create_server(Server, "127.0.0.1", port, server_host_keys=[key], process_factory=handle, line_editor=False)
    print(f"Pretend {HOST} listening on 127.0.0.1:{port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=2222)
    asyncio.run(main(parser.parse_args().port))
