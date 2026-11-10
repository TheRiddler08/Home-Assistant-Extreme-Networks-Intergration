"""Async SSH client for the Aerohive / IQ Engine CLI.

Keeps one interactive shell per device, serialises all commands through a
lock, throttles them (the BR100 CPU is weak) and reconnects on failure.
No Home Assistant imports so it can be tested standalone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
import re
import time
from typing import Any

import asyncssh

from .commands import DIALECT_HIVEOS, DIALECT_SWITCH, DISABLE_PAGING_COMMANDS, redact
from .parsers import (
    CONFIRM_TAIL_RE,
    MORE_TAIL_RE,
    PROMPT_RE,
    clean_output,
    find_cli_error,
    prompt_host,
    strip_echo_and_prompt,
)

_LOGGER = logging.getLogger(__name__)

# Modern algorithms first, then the legacy ones these devices require.
KEX_ALGS = [
    "curve25519-sha256",
    "curve25519-sha256@libssh.org",
    "ecdh-sha2-nistp256",
    "diffie-hellman-group-exchange-sha256",
    "diffie-hellman-group14-sha256",
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group1-sha1",
]
MAC_ALGS = [
    "hmac-sha2-256-etm@openssh.com",
    "hmac-sha2-256",
    "hmac-sha2-512",
    "hmac-sha1",
]
HOST_KEY_ALGS = [
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
]
ENCRYPTION_ALGS = [
    "aes128-ctr",
    "aes192-ctr",
    "aes256-ctr",
    "aes128-gcm@openssh.com",
    "aes256-gcm@openssh.com",
    "aes128-cbc",
    "aes256-cbc",
    "3des-cbc",
]

CONNECT_TIMEOUT = 20
COMMAND_TIMEOUT = 45
MIN_COMMAND_INTERVAL = 0.3  # seconds between commands, be gentle with BR100
READ_CHUNK = 65536
TERM_SIZE = (250, 1000)  # wide/tall pty to minimise wrapping and paging


class AerohiveError(Exception):
    """Base error."""


class AerohiveConnectionError(AerohiveError):
    """Could not talk to the device."""


class AerohiveAuthError(AerohiveError):
    """Authentication failed."""


class AerohiveCommandError(AerohiveError):
    """The device rejected a command."""

    def __init__(self, command: str, message: str) -> None:
        super().__init__(f"{redact(command)}: {redact(message)}")
        self.command = redact(command)
        self.device_message = redact(message)


class AerohiveClient:
    """One persistent CLI session to one device."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None = None,
        *,
        client_key: Any | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        command_timeout: float = COMMAND_TIMEOUT,
        min_interval: float = MIN_COMMAND_INTERVAL,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_key = client_key
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._conn: asyncssh.SSHClientConnection | None = None
        self._proc: asyncssh.SSHClientProcess | None = None
        self._last_command = 0.0
        self.hostname: str | None = None
        self.prompt_mode: str | None = None
        self.prompt_submode: str | None = None  # "Config", "Interface 1/0/1" (switch CLI)
        self.dialect: str = DIALECT_HIVEOS
        self.paging_disabled = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def connected(self) -> bool:
        return self._proc is not None and self._conn is not None

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    async def _open(self) -> None:
        _LOGGER.debug("Connecting to %s:%s", self._host, self._port)
        options: dict[str, Any] = {
            "port": self._port,
            "username": self._username,
            "known_hosts": None,
            "config": None,
            "agent_path": None,
            "kex_algs": KEX_ALGS,
            "mac_algs": MAC_ALGS,
            "server_host_key_algs": HOST_KEY_ALGS,
            "encryption_algs": ENCRYPTION_ALGS,
            "keepalive_interval": 30,
            "keepalive_count_max": 3,
            "login_timeout": self._connect_timeout,
        }
        if self._client_key is not None:
            options["client_keys"] = [self._client_key]
        else:
            options["client_keys"] = None
        if self._password:
            options["password"] = self._password
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(self._host, **options), self._connect_timeout
            )
        except asyncssh.PermissionDenied as err:
            raise AerohiveAuthError("Authentication failed") from err
        except (OSError, asyncssh.Error, TimeoutError) as err:
            raise AerohiveConnectionError(f"Cannot connect to {self._host}: {err}") from err

        try:
            proc = await conn.create_process(
                term_type="vt100",
                term_size=TERM_SIZE,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, asyncssh.Error) as err:
            conn.close()
            raise AerohiveConnectionError(f"Cannot open shell on {self._host}: {err}") from err

        self._conn = conn
        self._proc = proc
        try:
            banner = await self._read_until_prompt(self._connect_timeout)
        except AerohiveError:
            await self._close()
            raise
        match = PROMPT_RE.search(clean_output(banner).rstrip(" \t"))
        if match:
            self.hostname = prompt_host(match)
            self._note_prompt(match)
            self.dialect = DIALECT_SWITCH if match.group("phost") else DIALECT_HIVEOS
        _LOGGER.debug("Connected to %s (prompt %s%s)", self._host, self.hostname, self.prompt_mode)

        # Switch CLIs may start in user mode ("(AH-Switch) >"): enter privileged mode.
        if self.prompt_mode == ">":
            await self._enable()

        # Try to disable paging; fall back to answering --More-- prompts.
        self.paging_disabled = False
        for command in DISABLE_PAGING_COMMANDS:
            try:
                out = await self._exec(command, self._command_timeout)
            except AerohiveError:
                if not self.connected:
                    raise
                continue
            if find_cli_error(out) is None and "^" not in out:
                self.paging_disabled = True
                break
        if not self.paging_disabled:
            _LOGGER.debug("%s: could not disable paging, will answer --More--", self._host)

    def _note_prompt(self, match: re.Match[str]) -> None:
        self.prompt_mode = match.group("mode")
        self.prompt_submode = match.group("submode")

    async def _ensure_privileged(self) -> None:
        """Switch CLI: leave config modes and re-enter enable mode if needed."""
        for _ in range(3):
            if self.prompt_submode:
                self._write("exit\n")
                await self._read_until_prompt(self._command_timeout)
                continue
            if self.prompt_mode == ">":
                await self._enable()
            break

    async def _enable(self) -> None:
        """Send 'enable' (answering a password prompt with the login password)."""
        _LOGGER.debug("%s: entering privileged mode", self._host)
        self._write("enable\n")
        await self._read_until_prompt(self._command_timeout, password=self._password or "")
        if self.prompt_mode != "#":
            _LOGGER.warning("%s: could not enter privileged (enable) mode; some commands may fail", self._host)

    async def _close(self) -> None:
        proc, conn = self._proc, self._conn
        self._proc = None
        self._conn = None
        if proc is not None:
            try:
                proc.close()
            except Exception:  # noqa: BLE001
                pass
        if conn is not None:
            try:
                conn.close()
                await asyncio.wait_for(conn.wait_closed(), 5)
            except Exception:  # noqa: BLE001
                pass

    async def connect(self) -> None:
        async with self._lock:
            if not self.connected:
                await self._open()

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #

    def _write(self, data: str) -> None:
        if self._proc is None:
            raise AerohiveConnectionError(f"Not connected to {self._host}")
        try:
            self._proc.stdin.write(data)
        except (OSError, asyncssh.Error) as err:
            raise AerohiveConnectionError(f"Connection to {self._host} lost: {err}") from err

    def _prompt_matches(self, cleaned: str) -> bool:
        tail = cleaned.rstrip(" \t")[-300:]
        match = PROMPT_RE.search(tail)
        if not match:
            return False
        return self.hostname is None or prompt_host(match) == self.hostname

    async def _read_until_prompt(
        self, timeout: float, *, confirm: bool = False, password: str | None = None
    ) -> str:
        """Read until the CLI prompt, answering pager and y/n prompts."""
        assert self._proc is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        buf = ""
        confirm_from = 0
        logins = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AerohiveConnectionError(f"Timed out waiting for prompt from {self._host}")
            try:
                chunk = await asyncio.wait_for(self._proc.stdout.read(READ_CHUNK), remaining)
            except TimeoutError as err:
                raise AerohiveConnectionError(
                    f"Timed out waiting for prompt from {self._host}"
                ) from err
            except (OSError, asyncssh.Error) as err:
                raise AerohiveConnectionError(f"Connection to {self._host} lost: {err}") from err
            if not chunk:
                raise AerohiveConnectionError(f"Connection to {self._host} closed")
            buf += chunk

            more = MORE_TAIL_RE.search(buf.rstrip("\r\n"))
            if more:
                buf = buf[: more.start()]
                self._write(" ")
                continue

            tail = buf[confirm_from:]
            if logins < 4 and re.search(r"(?:^|\n)\s*(?:user|username|login)\s*:\s*$", tail, re.IGNORECASE):
                self._write(self._username + "\n")
                confirm_from = len(buf)
                logins += 1
                continue
            if logins < 4 and re.search(r"password\s*:\s*$", tail, re.IGNORECASE):
                self._write((password if password is not None else (self._password or "")) + "\n")
                confirm_from = len(buf)
                logins += 1
                continue

            if confirm:
                tail = clean_output(buf[confirm_from:])
                if CONFIRM_TAIL_RE.search(tail.rstrip("\n")):
                    _LOGGER.debug("%s: answering confirmation prompt", self._host)
                    self._write("y\n")
                    confirm_from = len(buf)
                    continue

            if self._prompt_matches(clean_output(buf[-1000:])):
                match = PROMPT_RE.search(clean_output(buf[-1000:]).rstrip(" \t"))
                if match:
                    self._note_prompt(match)
                return buf

    async def _exec(self, command: str, timeout: float, *, confirm: bool = False) -> str:
        assert self._proc is not None
        self._write(command + "\n")
        raw = await self._read_until_prompt(timeout, confirm=confirm)
        return strip_echo_and_prompt(clean_output(raw), command)

    async def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_command)
        if wait > 0:
            await asyncio.sleep(wait)

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        confirm: bool = False,
        retry: bool = True,
        check: bool = False,
        keep_mode: bool = False,
    ) -> str:
        """Run one command and return its output (echo and prompt removed).

        With ``check=True`` an :class:`AerohiveCommandError` is raised if the
        output contains a CLI error message.
        """
        timeout = timeout or self._command_timeout
        async with self._lock:
            attempts = 2 if retry else 1
            for attempt in range(1, attempts + 1):
                await self._throttle()
                try:
                    if not self.connected:
                        await self._open()
                    elif self.dialect == DIALECT_SWITCH and not keep_mode:
                        await self._ensure_privileged()
                    _LOGGER.debug("%s: > %s", self._host, redact(command))
                    output = await self._exec(command, timeout, confirm=confirm)
                except AerohiveAuthError:
                    await self._close()
                    raise
                except AerohiveConnectionError:
                    await self._close()
                    if attempt >= attempts:
                        raise
                    _LOGGER.debug("%s: command failed, reconnecting", self._host)
                    continue
                finally:
                    self._last_command = time.monotonic()
                if check:
                    error = find_cli_error(output)
                    if error:
                        raise AerohiveCommandError(command, error)
                return output
        raise AerohiveConnectionError("unreachable")  # pragma: no cover

    async def run_many(self, commands: Sequence[str], *, check: bool = False) -> list[str]:
        return [await self.run(cmd, check=check) for cmd in commands]

    async def run_sequence(self, commands: Sequence[str]) -> list[str]:
        """Run commands that depend on CLI mode (configure -> interface -> ...).

        Mode is kept between them; on any error the session is returned to
        privileged mode before the error is raised. No automatic retry, so a
        half-applied block is never replayed.
        """
        outputs: list[str] = []
        try:
            for cmd in commands:
                out = await self.run(cmd, keep_mode=True, retry=False)
                outputs.append(out)
                error = find_cli_error(out)
                if error:
                    raise AerohiveCommandError(cmd, error)
        finally:
            if self.connected and self.dialect == DIALECT_SWITCH:
                async with self._lock:
                    try:
                        await self._ensure_privileged()
                    except AerohiveError:
                        await self._close()
        return outputs

    async def reboot(self, command: str = "reboot") -> None:
        """Send reboot, confirm, and drop the session (device goes away)."""
        async with self._lock:
            await self._throttle()
            if not self.connected:
                await self._open()
            elif self.dialect == DIALECT_SWITCH:
                await self._ensure_privileged()
            try:
                _LOGGER.debug("%s: > %s", self._host, command)
                output = await self._exec(command, 15, confirm=True)
                error = find_cli_error(output)
                if error:
                    raise AerohiveCommandError(command, error)
            except AerohiveConnectionError:
                # Expected: the device closes the session while rebooting.
                pass
            finally:
                self._last_command = time.monotonic()
                await self._close()
