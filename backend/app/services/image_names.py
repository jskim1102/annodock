"""Collision-safe image stem allocation shared by ingest and dataset merge."""

from __future__ import annotations

from pathlib import Path


PairKey = tuple[str | None, str]


def available_pair_key(
    key: PairKey,
    occupied: set[PairKey],
    extension: str,
) -> PairKey:
    split, stem = key
    max_stem_length = max(1, 1024 - len(extension))
    index = 1
    while True:
        suffix = f" ({index})"
        base_length = max(0, max_stem_length - len(suffix))
        candidate = (split, f"{stem[:base_length]}{suffix}")
        if candidate not in occupied:
            return candidate
        index += 1


def replace_filename_stem(rel_path: str, stem: str) -> str:
    relative = Path(rel_path)
    return relative.with_name(f"{stem}{relative.suffix}").as_posix()
