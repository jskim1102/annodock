"""Classify and persist detached-worker failures without losing first cause."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
import torch

from app.services.quota import increase_bytes_used_sync, path_tree_bytes
from app.services.rundir import collect_run_artifacts


OOM_PATTERN = re.compile(
    r"out of memory|CUBLAS_STATUS_ALLOC_FAILED|CUDA error",
    re.IGNORECASE,
)
NOISE_PATTERNS = (
    re.compile(
        r"DataLoader worker .*?(?:killed|exited).*?signal",
        re.IGNORECASE,
    ),
    re.compile(
        r"ConnectionResetError:.*Connection reset by peer",
        re.IGNORECASE,
    ),
)
LOG_READ_BYTES = 128 * 1024
LOG_TAIL_LINES = 80
MAX_REASON_CHARS = 8_000


@dataclass(frozen=True)
class FailureReport:
    reason: str
    is_oom: bool
    effective_batch: int | None
    exit_code: int | None


def filter_dataloader_noise(text: str) -> str:
    """Remove expected multiprocessing shutdown lines from a failure tail."""
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip()
        and not any(pattern.search(line) for pattern in NOISE_PATTERNS)
    )


def read_stderr_tail(out_dir: str | Path) -> str:
    """Read a bounded, filtered tail from the worker's redirected stderr log."""
    log_path = Path(out_dir) / "artifacts" / "log"
    try:
        with log_path.open("rb") as log_file:
            log_file.seek(0, 2)
            size = log_file.tell()
            log_file.seek(max(0, size - LOG_READ_BYTES))
            raw = log_file.read()
    except OSError:
        return ""
    decoded = raw.decode("utf-8", errors="replace")
    return filter_dataloader_noise("\n".join(decoded.splitlines()[-LOG_TAIL_LINES:]))


def read_effective_batch(out_dir: str | Path) -> int | None:
    """Read Ultralytics' post-retry batch from the worker-owned checkpoint."""
    output = Path(out_dir)
    candidates = (
        output / "artifacts" / "last.pt",
        output / "workdir" / "train" / "weights" / "last.pt",
    )
    checkpoint_path = next((path for path in candidates if path.is_file()), None)
    if checkpoint_path is None:
        return None
    try:
        # This checkpoint is produced inside the run directory by our own
        # allowlisted trainer, rather than supplied by an API caller.
        checkpoint = torch.load(
            checkpoint_path,
            map_location="cpu",
            weights_only=False,
        )
        value = checkpoint.get("train_args", {}).get("batch")
        return int(value) if value is not None else None
    except Exception:
        # Failure reporting must survive truncated or otherwise unreadable
        # self-produced checkpoints and still persist the stderr reason.
        return None


def classify_failure(
    *,
    error: BaseException | None = None,
    stderr: str | None = None,
    exit_code: int | None = None,
    out_dir: str | Path | None = None,
) -> FailureReport:
    """Build a stable reason using exception, log-regex, and exit-137 signals."""
    tail = filter_dataloader_noise(stderr or "")
    if not tail and out_dir is not None:
        tail = read_stderr_tail(out_dir)
    error_text = filter_dataloader_noise(str(error)) if error is not None else ""
    evidence = "\n".join(part for part in (error_text, tail) if part)
    is_oom = bool(
        isinstance(error, torch.cuda.OutOfMemoryError)
        or OOM_PATTERN.search(evidence)
        or exit_code in {137, -9}
    )
    effective_batch = (
        read_effective_batch(out_dir) if out_dir is not None else None
    )

    parts = [
        (
            "메모리 부족으로 학습이 중단되었습니다."
            if is_oom
            else "학습 워커가 실패하여 학습이 중단되었습니다."
        )
    ]
    if effective_batch is not None:
        parts.append(f"실제 batch: {effective_batch}")
    if exit_code is not None:
        normalized_exit = 137 if exit_code == -9 else exit_code
        parts.append(f"종료 코드: {normalized_exit}")
    if evidence:
        parts.append(evidence)
    reason = "\n".join(parts)[:MAX_REASON_CHARS]
    return FailureReport(
        reason=reason,
        is_oom=is_oom,
        effective_batch=effective_batch,
        exit_code=exit_code,
    )


def persist_worker_failure(
    run_id: int,
    owner_id: int,
    dsn: str,
    report: FailureReport,
    *,
    out_dir: str | Path | None = None,
) -> bool:
    """Persist the first failure exactly once from the synchronous worker."""
    artifact_bytes = 0
    if out_dir is not None:
        try:
            artifact_bytes = collect_run_artifacts(out_dir)
        except (OSError, ValueError):
            artifact_bytes = path_tree_bytes(Path(out_dir) / "artifacts")
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE training_runs
                SET state='failed',
                    finished_at=COALESCE(finished_at, now()),
                    error=COALESCE(error, %s),
                    artifact_bytes=%s
                WHERE id=%s AND owner_id=%s AND state='running'
                """,
                (report.reason, artifact_bytes, run_id, owner_id),
            )
            updated = cursor.rowcount == 1
            if updated:
                increase_bytes_used_sync(cursor, owner_id, artifact_bytes)
            return updated
