"""Deterministic class-name conflict plans for resumable ingestion."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypedDict


ClassResolutionAction = Literal["use_project", "use_upload"]


class ClassNameConflict(TypedDict):
    key: str
    class_id: int
    source_path: str
    project_name: str
    uploaded_name: str


class ClassResolutionPlan(TypedDict):
    revision: str
    conflicts: list[ClassNameConflict]


class StoredClassResolution(TypedDict):
    key: str
    action: ClassResolutionAction


class ClassResolutionNameConflict(ValueError):
    """Raised when an upload choice would duplicate a project class name."""


def build_class_resolution_plan(
    *,
    dataset_id: int,
    project_id: int,
    project_classes: dict[int, str],
    uploaded_classes: dict[int, str],
    class_sources: dict[int, str],
) -> ClassResolutionPlan:
    """Return only same-ID name mismatches that require a user decision."""

    project_names = set(project_classes.values())
    conflicts: list[ClassNameConflict] = []
    for class_id, uploaded_name in sorted(uploaded_classes.items()):
        project_name = project_classes.get(class_id)
        if (
            project_name is None
            or project_name == uploaded_name
            or uploaded_name in project_names
        ):
            continue
        conflicts.append(
            {
                "key": f"class:{class_id}",
                "class_id": class_id,
                "source_path": class_sources[class_id],
                "project_name": project_name,
                "uploaded_name": uploaded_name,
            }
        )

    revision_payload = {
        "dataset_id": dataset_id,
        "project_id": project_id,
        "project_classes": sorted(project_classes.items()),
        "uploaded_classes": sorted(uploaded_classes.items()),
        "class_sources": sorted(class_sources.items()),
        "conflicts": conflicts,
    }
    revision = hashlib.sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {"revision": revision, "conflicts": conflicts}


def validate_class_resolutions(
    plan: ClassResolutionPlan,
    resolutions: object,
) -> dict[str, ClassResolutionAction]:
    if not isinstance(resolutions, list):
        raise ValueError("클래스 명칭 선택값이 올바르지 않습니다.")

    expected_keys = {conflict["key"] for conflict in plan["conflicts"]}
    actions: dict[str, ClassResolutionAction] = {}
    for item in resolutions:
        if not isinstance(item, dict):
            raise ValueError("클래스 명칭 선택값이 올바르지 않습니다.")
        key = item.get("key")
        action = item.get("action")
        if not isinstance(key, str) or action not in {
            "use_project",
            "use_upload",
        }:
            raise ValueError("클래스 명칭 선택값이 올바르지 않습니다.")
        if key in actions:
            raise ValueError("같은 클래스의 선택값이 중복되었습니다.")
        actions[key] = action

    if set(actions) != expected_keys:
        raise ValueError("모든 클래스 오류의 명칭을 선택해 주세요.")
    return actions


def project_renames_for_resolutions(
    plan: ClassResolutionPlan,
    actions: dict[str, ClassResolutionAction],
    project_classes: dict[int, str],
) -> dict[int, str]:
    """Validate and return project-wide renames selected by the user."""

    name_owner = {
        name: class_id for class_id, name in project_classes.items()
    }
    renames: dict[int, str] = {}
    for conflict in plan["conflicts"]:
        class_id = conflict["class_id"]
        current_name = project_classes.get(class_id)
        if current_name != conflict["project_name"]:
            raise ClassResolutionNameConflict(
                "프로젝트 클래스가 변경되었습니다. 다시 확인해 주세요."
            )
        if actions[conflict["key"]] != "use_upload":
            continue
        uploaded_name = conflict["uploaded_name"]
        conflicting_id = name_owner.get(uploaded_name)
        if conflicting_id is not None and conflicting_id != class_id:
            raise ClassResolutionNameConflict(
                "업로드 클래스명이 다른 프로젝트 클래스와 중복됩니다."
            )
        renames[class_id] = uploaded_name
        name_owner.pop(current_name, None)
        name_owner[uploaded_name] = class_id
    return renames
