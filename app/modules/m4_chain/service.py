"""链判定服务：计算、按评估日装饰状态、给出截止推算依据。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config
from ..m2_parse import service as parse_service
from . import chain, workdays

STATUS_LABELS = {"ACTIVE": "有效", "VOIDED": "已撤销", "CONFLICT": "冲突隔离"}


def compute(announcements: list[dict], settings) -> chain.ChainResult:
    return chain.build_chains(announcements, settings)


def decorate(row: dict, settings) -> dict:
    """补充展示状态：有效但已过截止日的显示为「已过期」。"""
    item = dict(row)
    deadline = item.get("final_deadline")
    status = item["status"]
    expiring_soon = False
    if status == "ACTIVE" and deadline:
        day = dt.date.fromisoformat(deadline)
        item["expired"] = day < settings.eval_date
        expiring_soon = settings.eval_date <= day <= settings.eval_date + dt.timedelta(days=7)
        status = "EXPIRED" if item["expired"] else "ACTIVE"
    item["display_status"] = status
    item["status_label"] = config.CHAIN_STATUS_LABELS.get(status, status)
    item["expiring_soon"] = expiring_soon
    item["days_left"] = None
    if deadline:
        item["days_left"] = (dt.date.fromisoformat(deadline) - settings.eval_date).days
    return item


def list_chains(store, batch_id: str, settings, *, status: str | None = None,
                keyword: str | None = None) -> dict:
    rows = [decorate(r, settings) for r in store.load_chains(batch_id)]
    listed = [r for r in rows if r["listed"]]
    filtered = rows
    if status:
        filtered = [r for r in filtered if r["display_status"] == status]
    if keyword:
        filtered = [r for r in filtered
                    if keyword in r["doc_no"] or keyword in r["title"]]
    amount_total = sum((r["final_amount_wan"] for r in listed
                        if r["final_amount_wan"] is not None), start=Decimal("0"))
    return {
        "chains": filtered,
        "all": rows,
        "listed": listed,
        "counts": {
            "total": len(rows),
            "listed": len(listed),
            "voided": sum(1 for r in rows if r["status"] == "VOIDED"),
            "conflict": sum(1 for r in rows if r["status"] == "CONFLICT"),
            "expired": sum(1 for r in rows if r["display_status"] == "EXPIRED"),
        },
        "amount_total": amount_total,
        "filters": {"status": status or "", "keyword": keyword or ""},
    }


def deadline_trace(chain_row: dict) -> dict:
    """回溯最后一条决定截止的版本，给出可复核的推算依据。"""
    decisive = None
    for version in chain_row.get("version_rows", []):
        spec = parse_service.effective_deadline_spec(version)
        if spec is not None:
            decisive = (version, spec)
    if decisive is None:
        return {"lines": ["截止说明未登记，需人工确认"], "trace": None, "spec": None}
    version, spec = decisive
    base = dt.datetime.fromisoformat(version["published_at"]).astimezone(config.CHINA_TZ).date()
    trace = workdays.deadline_for_spec(spec, base)
    return {
        "lines": workdays.explain(spec, base, trace),
        "trace": trace,
        "spec": spec,
        "source_seq": version["seq"],
        "source_kind": version["kind"],
        "base": base.isoformat(),
    }


def detail(store, batch_id: str, doc_no: str, settings) -> dict | None:
    row = store.load_chain(batch_id, doc_no)
    if row is None:
        return None
    item = decorate(row, settings)
    item["trace"] = deadline_trace(row)
    quarantine_rows = [q for q in store.load_quarantine(batch_id)
                       if q.get("row_ref") in
                       {v["row_ref"] for v in row["version_rows"]}]
    item["quarantine"] = quarantine_rows
    item["versions_detail"] = [
        dict(v, published_local=dt.datetime.fromisoformat(v["published_at"])
             .astimezone(config.CHINA_TZ))
        for v in row["version_rows"]
    ]
    return item
