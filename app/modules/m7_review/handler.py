"""M7 入口：复核队列、按公告号回放、人工裁定、复核页。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ... import errors
from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M7 复核与回放"])


class RulingIn(BaseModel):
    doc_no: str
    seq: int
    reviewer: str = ""
    basis: str = ""


def _batch_id(ctx: Ctx, batch_id: str | None) -> str:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        raise errors.NotFound("还没有任何批次，请先触发一次采集", code="B_EMPTY")
    return batch["batch_id"]


def _num(value) -> str | None:
    return None if value is None else str(value)


def _replay_payload(item: dict) -> dict:
    return {
        "doc_no": item["doc_no"],
        "title": item["title"],
        "auto": {
            "status": item["auto"]["status"],
            "status_label": item["auto"]["status_label"],
            "final_deadline": item["auto"]["deadline"],
            "final_amount_wan": _num(item["auto"]["amount"]),
            "listed": item["auto"]["listed"],
            "note": item["auto"]["note"],
            "conflicts": item["auto"]["conflicts"],
        },
        "replay_state": item["replay_state"],
        "checks": item["checks"],
        "illegal": item["illegal"],
        "seqs": item["seqs"],
        "versions": [
            {
                "seq": v["seq"], "kind": v["kind"], "kind_label": v["kind_label"],
                "published_at": v["published_at"], "portal": v["portal"],
                "role_label": v["role_label"], "role_owner": v["role_owner"],
                "winner": v["winner"], "priority_tie": v["priority_tie"],
                "row_ref": v["row_ref"], "source_url": v["source_url"],
                "amount_wan": _num(v["amount_wan"]),
                "deadline_spec": v["deadline_spec"],
                "target_field": v["target_field"], "target_value": v["target_value"],
                "raw": v["raw"],
            }
            for v in item["versions"]
        ],
        "steps": item["steps"],
        "ruling": item["ruling"],
    }


@router.get("/api/review")
def api_review(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    data = service.review_view(ctx.store, resolved, ctx.settings)
    return {
        "batch_id": resolved,
        "eval_date": ctx.settings.eval_date.isoformat(),
        "counts": data["counts"],
        "manual_list": {"count": data["manual_list"]["count"],
                        "amount": _num(data["manual_list"]["amount"])},
        "queue": data["queue"],
        "rulings": [
            {
                "doc_no": r["doc_no"], "seq": r["seq"], "reviewer": r["reviewer"],
                "basis": r["basis"], "code": r["code"], "created_at": r["created_at"],
                "status": r["status"], "deadline": r["deadline"],
                "amount": r["amount"], "listed": r["listed"], "diffs": r["diffs"],
            }
            for r in data["reviewed"]
        ],
    }


@router.get("/api/review/replay")
def api_replay(doc_no: str, batch_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    item = service.replay_doc(ctx.store, resolved, doc_no, ctx.settings)
    if item is None:
        raise errors.NotFound(f"批次 {resolved} 中没有公文号 {doc_no}", code="E_NOT_FOUND")
    payload = _replay_payload(item)
    payload["batch_id"] = resolved
    return payload


@router.post("/api/review/ruling")
def api_ruling(payload: RulingIn, batch_id: str | None = None,
               ctx: Ctx = Depends(get_ctx)) -> dict:
    resolved = _batch_id(ctx, batch_id)
    return service.submit_ruling(ctx.store, resolved, payload.doc_no, payload, ctx.settings)


@router.get("/review")
def page_review(request: Request, batch_id: str | None = None,
                doc_no: str | None = None, ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    data = {
        "queue": [], "reviewed": [], "selected": None, "chains": [],
        "counts": {"queue": 0, "ruled": 0, "overridden": 0, "long_chains": 0},
        "manual_list": {"count": 0, "amount": None},
        "filters": {"doc_no": ""},
    }
    if batch is not None:
        data = service.review_view(ctx.store, batch["batch_id"], ctx.settings,
                                   doc_no=doc_no)
    return render(request, "review.html", batch=batch, page="review", data=data,
                  page_title="复核与回放")
