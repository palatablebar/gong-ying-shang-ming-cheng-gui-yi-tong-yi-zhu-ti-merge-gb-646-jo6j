"""层级派生：付款主体按法律主体识别，集团标识只形成上层归集。

关键判断（本模块的存在理由）：
- 付款主体 = 可独立收款的法律主体。身份按"税号 → 收款账户 → 单条记录"取第一可用者，
  **绝不使用集团标识**。集团名相同但税号不同的记录会落到不同主体，避免款项打错账户。
- 集团 = 上层归集。集团行没有税号、没有账户、不能付款，只汇总用量与成员主体，
  供采购按集团口径看支出、返利；财务仍按主体逐笔付款。
- 每个主体、每个集团都带 `basis` 归集依据，复核员能看到成员、税号、账户与依据。
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from ...reasons import (
    H_GROUP_CONFLICT,
    H_GROUP_MIXED,
    H_GROUP_NAME_MISMATCH,
    H_MULTI_ACCOUNT,
    H_NO_ACCOUNT,
    H_NO_TAX,
)
from ..m5_clusters import allocate
from . import identity

def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _groups_of(records) -> dict:
    grouped = {}
    for record in records:
        grouped.setdefault(identity.subject_key(record), []).append(record)
    return grouped


def _subject_status(taxes, accounts, member_group_keys, member_group_names) -> list:
    """主体层质量标记。税号相同才会同主体，因此这里不会再出现税号冲突。"""
    codes = []
    if len(accounts) > 1:
        codes.append(H_MULTI_ACCOUNT)
    if not accounts:
        codes.append(H_NO_ACCOUNT)
    if not taxes:
        codes.append(H_NO_TAX)
    filled = [key for key in member_group_keys if key]
    if len(set(filled)) > 1:
        codes.append(H_GROUP_CONFLICT)
    elif filled and len(filled) != len(member_group_keys):
        codes.append(H_GROUP_MIXED)
    if len(set(member_group_names)) > 1:
        codes.append(H_GROUP_NAME_MISMATCH)
    return codes


def _subject_basis(key, taxes, accounts, group_key, group_name, codes) -> str:
    if key.startswith("T|"):
        head = f"按税号 {sorted(taxes)[0]} 识别付款主体"
    elif key.startswith("A|"):
        head = f"无税号，按收款账户 {sorted(accounts)[0]} 识别付款主体"
    else:
        head = "无税号且无收款账户，按单条记录列示，付款前须补税号或账户"
    accounts_text = "、".join(sorted(accounts)) if accounts else "无"
    group_text = (f"集团归集 {group_key}（{group_name}）" if group_key
                  else "未归集集团")
    tail = ""
    if H_GROUP_CONFLICT in codes:
        tail = "；成员集团标识不一致，暂不归集，待复核"
    elif H_GROUP_MIXED in codes:
        tail = "；部分成员缺集团标识，已按有标识者归集并标记"
    elif H_GROUP_NAME_MISMATCH in codes:
        tail = "；同一集团标识出现多个名称，待复核"
    if H_MULTI_ACCOUNT in codes:
        tail += "；收款账户不唯一，财务付款前须指定唯一账户"
    return f"{head}；收款账户 {accounts_text}；{group_text}{tail}"


def _member_signature(records) -> tuple:
    """主体复用签名：身份、金额与集团归属都没变的主体不重算分摊。"""
    return tuple(sorted((record["supplier_id"],
                         identity.identity_fp(record["tax_id"], record["bank_account"]),
                         str(_decimal(record["ytd_amount"])),
                         identity.group_fp(record.get("group_key", ""),
                                           record.get("group_name", "")))
                        for record in records))


def _build_subjects(records, ids, group_ids, previous_subjects, stats) -> list:
    subjects = []
    for key, members in sorted(_groups_of(records).items()):
        taxes = sorted({member["tax_id"] for member in members if member["tax_id"]})
        accounts = sorted({member["bank_account"] for member in members
                           if member["bank_account"]})
        group_keys = sorted({member["group_key"] for member in members if member["group_key"]})
        group_names = sorted({member["group_name"] for member in members if member["group_name"]})
        conflict_group = len(group_keys) > 1
        group_key = "" if conflict_group else (group_keys[0] if group_keys else "")
        group_name = ""
        if group_key:
            names = sorted({member["group_name"] for member in members
                            if member["group_key"] == group_key and member["group_name"]})
            group_name = names[0] if names else ""
        codes = _subject_status(
            taxes, accounts,
            [member.get("group_key", "") for member in members],
            [member.get("group_name", "") for member in members if member.get("group_name")],
        )
        total = sum((_decimal(member["ytd_amount"]) for member in members), Decimal("0.00"))
        ordered = sorted(members, key=lambda item: item["supplier_id"])
        previous = (previous_subjects or {}).get(key)
        # 只让受影响的主体重算：成员身份、金额与集团归属都没变时直接复用上一版分摊。
        reusable = (previous is not None
                    and previous.get("signature") == _member_signature(ordered))
        if reusable:
            shares = dict(previous["shares"])
            stats["reused_subjects"] += 1
        else:
            shares = dict(allocate.allocate(total, [(member["supplier_id"],
                                                     _decimal(member["ytd_amount"]))
                                                    for member in ordered]))
            stats["recomputed_subjects"] += 1
        fingerprint = identity.subject_membership_fp(key, members)
        subjects.append({
            "subject_key": key,
            "signature": _member_signature(ordered),
            "subject_id": ids[key],
            "tax_id": taxes[0] if len(taxes) == 1 else "",
            "accounts": accounts,
            "group_key": group_key,
            "group_id": group_ids.get(group_key, "") if group_key else "",
            "group_name": group_name,
            "member_count": len(members),
            "total_amount": total,
            "status": codes[0] if codes else "",
            "codes": codes,
            "basis": _subject_basis(key, taxes, accounts, group_key, group_name, codes),
            "fingerprint": fingerprint,
            "members": [{
                "supplier_id": member["supplier_id"],
                "supplier_no": member["supplier_no"],
                "name": member["raw_name"],
                "tax_id": member["tax_id"],
                "bank_account": member["bank_account"],
                "city": member["city"],
                "erp": member["erp"],
                "amount": str(_decimal(member["ytd_amount"])),
                "share_amount": str(shares[member["supplier_id"]]),
            } for member in ordered],
        })
    return subjects


def _build_groups(subjects, ids, previous_groups, stats) -> list:
    buckets = {}
    for subject in subjects:
        if not subject["group_key"]:
            continue
        buckets.setdefault(subject["group_key"], []).append(subject)
    groups = []
    for key, members in sorted(buckets.items()):
        names = sorted({subject["group_name"] for subject in members if subject["group_name"]})
        group_name = names[0] if names else ""
        total = sum((subject["total_amount"] for subject in members), Decimal("0.00"))
        member_count = sum(subject["member_count"] for subject in members)
        status = H_GROUP_NAME_MISMATCH if len(names) > 1 else ""
        member_fps = tuple(sorted(
            f"{subject['fingerprint']}:{subject['total_amount']}" for subject in members))
        previous = (previous_groups or {}).get(key)
        reusable = (previous is not None
                    and previous.get("member_fps") == member_fps
                    and previous.get("group_name") == group_name)
        if reusable:
            stats["reused_groups"] += 1
        else:
            stats["recomputed_groups"] += 1
        taxes = sorted({subject["tax_id"] for subject in members if subject["tax_id"]})
        basis = (f"集团标识 {key} · 集团名称 {group_name or '未填'} · "
                 f"归集 {len(members)} 个付款主体（税号 {len(taxes)} 个）· "
                 "仅汇总用量与返利，不生成付款主体")
        if status:
            basis += "；同一标识出现多个集团名称，待复核"
        groups.append({
            "group_key": key,
            "group_id": ids[key],
            "group_name": group_name,
            "subject_count": len(members),
            "member_count": member_count,
            "total_amount": total,
            "status": status,
            "basis": basis,
            "fingerprint": identity.group_membership_fp(
                key, [subject["fingerprint"] for subject in members],
                [subject["total_amount"] for subject in members]),
            "subjects": [{
                "subject_id": subject["subject_id"],
                "subject_key": subject["subject_key"],
                "tax_id": subject["tax_id"],
                "accounts": subject["accounts"],
                "member_count": subject["member_count"],
                "total_amount": str(subject["total_amount"]),
            } for subject in sorted(members, key=lambda item: item["subject_id"])],
        })
    return groups


def _record_rows(records) -> list:
    rows = []
    for record in sorted(records, key=lambda item: item["supplier_id"]):
        rows.append({
            "supplier_id": record["supplier_id"],
            "subject_key": identity.subject_key(record),
            "group_key": record.get("group_key", ""),
            "name_fp": identity.name_fp(record["raw_name"]),
            "identity_fp": identity.identity_fp(record["tax_id"], record["bank_account"]),
            "group_fp": identity.group_fp(record.get("group_key", ""),
                                          record.get("group_name", "")),
            "amount": str(_decimal(record["ytd_amount"])),
        })
    return rows


def _changed(rows, previous_rows) -> dict:
    if not previous_rows:
        return {
            "levels": ["initial"],
            "affected_subjects": sorted({row["subject_key"] for row in rows}),
            "affected_groups": sorted({row["group_key"] for row in rows if row["group_key"]}),
            "name_changed": 0,
            "identity_changed": 0,
            "amount_changed": 0,
            "group_changed": 0,
            "records_added": len(rows),
            "records_removed": 0,
        }
    old = {row["supplier_id"]: row for row in previous_rows}
    new = {row["supplier_id"]: row for row in rows}
    name_changed = identity_changed = amount_changed = group_changed = 0
    affected_subject_keys, affected_group_keys = set(), set()
    for supplier_id in sorted(set(new) | set(old)):
        before, after = old.get(supplier_id), new.get(supplier_id)
        if before is None or after is None:
            identity_changed += 1
        else:
            if before["name_fp"] != after["name_fp"]:
                name_changed += 1
            if before["identity_fp"] != after["identity_fp"]:
                identity_changed += 1
            if before["amount"] != after["amount"]:
                amount_changed += 1
            if before["group_fp"] != after["group_fp"]:
                group_changed += 1
        touched = before is None or after is None
        if not touched and (before["identity_fp"] != after["identity_fp"]
                            or before["amount"] != after["amount"]):
            touched = True
        if touched:
            if before is not None:
                affected_subject_keys.add(before["subject_key"])
            if after is not None:
                affected_subject_keys.add(after["subject_key"])
        # 只改集团归属时：主体本身仍可付款、确认继续有效，只把集团层标为受影响。
        group_touched = touched
        if not group_touched and before["group_fp"] != after["group_fp"]:
            group_touched = True
        if group_touched:
            if before is not None and before["group_key"]:
                affected_group_keys.add(before["group_key"])
            if after is not None and after["group_key"]:
                affected_group_keys.add(after["group_key"])
    levels = []
    if name_changed:
        levels.append("name")
    if identity_changed:
        levels.append("identity")
    if amount_changed:
        levels.append("amount")
    if group_changed:
        levels.append("group")
    return {
        "levels": levels,
        "affected_subjects": sorted(affected_subject_keys),
        "affected_groups": sorted(affected_group_keys),
        "name_changed": name_changed,
        "identity_changed": identity_changed,
        "amount_changed": amount_changed,
        "group_changed": group_changed,
        "records_added": len(set(new) - set(old)),
        "records_removed": len(set(old) - set(new)),
    }


def _checks(records, subjects, groups, members) -> list:
    record_total = sum((_decimal(record["ytd_amount"]) for record in records), Decimal("0.00"))
    subject_total = sum((subject["total_amount"] for subject in subjects), Decimal("0.00"))
    grouped_total = sum((group["total_amount"] for group in groups), Decimal("0.00"))
    grouped_subject_total = sum((subject["total_amount"] for subject in subjects
                                 if subject["group_key"]), Decimal("0.00"))
    member_shares = sum((_decimal(member["share_amount"]) for member in members),
                        Decimal("0.00"))
    assigned = [member["supplier_id"] for member in members]
    taxes = [subject["tax_id"] for subject in subjects if subject["tax_id"]]
    duplicate_tax = len(taxes) - len(set(taxes))
    subject_ids = [subject["subject_id"] for subject in subjects]
    group_ids = [group["group_id"] for group in groups]
    checks = [
        {"name": "记录守恒", "left": len(records), "right": len(assigned),
         "ok": len(records) == len(assigned) == len(set(assigned))},
        {"name": "金额守恒", "left": str(record_total), "right": str(subject_total),
         "ok": record_total == subject_total == member_shares},
        {"name": "集团归集守恒", "left": str(grouped_subject_total), "right": str(grouped_total),
         "ok": grouped_subject_total == grouped_total},
        {"name": "税号不重复", "left": len(taxes), "right": len(set(taxes)),
         "ok": duplicate_tax == 0},
        {"name": "编号唯一", "left": len(subject_ids) + len(group_ids),
         "right": len(set(subject_ids)) + len(set(group_ids)),
         "ok": len(subject_ids) == len(set(subject_ids))
         and len(group_ids) == len(set(group_ids))},
    ]
    return checks


def build(records, previous_rows=(), previous_subjects=None, previous_groups=None) -> dict:
    """由采纳记录派生一份完整层级；纯函数，不落库。

    previous_rows 来自上一版层级，用来给出逐层变更集合，决定哪些主体/集团需要重算
    与重新确认；本函数本身总是全量派生，保证页面、主数据和汇总看到同一套层级。
    """
    subject_keys = {identity.subject_key(record) for record in records}
    group_keys = {record.get("group_key", "") for record in records if record.get("group_key")}
    subject_ids = identity.assign_unique("PS", subject_keys)
    group_ids = identity.assign_unique("GRP", group_keys)
    stats = {"reused_subjects": 0, "recomputed_subjects": 0,
             "reused_groups": 0, "recomputed_groups": 0}

    subjects = _build_subjects(records, subject_ids, group_ids,
                               previous_subjects or {}, stats)
    groups = _build_groups(subjects, group_ids, previous_groups or {}, stats)
    members = [{
        "subject_id": subject["subject_id"],
        "subject_key": subject["subject_key"],
        "supplier_id": member["supplier_id"],
        "share_amount": member["share_amount"],
    } for subject in subjects for member in subject["members"]]
    rows = _record_rows(records)
    changed = _changed(rows, list(previous_rows))
    changed.update(stats)
    checks = _checks(records, subjects, groups, members)
    return {
        "subjects": subjects,
        "groups": groups,
        "members": members,
        "records": rows,
        "changed": changed,
        "checks": checks,
    }


def fingerprint(payload: dict) -> str:
    """内容指纹：同一批输入重跑必须得到同一指纹，从而不新增版本。"""
    lines = []
    for row in payload["records"]:
        lines.append("|".join([row["supplier_id"], row["subject_key"], row["group_key"],
                               row["name_fp"], row["identity_fp"], row["group_fp"],
                               row["amount"]]))
    for subject in sorted(payload["subjects"], key=lambda item: item["subject_id"]):
        lines.append("S|" + "|".join([subject["subject_id"], subject["fingerprint"],
                                      subject["basis"]]))
    for group in sorted(payload["groups"], key=lambda item: item["group_id"]):
        lines.append("G|" + "|".join([group["group_id"], group["fingerprint"], group["basis"]]))
    return identity.digest("hierarchy", *lines)


def counts(payload: dict) -> dict:
    subjects = payload["subjects"]
    changed = payload["changed"]
    groups = payload["groups"]
    status = Counter(code for subject in subjects for code in subject["codes"])
    return {
        "records": len(payload["records"]),
        "subjects": len(subjects),
        "groups": len(groups),
        "ungrouped_subjects": sum(1 for subject in subjects if not subject["group_key"]),
        "grouped_subjects": sum(1 for subject in subjects if subject["group_key"]),
        "amount_total": str(sum((subject["total_amount"] for subject in subjects),
                                Decimal("0.00"))),
        "group_amount_total": str(sum((group["total_amount"] for group in groups),
                                      Decimal("0.00"))),
        "multi_account": status.get(H_MULTI_ACCOUNT, 0),
        "no_account": status.get(H_NO_ACCOUNT, 0),
        "no_tax": status.get(H_NO_TAX, 0),
        "group_conflict": status.get(H_GROUP_CONFLICT, 0),
        "group_mixed": status.get(H_GROUP_MIXED, 0),
        "group_name_mismatch": status.get(H_GROUP_NAME_MISMATCH, 0),
        "changed_levels": changed["levels"],
        "name_changed_records": changed.get("name_changed", 0),
        "identity_changed_records": changed.get("identity_changed", 0),
        "amount_changed_records": changed.get("amount_changed", 0),
        "group_changed_records": changed.get("group_changed", 0),
        "reused_subjects": changed.get("reused_subjects", 0),
        "recomputed_subjects": changed.get("recomputed_subjects", 0),
        "reused_groups": changed.get("reused_groups", 0),
        "recomputed_groups": changed.get("recomputed_groups", 0),
    }
