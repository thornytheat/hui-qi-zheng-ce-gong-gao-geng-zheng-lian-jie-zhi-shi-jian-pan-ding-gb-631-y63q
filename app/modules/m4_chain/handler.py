"""M4 入口：政策清单、政策链详情、清单 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import config, errors
from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M4 链判定与截止"])


def _batch_id(ctx: Ctx, batch_id: str | None) -> str:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        raise errors.NotFound("还没有任何批次，请先触发一次采集", code="B_EMPTY")
    return batch["batch_id"]


@router.get("/api/policies")
def api_policies(batch_id: str | None = None, status: str | None = None,
                 keyword: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    data = service.list_chains(ctx.store, resolved, ctx.settings,
                               status=status, keyword=keyword)
    return {
        "batch_id": resolved,
        "eval_date": ctx.settings.eval_date.isoformat(),
        "counts": data["counts"],
        "amount_total": str(data["amount_total"].quantize(config.DECIMAL_QUANT)),
        "policies": [
            {
                "doc_no": c["doc_no"], "title": c["title"], "status": c["display_status"],
                "status_label": c["status_label"], "listed": c["listed"],
                "final_deadline": c["final_deadline"],
                "final_amount_wan": None if c["final_amount_wan"] is None
                else str(c["final_amount_wan"]),
                "versions": c["versions"], "note": c["note"],
                "days_left": c["days_left"],
            }
            for c in data["chains"]
        ],
    }


@router.get("/api/policies/{doc_no}")
def api_chain(doc_no: str, batch_id: str | None = None,
              ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    item = service.detail(ctx.store, resolved, doc_no, ctx.settings)
    if item is None:
        raise errors.NotFound(f"批次 {resolved} 中没有公文号 {doc_no}", code="E_NOT_FOUND")
    return {
        "doc_no": item["doc_no"], "title": item["title"],
        "status": item["display_status"], "status_label": item["status_label"],
        "listed": item["listed"], "note": item["note"],
        "final_deadline": item["final_deadline"],
        "final_amount_wan": None if item["final_amount_wan"] is None
        else str(item["final_amount_wan"]),
        "trace": item["trace"]["lines"],
        "versions": [
            {"seq": v["seq"], "kind": v["kind"], "published_at": v["published_at"],
             "note": v["note"], "row_ref": v["row_ref"], "portal": v["portal"]}
            for v in item["versions_detail"]
        ],
        "conflicts": item["conflicts"],
    }


@router.get("/policies")
def page_policies(request: Request, batch_id: str | None = None,
                  status: str | None = None, keyword: str | None = None,
                  ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    data = {"chains": [], "all": [], "listed": [], "amount_total": 0,
            "counts": {"total": 0, "listed": 0, "voided": 0, "conflict": 0, "expired": 0},
            "filters": {"status": status or "", "keyword": keyword or ""}}
    if batch is not None:
        data = service.list_chains(ctx.store, batch["batch_id"], ctx.settings,
                                   status=status, keyword=keyword)
    return render(request, "policies.html", batch=batch, page="policies", data=data,
                  page_title="可申报政策清单")


@router.get("/policies/{doc_no:path}")
def page_chain(request: Request, doc_no: str, batch_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    item = None
    if batch is not None:
        item = service.detail(ctx.store, batch["batch_id"], doc_no, ctx.settings)
    if batch is not None and item is None:
        raise errors.NotFound(f"批次 {batch['batch_id']} 中没有公文号 {doc_no}",
                              code="E_NOT_FOUND")
    return render(request, "chain.html", batch=batch, page="policies", chain=item,
                  page_title="公告链详情")
