"""Dataset-level atomic ingestion from normalized collected files."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.class_colors import class_color
from app.config import Settings
from app.models import (
    Annotation,
    Dataset,
    DatasetClass,
    Image,
    ImportIssue,
    Project,
    ProjectClass,
    UploadJob,
    UploadSession,
)
from app.services.adapter_coco import CocoAdapterResult, adapt_coco
from app.services.adapter_voc import VocAdapterResult, adapt_voc
from app.services.collect import (
    CollectedFile,
    SourceFile,
    collect_sources,
    collect_zip,
)
from app.services.class_resolution import (
    ClassResolutionNameConflict,
    build_class_resolution_plan,
    project_renames_for_resolutions,
    validate_class_resolutions,
)
from app.services.derive import ImageDecodeError, prepare_image
from app.services.format_detect import (
    detect_formats,
    find_voc_annotation_documents,
)
from app.services.jobs import fail_job, transition_job
from app.services.image_names import (
    PairKey,
    available_pair_key,
    replace_filename_stem,
)
from app.services.labels import (
    IssueData,
    ParsedBox,
    classes_for_ids,
    load_classes_with_source,
    parse_yolo_label,
)
from app.services.quota import increase_bytes_used
from app.services.storage import contained_storage_path, storage_relative_path
from app.services.uploads import assembled_upload_path, upload_directory
from app.services.validate import RejectedFile, validate_image_file
from app.services.zipsafe import ZipIssue, ZipLimits, ZipSafetyError


BeforeCommit = Callable[[], None]
FORMAT_PRIORITY = ("yolo", "coco", "voc")


class ClassResolutionRequired(RuntimeError):
    """Signal that ingestion paused before any dataset content was written."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"upload job {job_id} requires class resolution")
        self.job_id = job_id


def _stem(item: CollectedFile) -> str:
    return Path(item.rel_path).stem


def _issue(kind: str, path: str, detail: str) -> IssueData:
    return IssueData(kind=kind, path=path, detail=detail)


def _ignored_detail(item: CollectedFile) -> str:
    suffix = Path(item.rel_path).suffix.lower()
    if item.kind == "zip" or suffix == ".zip":
        return "nested ZIP archives are not extracted"
    if suffix == ".json":
        return "COCO/JSON content was not recognized as annotations"
    if suffix == ".xml":
        return "VOC/XML content was not recognized as annotations"
    return "not an image, YOLO label, or supported class metadata file"


def _pair_key(item: CollectedFile) -> PairKey:
    return (item.split, _stem(item))


def _replace_item_stem(
    item: CollectedFile,
    stem: str,
) -> CollectedFile:
    return replace(
        item,
        rel_path=replace_filename_stem(item.rel_path, stem),
    )


def _index_pairable_items(
    items: list[CollectedFile],
    *,
    consumed_documents: set[str] | None = None,
) -> tuple[
    dict[PairKey, CollectedFile],
    dict[PairKey, CollectedFile],
    list[IssueData],
]:
    image_candidates: dict[PairKey, list[CollectedFile]] = defaultdict(list)
    label_candidates: dict[PairKey, list[CollectedFile]] = defaultdict(list)
    issues: list[IssueData] = []
    consumed = consumed_documents or set()
    for item in sorted(items, key=lambda candidate: candidate.rel_path):
        if item.kind in {"other", "zip"} and item.rel_path not in consumed:
            issues.append(
                _issue(
                    "ignored_file",
                    item.rel_path,
                    _ignored_detail(item),
                )
            )
        if item.kind not in {"image", "label"}:
            continue
        key = _pair_key(item)
        target = image_candidates if item.kind == "image" else label_candidates
        target[key].append(item)

    image_by_key: dict[PairKey, CollectedFile] = {}
    label_by_key: dict[PairKey, CollectedFile] = {}
    reserved_keys = set(image_candidates) | set(label_candidates)
    occupied_keys = set(reserved_keys)
    ordered_keys = [
        *image_candidates,
        *(key for key in label_candidates if key not in image_candidates),
    ]

    for key in ordered_keys:
        images = image_candidates.get(key, [])
        labels = label_candidates.get(key, [])
        assigned_image_keys: list[PairKey] = []
        for index, image in enumerate(images):
            assigned_key = key
            if index > 0:
                assigned_key = available_pair_key(
                    key,
                    occupied_keys,
                    Path(image.rel_path).suffix,
                )
                occupied_keys.add(assigned_key)
            image_by_key[assigned_key] = image
            assigned_image_keys.append(assigned_key)

        if assigned_image_keys:
            for index, label in enumerate(labels[: len(assigned_image_keys)]):
                label_by_key[assigned_image_keys[index]] = label
            extra_labels = labels[len(assigned_image_keys) :]
        else:
            if labels:
                label_by_key[key] = labels[0]
            extra_labels = labels[1:]

        for label in extra_labels:
            kept = labels[0]
            split, stem = key
            issues.append(
                _issue(
                    "ignored_file",
                    label.rel_path,
                    (
                        "duplicate YOLO label for "
                        f"split={split or 'null'}, stem={stem}; "
                        f"kept lexicographically first path {kept.rel_path}"
                    ),
                )
            )
    return image_by_key, label_by_key, issues


def _adapter_images(
    result: CocoAdapterResult | VocAdapterResult | None,
) -> dict[str, tuple]:
    if result is None:
        return {}
    return {
        image.rel_path: image.boxes
        for image in result.ir.images
    }


def _merge_class_catalogs(
    catalogs: dict[str, dict[int, str]],
    used_formats: set[str],
    source_paths: dict[str, str],
) -> tuple[
    dict[int, str],
    dict[str, dict[int, int]],
    dict[int, str],
]:
    classes: dict[int, str] = {}
    class_id_by_name: dict[str, int] = {}
    remaps: dict[str, dict[int, int]] = {}
    class_sources: dict[int, str] = {}

    for annotation_format in FORMAT_PRIORITY:
        if annotation_format not in used_formats:
            continue
        remap: dict[int, int] = {}
        for source_id, name in sorted(catalogs[annotation_format].items()):
            class_id = class_id_by_name.get(name)
            if class_id is None:
                if source_id not in classes:
                    class_id = source_id
                else:
                    class_id = 0
                    while class_id in classes:
                        class_id += 1
                classes[class_id] = name
                class_id_by_name[name] = class_id
                class_sources[class_id] = source_paths[annotation_format]
            remap[source_id] = class_id
        remaps[annotation_format] = remap
    return classes, remaps, class_sources


def _remap_boxes(
    boxes: list[ParsedBox] | tuple,
    remap: dict[int, int],
) -> list[ParsedBox]:
    return [
        ParsedBox(
            class_id=remap[box.class_id],
            cx=box.cx,
            cy=box.cy,
            w=box.w,
            h=box.h,
        )
        for box in boxes
    ]


async def ingest_collected(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    items: list[CollectedFile],
    *,
    phase_observer: Callable[[str], None] | None = None,
    initial_phases: tuple[str, ...] = ("uploading",),
    initial_issues: list[IssueData] | None = None,
    before_commit: BeforeCommit | None = None,
    require_class_resolution: bool = False,
) -> None:
    for index, phase in enumerate(initial_phases):
        await transition_job(
            session_factory,
            job_id,
            phase,
            state="running" if index == 0 else None,
            total=len(items) if index == 0 else None,
            processed=0 if index == 0 else None,
            failed=0 if index == 0 else None,
            observer=phase_observer,
        )
    await transition_job(
        session_factory,
        job_id,
        "collecting",
        observer=phase_observer,
    )

    issues: list[IssueData] = list(initial_issues or [])
    try:
        detection = await detect_formats(items)
        coco_paths = {
            path
            for path, annotation_format in detection.by_path.items()
            if annotation_format == "coco"
        }
        voc_paths = {
            path
            for path, annotation_format in detection.by_path.items()
            if annotation_format == "voc"
        }
        if voc_paths:
            voc_paths.update(await find_voc_annotation_documents(items))
        coco_result = (
            await adapt_coco(items, coco_paths=coco_paths)
            if coco_paths
            else None
        )
        voc_result = (
            await adapt_voc(items, voc_paths=voc_paths)
            if voc_paths
            else None
        )
    except Exception as error:
        await fail_job(session_factory, job_id, str(error))
        raise

    consumed_documents = coco_paths | voc_paths
    image_by_key, label_by_key, pairing_issues = _index_pairable_items(
        items,
        consumed_documents=consumed_documents,
    )
    issues.extend(pairing_issues)
    if coco_result is not None:
        issues.extend(coco_result.issues)
    if voc_result is not None:
        issues.extend(voc_result.issues)

    await transition_job(
        session_factory,
        job_id,
        "parsing",
        observer=phase_observer,
    )
    loaded_classes = load_classes_with_source(items)
    uploaded_yolo_classes = loaded_classes.classes
    parsed_labels: dict[PairKey, list] = {}
    empty_yolo_label_keys: set[PairKey] = set()
    seen_class_ids: set[int] = set()
    rejected_count = sum(
        issue.kind in {"broken_image", "rejected_file"}
        for issue in issues
    )
    staging: Path | None = None
    final_batch: Path | None = None

    try:
        async with session_factory() as session:
            job = await session.scalar(
                select(UploadJob).where(UploadJob.id == job_id)
            )
            if job is None:
                raise LookupError(f"upload job {job_id} does not exist")
            dataset = await session.scalar(
                select(Dataset)
                .where(Dataset.id == job.dataset_id)
                .with_for_update()
            )
            if dataset is None:
                raise LookupError(f"dataset {job.dataset_id} does not exist")
            project = await session.scalar(
                select(Project)
                .where(Project.id == dataset.project_id)
                .with_for_update()
            )
            if project is None:
                raise LookupError(
                    f"project {dataset.project_id} does not exist"
                )

            existing_keys = set(
                (
                    await session.execute(
                        select(Image.split, Image.stem).where(
                            Image.dataset_id == dataset.id
                        )
                    )
                )
                .tuples()
                .all()
            )
            reserved_incoming_keys = set(image_by_key)
            occupied_keys = existing_keys | reserved_incoming_keys
            dataset_class_rows = (
                await session.scalars(
                    select(DatasetClass).where(
                        DatasetClass.dataset_id == dataset.id
                    )
                )
            ).all()
            dataset_classes = {
                row.class_id: row.name for row in dataset_class_rows
            }
            project_class_rows = (
                await session.scalars(
                    select(ProjectClass)
                    .where(ProjectClass.project_id == project.id)
                    .with_for_update()
                )
            ).all()
            project_classes = {
                row.class_id: row.name for row in project_class_rows
            }
            # Project names are authoritative when a legacy dataset catalog
            # disagrees, while dataset-only rows are retained and promoted.
            existing_classes = {
                **dataset_classes,
                **project_classes,
            }
            known_yolo_classes = {
                **uploaded_yolo_classes,
                **existing_classes,
            }
            class_constraint = (
                known_yolo_classes if uploaded_yolo_classes else None
            )
            for key, label in label_by_key.items():
                parsed = parse_yolo_label(
                    label.abs_path,
                    class_constraint,
                    label.rel_path,
                )
                parsed_labels[key] = parsed.boxes
                issues.extend(parsed.issues)
                if not parsed.boxes and not parsed.issues:
                    empty_yolo_label_keys.add(key)
                seen_class_ids.update(box.class_id for box in parsed.boxes)

            for key, label in label_by_key.items():
                if key not in image_by_key and key not in existing_keys:
                    issues.append(
                        _issue(
                            "label_without_image",
                            label.rel_path,
                            "matching image split/stem was not found",
                        )
                    )

            coco_boxes = _adapter_images(coco_result)
            voc_boxes = _adapter_images(voc_result)
            selected_sources: dict[PairKey, str] = {}
            selected_raw_boxes: dict[
                PairKey,
                tuple[str, list[ParsedBox] | tuple],
            ] = {}
            used_formats: set[str] = set()
            displaced: dict[tuple[str, str, str], list[int]] = defaultdict(
                lambda: [0, 0]
            )
            for key, image_item in image_by_key.items():
                candidates: list[tuple[str, list[ParsedBox] | tuple]] = []
                if key in label_by_key:
                    candidates.append(("yolo", parsed_labels.get(key, [])))
                if (
                    coco_result is not None
                    and image_item.rel_path in coco_result.source_by_image
                ):
                    candidates.append(
                        ("coco", coco_boxes.get(image_item.rel_path, ()))
                    )
                if (
                    voc_result is not None
                    and image_item.rel_path in voc_result.source_by_image
                ):
                    candidates.append(
                        ("voc", voc_boxes.get(image_item.rel_path, ()))
                    )
                if not candidates:
                    continue
                winner, winner_boxes = candidates[0]
                selected_sources[key] = winner
                selected_raw_boxes[key] = (winner, winner_boxes)
                used_formats.add(winner)
                for loser, loser_boxes in candidates[1:]:
                    adapter = (
                        coco_result if loser == "coco" else voc_result
                    )
                    assert adapter is not None
                    source_path = adapter.source_by_image[image_item.rel_path]
                    counts = displaced[(source_path, winner, loser)]
                    counts[0] += 1
                    counts[1] += len(loser_boxes)

            for (
                source_path,
                winner,
                loser,
            ), counts in sorted(displaced.items()):
                issues.append(
                    _issue(
                        "ignored_file",
                        source_path,
                        (
                            f"{winner.upper()} won for {counts[0]} image(s); "
                            f"{counts[1]} annotation(s) from "
                            f"{loser.upper()} source not used"
                        ),
                    )
                )

            catalog_formats = set(used_formats)
            if label_by_key:
                catalog_formats.add("yolo")
            catalogs = {
                "yolo": (
                    uploaded_yolo_classes
                    if uploaded_yolo_classes
                    else classes_for_ids(seen_class_ids)
                ),
                "coco": (
                    {
                        item.class_id: item.name
                        for item in coco_result.ir.classes
                    }
                    if coco_result is not None
                    else {}
                ),
                "voc": (
                    {
                        item.class_id: item.name
                        for item in voc_result.ir.classes
                    }
                    if voc_result is not None
                    else {}
                ),
            }
            source_paths = {
                "yolo": (
                    loaded_classes.source.rel_path
                    if loaded_classes.source is not None
                    else min(
                        (item.rel_path for item in label_by_key.values()),
                        default="YOLO labels",
                    )
                ),
                "coco": (
                    coco_result.documents[0]
                    if coco_result is not None and coco_result.documents
                    else "COCO annotations"
                ),
                "voc": (
                    voc_result.documents[0]
                    if voc_result is not None and voc_result.documents
                    else "VOC annotations"
                ),
            }
            uploaded_classes, class_remaps, class_sources = (
                _merge_class_catalogs(
                    catalogs,
                    catalog_formats,
                    source_paths,
                )
            )
            selected_boxes = {
                key: _remap_boxes(boxes, class_remaps[annotation_format])
                for key, (annotation_format, boxes)
                in selected_raw_boxes.items()
            }
            resolution_actions: dict[str, str] = {}
            resolved_conflict_ids: set[int] = set()
            if require_class_resolution:
                resolution_plan = build_class_resolution_plan(
                    dataset_id=dataset.id,
                    project_id=project.id,
                    project_classes=project_classes,
                    uploaded_classes=uploaded_classes,
                    class_sources=class_sources,
                )
                if resolution_plan["conflicts"]:
                    stored_plan = job.class_resolution_plan
                    stored_resolutions = job.class_resolutions
                    should_pause = (
                        not isinstance(stored_plan, dict)
                        or stored_plan.get("revision")
                        != resolution_plan["revision"]
                        or stored_resolutions is None
                    )
                    if not should_pause:
                        try:
                            resolution_actions = validate_class_resolutions(
                                resolution_plan,
                                stored_resolutions,
                            )
                            project_renames = (
                                project_renames_for_resolutions(
                                    resolution_plan,
                                    resolution_actions,
                                    project_classes,
                                )
                            )
                        except (
                            ValueError,
                            ClassResolutionNameConflict,
                        ):
                            should_pause = True
                    else:
                        project_renames = {}

                    if should_pause:
                        job.state = "awaiting_class_resolution"
                        job.phase = "awaiting_class_resolution"
                        job.class_resolution_plan = resolution_plan
                        job.class_resolutions = None
                        await session.commit()
                        raise ClassResolutionRequired(job_id)

                    project_class_by_id = {
                        row.class_id: row for row in project_class_rows
                    }
                    for class_id, name in project_renames.items():
                        project_class = project_class_by_id[class_id]
                        project_class.name = name
                        project_classes[class_id] = name
                        if class_id in dataset_classes:
                            dataset_classes[class_id] = name
                        await session.execute(
                            update(DatasetClass)
                            .where(
                                DatasetClass.class_id == class_id,
                                DatasetClass.dataset_id.in_(
                                select(Dataset.id).where(
                                    Dataset.project_id == project.id,
                                    Dataset.owner_id == dataset.owner_id,
                                    or_(
                                        Dataset.is_placeholder.is_(False),
                                        Dataset.id == dataset.id,
                                    ),
                                )
                                ),
                            )
                            .values(name=name)
                        )
                    if project_renames:
                        project.updated_at = func.now()
                    resolved_conflict_ids = {
                        conflict["class_id"]
                        for conflict in resolution_plan["conflicts"]
                    }

            existing_classes = {
                **dataset_classes,
                **project_classes,
            }
            existing_id_by_name = {
                name: class_id
                for class_id, name in existing_classes.items()
            }
            uploaded_class_count = len(uploaded_classes)
            existing_class_count = len(existing_classes)
            for class_id, uploaded_name in uploaded_classes.items():
                if (
                    existing_classes
                    and uploaded_name not in existing_id_by_name
                    and class_id not in resolved_conflict_ids
                ):
                    existing_name = existing_classes.get(class_id)
                    count_detail = (
                        "; uploaded class count "
                        f"{uploaded_class_count} exceeds existing "
                        f"{existing_class_count}"
                        if uploaded_class_count > existing_class_count
                        else ""
                    )
                    resolution = (
                        f"existing '{existing_name}' kept; uploaded class "
                        f"'{uploaded_name}' ignored"
                        if existing_name is not None
                        else f"uploaded class '{uploaded_name}' registered"
                    )
                    issues.append(
                        _issue(
                            "class_conflict",
                            class_sources[class_id],
                            (
                                f"id {class_id}: {resolution}"
                                f"{count_detail}"
                            ),
                        )
                    )

            uploaded_id_remap = {
                class_id: existing_id_by_name.get(name, class_id)
                for class_id, name in uploaded_classes.items()
            }
            if any(
                source_id != target_id
                for source_id, target_id in uploaded_id_remap.items()
            ):
                selected_boxes = {
                    key: _remap_boxes(boxes, uploaded_id_remap)
                    for key, boxes in selected_boxes.items()
                }
                uploaded_classes = {
                    uploaded_id_remap[class_id]: name
                    for class_id, name in uploaded_classes.items()
                }

            classes_to_register = {
                **uploaded_classes,
                **existing_classes,
            }
            new_dataset_classes = {
                class_id: name
                for class_id, name in classes_to_register.items()
                if class_id not in dataset_classes
            }
            existing_project_names = set(project_classes.values())
            new_project_classes = {
                class_id: name
                for class_id, name in classes_to_register.items()
                if class_id not in project_classes
                and name not in existing_project_names
            }

            dataset_path = contained_storage_path(
                settings.storage_dir,
                dataset.storage_path,
            )
            batch_parent = dataset_path / "batches"
            batch_parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".job-{job_id}-",
                    dir=batch_parent,
                )
            )
            final_batch = batch_parent / str(job_id)
            if final_batch.exists():
                raise RuntimeError(f"batch {job_id} already exists")

            await transition_job(
                session_factory,
                job_id,
                "storing",
                total=len(items),
                processed=len(items) - len(image_by_key),
                failed=rejected_count,
                observer=phase_observer,
            )
            await transition_job(
                session_factory,
                job_id,
                "deriving",
                observer=phase_observer,
            )

            new_image_count = 0
            new_annotation_count = 0
            ingested_bytes = 0
            rejected_keys: set[PairKey] = set()
            base_processed = len(items) - len(image_by_key)
            progress_interval = max(
                1,
                (len(image_by_key) + 99) // 100,
            )

            async def publish_progress(image_index: int) -> None:
                if (
                    image_index % progress_interval != 0
                    and image_index != len(image_by_key)
                ):
                    return
                await transition_job(
                    session_factory,
                    job_id,
                    "deriving",
                    total=len(items),
                    processed=base_processed + image_index,
                    failed=rejected_count,
                )

            for image_index, (key, image_item) in enumerate(
                image_by_key.items(),
                start=1,
            ):
                split, _assigned_stem = key
                original_stem = _stem(image_item)
                storage_key = key
                if key in existing_keys:
                    storage_key = available_pair_key(
                        key,
                        occupied_keys,
                        Path(image_item.rel_path).suffix,
                    )
                    occupied_keys.add(storage_key)
                storage_stem = storage_key[1]
                storage_item = (
                    image_item
                    if storage_stem == original_stem
                    else _replace_item_stem(image_item, storage_stem)
                )
                try:
                    validate_image_file(
                        storage_item.abs_path,
                        storage_item.rel_path,
                        settings.allowed_image_exts,
                    )
                    prepared = await prepare_image(
                        storage_item.abs_path,
                        staging,
                        storage_item.rel_path,
                    )
                except (RejectedFile, ImageDecodeError) as error:
                    rejected_count += 1
                    rejected_keys.add(key)
                    issues.append(
                        _issue(
                            "broken_image",
                            image_item.rel_path,
                            str(error),
                        )
                    )
                    await publish_progress(image_index)
                    continue

                if storage_stem != original_stem:
                    issues.append(
                        _issue(
                            "duplicate_skipped",
                            image_item.rel_path,
                            (
                                "incoming image name collision for "
                                f"split={split or 'null'}, "
                                f"stem={original_stem}; "
                                f"stored as {storage_item.rel_path}"
                            ),
                        )
                    )

                if key not in selected_sources:
                    issues.append(
                        _issue(
                            "image_without_label",
                            image_item.rel_path,
                            "matching annotation source was not found",
                        )
                    )
                elif (
                    selected_sources[key] == "yolo"
                    and key in empty_yolo_label_keys
                ):
                    issues.append(
                        _issue(
                            "empty_label",
                            label_by_key[key].rel_path,
                            "matching label file contained no annotations",
                        )
                    )
                file_path = final_batch / prepared.file_relative
                display_path = (
                    final_batch / prepared.display_relative
                    if prepared.display_relative is not None
                    else None
                )
                thumb_path = final_batch / prepared.thumb_relative
                boxes = selected_boxes.get(key, [])
                image = Image(
                    dataset_id=dataset.id,
                    stem=storage_stem,
                    filename=Path(storage_item.rel_path).name,
                    rel_path=storage_item.rel_path,
                    split=storage_item.split,
                    width=prepared.width,
                    height=prepared.height,
                    file_path=storage_relative_path(
                        settings.storage_dir,
                        file_path,
                    ),
                    display_path=(
                        storage_relative_path(settings.storage_dir, display_path)
                        if display_path
                        else None
                    ),
                    thumb_path=storage_relative_path(
                        settings.storage_dir,
                        thumb_path,
                    ),
                    original_bytes=prepared.original_bytes,
                    display_bytes=prepared.display_bytes,
                    thumb_bytes=prepared.thumb_bytes,
                    box_count=len(boxes),
                    has_label_source=key in selected_sources,
                    is_modified=False,
                )
                session.add(image)
                await session.flush()
                session.add_all(
                    [
                        Annotation(
                            image_id=image.id,
                            class_id=box.class_id,
                            cx=box.cx,
                            cy=box.cy,
                            w=box.w,
                            h=box.h,
                        )
                        for box in boxes
                    ]
                )
                new_image_count += 1
                new_annotation_count += len(boxes)
                ingested_bytes += (
                    prepared.original_bytes
                    + prepared.display_bytes
                    + prepared.thumb_bytes
                )
                await publish_progress(image_index)

            for key in rejected_keys:
                label = label_by_key.get(key)
                if label is not None:
                    issues.append(
                        _issue(
                            "label_without_image",
                            label.rel_path,
                            "matching image was rejected",
                        )
                    )
                    continue
                selected_format = selected_sources.get(key)
                image_item = image_by_key[key]
                if selected_format == "coco" and coco_result is not None:
                    source_path = coco_result.source_by_image[
                        image_item.rel_path
                    ]
                elif selected_format == "voc" and voc_result is not None:
                    source_path = voc_result.source_by_image[
                        image_item.rel_path
                    ]
                else:
                    continue
                issues.append(
                    _issue(
                        "label_without_image",
                        source_path,
                        (
                            f"{selected_format.upper()} annotation image "
                            f"{image_item.rel_path} was rejected"
                        ),
                    )
                )

            await transition_job(
                session_factory,
                job_id,
                "thumbnailing",
                observer=phase_observer,
            )
            session.add_all(
                [
                    DatasetClass(
                        dataset_id=dataset.id,
                        class_id=class_id,
                        name=name,
                    )
                    for class_id, name in new_dataset_classes.items()
                ]
            )
            session.add_all(
                [
                    ProjectClass(
                        project_id=project.id,
                        class_id=class_id,
                        name=name,
                        color=class_color(class_id),
                    )
                    for class_id, name in new_project_classes.items()
                ]
            )
            session.add_all(
                [
                    ImportIssue(
                        job_id=job_id,
                        kind=issue.kind,
                        path=issue.path,
                        detail=issue.detail,
                    )
                    for issue in issues
                ]
            )
            dataset.image_count += new_image_count
            dataset.annotation_count += new_annotation_count
            dataset.class_count += len(new_dataset_classes)
            was_hidden_upload_draft = dataset.is_placeholder
            dataset.status = (
                "ready" if dataset.image_count > 0 else "failed"
            )
            if dataset.status == "ready":
                # Upload-created datasets stay hidden while parsing can pause
                # for class resolution. Publish only with committed images.
                dataset.is_placeholder = False
            if new_project_classes or (
                was_hidden_upload_draft and dataset.status == "ready"
            ):
                project.updated_at = func.now()
            job.state = "done"
            job.phase = "done"
            job.total = len(items)
            job.processed = len(items)
            job.failed = rejected_count
            job.class_resolution_plan = None
            job.class_resolutions = None

            await increase_bytes_used(
                session,
                dataset.owner_id,
                ingested_bytes,
            )

            os.replace(staging, final_batch)
            staging = None
            if before_commit is not None:
                before_commit()
            await session.commit()
    except ClassResolutionRequired:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if final_batch is not None:
            shutil.rmtree(final_batch, ignore_errors=True)
        raise
    except Exception as error:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if final_batch is not None:
            shutil.rmtree(final_batch, ignore_errors=True)
        await fail_job(session_factory, job_id, str(error))
        raise

    if phase_observer is not None:
        phase_observer("done")


async def run_upload_job(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    upload_id: int,
) -> None:
    await run_upload_batch_job(
        settings,
        session_factory,
        job_id,
        [upload_id],
    )


async def run_upload_batch_job(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: int,
    upload_ids: list[int],
) -> None:
    preserve_uploads = False
    try:
        async with session_factory() as session:
            uploads = (
                await session.scalars(
                    select(UploadSession)
                    .where(UploadSession.id.in_(upload_ids))
                    .order_by(UploadSession.id)
                )
            ).all()
            if len(uploads) != len(upload_ids):
                raise LookupError("one or more upload sessions do not exist")

        groups: list[list[CollectedFile]] = []
        zip_issues: list[ZipIssue] = []
        has_zip = False
        file_sources: list[SourceFile] = []
        for upload in uploads:
            source = assembled_upload_path(settings, upload.id)
            if upload.kind == "zip":
                has_zip = True
                extraction_directory = source.parent / "extracted"
                # A class-resolution pause intentionally retains the upload.
                # Recreate only this generated extraction on resume; the
                # assembled source and chunks remain the immutable input.
                if extraction_directory.exists():
                    shutil.rmtree(extraction_directory, ignore_errors=True)
                groups.append(
                    collect_zip(
                        source,
                        extraction_directory,
                        settings.allowed_image_exts,
                        ZipLimits(
                            max_extracted_bytes=settings.max_extracted_bytes,
                            max_file_count=settings.max_file_count,
                            max_compression_ratio=settings.max_compression_ratio,
                        ),
                        issues=zip_issues,
                    )
                )
            else:
                file_sources.append(
                    SourceFile(
                        rel_path=upload.filename,
                        abs_path=source,
                    )
                )
        if file_sources:
            groups.append(
                collect_sources(
                    file_sources,
                    settings.allowed_image_exts,
                )
            )
        items = [item for group in groups for item in group]
        await ingest_collected(
            settings,
            session_factory,
            job_id,
            items,
            initial_phases=(
                ("uploading", "extracting")
                if has_zip
                else ("uploading",)
            ),
            initial_issues=[
                _issue(issue.kind, issue.path, issue.detail)
                for issue in zip_issues
            ],
            require_class_resolution=True,
        )
    except ClassResolutionRequired:
        preserve_uploads = True
    except ZipSafetyError as error:
        await fail_job(
            session_factory,
            job_id,
            str(error),
            path=error.issue.path,
        )
    except Exception as error:
        await fail_job(session_factory, job_id, str(error))
    finally:
        if not preserve_uploads:
            for upload_id in upload_ids:
                shutil.rmtree(
                    upload_directory(settings, upload_id),
                    ignore_errors=True,
                )
