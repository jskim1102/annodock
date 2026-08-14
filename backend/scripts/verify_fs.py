"""Measure filesystem and offline-download assumptions using temporary files."""

from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from ultralytics import YOLO  # noqa: E402
from ultralytics.utils import downloads  # noqa: E402

from app.inference.models_dir import PRESET_MODELS  # noqa: E402


WEIGHTS_DIR = (BACKEND_ROOT / "weights").resolve()


def _weights_snapshot() -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for name in PRESET_MODELS:
        path = WEIGHTS_DIR / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise AssertionError(f"missing prefetched weight: {path}")
        stat = path.stat()
        snapshot[name] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def main() -> None:
    before_weights = _weights_snapshot()

    with tempfile.TemporaryDirectory(prefix="dataset-trainer-fs-") as temp:
        temp_root = Path(temp).resolve()

        original = temp_root / "original.bin"
        linked = temp_root / "linked.bin"
        renamed = temp_root / "renamed.bin"
        original.write_bytes(b"hardlink-observation")
        os.link(original, linked)
        if original.stat().st_ino != linked.stat().st_ino:
            raise AssertionError("os.link did not share an inode")
        original.rename(renamed)
        if linked.read_bytes() != b"hardlink-observation":
            raise AssertionError("hardlink did not survive source rename")
        print(f"hardlink_inode={linked.stat().st_ino}")
        print("hardlink_survives_rename=True")

        shm_root = Path("/dev/shm")
        if not shm_root.is_dir() or not os.access(shm_root, os.W_OK):
            raise AssertionError("/dev/shm unavailable for cross-device OSError")
        with tempfile.TemporaryDirectory(
            prefix="dataset-trainer-fs-", dir=shm_root
        ) as shm_temp:
            cross_source = Path(shm_temp) / "cross-device.bin"
            fallback_target = temp_root / "copy-fallback.bin"
            cross_source.write_bytes(b"copy2-fallback-observation")
            try:
                os.link(cross_source, fallback_target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise AssertionError(
                        f"expected EXDEV from cross-device link, got {error.errno}"
                    ) from error
                shutil.copy2(cross_source, fallback_target)
                fallback_errno = error.errno
            else:
                raise AssertionError("cross-device os.link unexpectedly succeeded")
            if fallback_target.read_bytes() != cross_source.read_bytes():
                raise AssertionError("copy2 fallback changed file contents")
            print(f"hardlink_fallback_errno={fallback_errno}")
            print("copy2_fallback=True")

        split_source = temp_root / "split-source"
        split_moved = temp_root / "split-moved"
        split_link = temp_root / "split-link"
        split_source.mkdir()
        split_link.symlink_to(split_source, target_is_directory=True)
        split_source.rename(split_moved)
        if not split_link.is_symlink() or split_link.exists():
            raise AssertionError("split symlink did not become dangling after rename")
        print("split_symlink_dangling_after_rename=True")

        offline_target = temp_root / "offline" / "yolo26n.pt"
        with mock.patch.dict(os.environ, {"YOLO_OFFLINE": "true"}):
            with mock.patch.object(
                downloads.request,
                "urlopen",
                side_effect=OSError("simulated offline transport"),
            ):
                try:
                    YOLO(str(offline_target))
                except ConnectionError as error:
                    offline_error = str(error)
                else:
                    raise AssertionError("YOLO_OFFLINE download unexpectedly succeeded")
        if offline_target.exists():
            raise AssertionError("offline reproduction left a partial weight")
        print(f"offline_error_type={ConnectionError.__name__}")
        print(f"offline_error={offline_error}")
        print("yolo_offline_failure=True")

    if _weights_snapshot() != before_weights:
        raise AssertionError("prefetched weights changed during filesystem checks")
    print(f"prefetched_weights_unchanged={len(before_weights)}")
    print("verify_fs=PASS")


if __name__ == "__main__":
    main()
