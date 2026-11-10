"""Tests for the HA-free discovery helpers (MAC prefixes, targets, scanning)."""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from custom_components.aerohive_local import discovery as d


@pytest.fixture(autouse=True)
def _allow_localhost_sockets() -> None:
    try:
        import pytest_socket
    except ImportError:
        return
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1"])


@pytest.mark.parametrize(
    ("mac", "expected"),
    [
        ("bc:f3:10:7c:06:80", True),
        ("bcf3:107c:0680", True),
        ("00-19-77-00-11-20", True),
        ("00:11:22:33:44:55", False),
        ("", False),
        (None, False),
        ("garbage", False),
    ],
)
def test_is_aerohive_mac(mac: str | None, expected: bool) -> None:
    assert d.is_aerohive_mac(mac) is expected


def test_hostname_match() -> None:
    assert d.is_aerohive_hostname("AH-7c0680")
    assert d.is_aerohive_hostname("ah-001000.lan")
    assert not d.is_aerohive_hostname("router")
    assert not d.is_aerohive_hostname(None)


def test_scan_targets_bounded_and_private() -> None:
    nets = [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("8.8.8.0/24")]
    targets = d.scan_targets(nets, exclude=["192.168.1.5"])
    assert len(targets) == 253
    assert "192.168.1.5" not in targets
    assert not any(t.startswith("8.8.8.") for t in targets)
    big = d.scan_targets([ipaddress.ip_network("10.0.0.0/16")])
    assert len(big) == d.MAX_HOSTS


def test_narrow_network() -> None:
    assert str(d.narrow_network("10.1.2.3", 8)) == "10.1.2.0/24"
    assert str(d.narrow_network("192.168.1.7", 24)) == "192.168.1.0/24"
    assert str(d.narrow_network("192.168.4.7", 22)) == "192.168.4.0/22"


def test_read_arp_table(tmp_path) -> None:
    arp = tmp_path / "arp"
    arp.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.10     0x1         0x2         bc:f3:10:7c:06:80     *        eth0\n"
        "192.168.1.11     0x1         0x0         00:00:00:00:00:00     *        eth0\n"
    )
    assert d.read_arp_table(str(arp)) == {"192.168.1.10": "bc:f3:10:7c:06:80"}
    assert d.read_arp_table(str(tmp_path / "missing")) == {}


def test_scan_result_label() -> None:
    r = d.ScanResult(host="192.168.1.10", mac="bc:f3:10:7c:06:80", hostname="AH-7c0680")
    assert r.likely_aerohive
    assert r.label == "192.168.1.10 – AH-7c0680 – Aerohive/Extreme MAC"
    other = d.ScanResult(host="192.168.1.20", mac="00:11:22:33:44:55")
    assert not other.likely_aerohive


async def test_async_scan_finds_local_ssh_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"SSH-2.0-OpenSSH_5.2\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(d, "read_arp_table", lambda path="/proc/net/arp": {"127.0.0.1": "bc:f3:10:7c:06:80"})
    monkeypatch.setattr(d, "reverse_dns", lambda host: "AH-7c0680")
    try:
        results = await d.async_scan(["127.0.0.1"], port=port)
    finally:
        server.close()
        await server.wait_closed()
    assert len(results) == 1
    assert results[0].banner == "SSH-2.0-OpenSSH_5.2"
    assert results[0].likely_aerohive


async def test_async_scan_closed_port() -> None:
    # Port 1 on localhost is closed in test environments.
    assert await d.async_scan(["127.0.0.1"], port=1) == []
