"""Persisted Ultralytics arguments and compatibility defaults."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


RTX_3090_TRAINING_ARGS: dict[str, object] = {
    "exclude_unlabeled_images": False,
    "include_unlabeled_images_in_test": False,
    "device": 0,
    "optimizer": "auto",
    "lr0": 0.01,
    "lrf": 0.01,
    "warmup_epochs": 3.0,
    "cos_lr": True,
    "patience": 30,
    "augment": True,
    "mosaic": 1.0,
    "mixup": 0.0,
    "copy_paste": 0.0,
    "close_mosaic": 10,
    "hsv_h": 0.015,
    "hsv_s": 0.7,
    "hsv_v": 0.4,
    "fliplr": 0.5,
    "scale": 0.5,
    "translate": 0.1,
    "workers": 8,
    "cache": "ram",
    "amp": True,
    "compile": True,
    "deterministic": False,
    "save_period": 25,
    "multi_scale": 0.0,
}

# Rows created before the training-argument migration used these Ultralytics
# defaults implicitly. Keeping them separate prevents a restarted legacy run
# from silently switching to the newer RTX 3090 recipe.
LEGACY_TRAINING_ARGS: dict[str, object] = {
    "exclude_unlabeled_images": False,
    "include_unlabeled_images_in_test": False,
    "device": 0,
    "optimizer": "auto",
    "lr0": 0.01,
    "lrf": 0.01,
    "warmup_epochs": 3.0,
    "cos_lr": False,
    "patience": 100,
    # Legacy detection training used augmentation; its old augment=False
    # Ultralytics default controlled prediction TTA only.
    "augment": True,
    "mosaic": 1.0,
    "mixup": 0.0,
    "copy_paste": 0.0,
    "close_mosaic": 10,
    "hsv_h": 0.015,
    "hsv_s": 0.7,
    "hsv_v": 0.4,
    "fliplr": 0.5,
    "scale": 0.5,
    "translate": 0.1,
    "workers": 8,
    "cache": "none",
    "amp": True,
    "compile": False,
    "deterministic": True,
    "save_period": -1,
    "multi_scale": 0.0,
}

TRAINING_ARGUMENT_KEYS = tuple(RTX_3090_TRAINING_ARGS)
APP_ONLY_TRAINING_ARGUMENT_KEYS = (
    "exclude_unlabeled_images",
    "include_unlabeled_images_in_test",
    "augment",
)

# Ultralytics' `augment` argument controls prediction TTA. Detection training
# always constructs an augmentation pipeline, so its individual transforms
# must be neutralized when the application's training toggle is off.
DETECTION_AUGMENTATION_KEYS = (
    "mosaic", "mixup", "cutmix", "copy_paste",
    "hsv_h", "hsv_s", "hsv_v",
    "degrees", "translate", "scale", "shear", "perspective",
    "flipud", "fliplr", "bgr", "multi_scale",
)


def normalize_training_args(
    persisted: Mapping[str, Any] | None,
) -> dict[str, object]:
    """Return a complete allowlisted argument snapshot for a run."""
    if not persisted:
        return dict(LEGACY_TRAINING_ARGS)
    return {
        key: persisted.get(key, LEGACY_TRAINING_ARGS[key])
        for key in TRAINING_ARGUMENT_KEYS
    }


def ultralytics_training_args(
    persisted: Mapping[str, Any] | None,
) -> dict[str, object]:
    """Translate stored UI values to arguments accepted by model.train()."""
    arguments = normalize_training_args(persisted)
    augment = arguments["augment"]
    for key in APP_ONLY_TRAINING_ARGUMENT_KEYS:
        arguments.pop(key, None)
    if not augment:
        arguments.update({key: 0.0 for key in DETECTION_AUGMENTATION_KEYS})
        # Also suppress the optional library's default Blur/CLAHE transforms.
        arguments["augmentations"] = []
    if arguments["cache"] == "none":
        arguments["cache"] = False
    return arguments
