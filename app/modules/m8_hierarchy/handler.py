"""M8 路由：付款主体、集团归集、层级版本、确认与发布日志。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from ..m1_intake import service as intake_service
from . import service

router = APIRouter(prefix="/api", tags=["m8-hierarchy"])


class ConfirmRequest(BaseModel):
    scope: str = Field(default="subject", max_length=10)
    target_key: str = Field(min_length=1, max_length=200)
    decision: str = Field(default="confirm", max_length=10)
    operator: str = Field(default="采购主数据组", max_length=40)
    expected_fingerprint: str = Field(default="", max_length=64)


def _csv(content: str, filename: str) -> Response:
    return Response(
        content="\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run_id() -> str:
    return intake_service.ensure_run()["run_id"]


@router.get("/hierarchy")
def hierarchy(subject_limit: int = 200, group_limit: int = 100):
    run_id = _run_id()
    version = service.ensure(run_id)
    subject_rows = service.subjects(version["version_id"])
    group_rows = service.groups(version["version_id"])
    confirmations = service.confirmations(version["version_id"])
    by_target = {(item["scope"], item["target_key"]): item for item in confirmations}
    for subject in subject_rows:
        subject["confirmation"] = by_target.get(("subject", subject["subject_key"]))
    for group in group_rows:
        group["confirmation"] = by_target.get(("group", group["group_key"]))
    return {
        "run_id": run_id,
        "summary": service.summary(run_id),
        "subjects": subject_rows[:subject_limit],
        "groups": group_rows[:group_limit],
        "reconcile": service.reconcile(run_id),
    }


@router.get("/export/payable-subjects.csv")
def export_subjects():
    run_id = _run_id()
    return _csv(service.export_subjects(run_id), f"payable-subjects-{run_id}.csv")


@router.get("/export/groups.csv")
def export_groups():
    run_id = _run_id()
    return _csv(service.export_groups(run_id), f"groups-{run_id}.csv")


@router.get("/export/hierarchy-members.csv")
def export_members():
    run_id = _run_id()
    return _csv(service.export_members(run_id), f"hierarchy-members-{run_id}.csv")


@router.get("/hierarchy/subjects")
def hierarchy_subjects(limit: int = 200):
    run_id = _run_id()
    version = service.ensure(run_id)
    items = service.subjects(version["version_id"])
    return {"run_id": run_id, "version_id": version["version_id"],
            "total": len(items), "items": items[:limit]}


@router.get("/hierarchy/groups")
def hierarchy_groups(limit: int = 100):
    run_id = _run_id()
    version = service.ensure(run_id)
    items = service.groups(version["version_id"])
    return {"run_id": run_id, "version_id": version["version_id"],
            "total": len(items), "items": items[:limit]}


@router.post("/hierarchy/publish")
def publish(fail: bool = Query(default=False)):
    run_id = _run_id()
    version = service.publish(run_id, fail=fail)
    return {
        "run_id": run_id,
        "version_id": version["version_id"],
        "fingerprint": version["fingerprint"],
        "parent_version": version["parent_version"],
        "changed_levels": version["changed_levels"],
        "affected_subjects": version["affected_subjects"],
        "affected_groups": version["affected_groups"],
        "summary": service.summary(run_id),
    }


@router.get("/hierarchy/versions")
def versions(limit: int = 20):
    run_id = _run_id()
    return {"run_id": run_id, "items": service.versions(run_id, limit)}


@router.get("/hierarchy/versions/{version_id}")
def version_detail(version_id: str):
    return service.version_detail(version_id)


@router.post("/hierarchy/confirm")
def confirm(body: ConfirmRequest):
    record = service.confirm(body.scope, body.target_key, body.decision, body.operator,
                             body.expected_fingerprint)
    return {"confirmation": record}


@router.get("/hierarchy/confirmations")
def confirmations():
    run_id = _run_id()
    version = service.ensure(run_id)
    return {"run_id": run_id, "version_id": version["version_id"],
            "items": service.confirmations(version["version_id"])}


@router.get("/hierarchy/log")
def publish_log(limit: int = 20):
    return {"run_id": _run_id(), "items": service.publish_log(_run_id())[:limit]}
