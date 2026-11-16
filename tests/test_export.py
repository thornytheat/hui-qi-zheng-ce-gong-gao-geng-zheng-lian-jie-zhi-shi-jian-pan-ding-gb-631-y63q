"""M6 复核与导出：清单 CSV、隔离 CSV、链版本 CSV、运行清单与对账 JSON。"""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

import pytest

from app import errors
from app.modules.m6_export import csv_out, service
from fixtures import expected as X


def _rows(body: bytes) -> list[list[str]]:
    text = body.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_policies_csv_has_bom_and_crlf(store, settings, run, batch_id):
    """导出编码：UTF-8 BOM + CRLF，Excel 直接打开不乱码，行尾是 \\r\\n。"""
    body = service.policies_csv(store, batch_id, settings)
    assert body.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in body
    rows = _rows(body)
    assert rows[0][0] == "公文号"
    assert len(rows) - 1 == X.LISTED


def test_policies_csv_amount_matches_reconcile_total(store, settings, run, batch_id):
    """清单 CSV 金额合计与对账「清单合计」逐分相等（Decimal，2 位）。"""
    rows = _rows(service.policies_csv(store, batch_id, settings))
    total = sum(Decimal(row[5].replace("—", "0")) for row in rows[1:])
    assert total == Decimal(X.AMOUNT_TOTAL)
    assert total == Decimal(run["reconciliation"]["amounts"]["parts"]["清单合计"])


def test_quarantine_csv_matches_quarantine_rows(store, batch_id, run):
    """隔离 CSV 行数 = 隔离行数，且每行都有原因码与证据行。"""
    rows = _rows(service.quarantine_csv(store, batch_id))
    assert len(rows) - 1 == len(run["quarantine_rows"])
    codes = {row[0] for row in rows[1:]}
    assert {"Q_COLUMN_COUNT", "Q_SENTINEL_ONLY"} <= codes
    assert all(row[1] in ("隔离", "提示", "记录") for row in rows[1:])


def test_chains_csv_lists_every_version(store, batch_id, run):
    """链版本 CSV：每个版本一行，带证据行与处理说明。"""
    rows = _rows(service.chains_csv(store, batch_id))
    assert len(rows) - 1 == X.ACCEPTED
    assert any("延期" in row[10] for row in rows[1:])
    assert any("撤销" in row[10] for row in rows[1:])


def test_manifest_json_roundtrip(store, batch_id):
    """运行清单 JSON 可解析，含 run_id、输入摘要与阶段记录。"""
    manifest = json.loads(service.manifest_json(store, batch_id))
    assert manifest["run_id"].startswith("NC-")
    assert manifest["timezone"].startswith("入库 UTC")
    assert len(manifest["stages"]) == 6
    assert manifest["counters"]["accepted"] == str(X.ACCEPTED)


def test_reconcile_json_accounts_and_balance(store, batch_id):
    """对账 JSON：四账各有 balanced 标志与差额，整体平衡。"""
    payload = json.loads(service.reconcile_json(store, batch_id))
    assert payload["all_balanced"] is True
    labels = [account["label"] for account in payload["accounts"]]
    assert labels == ["页账", "行账", "金额账", "清单账"]
    assert payload["amounts"]["diff"] == "0.00"


def test_unknown_export_kind_rejected(store, settings, batch_id):
    """失败输入：未知导出类型报 400 语义错误并列出可用类型。"""
    with pytest.raises(errors.BadRequest) as exc:
        service.build(store, batch_id, settings, "excel")
    assert "policies" in str(exc.value)


def test_export_requires_existing_batch(store, batch_id):
    """缺失批次：导出不存在的批次报 404 语义错误，不返回空文件。"""
    with pytest.raises(errors.NotFound):
        service.manifest_json(store, "b000000000000000")


def test_csv_out_builder_is_deterministic():
    """CSV 构造是纯函数：同样输入两次得到同样字节，便于复核。"""
    header = ["公文号", "额度"]
    rows = [["澄企服〔2026〕13号", "120.00"]]
    assert csv_out.encode_csv(csv_out.to_csv(header, rows)) == \
           csv_out.encode_csv(csv_out.to_csv(header, rows))


def test_export_summary_matches_pages(store, settings, batch_id):
    """导出页数据与页面一致：计数、四账、运行清单来自同一次查询。"""
    summary = service.summary(store, batch_id, settings)
    assert summary["counts"]["announcements"] == X.ACCEPTED
    assert summary["counts"]["chains"] == X.CHAINS
    assert summary["reconcile"]["all_balanced"] is True
    assert len(summary["exports"]) == 5
    assert summary["manifest_rows"]
