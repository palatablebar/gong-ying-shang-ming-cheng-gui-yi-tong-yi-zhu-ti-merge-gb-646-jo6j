"""付款主体身份与层级指纹。

付款主体按"可独立收款的法律主体"识别：税号优先，无税号时以收款账户为凭，
两者皆无时按单条记录独立；集团标识只参与上层归集，绝不单独形成付款主体。

指纹分四层，便于上游只改动某一层时定位重算范围：
- name_fp  名称，只影响展示层
- identity_fp 税号与账户，决定付款主体成员
- group_fp 集团标识与名称，决定归集层
- amount   金额，影响主体与集团合计
"""
from __future__ import annotations

import hashlib

SEP = "\x1f"


def digest(*parts: str) -> str:
    raw = SEP.join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def name_fp(raw_name: str) -> str:
    return digest("name", raw_name)


def identity_fp(tax_id: str, bank_account: str) -> str:
    return digest("identity", tax_id, bank_account)


def group_fp(group_key: str, group_name: str) -> str:
    return digest("group", group_key, group_name)


def subject_key(record: dict) -> str:
    """付款主体键：税号 > 收款账户 > 单条记录。

    注意这里不看 group_key：集团标识不是付款凭据，不能把不同税号并成一个
    能付款的主体。
    """
    tax_id = record.get("tax_id", "")
    if tax_id:
        return f"T|{tax_id}"
    account = record.get("bank_account", "")
    if account:
        return f"A|{account}"
    return f"R|{record['supplier_id']}"


def subject_membership_fp(key: str, members) -> str:
    """主体身份指纹：只含主体键与成员身份，名称与金额变化不会让确认失效。"""
    identities = sorted(
        identity_fp(member.get("tax_id", ""), member.get("bank_account", ""))
        for member in members
    )
    return digest("subject", key, *identities)


def group_membership_fp(key: str, subject_fps, subject_totals) -> str:
    """集团身份指纹：成员主体身份 + 各自合计。

    集团是采购口径的用量与返利汇总，成员金额变化必须让集团指纹变化，
    否则集团确认会在合计已变的情况下继续显示为有效。
    """
    pairs = sorted(zip(subject_fps, (str(total) for total in subject_totals)))
    return digest("groupmembers", key, *[f"{fp}:{total}" for fp, total in pairs])


def stable_id(prefix: str, key: str) -> str:
    """由键派生稳定编号：上游增删其他主体时，未受影响主体的编号不漂移。"""
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:6].upper()}"


def assign_unique(prefix: str, keys) -> dict:
    """键 -> 稳定编号；极端前缀碰撞时逐步加长摘要，保证一一对应。"""
    mapping, used = {}, set()
    for key in sorted(keys):
        width = 6
        candidate = stable_id(prefix, key)
        while candidate in used and width < 64:
            width += 2
            candidate = f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:width].upper()}"
        used.add(candidate)
        mapping[key] = candidate
    return mapping
