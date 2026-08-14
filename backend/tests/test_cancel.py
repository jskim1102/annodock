from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import TrainingRun
from app.services import cancel
from app.services.proc_identity import read_process_identity


pytestmark = pytest.mark.asyncio


def _run(
    out_dir: Path,
    *,
    state: str = "running",
    pid: int | None = None,
    pid_started_at: str | None = None,
    boot_id: str | None = None,
) -> TrainingRun:
    return TrainingRun(
        owner_id=1,
        dataset_id=None,
        dataset_name=f"test-cancel-{uuid4().hex}",
        weights="yolo26n.pt",
        epochs=1,
        imgsz=640,
        batch=4,
        split_mode="2way",
        ratios={"train": 0.8, "valid": 0.2},
        seed=1,
        state=state,
        pid=pid,
        pid_started_at=pid_started_at,
        boot_id=boot_id,
        started_at=datetime.now(timezone.utc),
        out_dir=str(out_dir),
    )


async def test_cancel_pid_null_becomes_canceled_without_killpg(
    client,
    app,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with app.state.session_factory() as session:
        run = _run(tmp_path)
        session.add(run)
        await session.commit()
        run_id = run.id

    def unexpected_killpg(_pgid: int, _signal: int) -> None:
        raise AssertionError("pid-null cancellation must not signal a process group")

    monkeypatch.setattr(cancel.os, "killpg", unexpected_killpg)

    response = await client.post(f"/api/runs/{run_id}/cancel")

    assert response.status_code == 202
    assert response.json() == {"run_id": run_id, "state": "canceled"}
    async with app.state.session_factory() as session:
        persisted = await session.get(TrainingRun, run_id)
        assert persisted is not None
        assert persisted.state == "canceled"
        assert persisted.finished_at is not None
        assert persisted.error is None


async def test_cancel_rejects_terminal_run(client, app, tmp_path: Path) -> None:
    async with app.state.session_factory() as session:
        run = _run(tmp_path, state="done")
        session.add(run)
        await session.commit()
        run_id = run.id

    response = await client.post(f"/api/runs/{run_id}/cancel")

    assert response.status_code == 409


async def test_cancel_kills_real_process_group_including_child(
    app,
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    parent_code = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"open({str(child_pid_file)!r},'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if child_pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        identity = read_process_identity(process.pid)
        assert identity is not None
        assert os.getpgid(process.pid) == process.pid

        async with app.state.session_factory() as session:
            run = _run(
                tmp_path,
                pid=process.pid,
                pid_started_at=identity.started_at,
                boot_id=identity.boot_id,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        result = await cancel.cancel_training_run(
            app.state.session_factory,
            run_id,
            1,
            term_grace_seconds=0.05,
            kill_wait_seconds=2.0,
            poll_interval_seconds=0.01,
        )

        assert result.state == "canceled"
        assert read_process_identity(process.pid) is None
        assert read_process_identity(child_pid) is None
        async with app.state.session_factory() as session:
            persisted = await session.get(TrainingRun, run_id)
            assert persisted is not None
            assert persisted.state == "canceled"
            assert persisted.error is None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(process.pid, os.WNOHANG)
        except ChildProcessError:
            pass
