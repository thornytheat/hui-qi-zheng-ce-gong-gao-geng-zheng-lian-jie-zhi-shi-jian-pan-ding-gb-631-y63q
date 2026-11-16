"""导出服务：清单 CSV、隔离 CSV、链版本 CSV、运行清单 JSON、对账 JSON。

导出内容与页面完全一致（同一次查询、同一套规则），差异只在于格式。
"""
from __future__ import annotations

import json
import re

from ... import errors
from ..m2_parse import coerce
from ..m4_chain import service as m4_service
from ..m3_quality import reconcile as reconcile_mod
from . import csv_out, manifest_out

EXPORTS = {
    "policies": {"label": "可申报清单", "suffix": "policies.csv", "media": "text/csv"},
    "quarantine": {"label": "隔离区", "suffix": "quarantine.csv", "media": "text/csv"},
    "chains": {"label": "链版本明细", "suffix": "chains.csv", "media": "text/csv"},
    "manifest": {"label": "运行清单", "suffix": "manifest.json", "media": "application/json"},
    "reconcile": {"label": "对账四账", "suffix": "reconcile.json", "media": "application/json"},
}
SLUG_RE = re.compile(r"[^0-9A-Za-z_-]+")


def _kind(kind: str) -> dict:
    if kind not in EXPORTS:
        raise errors.BadRequest(
            f"未知导出类型 {kind!r}；可选 " + " / ".join(EXPORTS), code="X_EMPTY_SELECTION")
    return EXPORTS[kind]


def _filename(kind: str, batch_id: str) -> str:
    return f"noticechain-{kind}-{batch_id}.{_kind(kind)['suffix'].split('.')[-1]}"


def policies_csv(store, batch_id: str, settings) -> bytes:
    data = m4_service.list_chains(store, batch_id, settings)
    rows = []
    for chain in sorted(data["listed"], key=lambda c: (c["final_deadline"] or "9999", c["doc_no"])):
        detail = store.load_chain(batch_id, chain["doc_no"])
        trace = m4_service.deadline_trace(detail) if detail else {"spec": None}
        rows.append([
            chain["doc_no"], chain["title"], chain["status_label"],
            chain["final_deadline"] or "待确认",
            coerce.spec_label(trace.get("spec")),
            "—" if chain["final_amount_wan"] is None else f"{chain['final_amount_wan']:.2f}",
            chain["versions"],
            store.load_chain(batch_id, chain["doc_no"])["version_rows"][0]["portal"],
            chain["note"],
        ])
    return csv_out.encode_csv(csv_out.to_csv(csv_out.POLICY_HEADER, rows))


def quarantine_csv(store, batch_id: str) -> bytes:
    from ..m3_quality import quarantine
    rows = store.load_quarantine(batch_id)
    return csv_out.encode_csv(quarantine.to_csv_text(rows))


def chains_csv(store, batch_id: str) -> bytes:
    rows = []
    for chain in store.load_chains(batch_id):
        detail = store.load_chain(batch_id, chain["doc_no"])
        for version in detail["version_rows"]:
            rows.append([
                chain["doc_no"], version["seq"], version["kind"],
                version["published_at"], coerce.spec_label(version.get("deadline_spec")),
                "—" if version.get("amount_wan") is None else f"{version['amount_wan']:.2f}",
                version.get("target_field") or "", version.get("target_value") or "",
                version.get("portal") or "", version.get("row_ref") or "",
                version.get("note") or "",
            ])
    return csv_out.encode_csv(csv_out.to_csv(csv_out.CHAIN_HEADER, rows))


def manifest_json(store, batch_id: str) -> bytes:
    manifest = store.load_manifest(batch_id)
    if manifest is None:
        raise errors.NotFound(f"批次 {batch_id} 没有运行清单", code="E_NOT_FOUND")
    return manifest_out.to_json(manifest).encode("utf-8")


def reconcile_json(store, batch_id: str) -> bytes:
    totals = store.load_totals(batch_id)
    if "reconcile_json" not in totals:
        raise errors.NotFound(f"批次 {batch_id} 没有对账结果", code="X_EMPTY_SELECTION")
    payload = json.loads(totals["reconcile_json"])
    payload["accounts"] = reconcile_mod.rows_for_page(payload)
    payload["batch_id"] = batch_id
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def build(store, batch_id: str, settings, kind: str) -> tuple[bytes, str, str]:
    _kind(kind)
    if kind == "policies":
        body = policies_csv(store, batch_id, settings)
    elif kind == "quarantine":
        body = quarantine_csv(store, batch_id)
    elif kind == "chains":
        body = chains_csv(store, batch_id)
    elif kind == "manifest":
        body = manifest_json(store, batch_id)
    else:
        body = reconcile_json(store, batch_id)
    return body, _filename(kind, batch_id), _kind(kind)["media"]


def summary(store, batch_id: str, settings) -> dict:
    totals = store.load_totals(batch_id)
    reconcile = json.loads(totals["reconcile_json"]) if "reconcile_json" in totals else None
    accounts = reconcile_mod.rows_for_page(reconcile) if reconcile else []
    manifest = store.load_manifest(batch_id) or {}
    return {
        "reconcile": reconcile,
        "accounts": accounts,
        "totals": totals,
        "manifest": manifest,
        "manifest_rows": manifest_out.flat_rows(manifest) if manifest else [],
        "exports": [
            {"kind": key, "label": meta["label"], "suffix": meta["suffix"]}
            for key, meta in EXPORTS.items()
        ],
        "counts": store.count_rows(batch_id),
    }
