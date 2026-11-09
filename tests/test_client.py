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
