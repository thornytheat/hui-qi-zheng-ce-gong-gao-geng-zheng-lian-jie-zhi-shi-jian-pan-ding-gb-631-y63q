"""M1 入口：触发采集、轮询进度、失败重试，以及首页阶段看板。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from ... import config, errors
from ...web import Ctx, get_ctx, render
from ..m3_quality import reconcile as reconcile_mod
from ..m5_batches import service as batch_service
from . import service

router = APIRouter(tags=["M1 采信与抓取"])


@router.post("/api/crawl")
def api_crawl(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    source = str(body.get("source") or "all")
    mode = str(body.get("mode") or "full")
    if mode not in ("full", "resume"):
        raise errors.BadRequest(f"mode 只支持 full / resume，收到 {mode!r}", code="E_BAD_MODE")
    fetcher = service.make_fetcher(ctx.settings)
    result = batch_service.run(settings=ctx.settings, store=ctx.store,
                               source=source, fetcher=fetcher,
                               resume_of=body.get("resume_of"), mode=mode)
    return _run_payload(result)


def _run_payload(result: dict) -> dict:
    batch = result["batch"]
    return {
        "status": result["status"],
        "batch_id": batch["batch_id"],
        "batch_key": batch["batch_key"],
        "run_id": batch["run_id"],
        "failed_stage": result.get("failed_stage"),
        "error": result.get("error"),
        "idempotent_replay": result.get("idempotent_replay", False),
        "replay_of": result.get("replay_of"),
        "reconcile_note": result.get("reconcile_note"),
        "stages": [
            {"stage": s["stage"],
             "label": s.get("label") or config.STAGE_LABELS.get(s["stage"], s["stage"]),
             "in_count": s["in_count"],
             "out_count": s["out_count"], "quarantine_count": s["quarantine_count"],
             "status": s["status"], "elapsed_ms": s["elapsed_ms"], "note": s["note"]}
            for s in result["stages"]
        ],
        "totals": {k: v for k, v in result.get("totals", {}).items()
                   if k != "reconcile_json"},
        "all_balanced": (result.get("reconciliation") or {}).get("all_balanced"),
        "resume_hint": result.get("resume_hint"),
    }


@router.get("/api/crawl/{batch_id}")
def api_crawl_status(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_NOT_FOUND")
    stages = ctx.store.load_stages(batch_id)
    totals = ctx.store.load_totals(batch_id)
    recon = json.loads(totals.pop("reconcile_json", "null") or "null")
    return {
        "batch_id": batch_id, "status": batch["status"], "note": batch["note"],
        "stages": stages, "totals": totals,
        "reconciliation": recon,
        "pages": ctx.store.load_pages(batch_id),
        "log": service.rows_to_log(ctx.store.load_pages(batch_id)),
    }


@router.post("/api/crawl/{batch_id}/retry")
def api_retry(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """失败阶段重试：链判定之后可续跑复用公告；更早的阶段按全新批次重跑。"""
    from ..m5_batches import resume as resume_plan
    plan = resume_plan.plan(ctx.store, batch_id)
    fetcher = service.make_fetcher(ctx.settings)
    if plan["can_resume"]:
        result = batch_service.resume_batch(settings=ctx.settings, store=ctx.store,
                                            batch_id=batch_id, fetcher=fetcher)
    else:
        batch = ctx.store.get_batch(batch_id)
        params = json.loads((batch or {}).get("params_json") or "{}")
        result = batch_service.run(settings=ctx.settings, store=ctx.store,
                                   source=params.get("source", "all"), fetcher=fetcher)
    payload = _run_payload(result)
    payload["resume_plan"] = plan
    return payload


@router.get("/")
def page_index(request: Request, batch_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    pages: list[dict] = []
    stages: list[dict] = []
    diagnosis: list[dict] = []
    log: list[str] = []
    reconcile = None
    accounts: list[dict] = []
    totals: dict = {}
    if batch is not None:
        pages = ctx.store.load_pages(batch["batch_id"])
        stages = ctx.store.load_stages(batch["batch_id"])
        totals = ctx.store.load_totals(batch["batch_id"])
        diagnosis = service.page_diagnostics_rows(pages)
        log = service.rows_to_log(pages)
        if "reconcile_json" in totals:
            reconcile = json.loads(totals["reconcile_json"])
            accounts = reconcile_mod.rows_for_page(reconcile)
    batches = ctx.store.list_batches(limit=6)
    return render(request, "index.html", batch=batch, page="ingest",
                  pages=pages, stages=stages, diagnosis=diagnosis, log=log,
                  reconcile=reconcile, accounts=accounts, totals=totals,
                  the_batches=batches, page_title="采集运行与阶段看板")
