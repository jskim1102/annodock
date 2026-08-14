"""Verify pinned CUDA and Ultralytics source assumptions without changing settings."""

from __future__ import annotations

import inspect
from pathlib import Path

import torch
import ultralytics
from ultralytics.engine.trainer import BaseTrainer
from ultralytics.engine.validator import BaseValidator
from ultralytics.utils.downloads import attempt_download_asset


EXPECTED_ULTRALYTICS_VERSION = "8.4.56"


def _position(source: str, snippet: str, start: int = 0) -> int:
    position = source.find(snippet, start)
    if position < 0:
        raise AssertionError(f"installed source no longer contains {snippet!r}")
    return position


def main() -> None:
    if ultralytics.__version__ != EXPECTED_ULTRALYTICS_VERSION:
        raise AssertionError(
            f"expected ultralytics {EXPECTED_ULTRALYTICS_VERSION}, "
            f"got {ultralytics.__version__}"
        )
    if not torch.cuda.is_available():
        raise AssertionError("torch.cuda.is_available() is False")

    free_bytes, total_bytes = torch.cuda.mem_get_info()
    print(f"ultralytics_version={ultralytics.__version__}")
    print(f"torch_version={torch.__version__}")
    print(f"cuda_runtime={torch.version.cuda}")
    print(f"cuda_device={torch.cuda.get_device_name(0)}")
    print(f"cuda_mem_free_bytes={free_bytes}")
    print(f"cuda_mem_total_bytes={total_bytes}")

    train_source = inspect.getsource(BaseTrainer._do_train)
    train_epoch_end = _position(
        train_source, 'self.run_callbacks("on_train_epoch_end")'
    )
    validate = _position(train_source, "self.metrics, self.fitness = self.validate()")
    save_metrics = _position(train_source, "self.save_metrics(")
    fit_epoch_end = _position(
        train_source, 'self.run_callbacks("on_fit_epoch_end")'
    )
    if not train_epoch_end < validate < save_metrics < fit_epoch_end:
        raise AssertionError("epoch callback/validation/save ordering changed")

    trainer_file = Path(inspect.getsourcefile(BaseTrainer) or "").resolve()
    trainer_start = inspect.getsourcelines(BaseTrainer._do_train)[1]
    print(f"trainer_source={trainer_file}")
    print(f"trainer_do_train_line={trainer_start}")
    print(
        "epoch_order="
        "on_train_epoch_end<validate<save_metrics<on_fit_epoch_end"
    )
    print("on_fit_epoch_end_after_validate=True")

    final_source = inspect.getsource(BaseTrainer.final_eval)
    plus_one = _position(final_source, "self.epoch += 1")
    final_callback = _position(
        final_source, 'self.run_callbacks("on_fit_epoch_end")'
    )
    minus_one = _position(final_source, "self.epoch -= 1")
    if not plus_one < final_callback < minus_one:
        raise AssertionError("final_eval epoch+1 callback structure changed")

    validator_source = inspect.getsource(BaseValidator.__call__)
    loss_merge = _position(
        validator_source,
        'trainer.label_loss_items(loss.cpu() / len(self.dataloader), prefix="val")',
    )
    standalone_branch = _position(validator_source, "else:", loss_merge)
    standalone_return = _position(validator_source, "return stats", standalone_branch)
    if not loss_merge < standalone_branch < standalone_return:
        raise AssertionError("standalone final validation now appears to merge val loss")

    print("final_eval_epoch_plus_one=True")
    print("final_eval_uses_standalone_validator=True")
    print("final_eval_contains_val_loss=False")
    print("guard_key=val/box_loss")
    print("guard_skips_final_eval=True")

    download_source = inspect.getsource(attempt_download_asset)
    cwd_lookup = _position(download_source, "if file.exists():")
    weights_lookup = _position(
        download_source, 'elif (SETTINGS["weights_dir"] / file).exists():'
    )
    download_target = _position(
        download_source,
        'safe_download(url=f"{download_url}/{release}/{name}", file=file',
    )
    if not cwd_lookup < weights_lookup < download_target:
        raise AssertionError("weight lookup/download destination ordering changed")

    downloads_file = Path(
        inspect.getsourcefile(attempt_download_asset) or ""
    ).resolve()
    downloads_line = inspect.getsourcelines(attempt_download_asset)[1]
    print(f"downloads_source={downloads_file}")
    print(f"attempt_download_asset_line={downloads_line}")
    print("download_lookup_order=cwd_then_weights_dir")
    print("download_destination=file_argument_relative_to_cwd")
    print("verify_env=PASS")


if __name__ == "__main__":
    main()
