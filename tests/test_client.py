"""End-to-end tests of AerohiveClient against a fake HiveOS SSH server.

The fake server only offers the legacy algorithms real devices use
(diffie-hellman-group14-sha1, hmac-sha1, aes128-cbc, RSA host key), echoes input, paginates
long output with --More-- and asks for confirmation on reboot.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import asyncssh
import pytest

from custom_components.aerohive_local.client import (
    AerohiveAuthError,
    AerohiveClient,
    AerohiveCommandError,
)
from custom_components.aerohive_local.parsers import parse_show_interface, parse_show_ssid

from .conftest import load_fixture

PROMPT = "AH-7c0680#"
USER = "admin"
PASSWORD = "aerohive"


@dataclass
class FakeDevice:
    paging_supported: bool = True
    page_size: int = 20
    received: list[str] = field(default_factory=list)
    sessions: int = 0


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD


async def _read_line(process: asyncssh.SSHServerProcess) -> str | None:
    line = ""
    while True:
        char = await process.stdin.read(1)
        if not char:
            return None
        if char in "\r\n":
            process.stdout.write("\r\n")
            return line
        process.stdout.write(char)  # echo like a real CLI
        line += char


def _make_handler(device: FakeDevice):
    async def handler(process: asyncssh.SSHServerProcess) -> None:
        device.sessions += 1
        paging = True
        process.stdout.write("\r\nAerohive Networks Inc.\r\nCopyright (c) 2006-2024\r\n\r\n" + PROMPT)
        while True:
            line = await _read_line(process)
            if line is None:
                break
            cmd = line.strip()
            device.received.append(cmd)
            if cmd == "console page 0":
                if device.paging_supported:
                    paging = False
                else:
                    process.stdout.write("            ^-- unknown keyword or invalid input\r\n")
            elif cmd == "terminal length 0":
                process.stdout.write("            ^-- unknown keyword or invalid input\r\n")
            elif cmd == "show interface":
                process.stdout.write(load_fixture("ap460c_show_interface.txt").replace("\n", "\r\n"))
            elif cmd == "show ssid":
                process.stdout.write(load_fixture("ap460c_show_ssid.txt").replace("\n", "\r\n"))
            elif cmd == "show long":
                for i in range(1, 51):
                    process.stdout.write(f"line {i}\r\n")
                    if paging and i % device.page_size == 0 and i < 50:
                        process.stdout.write(" --More-- ")
                        key = await process.stdin.read(1)
                        if not key:
                            return
                        process.stdout.write("\b" * 10 + " " * 10 + "\b" * 10)
            elif cmd == "reboot":
                process.stdout.write("Do you really want to reboot? (Y/N)")
                answer = await _read_line(process)
                device.received.append(f"answer:{answer}")
                if answer and answer.lower().startswith("y"):
                    process.stdout.write("Rebooting...\r\n")
                    process.exit(0)
                    return
            elif cmd == "bad":
                process.stdout.write("ERROR: something went wrong\r\n")
            elif cmd == "drop":
                process.exit(0)
                return
            elif cmd:
                process.stdout.write(f"ok {cmd}\r\n")
            process.stdout.write(PROMPT)
        process.exit(0)

    return handler


@pytest.fixture(autouse=True)
def _allow_localhost_sockets() -> None:
    """pytest-homeassistant-custom-component blocks sockets; re-enable them."""
    try:
        import pytest_socket
    except ImportError:
        return
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1"])


@pytest.fixture
async def server(tmp_path) -> AsyncIterator[tuple[int, FakeDevice]]:
    device = FakeDevice()
    key = asyncssh.generate_private_key("ssh-rsa", 2048)
    srv = await asyncssh.create_server(
        _Server,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        process_factory=_make_handler(device),
        kex_algs=["diffie-hellman-group14-sha1"],
        mac_algs=["hmac-sha1"],
        encryption_algs=["aes128-cbc"],
        line_editor=False,
    )
    port = srv.sockets[0].getsockname()[1]
    try:
        yield port, device
    finally:
        srv.close()
        await srv.wait_closed()


def _client(port: int, password: str = PASSWORD) -> AerohiveClient:
    return AerohiveClient("127.0.0.1", port, USER, password, min_interval=0, command_timeout=10)


async def test_connect_legacy_algorithms_and_run(server) -> None:
    port, device = server
    client = _client(port)
    try:
        out = await client.run("show interface")
        assert client.hostname == "AH-7c0680"
        assert client.paging_disabled is True
        assert "show interface" not in out.split("\n")[0]
        assert PROMPT not in out
        ifaces = parse_show_interface(out)
        assert ifaces["wifi0.1"].ssid == "AP460C"
        ssids = parse_show_ssid(await client.run("show ssid"))
        assert ssids["AP460C"].max_client == 50
        assert device.sessions == 1  # session reused
        assert device.received[0] == "console page 0"
    finally:
        await client.close()


async def test_paging_fallback(server) -> None:
    port, device = server
    device.paging_supported = False
    client = _client(port)
    try:
        out = await client.run("show long")
        assert client.paging_disabled is False
        lines = [line for line in out.split("\n") if line.strip()]
        assert lines == [f"line {i}" for i in range(1, 51)]
    finally:
        await client.close()


async def test_command_error_check(server) -> None:
    port, _ = server
    client = _client(port)
    try:
        with pytest.raises(AerohiveCommandError, match="something went wrong"):
            await client.run("bad", check=True)
        # unchecked returns the text
        assert "ERROR" in await client.run("bad")
    finally:
        await client.close()


async def test_reconnect_after_drop(server) -> None:
    port, device = server
    client = _client(port)
    try:
        await client.run("show ssid")
        # The device drops the session; first attempt fails, client reconnects
        # and retries the command once.
        client._proc.stdin.write("drop\n")  # make the server close the session
        await asyncio.sleep(0.2)
        out = await client.run("show ssid")
        assert "AP460C" in out
        assert device.sessions == 2
    finally:
        await client.close()


async def test_reboot_confirmation(server) -> None:
    port, device = server
    client = _client(port)
    try:
        await client.reboot()
        assert "reboot" in device.received
        assert any(r.startswith("answer:y") for r in device.received)
        assert client.connected is False
    finally:
        await client.close()


async def test_bad_password(server) -> None:
    port, _ = server
    client = _client(port, password="wrong")
    with pytest.raises(AerohiveAuthError):
        await client.run("show version")
    await client.close()


async def test_concurrent_commands_are_serialised(server) -> None:
    port, device = server
    client = _client(port)
    try:
        results = await asyncio.gather(*(client.run(f"cmd{i}") for i in range(5)))
        assert results == [f"ok cmd{i}" for i in range(5)]
        assert device.sessions == 1
    finally:
        await client.close()


# --------------------------------------------------------------------------
# FASTPATH-style switch CLI (SR2208P): "(AH-Switch) >" / "(AH-Switch) #"
# --------------------------------------------------------------------------


def _make_switch_handler(received: list[str], login: bool = False):
    async def handler(process: asyncssh.SSHServerProcess) -> None:
        mode = ">"
        paging = True
        submode = ""

        def prompt() -> None:
            process.stdout.write(f"\r\n(AH-Switch) {submode}{mode}")

        if login:  # SR switches may ask for credentials again inside the shell
            process.stdout.write("\r\nUser:")
            user = await _read_line(process)
            process.stdout.write("Password:")
            pwd = await _read_line(process)
            received.append(f"login:{user}:{'ok' if pwd == PASSWORD else 'bad'}")
        prompt()
        while True:
            line = await _read_line(process)
            if line is None:
                break
            cmd = line.strip()
            received.append(cmd)
            if cmd == "enable":
                process.stdout.write("Password:")
                pwd = await _read_line(process)
                received.append("enable-password-ok" if pwd == PASSWORD else "enable-password-bad")
                if pwd == PASSWORD:
                    mode = "#"
            elif cmd == "console page 0":
                process.stdout.write("                             ^\r\n% Invalid input detected at '^' marker.\r\n")
            elif cmd == "terminal length 0" and mode == "#":
                paging = False
            elif cmd == "configure" and mode == "#":
                submode = "(Config)"
            elif cmd.startswith("interface ") and submode == "(Config)":
                submode = f"(Interface {cmd.split()[1]})"
            elif cmd == "exit" and submode.startswith("(Interface"):
                submode = "(Config)"
            elif cmd == "exit" and submode:
                submode = ""
            elif cmd == "exit" and mode == "#":
                mode = ">"
            elif cmd in {"shutdown", "no shutdown", "poe", "no poe"} and submode.startswith("(Interface"):
                pass
            elif cmd == "write memory":
                process.stdout.write("\r\nThis operation may take a few minutes.\r\nAre you sure you want to save? (y/n) ")
                answer = await _read_line(process)
                received.append(f"save:{answer}")
                process.stdout.write("\r\nConfiguration Saved!\r\n")
            elif cmd == "show interface mgt0":
                process.stdout.write("                             ^\r\nAn invalid interface has been used for this command.\r\n")
            elif submode:
                process.stdout.write("          ^\r\n% Invalid input detected at '^' marker.\r\n")
            elif cmd == "show long":
                for i in range(1, 41):
                    process.stdout.write(f"line {i}\r\n")
                    if paging and i % 20 == 0 and i < 40:
                        process.stdout.write("--More-- or (q)uit")
                        if not await process.stdin.read(1):
                            return
                        process.stdout.write("\r                  \r")
            elif cmd:
                process.stdout.write(f"ok {cmd}\r\n")
            prompt()
        process.exit(0)

    return handler


async def test_switch_cli_prompt_enable_and_paging(tmp_path) -> None:
    received: list[str] = []
    key = asyncssh.generate_private_key("ssh-rsa", 2048)
    srv = await asyncssh.create_server(
        _Server, "127.0.0.1", 0, server_host_keys=[key],
        process_factory=_make_switch_handler(received), line_editor=False,
    )  # fmt: skip
    port = srv.sockets[0].getsockname()[1]
    client = _client(port)
    try:
        out = await client.run("show long")
        assert client.hostname == "AH-Switch"
        assert client.prompt_mode == "#"
        assert "enable-password-ok" in received
        assert received.index("terminal length 0") > received.index("console page 0")
        assert client.paging_disabled is True
        assert [x for x in out.split("\n") if x.strip()] == [f"line {i}" for i in range(1, 41)]
        bad = await client.run("show interface mgt0")
        from custom_components.aerohive_local.parsers import find_cli_error, is_unsupported_command

        assert is_unsupported_command(bad)
        assert find_cli_error(bad) == "An invalid interface has been used for this command."
    finally:
        await client.close()
        srv.close()
        await srv.wait_closed()


async def test_switch_cli_login_prompt_config_mode_and_save() -> None:
    received: list[str] = []
    key = asyncssh.generate_private_key("ssh-rsa", 2048)
    srv = await asyncssh.create_server(
        _Server, "127.0.0.1", 0, server_host_keys=[key],
        process_factory=_make_switch_handler(received, login=True), line_editor=False,
    )  # fmt: skip
    port = srv.sockets[0].getsockname()[1]
    client = _client(port)
    try:
        await client.run("show version")
        assert received[0] == f"login:{USER}:ok"
        assert client.dialect == "switch"
        await client.run_sequence(["configure", "interface 1/0/3", "no poe", "exit", "exit"])
        assert client.prompt_submode is None and client.prompt_mode == "#"
        await client.run("write memory", confirm=True)
        assert "save:y" in received
        # A failing block leaves config mode before raising.
        from custom_components.aerohive_local.client import AerohiveCommandError

        with pytest.raises(AerohiveCommandError):
            await client.run_sequence(["configure", "bogus-command", "exit"])
        assert client.prompt_submode is None and client.prompt_mode == "#"
        assert (await client.run("show x")) == "ok show x"
    finally:
        await client.close()
        srv.close()
        await srv.wait_closed()
