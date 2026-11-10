"""Find Aerohive / Extreme Networks devices on the local network.

Two mechanisms feed the config flow:

* Passive: Home Assistant's DHCP watcher matches the hostname / MAC
  prefixes listed in manifest.json and starts a discovery flow on its own.
* Active ("Scan network" in the config flow): connect to TCP port 22 on every
  address of the local IPv4 subnets (bounded and rate limited), then rank the
  hosts that answer with an SSH banner by MAC vendor prefix (from the kernel
  ARP table) and reverse-DNS hostname ("AH-xxxxxx" is the HiveOS default).

No Home Assistant imports here so it can be unit tested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
import ipaddress
import logging
from pathlib import Path
import re
import socket

_LOGGER = logging.getLogger(__name__)

# MAC prefixes (OUIs) used by Aerohive and by Extreme Networks' Aerohive
# hardware. Not exhaustive: a device that doesn't match can still be found by
# hostname or added by IP.
AEROHIVE_OUIS: frozenset[str] = frozenset(
    {
        "001977",  # Aerohive Networks
        "08ea44",  # Aerohive Networks
        "4018b1",  # Aerohive Networks
        "809562",  # Extreme Networks (Aerohive APs)
        "b87cf2",  # Aerohive / Extreme (SR2208P switch)
        "885bdd",  # Aerohive Networks
        "9c5d12",  # Aerohive Networks
        "bcf310",  # Aerohive / Extreme (AP460C)
        "c413e2",  # Aerohive Networks
        "e01c41",  # Aerohive Networks
        "f09ce9",  # Aerohive Networks
    }
)
HOSTNAME_RE = re.compile(r"^ah-[0-9a-f]{6}", re.IGNORECASE)

SCAN_PORT = 22
MAX_HOSTS = 1024  # never scan more than this many addresses
CONCURRENCY = 32
CONNECT_TIMEOUT = 0.8
BANNER_TIMEOUT = 1.5


@dataclass
class ScanResult:
    host: str
    banner: str | None = None
    mac: str | None = None
    hostname: str | None = None

    @property
    def mac_match(self) -> bool:
        return is_aerohive_mac(self.mac)

    @property
    def hostname_match(self) -> bool:
        return bool(self.hostname and HOSTNAME_RE.match(self.hostname))

    @property
    def likely_aerohive(self) -> bool:
        return self.mac_match or self.hostname_match

    @property
    def label(self) -> str:
        parts = [self.host]
        if self.hostname:
            parts.append(self.hostname)
        if self.mac_match:
            parts.append("Aerohive/Extreme MAC")
        elif self.mac:
            parts.append(self.mac)
        return " – ".join(parts)


def is_aerohive_mac(mac: str | None) -> bool:
    if not mac:
        return False
    hexchars = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    return len(hexchars) == 12 and hexchars[:6] in AEROHIVE_OUIS


def is_aerohive_hostname(hostname: str | None) -> bool:
    return bool(hostname and HOSTNAME_RE.match(hostname))


def scan_targets(networks: Iterable[ipaddress.IPv4Network], exclude: Iterable[str] = ()) -> list[str]:
    """Addresses to probe: private networks only, capped at MAX_HOSTS."""
    skip = set(exclude)
    targets: list[str] = []
    for network in networks:
        if not network.is_private or network.prefixlen < 16:
            # Refuse to sweep public or huge ranges; narrow /16s to a /24 is
            # done by the caller (see narrow_network).
            continue
        for ip in network.hosts():
            text = str(ip)
            if text not in skip and text not in targets:
                targets.append(text)
            if len(targets) >= MAX_HOSTS:
                return targets
    return targets


def narrow_network(address: str, prefix: int) -> ipaddress.IPv4Network:
    """Large subnets (bigger than /22) are narrowed to the /24 around our own address."""
    if prefix < 22:
        prefix = 24
    return ipaddress.ip_network(f"{address}/{prefix}", strict=False)  # type: ignore[return-value]


def read_arp_table(path: str = "/proc/net/arp") -> dict[str, str]:
    """Blocking: IP -> MAC from the Linux ARP cache (empty elsewhere)."""
    result: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return result
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
            result[parts[0]] = parts[3].lower()
    return result


def reverse_dns(host: str) -> str | None:
    """Blocking reverse lookup, short label only."""
    try:
        name = socket.gethostbyaddr(host)[0]
    except (OSError, UnicodeError):
        return None
    return name.split(".", 1)[0] or None


async def probe_ssh(host: str, port: int = SCAN_PORT) -> str | None:
    """Return the SSH banner if the host accepts a connection on the port."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    try:
        line = await asyncio.wait_for(reader.readline(), BANNER_TIMEOUT)
        banner = line.decode("utf-8", "replace").strip()
    except (OSError, TimeoutError):
        banner = ""
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, TimeoutError):
            pass
    return banner if banner.startswith("SSH-") else (banner or "open")


async def async_scan(
    targets: list[str],
    *,
    port: int = SCAN_PORT,
    concurrency: int = CONCURRENCY,
) -> list[ScanResult]:
    """Probe targets for SSH, then enrich responders with MAC and hostname."""
    semaphore = asyncio.Semaphore(concurrency)

    async def one(host: str) -> ScanResult | None:
        async with semaphore:
            banner = await probe_ssh(host, port)
        return ScanResult(host=host, banner=banner) if banner else None

    found = [r for r in await asyncio.gather(*(one(h) for h in targets)) if r is not None]
    if not found:
        return []
    loop = asyncio.get_running_loop()
    arp = await loop.run_in_executor(None, read_arp_table)
    names = await asyncio.gather(
        *(asyncio.wait_for(loop.run_in_executor(None, reverse_dns, r.host), 3) for r in found),
        return_exceptions=True,
    )
    for result, name in zip(found, names, strict=True):
        result.mac = arp.get(result.host)
        result.hostname = name if isinstance(name, str) else None
    found.sort(key=lambda r: (not r.likely_aerohive, ipaddress.ip_address(r.host)))
    _LOGGER.debug("Scan found %d SSH hosts (%d likely Aerohive)", len(found), sum(r.likely_aerohive for r in found))
    return found
