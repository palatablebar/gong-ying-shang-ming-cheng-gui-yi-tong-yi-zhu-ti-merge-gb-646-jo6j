"""M8 付款主体与集团归集：集团不形成付款主体、按层重算、幂等与发布原子性。"""
from __future__ import annotations

import csv
import io
import json
import sqlite3

import pytest

from app import store
from app.errors import Conflict, DomainError
from app.modules.m1_intake import service as intake_service
from app.modules.m8_hierarchy import build, identity, service
from tests.fixtures import expected as EXP

HEADER = ("supplier_id,supplier_no,name,tax_id,city,bank_account,erp_source,"
          "ref_master_id,group_key,group_name,ytd_amount,last_update")


def _write(directory, name, rows, encoding="utf-8"):
    lines = [HEADER] + [",".join(row) for row in rows]
    data = ("\n".join(lines) + "\n").encode(encoding)
    (directory / name).write_bytes(data)


def _row(supplier_id, name, tax_id, account, group_key, group_name, amount="100.00"):
    return [supplier_id, supplier_id, name, tax_id, "北京", account, "ERP-A", "",
            group_key, group_name, amount, "2026-03-01"]


@pytest.fixture()
def sample_dir(db, tmp_path, monkeypatch):
    directory = tmp_path / "samples"
    directory.mkdir()
    monkeypatch.setattr(intake_service, "SAMPLE_DIR", directory)
    return directory


def _subjects_of(version_id):
    return {row["subject_key"]: row for row in service.subjects(version_id)}


def _groups_of(version_id):
    return {row["group_key"]: row for row in service.groups(version_id)}


# ---- 主体口径 ----

def test_group_with_two_tax_ids_is_two_payable_subjects(sample_dir):
    """核心判断：同集团不同税号必须拆成两个可收款主体，集团只是上层归集。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "100.00"),
        _row("S-2", "鼎新科技发展有限公司", "TAX-B", "ACC-2", "G-DX", "鼎新集团", "200.00"),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subjects = service.subjects(version["version_id"])
    groups = service.groups(version["version_id"])

    assert len(subjects) == 2
    assert {subject["tax_id"] for subject in subjects} == {"TAX-A", "TAX-B"}
    assert all(subject["group_key"] == "G-DX" for subject in subjects)
    assert len(groups) == 1
    group = groups[0]
    assert group["subject_count"] == 2
    assert group["member_count"] == 2
    assert group["total_amount"] == "300.00"
    assert "不生成付款主体" in group["basis"]
    assert "仅汇总" in group["basis"]


def test_same_tax_id_members_merge_into_one_subject(sample_dir):
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "100.00"),
        _row("S-2", "鼎新科技（北京）有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "50.50"),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subjects = service.subjects(version["version_id"])
    assert len(subjects) == 1
    assert subjects[0]["member_count"] == 2
    assert subjects[0]["total_amount"] == "150.50"
    assert subjects[0]["tax_id"] == "TAX-A"
    assert subjects[0]["accounts"] == ["ACC-1"]
    assert "按税号 TAX-A 识别付款主体" in subjects[0]["basis"]


def test_group_name_alone_never_creates_payment_identity(sample_dir):
    """集团名相同但税号不同：若按集团名并主体，款项会落到错误税号/账户。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "华远物流有限公司", "TAX-A", "ACC-1", "G-HY", "华远集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subjects = service.subjects(version["version_id"])
    assert len(subjects) == 2
    assert identity.subject_key({"tax_id": "TAX-A", "bank_account": "ACC-1",
                                 "supplier_id": "S-1"}) != identity.subject_key(
        {"tax_id": "TAX-B", "bank_account": "ACC-2", "supplier_id": "S-2"})
    taxes = {subject["tax_id"] for subject in subjects}
    assert taxes == {"TAX-A", "TAX-B"}


def test_missing_tax_falls_back_to_account_then_record(sample_dir):
    _write(sample_dir, "erp.csv", [
        _row("S-1", "冰城乳业有限公司", "", "ACC-1", "", ""),
        _row("S-2", "冰城乳业（北方）有限公司", "", "ACC-1", "", ""),
        _row("S-3", "无账户记录有限公司", "", "", "", ""),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subjects = {subject["subject_key"]: subject for subject in service.subjects(
        version["version_id"])}
    account_subject = subjects["A|ACC-1"]
    assert account_subject["member_count"] == 2
    assert account_subject["tax_id"] == ""
    assert "按收款账户 ACC-1" in account_subject["basis"]
    assert "H_NO_TAX" in account_subject["codes"]
    record_subject = subjects["R|S-3"]
    assert record_subject["member_count"] == 1
    assert "付款前须补税号或账户" in record_subject["basis"]


def test_multi_account_subject_is_flagged(sample_dir):
    _write(sample_dir, "erp.csv", [
        _row("S-1", "华远物流有限公司", "TAX-A", "ACC-1", "", ""),
        _row("S-2", "华远物流有限公司", "TAX-A", "ACC-2", "", ""),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subject = service.subjects(version["version_id"])[0]
    assert subject["accounts"] == ["ACC-1", "ACC-2"]
    assert "H_MULTI_ACCOUNT" in subject["codes"]
    assert "财务付款前须指定唯一账户" in subject["basis"]


def test_group_name_mismatch_flagged(sample_dir):
    _write(sample_dir, "erp.csv", [
        _row("S-1", "甲乙丙有限公司", "TAX-A", "ACC-1", "G-1", "甲集团"),
        _row("S-2", "丁戊己有限公司", "TAX-B", "ACC-2", "G-1", "乙集团"),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    group = service.groups(version["version_id"])[0]
    assert group["status"] == "H_GROUP_NAME_MISMATCH"
    assert "多个集团名称" in group["basis"]


# ---- 按层重算 ----

def test_name_change_only_recomputes_display_layer(sample_dir):
    """上游只改名称：只算展示层，身份指纹不变，确认继续有效。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    first = intake_service.ingest(files=["erp.csv"])
    version_one = service.publish(first["run_id"])
    target = next(subject for subject in service.subjects(version_one["version_id"])
                  if subject["tax_id"] == "TAX-A")
    service.confirm("subject", target["subject_key"], "confirm", "王核")

    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技集团有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    second = intake_service.ingest(files=["erp.csv"], force=True)
    assert second["run_id"] != first["run_id"]
    version_two = service.publish(second["run_id"])
    assert version_two["version_id"] != version_one["version_id"]
    assert version_two["changed_levels"] == ["name"]

    # 未受影响的主体的确认仍然适用，不需要重新确认。
    confirmations = {item["target_key"]: item for item in
                     service.confirmations(version_two["version_id"])}
    assert confirmations[target["subject_key"]]["applicable"] is True
    assert confirmations[target["subject_key"]]["stale"] == 0


def test_tax_change_invalidates_only_affected_subject(sample_dir):
    """上游改税号：只有受影响主体与所在集团的确认失效，其他原样保留。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    first = intake_service.ingest(files=["erp.csv"])
    version_one = service.publish(first["run_id"])
    subjects_one = {subject["tax_id"]: subject for subject in
                    service.subjects(version_one["version_id"])}
    service.confirm("subject", subjects_one["TAX-A"]["subject_key"], "confirm", "王核")
    service.confirm("subject", subjects_one["TAX-B"]["subject_key"], "confirm", "王核")

    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A2", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    second = intake_service.ingest(files=["erp.csv"], force=True)
    version_two = service.publish(second["run_id"])
    assert "identity" in version_two["changed_levels"]
    confirmations = {item["target_key"]: item for item in
                     service.confirmations(version_two["version_id"])}
    assert confirmations[subjects_one["TAX-B"]["subject_key"]]["applicable"] is True
    assert confirmations[subjects_one["TAX-B"]["subject_key"]]["stale"] == 0
    assert confirmations[subjects_one["TAX-A"]["subject_key"]]["applicable"] is False
    assert confirmations[subjects_one["TAX-A"]["subject_key"]]["stale"] == 1


def test_changed_scope_lists_affected_only(sample_dir):
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    first = intake_service.ingest(files=["erp.csv"])
    version_one = service.publish(first["run_id"])
    unaffected = next(subject for subject in service.subjects(version_one["version_id"])
                      if subject["tax_id"] == "TAX-B")

    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "999.00"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    second = intake_service.ingest(files=["erp.csv"], force=True)
    version_two = service.publish(second["run_id"])
    affected = version_two["affected_subjects"]
    assert unaffected["subject_key"] not in affected
    assert len(affected) == 1


# ---- 幂等与发布原子性 ----

def test_republish_same_batch_is_noop(sample_dir):
    """同一批重跑：不新增主体、不重复归集、不改变确认。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团"),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subject = service.subjects(version["version_id"])[0]
    service.confirm("subject", subject["subject_key"], "confirm", "王核")
    before_groups = {group["group_key"]: group["member_count"]
                     for group in service.groups(version["version_id"])}

    again = service.publish(run["run_id"])
    assert again["version_id"] == version["version_id"]
    assert len(service.versions(run["run_id"])) == 1
    assert store.count_version(version["version_id"], "hierarchy_subjects") == 2
    subjects = service.subjects(version["version_id"])
    groups = service.groups(version["version_id"])
    assert len(subjects) == 2
    assert {group["group_key"]: group["member_count"] for group in groups} == before_groups
    confirmation = service.confirmations(version["version_id"])[0]
    assert confirmation["applicable"] is True


def test_publish_failure_keeps_previous_version(run):
    version = service.publish(run["run_id"])
    before_subjects = len(service.subjects(version["version_id"]))
    with pytest.raises(DomainError) as error:
        service.publish(run["run_id"], fail=True)
    assert error.value.code == "H_PUBLISH_FAILED"
    latest = store.latest_hierarchy()
    assert latest["version_id"] == version["version_id"]
    assert len(service.subjects(latest["version_id"])) == before_subjects
    log = store.list_publish_log(run["run_id"])
    assert log[0]["status"] == "failed"


def test_publish_transaction_rolls_back_on_store_error(run):
    """写库中途失败：整个版本回滚，旧版本完整保留，不留下半个版本。"""
    version = service.publish(run["run_id"])
    tables = ("hierarchy_subjects", "hierarchy_groups", "hierarchy_members",
              "hierarchy_records")
    before = {table: store.count_version(version["version_id"], table) for table in tables}
    records = intake_service.load_records(run["run_id"])
    payload = build.build(records)
    members = [dict(member) for member in payload["members"]]
    # 重复主键：在写入成员表的中途失败，验证整个版本回滚。
    members.append(dict(members[0]))

    new_version = {
        "version_id": "HV-ROLLBACK01", "run_id": run["run_id"], "fingerprint": "x" * 16,
        "parent_version": version["version_id"], "rule_version": "em-1.3.0",
        "hierarchy_version": "H-1.0.0", "status": "published",
        "created_at": "2026-01-01T00:00:00Z", "published_at": "2026-01-01T00:00:00Z",
        "changed_levels": ["identity"], "affected_subjects": [], "affected_groups": [],
        "counts": {}, "checks": [],
    }
    with pytest.raises(sqlite3.IntegrityError):
        store.publish_hierarchy(new_version, payload["subjects"], payload["groups"],
                                members, payload["records"])

    assert store.get_hierarchy_version("HV-ROLLBACK01") is None
    assert {table: store.count_version("HV-ROLLBACK01", table) for table in tables} == {
        table: 0 for table in tables}
    latest = store.latest_hierarchy()
    assert latest["version_id"] == version["version_id"]
    assert {table: store.count_version(version["version_id"], table) for table in tables} == before


# ---- 页面 / 主数据 / 汇总同一套层级 ----

def test_page_export_and_summary_share_one_version(run, client):
    page = client.get("/api/hierarchy").json()
    version_id = page["summary"]["version_id"]
    subjects_api = client.get("/api/hierarchy/subjects").json()["items"]
    exported = client.get("/api/export/payable-subjects.csv").text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(exported)))
    assert len(rows) - 1 == len(subjects_api) == page["summary"]["counts"]["subjects"]

    by_id = {row[0]: row for row in rows[1:]}
    for subject in subjects_api:
        assert subject["subject_id"] in by_id
        assert by_id[subject["subject_id"]][1] == subject["tax_id"]
        assert by_id[subject["subject_id"]][9] == subject["basis"]
    reconcile = page["reconcile"]
    assert reconcile["version_id"] == version_id
    assert reconcile["ok"] is True


def test_group_export_is_marked_not_payable(run, client):
    export = client.get("/api/export/groups.csv").text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(export)))
    header = rows[0]
    assert "可付款" in header
    assert all(row[header.index("可付款")] == "否（仅归集）" for row in rows[1:])
    assert all(row[header.index("归集依据")] for row in rows[1:])


def test_hierarchy_reconcile_balances(run):
    version = service.publish(run["run_id"])
    ledger = service.reconcile(run["run_id"])
    assert ledger["version_id"] == version["version_id"]
    assert ledger["ok"] is True
    assert ledger["subject_total"] == str(EXP.AMOUNT_TOTAL)
    assert ledger["share_total"] == str(EXP.AMOUNT_TOTAL)


def test_confirmation_requires_current_fingerprint(run):
    version = service.publish(run["run_id"])
    subject = service.subjects(version["version_id"])[0]
    with pytest.raises(Conflict) as error:
        service.confirm("subject", subject["subject_key"], "confirm", "王核",
                        expected_fingerprint="deadbeef")
    assert error.value.code == "H_BAD_CONFIRM"


def test_unknown_scope_rejected(run):
    version = service.publish(run["run_id"])
    subject = service.subjects(version["version_id"])[0]
    with pytest.raises(DomainError):
        service.confirm("invoice", subject["subject_key"], "confirm", "王核")
    with pytest.raises(DomainError):
        service.confirm("subject", "T|NOT-EXIST", "confirm", "王核")


def test_rerun_with_single_file_change_keeps_unaffected(run, sample_dir):
    """只更新一个文件：未受影响的成员、主体与集团合计保持逐分不变。"""
    _write(sample_dir, "a.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "100.00"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团", "200.00"),
    ])
    _write(sample_dir, "b.csv", [
        _row("S-3", "金鼎电子有限公司", "TAX-C", "ACC-3", "G-JD", "金鼎集团", "300.00"),
    ])
    first = intake_service.ingest(files=["a.csv", "b.csv"])
    version_one = service.publish(first["run_id"])
    before = {subject["subject_key"]: subject["fingerprint"]
              for subject in service.subjects(version_one["version_id"])}

    _write(sample_dir, "b.csv", [
        _row("S-3", "金鼎电子有限公司", "TAX-C", "ACC-3", "G-JD", "金鼎集团", "350.00"),
    ])
    second = intake_service.ingest(files=["a.csv", "b.csv"], force=True)
    version_two = service.publish(second["run_id"])
    after = {subject["subject_key"]: subject["fingerprint"]
             for subject in service.subjects(version_two["version_id"])}
    assert after["T|TAX-A"] == before["T|TAX-A"]
    assert after["T|TAX-B"] == before["T|TAX-B"]
    assert after["T|TAX-C"] == before["T|TAX-C"]   # 金额不进身份指纹
    assert version_two["affected_subjects"] == ["T|TAX-C"]
    assert version_two["affected_groups"] == ["G-JD"]
    groups = {group["group_key"]: group for group in service.groups(version_two["version_id"])}
    assert groups["G-DX"]["total_amount"] == "100.00"
    assert groups["G-JD"]["total_amount"] == "350.00"


def test_page_hierarchy_reports_same_version_as_export(run, client):
    page = client.get("/hierarchy")
    assert page.status_code == 200
    summary = client.get("/api/hierarchy").json()["summary"]
    export_groups = client.get("/api/export/groups.csv").text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(export_groups)))
    assert len(rows) - 1 == summary["counts"]["groups"]
    assert summary["version_id"] in page.text
    assert summary["version_id"] in client.get("/export").text


def test_idempotent_rerun_does_not_duplicate_groups(run):
    version = service.publish(run["run_id"])
    groups_before = store.count_version(version["version_id"], "hierarchy_groups")
    members_before = store.count_version(version["version_id"], "hierarchy_members")
    service.publish(run["run_id"])
    service.publish(run["run_id"])
    assert store.count_version(version["version_id"], "hierarchy_groups") == groups_before
    assert store.count_version(version["version_id"], "hierarchy_members") == members_before
    assert len(store.hierarchy_versions_for(run["run_id"])) == 1


def test_check_failure_never_publishes(run, monkeypatch):
    """校验不通过时整版不发布，旧版保持可读。"""
    version = service.publish(run["run_id"])
    original = build._checks

    def broken(records, subjects, groups, members):
        checks = original(records, subjects, groups, members)
        checks[0] = {**checks[0], "ok": False, "right": -1}
        return checks

    monkeypatch.setattr(build, "_checks", broken)
    with pytest.raises(DomainError) as error:
        service.publish(run["run_id"], fail=False)
    assert error.value.code == "H_CHECK_FAILED"
    assert store.latest_hierarchy()["version_id"] == version["version_id"]


def test_name_only_change_reuses_version_on_read(run, monkeypatch):
    """只改名称时 ensure 仍会发布展示层版本，但不改身份指纹与金额。"""
    version = service.publish(run["run_id"])
    records = intake_service.load_records(run["run_id"])
    renamed = [dict(record) for record in records]
    renamed[0]["raw_name"] = renamed[0]["raw_name"] + "二部"
    monkeypatch.setattr(intake_service, "load_records", lambda run_id: renamed)
    version_two = service.ensure(run["run_id"])
    assert version_two["version_id"] != version["version_id"]
    assert version_two["changed_levels"] == ["name"]
    assert version_two["counts"]["amount_total"] == version["counts"]["amount_total"]
    assert version_two["affected_subjects"] == []


def test_subject_with_conflicting_group_tags_is_not_grouped(sample_dir):
    """同一税号下成员集团标识不一致：主体照常可付款，但不归入任何集团。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "鼎新科技（北京）有限公司", "TAX-A", "ACC-1", "G-OTHER", "其他集团"),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subject = service.subjects(version["version_id"])[0]
    assert subject["group_key"] == ""
    assert "H_GROUP_CONFLICT" in subject["codes"]
    assert "暂不归集" in subject["basis"]
    assert service.groups(version["version_id"]) == []


def test_subject_partially_tagged_is_flagged_and_grouped(sample_dir):
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团"),
        _row("S-2", "鼎新科技（北京）有限公司", "TAX-A", "ACC-1", "", ""),
    ])
    run = intake_service.ingest(files=["erp.csv"])
    version = service.publish(run["run_id"])
    subject = service.subjects(version["version_id"])[0]
    assert subject["group_key"] == "G-DX"
    assert "H_GROUP_MIXED" in subject["codes"]
    group = service.groups(version["version_id"])[0]
    assert group["member_count"] == 2


def test_only_affected_subjects_are_recomputed(sample_dir):
    """上游改一个主体的金额：只有该主体重算分摊，其他主体与集团直接复用。"""
    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "100.00"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团", "200.00"),
        _row("S-3", "金鼎电子有限公司", "TAX-C", "ACC-3", "G-JD", "金鼎集团", "300.00"),
    ])
    first = intake_service.ingest(files=["erp.csv"])
    service.publish(first["run_id"])

    _write(sample_dir, "erp.csv", [
        _row("S-1", "鼎新科技有限公司", "TAX-A", "ACC-1", "G-DX", "鼎新集团", "100.00"),
        _row("S-2", "华远物流有限公司", "TAX-B", "ACC-2", "G-HY", "华远集团", "200.00"),
        _row("S-3", "金鼎电子有限公司", "TAX-C", "ACC-3", "G-JD", "金鼎集团", "360.00"),
    ])
    second = intake_service.ingest(files=["erp.csv"], force=True)
    version = service.publish(second["run_id"])
    stats = version["counts"]
    # 集团 G-JD 的成员身份没变，只是金额变化 -> 集团汇总仍然重算。
    assert version["affected_subjects"] == ["T|TAX-C"]
    assert version["affected_groups"] == ["G-JD"]
    record_rows = {row["supplier_id"]: row for row in
                   store.list_hierarchy_records(version["version_id"])}
    assert record_rows["S-1"]["amount"] == "100.00"
    assert record_rows["S-3"]["amount"] == "360.00"
    groups = {group["group_key"]: group["total_amount"]
              for group in service.groups(version["version_id"])}
    assert groups == {"G-DX": "100.00", "G-HY": "200.00", "G-JD": "360.00"}
    assert stats["subjects"] == 3
    assert stats["recomputed_subjects"] == 1
    assert stats["reused_subjects"] == 2
    assert stats["recomputed_groups"] == 1
    assert stats["reused_groups"] == 2
