"""层级发布编排：全量派生、单事务发布、按层失效确认、幂等重跑。

发布语义：
- 先派生、再校验、最后在一个事务里写入整个版本。任一步失败都不落库，
  已发布的旧版本继续对外可见，页面与导出的读数不会出现半成品。
- 同一批输入重跑内容指纹相同，直接复用已发布版本：不新增主体、不重复归集、
  不触碰任何确认。
- 上游只有名称变化时只算展示层，主体身份指纹不变，确认保持有效；
  只有税号/账户变化才会让受影响主体的确认失效，未受影响的主体原样保留。
- 增量是真实的：签名未变的主体跳过金额分摊直接复用上一版，成员身份与合计未变的
  集团复用上一版汇总；受影响集合与复用计数随版本一起落库，可在页面与清单里核对。
"""
from __future__ import annotations

import json
from decimal import Decimal

from ... import store
from ...config import HIERARCHY_VERSION, RULE_VERSION
from ...errors import Conflict, DomainError, NotFound
from ...manifest import utc_iso, utc_now
from ...reasons import (
    H_BAD_CONFIRM,
    H_CHECK_FAILED,
    H_UNKNOWN_SCOPE,
    H_PUBLISH_FAILED,
    V_UNKNOWN_RUN,
    V_UNKNOWN_VERSION,
)
from ..m1_intake import service as intake_service
from . import build, export_out, identity

SCOPES = ("subject", "group")
DECISIONS = ("confirm", "reject")


def _version_id(run_id: str, fingerprint: str) -> str:
    return "HV-" + identity.digest(run_id, fingerprint)[:12].upper()


def _previous_rows(version_id: str) -> list:
    return store.list_hierarchy_records(version_id) if version_id else []


def _previous_state(version_id: str) -> dict:
    """上一版的主体签名/分摊与集团成员指纹，用于真正增量重算。

    签名必须与 build 内计算方式一致：成员身份、金额与集团归属都没变的主体可以
    直接复用上一版分摊；集团的成员身份指纹集合与名称都没变即可复用上一版汇总。
    """
    if not version_id:
        return {"subjects": {}, "groups": {}}
    subjects = {}
    grouped_fps = {}
    for row in store.list_hierarchy_subjects(version_id):
        members = json.loads(row["members_json"] or "[]")
        subjects[row["subject_key"]] = {
            "signature": tuple(sorted(
                (member["supplier_id"],
                 identity.identity_fp(member["tax_id"], member["bank_account"]),
                 member["amount"],
                 identity.group_fp(row["group_key"], row["group_name"]))
                for member in members)),
            "shares": {member["supplier_id"]: member["share_amount"] for member in members},
        }
        if row["group_key"]:
            grouped_fps.setdefault(row["group_key"], []).append(
                f"{row['fingerprint']}:{row['total_amount']}")
    groups = {}
    for row in store.list_hierarchy_groups(version_id):
        groups[row["group_key"]] = {
            "member_fps": tuple(sorted(grouped_fps.get(row["group_key"], []))),
            "group_name": row["group_name"],
        }
    return {"subjects": subjects, "groups": groups}


def latest(run_id: str = ""):
    return store.latest_hierarchy(run_id)


def _publish_payload(run_id: str, fail: bool = False) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    records = intake_service.load_records(run_id)
    # 增量基准是"当前对外发布的层级"，而不是本批次自己：上游改名/换税号通常带来
    # 新 batch（run_id），若只和同批次比，每个新批次都会退化成全量初始版本。
    previous = store.latest_hierarchy()
    previous_id = previous["version_id"] if previous else ""
    state = _previous_state(previous_id)
    payload = build.build(records, _previous_rows(previous_id),
                          state["subjects"], state["groups"])
    fingerprint = build.fingerprint(payload)
    now = utc_iso(utc_now())
    version_id = _version_id(run_id, fingerprint)
    version = {
        "version_id": version_id,
        "run_id": run_id,
        "fingerprint": fingerprint,
        "parent_version": previous["version_id"] if previous else "",
        "rule_version": RULE_VERSION,
        "hierarchy_version": HIERARCHY_VERSION,
        "status": "published",
        "created_at": now,
        "published_at": now,
        "changed_levels": payload["changed"]["levels"],
        "affected_subjects": payload["changed"]["affected_subjects"],
        "affected_groups": payload["changed"]["affected_groups"],
        "counts": build.counts(payload),
        "checks": payload["checks"],
    }
    if fail:
        store.log_publish(run_id, fingerprint, "failed", "发布中止，旧版本保留", now)
        raise DomainError(H_PUBLISH_FAILED, "发布中止，旧版本保留")
    return {"version": version, "payload": payload, "previous": previous}


def publish(run_id: str, fail: bool = False) -> dict:
    """派生并发布一个层级版本；内容未变则复用，不做任何写入。"""
    plan = _publish_payload(run_id, fail=fail)
    version, payload, previous = plan["version"], plan["payload"], plan["previous"]
    failed = [check for check in payload["checks"] if not check["ok"]]
    if failed:
        store.log_publish(run_id, version["fingerprint"], "failed",
                          "；".join(check["name"] for check in failed),
                          version["published_at"])
        raise DomainError(H_CHECK_FAILED,
                          "；".join(f"{check['name']} {check['left']}!={check['right']}"
                                    for check in failed))
    own = store.latest_hierarchy(run_id)
    if own and own["fingerprint"] == version["fingerprint"]:
        # 同一批输入重跑：复用既有版本，不新增主体、不重复归集、不动确认。
        return _read_version(own["version_id"])
    try:
        store.publish_hierarchy(version, payload["subjects"], payload["groups"],
                                payload["members"], payload["records"])
    except Exception as error:  # 事务已回滚，旧版本仍是当前发布版
        store.log_publish(run_id, version["fingerprint"], "failed", str(error)[:200],
                          version["published_at"])
        raise DomainError(H_PUBLISH_FAILED, str(error)[:200]) from error
    _refresh_confirmations(version, payload)
    store.log_publish(run_id, version["fingerprint"], "published",
                      ",".join(version["changed_levels"]), version["published_at"])
    # 落库读回的是 JSON 文本，返回前统一成结构化字段，避免调用方各写一套解析。
    return _read_version(version["version_id"])


def _refresh_confirmations(version: dict, payload: dict) -> None:
    """发布后按指纹重投影确认。

    确认的有效性只取决于它记录的指纹是否仍与当前主体/集团一致：
    名称变化不改身份指纹，确认继续有效；税号、账户或集团成员变化才会失效。
    页面读取时还会再投影一次，避免并发发布期间出现短暂的错配。
    """
    subject_fp = {subject["subject_key"]: subject["fingerprint"]
                  for subject in payload["subjects"]}
    group_fp = {group["group_key"]: group["fingerprint"] for group in payload["groups"]}
    for record in store.list_confirmations():
        if record["stale"]:
            continue  # 已失效的确认只能由人工重新确认，不因数据回退自动复活
        current = (subject_fp.get(record["target_key"]) if record["scope"] == "subject"
                   else group_fp.get(record["target_key"]))
        if current != record["fingerprint"]:
            store.save_confirmation({**record, "stale": 1})


def _read_version(version_id: str) -> dict:
    version = store.get_hierarchy_version(version_id)
    if version is None:  # pragma: no cover - 刚写入即被删除属于存储故障
        raise NotFound(V_UNKNOWN_VERSION, version_id)
    return {
        **version,
        "changed_levels": json.loads(version["changed_levels"] or "[]"),
        "affected_subjects": json.loads(version["affected_subjects"] or "[]"),
        "affected_groups": json.loads(version["affected_groups"] or "[]"),
        "counts": json.loads(version["counts_json"] or "{}"),
        "checks": json.loads(version["checks_json"] or "[]"),
    }


def ensure(run_id: str) -> dict:
    """保证符合当前批次内容的版本已发布；内容未变时只读，不写库。"""
    own = store.latest_hierarchy(run_id)
    previous = store.latest_hierarchy()
    previous_id = previous["version_id"] if previous else ""
    state = _previous_state(previous_id)
    records = intake_service.load_records(run_id)
    payload = build.build(records, _previous_rows(previous_id),
                          state["subjects"], state["groups"])
    fingerprint = build.fingerprint(payload)
    if own and own["fingerprint"] == fingerprint:
        return _read_version(own["version_id"])
    return publish(run_id)


def get_version(version_id: str) -> dict:
    version = store.get_hierarchy_version(version_id)
    if not version:
        raise NotFound(V_UNKNOWN_VERSION, version_id)
    return version


def subjects(version_id: str) -> list:
    rows = []
    for row in store.list_hierarchy_subjects(version_id):
        rows.append({
            **row,
            "accounts": json.loads(row["accounts_json"] or "[]"),
            "codes": json.loads(row["codes_json"] or "[]"),
            "members": json.loads(row["members_json"] or "[]"),
        })
    return rows


def groups(version_id: str) -> list:
    rows = []
    for row in store.list_hierarchy_groups(version_id):
        rows.append({**row, "codes": json.loads(row["codes_json"] or "[]"),
                     "subjects": json.loads(row["subjects_json"] or "[]")})
    return rows


def summary(run_id: str) -> dict:
    """页面顶栏、主数据导出与对账共用同一份汇总。"""
    version = ensure(run_id)
    return {
        "run_id": run_id,
        "version_id": version["version_id"],
        "fingerprint": version["fingerprint"],
        "parent_version": version["parent_version"],
        "hierarchy_version": version["hierarchy_version"],
        "changed_levels": version["changed_levels"],
        "affected_subjects": version["affected_subjects"],
        "affected_groups": version["affected_groups"],
        "counts": version["counts"],
        "checks": version["checks"],
        "ok": all(check["ok"] for check in version["checks"]),
    }


def reconcile(run_id: str) -> dict:
    version = ensure(run_id)
    subject_rows = subjects(version["version_id"])
    group_rows = groups(version["version_id"])
    members = store.list_hierarchy_members(version["version_id"])
    subject_total = sum((Decimal(row["total_amount"]) for row in subject_rows), Decimal("0"))
    group_total = sum((Decimal(row["total_amount"]) for row in group_rows), Decimal("0"))
    share_total = sum((Decimal(row["share_amount"]) for row in members), Decimal("0"))
    grouped = sum((Decimal(row["total_amount"]) for row in subject_rows
                   if row["group_key"]), Decimal("0"))
    return {
        "run_id": run_id,
        "version_id": version["version_id"],
        "subjects": len(subject_rows),
        "groups": len(group_rows),
        "members": len(members),
        "subject_total": str(subject_total),
        "group_total": str(group_total),
        "share_total": str(share_total),
        "grouped_subject_total": str(grouped),
        "ungrouped_subject_total": str(subject_total - grouped),
        # 集团只归集有集团标识的主体，因此两条守恒分开校验：
        # 全部主体 = 成员分摊；有标识主体 = 集团合计。
        "ok": subject_total == share_total and grouped == group_total,
    }


def export_subjects(run_id: str) -> str:
    version = ensure(run_id)
    return export_out.render_subjects(subjects(version["version_id"]))


def export_groups(run_id: str) -> str:
    version = ensure(run_id)
    return export_out.render_groups(groups(version["version_id"]))


def export_members(run_id: str) -> str:
    version = ensure(run_id)
    return export_out.render_members(subjects(version["version_id"]))


def confirm(scope: str, target_key: str, decision: str, operator: str,
            expected_fingerprint: str = "") -> dict:
    """确认一个付款主体或集团；主体确认只对身份指纹负责。"""
    if scope not in SCOPES:
        raise DomainError(H_UNKNOWN_SCOPE, scope)
    if decision not in DECISIONS:
        raise DomainError(H_BAD_CONFIRM, decision)
    run = intake_service.ensure_run()
    version = ensure(run["run_id"])
    if scope == "subject":
        match = next((row for row in subjects(version["version_id"])
                      if row["subject_key"] == target_key), None)
    else:
        match = next((row for row in groups(version["version_id"])
                      if row["group_key"] == target_key), None)
    if match is None:
        raise NotFound(H_UNKNOWN_SCOPE, target_key)
    current = match["fingerprint"]
    if expected_fingerprint and expected_fingerprint != current:
        raise Conflict(H_BAD_CONFIRM, f"指纹已变化 {current}")
    store.save_confirmation({
        "scope": scope,
        "target_key": target_key,
        "fingerprint": current,
        "decision": decision,
        "operator": operator,
        "decided_at": utc_iso(utc_now()),
        "version_id": version["version_id"],
        "stale": 0,
    })
    return store.get_confirmation(scope, target_key)


def confirmations(version_id: str) -> list:
    """把确认投影到当前版本：指纹不符的显示为待复核，不静默沿用。"""
    subject_fp = {row["subject_key"]: row["fingerprint"]
                  for row in subjects(version_id)}
    group_fp = {row["group_key"]: row["fingerprint"] for row in groups(version_id)}
    rows = []
    for record in store.list_confirmations():
        current = (subject_fp.get(record["target_key"]) if record["scope"] == "subject"
                   else group_fp.get(record["target_key"]))
        applicable = (not record["stale"]) and bool(current) and current == record["fingerprint"]
        rows.append({**record,
                     "current_fingerprint": current or "",
                     "applicable": applicable})
    return rows


def versions(run_id: str, limit: int = 20) -> list:
    """版本链：每个版本带父版本与层级变化，便于追溯发布与回滚。"""
    rows = []
    for version in store.hierarchy_versions_for(run_id, limit):
        rows.append({
            "version_id": version["version_id"],
            "run_id": version["run_id"],
            "fingerprint": version["fingerprint"],
            "parent_version": version["parent_version"],
            "status": version["status"],
            "created_at": version["created_at"],
            "published_at": version["published_at"],
            "changed_levels": json.loads(version["changed_levels"] or "[]"),
            "affected_subjects": json.loads(version["affected_subjects"] or "[]"),
            "affected_groups": json.loads(version["affected_groups"] or "[]"),
            "counts": json.loads(version["counts_json"] or "{}"),
            "ok": all(check["ok"] for check in json.loads(version["checks_json"] or "[]")),
        })
    return rows


def version_detail(version_id: str) -> dict:
    version = get_version(version_id)
    return {
        "version_id": version["version_id"],
        "run_id": version["run_id"],
        "fingerprint": version["fingerprint"],
        "parent_version": version["parent_version"],
        "status": version["status"],
        "changed_levels": json.loads(version["changed_levels"] or "[]"),
        "affected_subjects": json.loads(version["affected_subjects"] or "[]"),
        "affected_groups": json.loads(version["affected_groups"] or "[]"),
        "counts": json.loads(version["counts_json"] or "{}"),
        "checks": json.loads(version["checks_json"] or "[]"),
        "subjects": subjects(version_id),
        "groups": groups(version_id),
    }


def publish_log(run_id: str) -> list:
    return store.list_publish_log(run_id)
