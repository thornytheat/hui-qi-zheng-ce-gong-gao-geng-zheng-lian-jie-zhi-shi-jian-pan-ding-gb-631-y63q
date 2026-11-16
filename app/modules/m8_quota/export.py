"""M8 导出：额度版本账、受理回执账（逐项带钉版）、快照汇总、对账 JSON、事件留痕。

与页面同一套 snapshot / reconcile 结果，差异只在格式；UTF-8 BOM + CRLF，
Excel 双击不乱码，且回环导入时采信层同样剥 BOM。
"""
from __future__ import annotations

import json

from ... import config
from . import ledger
from .csv_out import encode_csv, to_csv

VERSION_HEADER = ["资金池", "版本序号", "类型", "总额(万元)", "发布时间(UTC)",
                  "到达时间(UTC)", "公文号", "状态", "拒绝原因码", "拒绝说明", "证据行"]
RECEIPT_HEADER = ["资金池", "回执号", "企业", "受理金额(万元)", "受理时间(UTC)",
                  "到达时间(UTC)", "状态", "钉版序号", "是否超占", "挂起/拒绝原因", "证据行"]
SNAPSHOT_HEADER = ["资金池", "状态", "当前版本", "当前总额(万元)", "当前占用(万元)",
                   "可用余额(万元)", "超额缺口(万元)", "全池占用(万元)",
                   "回执笔数", "超占笔数", "挂起笔数"]
EVENT_HEADER = ["运行号", "事件摘要", "事件类型", "动作", "原因码", "说明", "时间(UTC)"]


def _dt(value: str) -> str:
    return (value or "").replace("T", " ")[:19]


def versions_csv(store, pool_id: str | None = None) -> bytes:
    pool_ids = [p["pool_id"] for p in store.quota_list_pools()] if pool_id is None \
        else [pool_id]
    rows = []
    for pid in pool_ids:
        for v in store.quota_load_versions(pid):
            rows.append([
                pid, v["version_no"], v["kind"], f"{v['total_wan']:.2f}",
                _dt(v["published_at"]), _dt(v["received_at"]), v["doc_no"],
                v["status"], v["reject_code"], v["reject_detail"], v["row_ref"],
            ])
    return encode_csv(to_csv(VERSION_HEADER, rows))


def receipts_csv(store, pool_id: str | None = None) -> bytes:
    pool_ids = [p["pool_id"] for p in store.quota_list_pools()] if pool_id is None \
        else [pool_id]
    rows = []
    for pid in pool_ids:
        for r in store.quota_load_receipts(pid):
            rows.append([
                pid, r["receipt_no"], r["enterprise"], f"{r['amount_wan']:.2f}",
                _dt(r["accepted_at"]), _dt(r["received_at"]), r["status"],
                r["pegged_version_no"] if r["pegged_version_no"] is not None else "",
                "是" if r["overshoot"] else "否", r["pending_reason"], r["row_ref"],
            ])
    return encode_csv(to_csv(RECEIPT_HEADER, rows))


def snapshots_csv(store) -> bytes:
    rows = []
    for pool in store.quota_list_pools():
        snap = ledger.pool_snapshot(
            pool, store.quota_load_versions(pool["pool_id"]),
            store.quota_load_receipts(pool["pool_id"]))
        rows.append([
            snap["pool_id"], snap["status"], snap["current_version_no"] or "",
            f"{snap['current_total_wan']:.2f}", f"{snap['current_occupied_wan']:.2f}",
            f"{snap['available_wan']:.2f}", f"{snap['gap_wan']:.2f}",
            f"{snap['total_occupied_wan']:.2f}", snap["receipt_total"],
            snap["overshoot_total"], snap["pending_total"],
        ])
    return encode_csv(to_csv(SNAPSHOT_HEADER, rows))


def events_csv(store, run_id: str | None = None) -> bytes:
    rows = [[e["run_id"], e["digest"][:16], e["event_type"], e["action"], e["code"],
             e["detail"], _dt(e["created_at"])]
            for e in store.quota_load_events(run_id)]
    return encode_csv(to_csv(EVENT_HEADER, rows))


def reconcile_json(store) -> bytes:
    snapshots = []
    for pool in store.quota_list_pools():
        snapshots.append(ledger.pool_snapshot(
            pool, store.quota_load_versions(pool["pool_id"]),
            store.quota_load_receipts(pool["pool_id"])))
    payload = ledger.reconcile(snapshots)
    payload["rule_version"] = config.RULE_VERSION
    payload["decimals"] = "金额 Decimal，单位万元，最小刻度 0.01"
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


EXPORTS = {
    "quota_versions": {"label": "额度版本账", "suffix": "quota_versions.csv",
                       "media": "text/csv", "builder": versions_csv},
    "quota_receipts": {"label": "受理回执账", "suffix": "quota_receipts.csv",
                       "media": "text/csv", "builder": receipts_csv},
    "quota_snapshot": {"label": "额度快照汇总", "suffix": "quota_snapshot.csv",
                       "media": "text/csv", "builder": lambda store, pool_id: snapshots_csv(store)},
    "quota_events": {"label": "事件留痕", "suffix": "quota_events.csv",
                     "media": "text/csv",
                     "builder": lambda store, pool_id: events_csv(store)},
    "quota_reconcile": {"label": "额度对账", "suffix": "quota_reconcile.json",
                        "media": "application/json",
                        "builder": lambda store, pool_id: reconcile_json(store)},
}


def build(store, kind: str, pool_id: str | None = None) -> tuple[bytes, str, str]:
    if kind not in EXPORTS:
        from ... import errors
        raise errors.BadRequest(
            "未知额度导出类型；可选 " + " / ".join(EXPORTS), code="X_EMPTY_SELECTION")
    meta = EXPORTS[kind]
    body = meta["builder"](store, pool_id)
    suffix = meta["suffix"].split(".")[-1]
    scope = f"-{pool_id}" if pool_id and kind in ("quota_versions", "quota_receipts") else ""
    filename = f"noticechain-{kind}{scope}.{suffix}"
    return body, filename, meta["media"]
