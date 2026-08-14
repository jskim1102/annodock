"""Exercise the real upload and detached-training boundary with COCO8."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402
import torch  # noqa: E402

from app.db import create_engine, create_session_factory  # noqa: E402
from app.models import TrainingRun, UserStorage  # noqa: E402
from app.services.proc_identity import read_process_identity  # noqa: E402
from app.services.reaper import reconcile_training_runs  # noqa: E402
from app.services.storage import contained_storage_path  # noqa: E402
from ultralytics.data import utils as data_utils  # noqa: E402


WEIGHT_PATH = (BACKEND_ROOT / "weights" / "yolo26n.pt").resolve()
CHUNK_SIZE = 4 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30.0
JOB_TIMEOUT_SECONDS = 180.0
RUN_TIMEOUT_SECONDS = 15 * 60.0
POLL_INTERVAL_SECONDS = 1.0
EXPECTED_IMAGE_COUNT = 8
EXPECTED_CLASS_COUNT = 80
ARTIFACT_NAMES = ("best.pt", "last.pt", "results.csv")
DEFAULT_TRAIN_IMGSZ = 640
DEFAULT_TRAIN_EPOCHS = 2
TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


class ApiFailure(RuntimeError):
    """An API response failure whose message preserves FastAPI's detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class SmokeAuth:
    user_id: int
    access_token: str
    refresh_token: str


def _emit(message: str) -> None:
    print(message, flush=True)


def _literal_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE literally, matching dev.sh without shell evaluation."""
    if not path.is_file():
        raise AssertionError(f"project env file is missing: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.rstrip("\r")
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise AssertionError(f"invalid .env line {line_number}")
        values[key] = value
    return values


def _required_env(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise AssertionError(f"{key} is required in {PROJECT_ENV_FILE}")
    return value


def _api_base_url(values: dict[str, str]) -> str:
    raw_port = _required_env(values, "BACKEND_PORT")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise AssertionError("BACKEND_PORT must be an integer") from error
    if not 1 <= port <= 65_535:
        raise AssertionError("BACKEND_PORT is outside the TCP port range")
    return f"http://127.0.0.1:{port}"


def _auth_base_url(values: dict[str, str]) -> str:
    raw_port = _required_env(values, "AUTH_PORT")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise AssertionError("AUTH_PORT must be an integer") from error
    if not 1 <= port <= 65_535:
        raise AssertionError("AUTH_PORT is outside the TCP port range")
    return f"http://127.0.0.1:{port}"


def _host_database_url(values: dict[str, str]) -> str:
    return _required_env(values, "DATABASE_URL").replace(
        "harness-shared-postgres:5432",
        "localhost:5435",
    )


def _storage_dir(values: dict[str, str]) -> Path:
    configured = Path(_required_env(values, "STORAGE_DIR")).expanduser()
    if not configured.is_absolute():
        configured = BACKEND_ROOT / configured
    return configured.resolve()


def _training_imgsz() -> int:
    raw_value = os.environ.get(
        "DEEPLABEL_SMOKE_IMGSZ",
        str(DEFAULT_TRAIN_IMGSZ),
    )
    try:
        value = int(raw_value)
    except ValueError as error:
        raise AssertionError("DEEPLABEL_SMOKE_IMGSZ must be an integer") from error
    if not 32 <= value <= 2_048:
        raise AssertionError("DEEPLABEL_SMOKE_IMGSZ must be between 32 and 2048")
    return value


def _training_epochs() -> int:
    raw_value = os.environ.get(
        "DEEPLABEL_SMOKE_EPOCHS",
        str(DEFAULT_TRAIN_EPOCHS),
    )
    try:
        value = int(raw_value)
    except ValueError as error:
        raise AssertionError("DEEPLABEL_SMOKE_EPOCHS must be an integer") from error
    if not 2 <= value <= 100:
        raise AssertionError("DEEPLABEL_SMOKE_EPOCHS must be between 2 and 100")
    return value


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response.text.strip() or f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False, sort_keys=True)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _expect(response: httpx.Response, status_code: int) -> httpx.Response:
    if response.status_code == status_code:
        return response
    if not response.is_success:
        raise ApiFailure(response.status_code, _error_detail(response))
    raise AssertionError(
        f"expected HTTP {status_code}, received {response.status_code}"
    )


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AssertionError("API returned a non-JSON success response") from error


def _create_smoke_auth(
    auth_base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> SmokeAuth:
    suffix = uuid.uuid4().hex
    username = f"smoke-{suffix[:24]}"
    email = f"smoke-{suffix}@annodock.com"
    password = f"Smoke-{suffix}-A1!"
    with httpx.Client(
        base_url=auth_base_url,
        timeout=HTTP_TIMEOUT_SECONDS,
        transport=transport,
    ) as client:
        signup = _json_body(
            _expect(
                client.post(
                    "/auth/signup",
                    json={
                        "username": username,
                        "email": email,
                        "password": password,
                    },
                ),
                201,
            )
        )
        tokens = _json_body(
            _expect(
                client.post(
                    "/auth/login",
                    json={"identifier": email, "password": password},
                ),
                200,
            )
        )
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            raise AssertionError("auth login did not return an access token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AssertionError("auth login did not return a refresh token")
        me = _json_body(
            _expect(
                client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                ),
                200,
            )
        )
        user_id = int(signup["id"])
        if int(me.get("id", -1)) != user_id:
            raise AssertionError("auth /me returned a different user")
        return SmokeAuth(
            user_id=user_id,
            access_token=access_token,
            refresh_token=refresh_token,
        )


def _logout_smoke_auth(
    auth_base_url: str,
    auth: SmokeAuth,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    with httpx.Client(
        base_url=auth_base_url,
        timeout=HTTP_TIMEOUT_SECONDS,
        transport=transport,
    ) as client:
        _expect(
            client.post(
                "/auth/logout",
                json={"refresh_token": auth.refresh_token},
            ),
            204,
        )


def _poll_get(
    client: httpx.Client,
    path: str,
    *,
    deadline: float,
    phase: str,
) -> httpx.Response:
    """Retry only transport failures while a bounded poll is in progress."""
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{phase} timed out")
        try:
            return client.get(path)
        except TRANSIENT_HTTP_ERRORS as error:
            _emit(f"{phase}_connection_retry={type(error).__name__}")
            time.sleep(POLL_INTERVAL_SECONDS)


def _prepare_coco8_archive(temp_root: Path) -> tuple[Path, int]:
    datasets_dir = (temp_root / "datasets").resolve()
    datasets_dir.mkdir()
    original_datasets_dir = data_utils.DATASETS_DIR
    original_check_font = data_utils.check_font
    data_utils.DATASETS_DIR = datasets_dir
    data_utils.check_font = lambda *args, **kwargs: None
    try:
        dataset = data_utils.check_det_dataset("coco8.yaml")
    finally:
        data_utils.DATASETS_DIR = original_datasets_dir
        data_utils.check_font = original_check_font

    dataset_root = Path(dataset["path"]).resolve()
    try:
        dataset_root.relative_to(datasets_dir)
    except ValueError as error:
        raise AssertionError(
            f"COCO8 escaped the temporary datasets directory: {dataset_root}"
        ) from error

    raw_names = dataset.get("names")
    if isinstance(raw_names, list):
        names = {index: str(name) for index, name in enumerate(raw_names)}
    elif isinstance(raw_names, dict):
        names = {int(index): str(name) for index, name in raw_names.items()}
    else:
        raise AssertionError("COCO8 names are missing")
    if sorted(names) != list(range(EXPECTED_CLASS_COUNT)):
        raise AssertionError("COCO8 must expose contiguous class ids 0..79")

    # Some real COCO class names contain spaces, while the plain-text class
    # detector intentionally accepts only one token per line. A names mapping
    # in YAML preserves the official names and is the production-supported
    # representation for this dataset.
    classes_path = dataset_root / "classes.yaml"
    classes_path.write_text(
        json.dumps({"names": names}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    image_files = [
        path
        for path in (dataset_root / "images").rglob("*")
        if path.is_file()
    ]
    if len(image_files) != EXPECTED_IMAGE_COUNT:
        raise AssertionError(
            f"expected {EXPECTED_IMAGE_COUNT} COCO8 images, found {len(image_files)}"
        )

    # The production splitter stratifies each class-presence signature. COCO8's
    # tiny heterogeneous labels can otherwise yield an empty valid split even
    # when the dataset-level ratio is valid. Keep the real COCO8 pixels while
    # giving every smoke image one identical, valid YOLO box so both splits are
    # deterministic and the test remains about the application boundary.
    for image_path in image_files:
        relative = image_path.relative_to(dataset_root / "images")
        label_path = (dataset_root / "labels" / relative).with_suffix(".txt")
        label_path.parent.mkdir(parents=True, exist_ok=True)
        label_path.write_text("0 0.500000 0.500000 0.250000 0.250000\n", encoding="utf-8")

    archive_path = temp_root / "coco8.zip"
    source_files = sorted(
        path for path in dataset_root.rglob("*") if path.is_file()
    )
    extracted_size = sum(path.stat().st_size for path in source_files)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        for path in source_files:
            archive.write(path, path.relative_to(dataset_root).as_posix())
    if not archive_path.is_file() or archive_path.stat().st_size <= 0:
        raise AssertionError("COCO8 ZIP was not created")
    _emit(f"coco8_path={dataset_root}")
    _emit(f"coco8_image_count={len(image_files)}")
    _emit(f"coco8_class_count={len(names)}")
    _emit(f"coco8_smoke_label_count={len(image_files)}")
    _emit(f"coco8_zip_bytes={archive_path.stat().st_size}")
    _emit(f"coco8_extracted_bytes={extracted_size}")
    return archive_path, extracted_size


def _create_and_ingest_dataset(
    client: httpx.Client,
    archive_path: Path,
    extracted_size: int,
) -> int:
    dataset_name = f"smoke-coco8-{uuid.uuid4().hex}"
    _emit("dataset_create=started")
    created = _json_body(
        _expect(
            client.post("/api/datasets", json={"name": dataset_name}),
            201,
        )
    )
    dataset_id = int(created["id"])
    _emit(f"dataset_id={dataset_id}")

    archive_size = archive_path.stat().st_size
    preflight = {
        "total_size": archive_size,
        "largest_file_size": archive_size,
        "file_count": 1,
        "expected_extracted_size": extracted_size,
    }
    _emit("upload_preflight=started")
    _expect(
        client.post(
            f"/api/datasets/{dataset_id}/upload-batches/preflight",
            json=preflight,
        ),
        204,
    )
    _emit("upload_preflight=PASS")

    session = _json_body(
        _expect(
            client.post(
                f"/api/datasets/{dataset_id}/uploads",
                json={
                    "filename": archive_path.name,
                    "size": archive_size,
                    "chunk_size": CHUNK_SIZE,
                    "kind": "zip",
                    "file_count": 1,
                    "expected_extracted_size": extracted_size,
                },
            ),
            201,
        )
    )
    upload_id = int(session["upload_id"])
    chunk_size = int(session["chunk_size"])
    if chunk_size <= 0:
        raise AssertionError("upload API returned a non-positive chunk size")
    _emit(f"upload_id={upload_id}")
    _emit(f"upload_chunk_size={chunk_size}")

    chunk_number = 0
    uploaded_bytes = 0
    with archive_path.open("rb") as source:
        while chunk := source.read(chunk_size):
            _expect(
                client.put(
                    f"/api/uploads/{upload_id}/chunks/{chunk_number}",
                    content=chunk,
                ),
                204,
            )
            uploaded_bytes += len(chunk)
            _emit(f"upload_chunk={chunk_number} bytes={len(chunk)}")
            chunk_number += 1
    if uploaded_bytes != archive_size or chunk_number == 0:
        raise AssertionError("uploaded byte count does not match the COCO8 ZIP")

    completed = _json_body(
        _expect(
            client.post(
                f"/api/datasets/{dataset_id}/upload-batches/complete",
                json={"upload_ids": [upload_id]},
            ),
            202,
        )
    )
    job_id = int(completed["job_id"])
    _emit(f"job_id={job_id}")

    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while True:
        response = _poll_get(
            client,
            f"/api/jobs/{job_id}",
            deadline=deadline,
            phase="job_poll",
        )
        job = _json_body(_expect(response, 200))
        _emit(
            "job_state="
            f"{job['state']} phase={job['phase']} "
            f"processed={job['processed']} failed={job['failed']}"
        )
        if job["state"] == "done":
            break
        if job["state"] == "failed":
            raise AssertionError("COCO8 ingestion job failed")
        time.sleep(POLL_INTERVAL_SECONDS)

    detail = _json_body(
        _expect(client.get(f"/api/datasets/{dataset_id}"), 200)
    )
    if detail.get("status") != "ready":
        raise AssertionError(f"dataset did not become ready: {detail.get('status')}")
    if int(detail.get("image_count", -1)) != EXPECTED_IMAGE_COUNT:
        raise AssertionError(
            f"dataset image count is {detail.get('image_count')}, expected 8"
        )
    if int(detail.get("class_count", -1)) != EXPECTED_CLASS_COUNT:
        raise AssertionError(
            f"dataset class count is {detail.get('class_count')}, expected 80"
        )
    _emit(f"dataset_image_count={detail['image_count']}")
    _emit(f"dataset_class_count={detail['class_count']}")
    return dataset_id


def _submit_training(
    client: httpx.Client,
    dataset_id: int,
    *,
    imgsz: int,
    epochs: int,
) -> int:
    _emit("training_submit=started")
    result = _json_body(
        _expect(
            client.post(
                f"/api/datasets/{dataset_id}/train",
                json={
                    "weights": "yolo26n.pt",
                    "epochs": epochs,
                    "imgsz": imgsz,
                    "batch": -1,
                    "split_mode": "2way",
                    "ratios": {"train": 0.8, "valid": 0.2},
                },
            ),
            201,
        )
    )
    run_id = int(result["run_id"])
    warnings = result.get("warnings")
    if not isinstance(warnings, list) or any(
        not isinstance(warning, str) for warning in warnings
    ):
        raise AssertionError("training response must always contain warnings[]")
    _emit(f"run_id={run_id}")
    _emit(f"training_imgsz={imgsz}")
    _emit(f"training_epochs={epochs}")
    _emit("run_warnings=" + json.dumps(warnings, ensure_ascii=False))
    return run_id


async def _verify_detached_worker(
    database_url: str,
    storage_dir: Path,
    run_id: int,
) -> None:
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            run = await session.get(TrainingRun, run_id)
            if run is None:
                raise AssertionError(f"training run {run_id} is missing from DB")
            if run.state != "running":
                raise AssertionError(
                    f"run left running before worker verification: {run.state}"
                )
            pid = run.pid
            pid_started_at = run.pid_started_at
            boot_id = run.boot_id
        if pid is None or pid_started_at is None or boot_id is None:
            raise AssertionError("detached worker identity was not persisted")
        identity = read_process_identity(pid)
        if identity is None:
            raise AssertionError(f"detached worker PID {pid} is not alive")
        if identity.started_at != pid_started_at or identity.boot_id != boot_id:
            raise AssertionError("detached worker identity differs from the DB row")
        if os.getsid(pid) != pid:
            raise AssertionError("training worker is not a session leader")
        _emit(f"worker_pid={pid}")
        _emit(f"worker_process_state={identity.state}")
        _emit(f"worker_session_id={os.getsid(pid)}")
        _emit("worker_identity=PASS")

        reconciled = await reconcile_training_runs(
            session_factory,
            storage_dir=storage_dir,
        )
        _emit(
            "reaper_result="
            f"preserved:{reconciled.preserved},failed:{reconciled.failed},"
            f"pending_identity:{reconciled.pending_identity}"
        )
        if reconciled.preserved < 1 or reconciled.failed != 0:
            raise AssertionError("reaper did not preserve the live detached worker")
        async with session_factory() as session:
            after = await session.get(TrainingRun, run_id)
            if after is None or after.state not in {"running", "done"}:
                raise AssertionError("reaper changed the live run to a failure state")
        _emit("reaper_preserved=PASS")
    finally:
        await engine.dispose()


def _poll_training(
    client: httpx.Client,
    run_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while True:
        run = _json_body(
            _expect(
                _poll_get(
                    client,
                    f"/api/runs/{run_id}",
                    deadline=deadline,
                    phase="run_poll",
                ),
                200,
            )
        )
        metrics = _json_body(
            _expect(
                _poll_get(
                    client,
                    f"/api/runs/{run_id}/metrics",
                    deadline=deadline,
                    phase="metrics_poll",
                ),
                200,
            )
        )
        log_response = _expect(
            _poll_get(
                client,
                f"/api/runs/{run_id}/log?tail=200",
                deadline=deadline,
                phase="log_poll",
            ),
            200,
        )
        if not isinstance(metrics, list):
            raise AssertionError("metrics API did not return a list")
        log = log_response.text
        _emit(
            f"run_state={run['state']} epoch={run['epoch']}/{run['epochs']} "
            f"metrics={len(metrics)} log_bytes={len(log.encode('utf-8'))}"
        )
        if run["state"] == "done":
            return run, metrics, log
        if run["state"] in {"failed", "canceled"}:
            raise AssertionError(run.get("error") or f"run ended as {run['state']}")
        time.sleep(POLL_INTERVAL_SECONDS)


def _download_artifacts(
    client: httpx.Client,
    run_id: int,
) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for name in ARTIFACT_NAMES:
        response = _expect(
            client.get(f"/api/runs/{run_id}/artifacts/{name}"),
            200,
        )
        size = len(response.content)
        if size <= 0:
            raise AssertionError(f"downloaded artifact is empty: {name}")
        sizes[name] = size
        _emit(f"http_artifact_{name.replace('.', '_')}_bytes={size}")
    return sizes


async def _verify_completed_storage(
    database_url: str,
    storage_dir: Path,
    run_id: int,
    http_sizes: dict[str, int],
) -> None:
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            run = await session.get(TrainingRun, run_id)
            if run is None:
                raise AssertionError(f"training run {run_id} disappeared")
            if run.state != "done":
                raise AssertionError(f"DB run state is not done: {run.state}")
            out_dir_value = run.out_dir
            persisted_artifact_bytes = run.artifact_bytes
            usage = await session.get(UserStorage, run.owner_id)
        if not out_dir_value or Path(out_dir_value).is_absolute():
            raise AssertionError(f"run out_dir is not storage-relative: {out_dir_value}")
        out_dir = contained_storage_path(storage_dir, out_dir_value)
        artifacts_dir = out_dir / "artifacts"
        _emit(f"run_out_dir={out_dir_value}")
        for name in ARTIFACT_NAMES:
            artifact = artifacts_dir / name
            if not artifact.is_file() or artifact.stat().st_size <= 0:
                raise AssertionError(f"storage artifact is missing or empty: {artifact}")
            disk_size = artifact.stat().st_size
            if disk_size != http_sizes[name]:
                raise AssertionError(
                    f"HTTP and storage sizes differ for {name}: "
                    f"{http_sizes[name]} != {disk_size}"
                )
            _emit(f"storage_artifact_{name.replace('.', '_')}_bytes={disk_size}")
        total_artifact_bytes = sum(
            path.stat().st_size
            for path in artifacts_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if persisted_artifact_bytes != total_artifact_bytes:
            raise AssertionError(
                "persisted artifact accounting differs from storage: "
                f"{persisted_artifact_bytes} != {total_artifact_bytes}"
            )
        if usage is None or usage.bytes_used < total_artifact_bytes:
            raise AssertionError("user storage counter omits training artifacts")
        _emit(f"run_artifact_bytes={total_artifact_bytes}")
        _emit(f"user_storage_bytes={usage.bytes_used}")
        _emit("storage_artifacts=PASS")
    finally:
        await engine.dispose()


def main() -> None:
    if not torch.cuda.is_available():
        raise AssertionError("GPU smoke train requires torch CUDA")
    if not WEIGHT_PATH.is_file() or WEIGHT_PATH.stat().st_size <= 0:
        raise AssertionError(f"missing prefetched weight: {WEIGHT_PATH}")

    env = _literal_env(PROJECT_ENV_FILE)
    api_base_url = _api_base_url(env)
    auth_base_url = _auth_base_url(env)
    database_url = _host_database_url(env)
    storage_dir = _storage_dir(env)
    training_imgsz = _training_imgsz()
    training_epochs = _training_epochs()
    free_bytes, total_bytes = torch.cuda.mem_get_info()
    _emit(f"cuda_device={torch.cuda.get_device_name(0)}")
    _emit(f"cuda_mem_free_bytes={free_bytes}")
    _emit(f"cuda_mem_total_bytes={total_bytes}")
    _emit(f"cuda_mem_free_gib={free_bytes / (1 << 30):.3f}")
    _emit(f"cuda_mem_total_gib={total_bytes / (1 << 30):.3f}")
    _emit(f"api_base_url={api_base_url}")
    _emit(f"job_timeout_seconds={JOB_TIMEOUT_SECONDS:.0f}")
    _emit(f"run_timeout_seconds={RUN_TIMEOUT_SECONDS:.0f}")

    os.chdir(BACKEND_ROOT)
    smoke_auth = _create_smoke_auth(auth_base_url)
    _emit(f"auth_user_id={smoke_auth.user_id}")
    _emit("auth_local_roundtrip=PASS")
    try:
        with tempfile.TemporaryDirectory(prefix="deeplabel-smoke-") as temp:
            archive_path, extracted_size = _prepare_coco8_archive(
                Path(temp).resolve()
            )
            with httpx.Client(
                base_url=api_base_url,
                timeout=HTTP_TIMEOUT_SECONDS,
                headers={
                    "Authorization": f"Bearer {smoke_auth.access_token}",
                },
            ) as client:
                health = _json_body(_expect(client.get("/api/health"), 200))
                if health != {"status": "ok"}:
                    raise AssertionError(f"unexpected health response: {health}")
                _emit("api_health=PASS")
                dataset_id = _create_and_ingest_dataset(
                    client,
                    archive_path,
                    extracted_size,
                )
                run_id = _submit_training(
                    client,
                    dataset_id,
                    imgsz=training_imgsz,
                    epochs=training_epochs,
                )
                asyncio.run(
                    _verify_detached_worker(database_url, storage_dir, run_id)
                )
                run, metrics, log = _poll_training(client, run_id)
                if run.get("state") != "done":
                    raise AssertionError("training run did not finish as done")
                if len(metrics) < training_epochs:
                    raise AssertionError(
                        f"expected at least {training_epochs} metric rows, "
                        f"found {len(metrics)}"
                    )
                if not log.strip():
                    raise AssertionError("training log is empty")
                _emit(f"final_metric_count={len(metrics)}")
                _emit(f"final_log_bytes={len(log.encode('utf-8'))}")
                http_sizes = _download_artifacts(client, run_id)
                asyncio.run(
                    _verify_completed_storage(
                        database_url,
                        storage_dir,
                        run_id,
                        http_sizes,
                    )
                )
    finally:
        _logout_smoke_auth(auth_base_url, smoke_auth)
        _emit("auth_logout=PASS")

    if list(BACKEND_ROOT.glob("*.pt")):
        raise AssertionError("weight file escaped into backend root")
    _emit("artifacts_verified=best.pt,last.pt,results.csv")
    _emit("smoke_train=PASS")


if __name__ == "__main__":
    main()
