"""M6 入口：五类导出 + 导出页（四账、运行清单、列契约）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import config, errors
from ...web import Ctx, get_ctx, render
from ..m2_parse import contract
from . import service

router = APIRouter(tags=["M6 复核与导出"])


def _batch_id(ctx: Ctx, batch_id: str | None) -> str:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        raise errors.NotFound("还没有任何批次，请先触发一次采集", code="X_EMPTY_SELECTION")
    return batch["batch_id"]


@router.get("/api/export")
def api_export(kind: str = "policies", batch_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)) -> Response:
    resolved = _batch_id(ctx, batch_id)
    body, filename, media = service.build(ctx.store, resolved, ctx.settings, kind)
    return Response(
        content=body, media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Noticechain-Export": kind,
                 "X-Noticechain-Batch": resolved})


@router.get("/api/export/summary")
def api_export_summary(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    data = service.summary(ctx.store, resolved, ctx.settings)
    return {
        "batch_id": resolved,
        "counts": data["counts"],
        "accounts": data["accounts"],
        "all_balanced": (data["reconcile"] or {}).get("all_balanced"),
        "exports": data["exports"],
        "manifest_run_id": data["manifest"].get("run_id"),
        "amount_total": data["totals"].get("amount_total"),
        "decimals": config.DECIMAL_QUANT,
    }


@router.get("/export")
def page_export(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    data = {"reconcile": None, "accounts": [], "totals": {}, "manifest": {},
            "manifest_rows": [], "exports": [], "counts": {}}
    if batch is not None:
        data = service.summary(ctx.store, batch["batch_id"], ctx.settings)
    return render(request, "export.html", batch=batch, page="export", data=data,
                  contract=contract.document(), page_title="复核与导出")
