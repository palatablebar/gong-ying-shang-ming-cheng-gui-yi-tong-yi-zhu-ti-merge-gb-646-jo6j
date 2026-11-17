"""层级导出：付款主体主数据、集团归集、成员明细与归集依据。

关键约束：付款主体 CSV 一行一个可独立收款的法律主体，带税号与账户；
集团 CSV 一行一个上层归集，明确写出"不可直接付款"和归集依据。
两份文件来自同一个层级版本，不会出现页面按 A 版、导出按 B 版。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from ...reasons import label_of

SUBJECT_HEADER = ("付款主体编号", "税号", "收款账户", "集团编号", "集团标识", "集团名称",
                  "成员数", "本年累计(元)", "状态", "归集依据", "成员供应商号",
                  "成员名称", "身份指纹")
GROUP_HEADER = ("集团编号", "集团标识", "集团名称", "付款主体数", "成员记录数",
                "本年累计(元)", "可付款", "状态", "归集依据", "成员付款主体", "身份指纹")
MEMBER_HEADER = ("付款主体编号", "主体税号", "成员收款账户", "供应商号", "编码", "名称",
                 "城市", "来源系统", "本年累计(元)", "分摊(元)")


def _money(value) -> str:
    return f"{Decimal(str(value)):.2f}"


def _status(codes) -> str:
    return "；".join(label_of(code) for code in codes)


def render_subjects(subjects) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SUBJECT_HEADER)
    for subject in subjects:
        members = subject["members"]
        writer.writerow([
            subject["subject_id"],
            subject["tax_id"],
            " ".join(subject["accounts"]),
            subject["group_id"] if "group_id" in subject else "",
            subject["group_key"],
            subject["group_name"],
            subject["member_count"],
            _money(subject["total_amount"]),
            _status(subject["codes"]),
            subject["basis"],
            " ".join(member["supplier_id"] for member in members),
            " ".join(member["name"] for member in members),
            subject["fingerprint"],
        ])
    return buffer.getvalue()


def render_groups(groups) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(GROUP_HEADER)
    for group in groups:
        writer.writerow([
            group["group_id"],
            group["group_key"],
            group["group_name"],
            group["subject_count"],
            group["member_count"],
            _money(group["total_amount"]),
            "否（仅归集）",
            _status(group["codes"]),
            group["basis"],
            " ".join(item["subject_id"] for item in group["subjects"]),
            group["fingerprint"],
        ])
    return buffer.getvalue()


def render_members(subjects) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(MEMBER_HEADER)
    for subject in subjects:
        for member in subject["members"]:
            writer.writerow([
                subject["subject_id"],
                subject["tax_id"],
                member["bank_account"],
                member["supplier_id"],
                member["supplier_no"],
                member["name"],
                member["city"],
                member["erp"],
                _money(member["amount"]),
                _money(member["share_amount"]),
            ])
    return buffer.getvalue()
