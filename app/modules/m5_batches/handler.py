"""M5 入口：批次历史、断点续跑、幂等重跑。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, render
from ..m1_ingest import service as m1_service
from . import resume, service

router = APIRouter(tags=["M5 批次与续跑"])


@router.get("/api/batches")
def api_batches(limit: int = 30, ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = service.history(ctx.store, limit=limit)
    return {
        "batches": [
            {
                "batch_id": r["batch_id"], "run_id": r["run_id"], "source": r["source"],
                "status": r["status"], "created_at": r["created_at"],
                "finished_at": r["finished_at"], "batch_key": r["batch_key"][:16],
                "resume_of": r["resume_of"], "failed_stage": r["failed_stage"],
                "resumable": r["resumable"], "note": r["note"],
                "stages": r["stages"], "rows": r["rows"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/api/batches/{batch_id}/resume")
def api_resume(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = service.resume_batch(settings=ctx.settings, store=ctx.store,
                                 batch_id=batch_id,
                                 fetcher=m1_service.make_fetcher(ctx.settings))
    from ..m1_ingest.handler import _run_payload
    payload = _run_payload(result)
    payload["resume_plan"] = result.get("resume_plan")
    return payload


@router.post("/api/batches/{batch_id}/rerun")
def api_rerun(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    result = service.rerun(settings=ctx.settings, store=ctx.store, batch_id=batch_id)
    from ..m1_ingest.handler import _run_payload
    payload = _run_payload(result)
    payload["rerun_of"] = result.get("rerun_of")
    return payload


@router.get("/batches")
def page_batches(request: Request, ctx: Ctx = Depends(get_ctx)):
    rows = service.history(ctx.store)
    plans = {row["batch_id"]: resume.plan(ctx.store, row["batch_id"])
             for row in rows if row["status"] == "failed"}
    return render(request, "batches.html", page="batches", rows=rows, plans=plans,
                  page_title="批次历史与续跑")


@router.get("/api/batches/{batch_id}/manifest")
def api_manifest(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    from ... import errors
    manifest = ctx.store.load_manifest(batch_id)
    if manifest is None:
        raise errors.NotFound(f"批次 {batch_id} 没有运行清单", code="E_NOT_FOUND")
    return manifest
