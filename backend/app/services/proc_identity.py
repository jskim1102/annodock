"""Linux process identity checks resilient to PID reuse and zombies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROC_ROOT = Path("/proc")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    state: str
    started_at: str
    boot_id: str


def parse_proc_stat(raw: str) -> tuple[str, str]:
    """Return process state and raw field-22 start ticks from proc stat."""
    command_end = raw.rindex(")")
    rest = raw[command_end + 2 :].split()
    if len(rest) <= 19:
        raise ValueError("proc stat does not contain field 22")
    return rest[0], rest[19]


def read_process_identity(
    pid: int,
    *,
    proc_root: Path = PROC_ROOT,
    boot_id_path: Path = BOOT_ID_PATH,
) -> ProcessIdentity | None:
    """Read a live process identity; zombies and unreadable PIDs are dead."""
    try:
        state, started_at = parse_proc_stat(
            (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        )
        if state == "Z":
            return None
        boot_id = boot_id_path.read_text(encoding="utf-8").strip()
        if not boot_id:
            return None
    except (OSError, ValueError):
        return None
    return ProcessIdentity(
        pid=pid,
        state=state,
        started_at=started_at,
        boot_id=boot_id,
    )


def process_identity_matches(
    pid: int | None,
    started_at: str | None,
    boot_id: str | None,
) -> bool:
    """Return whether a non-zombie PID still has the persisted identity."""
    if pid is None or started_at is None or boot_id is None:
        return False
    identity = read_process_identity(pid)
    return bool(
        identity is not None
        and identity.started_at == started_at
        and identity.boot_id == boot_id
    )
