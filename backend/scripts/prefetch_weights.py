"""Download every trusted YOLO preset into the trainer-owned weights directory."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from ultralytics import YOLO  # noqa: E402

from app.inference.models_dir import PRESET_MODELS  # noqa: E402


WEIGHTS_DIR = (BACKEND_ROOT / "weights").resolve()


def main() -> None:
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

    for model_name in PRESET_MODELS:
        model_path = (WEIGHTS_DIR / model_name).resolve()
        if model_path.parent != WEIGHTS_DIR:
            raise RuntimeError(f"unsafe preset path: {model_name!r}")
        YOLO(str(model_path))

    missing = [
        model_name
        for model_name in PRESET_MODELS
        if not (WEIGHTS_DIR / model_name).is_file()
        or (WEIGHTS_DIR / model_name).stat().st_size <= 0
    ]
    if missing:
        raise SystemExit(f"weight prefetch incomplete: {', '.join(missing)}")

    print(f"Prefetched {len(PRESET_MODELS)} models into {WEIGHTS_DIR}")


if __name__ == "__main__":
    main()
