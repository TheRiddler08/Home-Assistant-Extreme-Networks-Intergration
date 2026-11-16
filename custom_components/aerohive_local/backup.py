"""Configuration backups (no Home Assistant imports; file I/O is blocking).

Backups are plain text files named ``YYYYmmdd-HHMMSS.cfg`` (UTC) in one folder
per device. A new file is only written when the configuration really changed:
comments and timestamps are ignored when comparing.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .commands import DIALECT_EXOS, DIALECT_HIVEOS, DIALECT_SWITCH

BACKUP_SUFFIX = ".cfg"
BACKUPS_TO_KEEP = 20
_NAME_RE = re.compile(r"^(\d{8}-\d{6})(?:-\d+)?\.cfg$")


@dataclass(frozen=True)
class BackupFile:
    path: Path
    created: datetime
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def _significant(line: str) -> bool:
    stripped = line.strip()
    # "#" = ExtremeXOS comments, "!" = SR switch header (uptime, time), both change on their own.
    return bool(stripped) and not stripped.startswith(("#", "!"))


def normalize_config(text: str) -> list[str]:
    """Configuration lines that matter for comparison (no comments, blanks, timestamps)."""
    return [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n") if _significant(line)]


def config_fingerprint(text: str) -> str:
    return hashlib.sha256("\n".join(normalize_config(text)).encode("utf-8")).hexdigest()


def diff_summary(old: str, new: str, max_lines: int = 12) -> dict[str, object]:
    """Lines added / removed between two configurations (after normalisation)."""
    added: list[str] = []
    removed: list[str] = []
    for line in difflib.unified_diff(normalize_config(old), normalize_config(new), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    sample = [f"- {line}" for line in removed[:max_lines]] + [f"+ {line}" for line in added[:max_lines]]
    return {"added": len(added), "removed": len(removed), "sample": sample[:max_lines]}


def list_backups(folder: Path) -> list[BackupFile]:
    """Backups in a folder, newest first."""
    if not folder.is_dir():
        return []
    result = []
    for path in folder.iterdir():
        match = _NAME_RE.match(path.name)
        if not match or not path.is_file():
            continue
        created = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        result.append(BackupFile(path=path, created=created, size=path.stat().st_size))
    return sorted(result, key=lambda b: (b.created, b.name), reverse=True)


def latest_backup(folder: Path) -> BackupFile | None:
    backups = list_backups(folder)
    return backups[0] if backups else None


def read_backup(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_backup(
    folder: Path, text: str, now: datetime, *, keep: int = BACKUPS_TO_KEEP, force: bool = False
) -> tuple[BackupFile, bool]:
    """Save the configuration unless the latest backup has the same content.

    Returns (backup, written). Old backups beyond ``keep`` are deleted.
    """
    folder.mkdir(parents=True, exist_ok=True)
    latest = latest_backup(folder)
    if latest is not None and not force and config_fingerprint(read_backup(latest.path)) == config_fingerprint(text):
        return latest, False
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}{BACKUP_SUFFIX}"
    counter = 1
    while path.exists():
        path = folder / f"{stamp}-{counter}{BACKUP_SUFFIX}"
        counter += 1
    content = text if text.endswith("\n") else text + "\n"
    path.write_text(content, encoding="utf-8")
    for old in list_backups(folder)[keep:]:
        old.path.unlink(missing_ok=True)
    created = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    return BackupFile(path=path, created=created, size=path.stat().st_size), True


def restore_lines(text: str, dialect: str) -> list[str]:
    """Lines of a backup that can be sent back to the CLI, in order.

    Comments are dropped. Lines with masked secrets ("***", as HiveOS shows
    Wi-Fi keys) are skipped because replaying them would set the key to "***".
    """
    lines = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if dialect == DIALECT_EXOS and stripped.startswith("#"):
            continue
        if dialect in (DIALECT_SWITCH, DIALECT_HIVEOS) and stripped.startswith("!"):
            continue
        if "***" in stripped:
            continue
        lines.append(stripped if dialect != DIALECT_SWITCH else line.strip())
    return lines
