"""M8 入口：事件收件、处理/续跑、额度看板、单池明细、新申报预检、五类导出。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import errors
from ...web import Ctx, get_ctx, render
from . import export, service

router = APIRouter(tags=["M8 额度账本"])


def _events_from_body(body: dict | None) -> list[dict]:
    payload = body or {}
    if isinstance(payload.get("events"), list):
        return payload["events"]
    return [payload] if payload else []


@router.post("/api/quota/ingest")
def api_ingest(body: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    """事件进收件箱（不处理）；规范化失败逐条计入 invalid，不污染已确认事实。"""
    events_in = _events_from_body(body)
    _fail_on_unknown_type(events_in)
    result = service.ingest_events(ctx.store, events_in)
    result["inbox_pending"] = len(ctx.store.quota_inbox_pending())
    return result


@router.post("/api/quota/process")
def api_process(body: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    """收件 + 处理一步完成；只动收件箱 pending，重跑命中摘要即幂等跳过。"""
    events_in = _events_from_body(body)
    _fail_on_unknown_type(events_in)
    return service.run(ctx.store, events_in if events_in else None)


def _fail_on_unknown_type(events_in: list[dict]) -> None:
    """未知 event_type 是请求级错误（400），区别于字段级 invalid（跳过该条）。"""
    for raw in events_in:
        event_type = str(raw.get("event_type") or "").strip()
        if event_type not in ("quota_version", "quota_receipt"):
            raise errors.BadRequest(
                f"event_type 只支持 quota_version / quota_receipt，收到 {event_type!r}",
                code="F_BAD_EVENT")


@router.post("/api/quota/resume")
def api_resume(ctx: Ctx = Depends(get_ctx)) -> dict:
    """中断续跑：从已确认更正之后继续，只处理剩余 pending，不重复占用。"""
    return service.resume(ctx.store)


@router.get("/api/quota/dashboard")
def api_dashboard(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.dashboard(ctx.store)


@router.get("/api/quota/pools/{pool_id}")
def api_pool(pool_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.pool_view(ctx.store, pool_id)


@router.get("/api/quota/precheck")
def api_precheck(pool_id: str, amount_wan: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """新申报预检：按更正后当前总额给可用余额与预计超额缺口。"""
    return service.precheck(ctx.store, pool_id, amount_wan)


@router.get("/api/quota/runs")
def api_runs(ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"runs": ctx.store.quota_list_runs(),
            "inbox_pending": len(ctx.store.quota_inbox_pending())}


@router.get("/api/quota/export")
def api_export(kind: str, pool_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)) -> Response:
    body, filename, media = export.build(ctx.store, kind, pool_id)
    return Response(
        content=body, media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Noticechain-Export": kind})


@router.get("/quota")
def page_quota(request: Request, ctx: Ctx = Depends(get_ctx)):
    data = service.dashboard(ctx.store)
    return render(request, "quota.html", page="quota", data=data,
                  exports=list(export.EXPORTS.keys()),
                  page_title="额度账本（公告版本 × 受理回执）")


@router.get("/quota/{pool_id}")
def page_pool(pool_id: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    snap = service.pool_view(ctx.store, pool_id)
    return render(request, "quota_pool.html", page="quota", snap=snap,
                  exports=list(export.EXPORTS.keys()),
                  page_title=f"额度账本 · {pool_id}")
