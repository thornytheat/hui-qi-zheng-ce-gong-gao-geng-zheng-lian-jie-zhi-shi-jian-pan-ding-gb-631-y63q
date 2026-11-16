"""M3 质量与隔离：转载合并、哨兵、范围校验、原因分布、对账四账。"""
from __future__ import annotations

import random
from decimal import Decimal

from app.modules.m3_quality import quarantine, reconcile, rules, service
from fixtures import expected as X


def test_duplicate_row_merged_not_counted_twice(run):
    """转载重复：门户 C 完整重复行合并计 1 条，并给 Q_DUP_ROW 证据。"""
    codes = [q["code"] for q in run["quarantine_rows"]]
    assert "Q_DUP_ROW" in codes
    assert sum(1 for q in run["quarantine_rows"] if q["code"] == "Q_DUP_ROW") == X.QUALITY_MERGED


def test_sentinel_only_row_isolated(run):
    """TRAP-07：整行关键字段都是哨兵 → 隔离，不参与合计。"""
    rows = [q for q in run["quarantine_rows"] if q["code"] == "Q_SENTINEL_ONLY"]
    assert len(rows) == 1
    assert rows[0]["field"] == "title"


def test_amount_suspect_isolated(run):
    """范围校验：999999 万元超过经验上限 50000，按疑似单位错误隔离。"""
    rows = [q for q in run["quarantine_rows"] if q["code"] == "Q_AMOUNT_SUSPECT"]
    assert len(rows) == 1
    assert Decimal(rows[0]["raw_value"]) > Decimal("50000")


def test_date_outside_window_isolated(run):
    """范围校验：发布时间早于采集窗口的公告隔离，不静默按旧日期算截止。"""
    rows = [q for q in run["quarantine_rows"] if q["code"] == "Q_DATE_SUSPECT"]
    assert len(rows) == 1
    assert rows[0]["raw_value"].startswith("2024")


def test_order_independence(run, settings):
    """TRAP-22：打乱输入顺序后，入链公告数与原因码分布不变。"""
    from app.modules.m2_parse import service as parse_service
    from app.modules.m1_ingest import service as ingest_service
    bundle = ingest_service.ingest(settings)
    records = [
        {"portal": r.portal, "source_url": r.source_url, "row_ref": r.row_ref,
         "header_count": r.header_count, "cell_count": r.cell_count,
         "fields": r.fields, "cells": r.cells, "page_url": r.page_url}
        for r in bundle.records
    ]
    parsed = parse_service.parse_records(records)
    base = rules.apply(parsed.announcements, settings)
    shuffled = [dict(a) for a in parsed.announcements]
    random.Random(20261115).shuffle(shuffled)
    other = rules.apply(shuffled, settings)
    assert len(base.accepted) == len(other.accepted) == X.ACCEPTED
    assert base.merged == other.merged
    assert {(a["doc_no"], a["seq"]) for a in base.accepted} == \
           {(a["doc_no"], a["seq"]) for a in other.accepted}


def test_quarantine_csv_has_reason_and_evidence(run):
    """证据层：隔离 CSV 每行都有原因码与证据行，可直接交给人工复核。"""
    text = quarantine.to_csv_text(run["quarantine_rows"])
    header = text.splitlines()[0]
    assert header.startswith("原因码,级别,阶段,门户,来源页面,证据行")
    assert "Q_COLUMN_COUNT" in text
    assert "b/list_p2.html#r5" in text


def test_distribution_counts_sum_to_total(run):
    """原因分布：每个原因码计数之和等于隔离行数，占比合计 100%（±0.5）。"""
    view = service.view(run["quarantine_rows"])
    assert sum(item["count"] for item in view["distribution"]) == view["total"]
    share = sum(item["share"] for item in view["distribution"])
    assert 99.5 <= share <= 100.5
    assert all(6 <= item["width"] <= 100 for item in view["distribution"])


def test_reconcile_four_accounts_all_balanced(run):
    """对账：页账、行账、金额账、清单账四条等式全部平衡，差额为 0。"""
    recon = run["reconciliation"]
    assert recon["all_balanced"] is True
    assert recon["pages"]["diff"] == 0
    assert recon["rows"]["diff"] == 0
    assert recon["amounts"]["diff"] == "0.00"
    assert recon["chains"]["diff"] == 0
    assert reconcile.rows_for_page(recon)[3]["label"] == "清单账"


def test_reconcile_amounts_invariant_holds(run):
    """不变量：公告金额合计 − 更正覆盖调整 = 清单合计 + 未入清单合计（Decimal 精确）。"""
    parts = run["reconciliation"]["amounts"]["parts"]
    total = Decimal(run["reconciliation"]["amounts"]["in"])
    delta = Decimal(parts["更正覆盖调整"])
    listed = Decimal(parts["清单合计"])
    unlisted = Decimal(parts["未入清单合计"])
    assert total - delta == listed + unlisted
    assert listed == Decimal(X.AMOUNT_TOTAL)
    assert delta == Decimal("1590.00")


def test_row_account_identity(run):
    """行账守恒：表格行 = 入链 + 转载合并 + 解析隔离 + 质量隔离。"""
    rows = run["reconciliation"]["rows"]
    assert rows["in"] == rows["parts"]["入链公告"] + rows["parts"]["转载合并"] \
        + rows["parts"]["解析隔离"] + rows["parts"]["质量隔离"]
    assert rows["in"] == X.ROWS_SEEN


def test_filter_by_code_and_level(run):
    """隔离区筛选：按原因码与级别过滤，结果数量与全量一致可核对。"""
    rows = run["quarantine_rows"]
    only = quarantine.filter_rows(rows, code="Q_COLUMN_COUNT")
    assert len(only) == sum(1 for r in rows if r["code"] == "Q_COLUMN_COUNT")
    errors = quarantine.filter_rows(rows, level="error")
    assert all(r["level"] == "error" for r in errors)
    assert not quarantine.filter_rows(rows, code="不存在的原因码")


def test_amount_limit_boundary_is_inclusive():
    """边界：额度上限 50000 万元含端点，超过 0.01 才判疑似单位写错。"""
    assert rules.amount_too_large({"amount_wan": Decimal("50000.00")}) is False
    assert rules.amount_too_large({"amount_wan": Decimal("50000.01")}) is True


def test_zero_amount_is_not_a_sentinel():
    """边界：额度真 0 与哨兵不同，0 不能被当成未登记整行隔离。"""
    real_zero = {"title": "惠企服务券", "amount_wan": Decimal("0.00"),
                 "deadline_spec": None}
    sentinel = {"title": "—", "amount_wan": None, "deadline_spec": None}
    assert rules.is_sentinel_only(real_zero) is False
    assert rules.is_sentinel_only(sentinel) is True
