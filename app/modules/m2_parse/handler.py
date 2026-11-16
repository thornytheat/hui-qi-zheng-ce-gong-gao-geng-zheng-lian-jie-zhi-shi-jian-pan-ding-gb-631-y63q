"""M2 入口：列契约文档 + 单条解析试跑。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import errors
from ...web import Ctx, get_ctx
from . import contract, service

router = APIRouter(tags=["M2 契约与解析"])


@router.get("/api/contract")
def get_contract() -> dict:
    return contract.document()


@router.post("/api/parse/preview")
def preview(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    raw = payload.get("fields")
    if not isinstance(raw, dict) or not raw:
        raise errors.BadRequest("fields 必须是「列名 → 原文」的非空对象",
                                code="Q_MISSING_REQUIRED")
    clean = {str(k): str(v) for k, v in raw.items()}
    announcement, quarantine = service.parse_one(clean)
    return {
        "ok": announcement is not None,
        "announcement": None if announcement is None else {
            "doc_no": announcement["doc_no"],
            "seq": announcement["seq"],
            "kind": announcement["kind"],
            "title": announcement["title"],
            "published_at": announcement["published_at"],
            "deadline_spec": announcement["deadline_spec"],
            "amount_wan": None if announcement["amount_wan"] is None
            else str(announcement["amount_wan"]),
            "target_field": announcement["target_field"],
            "target_value": announcement["target_value"],
        },
        "quarantine": quarantine,
        "contract_version": contract.CONTRACT_VERSION,
    }
