"""M3 入口：隔离区查看、隔离导出、四账对账。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import errors
from ...web import Ctx, get_ctx, render
from . import quarantine, reconcile, service

router = APIRouter(tags=["M3 质量与隔离"])


def _batch_id(ctx: Ctx, batch_id: str | None) -> str:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        raise errors.NotFound("还没有任何批次，请先触发一次采集", code="B_EMPTY")
    return batch["batch_id"]


@router.get("/api/quarantine")
def api_quarantine(batch_id: str | None = None, code: str | None = None,
                   stage: str | None = None, level: str | None = None,
                   ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    rows = ctx.store.load_quarantine(resolved)
    payload = service.view(rows, code=code, stage=stage, level=level)
    payload["batch_id"] = resolved
    return payload


@router.get("/api/quarantine/export.csv")
def api_quarantine_csv(batch_id: str | None = None,
                       ctx: Ctx = Depends(get_ctx)) -> Response:
    resolved = _batch_id(ctx, batch_id)
    rows = ctx.store.load_quarantine(resolved)
    body = quarantine.to_csv_text(rows).encode("utf-8-sig")
    return Response(
        content=body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="noticechain-quarantine-{resolved}.csv"'})


@router.get("/api/reconcile")
def api_reconcile(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    totals = ctx.store.load_totals(resolved)
    if "reconcile_json" not in totals:
        raise errors.NotFound("该批次没有对账结果", code="X_EMPTY_SELECTION")
    import json
    payload = json.loads(totals["reconcile_json"])
    payload["batch_id"] = resolved
    payload["accounts"] = reconcile.rows_for_page(payload)
    return payload


@router.get("/quarantine")
def page_quarantine(request: Request, batch_id: str | None = None,
                    code: str | None = None, stage: str | None = None,
                    level: str | None = None, ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    payload = {"rows": [], "total": 0, "filtered": 0, "distribution": [],
               "by_level": {"error": 0, "warn": 0, "info": 0},
               "filters": {"code": code or "", "stage": stage or "",
                           "level": level or ""}}
    if batch is not None:
        payload = service.view(ctx.store.load_quarantine(batch["batch_id"]),
                               code=code, stage=stage, level=level)
    return render(request, "quarantine.html", batch=batch, page="quarantine",
                  payload=payload, page_title="隔离区与原因分布")
