"""模板渲染与共享视图上下文：过滤器、导航、当前批次选择。

页面只读 store，不做业务计算；数字格式化统一在这里，避免每页各写一套。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config, reasons

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

NAV = (
    ("/", "采集运行", "ingest"),
    ("/policies", "政策清单", "chain"),
    ("/quarantine", "隔离区", "quality"),
    ("/review", "复核回放", "review"),
    ("/batches", "批次历史", "batches"),
    ("/export", "导出", "export"),
)
STATUS_LABELS = {
    "ACTIVE": "有效", "VOIDED": "已撤销", "CONFLICT": "冲突隔离", "EXPIRED": "已过期",
    "done": "完成", "failed": "失败", "running": "运行中", "ready": "就绪",
}


@dataclass
class Ctx:
    settings: config.Settings
    store: object

    def current_batch(self, batch_id: str | None = None) -> dict | None:
        if batch_id:
            return self.store.get_batch(batch_id)
        return self.store.latest_batch()

    def require_batch(self, batch_id: str | None = None) -> dict | None:
        return self.current_batch(batch_id)


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def setup(app) -> None:
    t = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = t.env
    env.filters["wan"] = fmt_wan
    env.filters["money"] = fmt_money
    env.filters["localdt"] = fmt_local_dt
    env.filters["localdate"] = fmt_local_date
    env.filters["status"] = lambda s: STATUS_LABELS.get(s, s or "—")
    env.filters["level"] = lambda s: reasons.LEVEL_LABELS.get(s, s)
    env.filters["dt_iso"] = lambda s: (s or "").replace("T", " ")[:19]
    from .modules.m2_parse.coerce import spec_label
    from .modules.m2_parse.fields import KIND_LABELS
    env.filters["spec"] = spec_label
    env.filters["kind"] = lambda k: KIND_LABELS.get(k, k)
    from .modules.m2_parse.fields import TARGET_LABELS
    env.globals["target_labels"] = TARGET_LABELS
    env.globals.update(
        nav=NAV,
        stage_flow=config.STAGE_FLOW,
        stage_labels=config.STAGE_LABELS,
        stage_keys=config.STAGES,
        reason_catalog=reasons.catalog(),
        reasons=reasons.REASONS,
        chain_status_labels=config.CHAIN_STATUS_LABELS,
        money_unit=config.MONEY_UNIT,
        generated_at=now_local().strftime("%Y-%m-%d %H:%M:%S"),
        timezone_label="Asia/Shanghai (+08:00)",
    )
    app.state.templates = t


def render(request: Request, name: str, **context):
    ctx: Ctx = request.app.state.ctx
    batch = context.pop("batch", None)
    if batch is None:
        batch = ctx.current_batch(context.get("batch_id"))
    stage_rows = context.pop("stages", None)
    if stage_rows is None and batch is not None:
        stage_rows = ctx.store.load_stages(batch["batch_id"])
    base = {
        "request": request,
        "stages": stage_rows or [],
        "ctx": ctx,
        "settings": ctx.settings,
        "batch": batch,
        "rule_version": ctx.settings.rule_version,
        "eval_date": ctx.settings.eval_date.isoformat(),
        "print_ts": now_local().strftime("%Y-%m-%d %H:%M"),
    }
    base.update(context)
    return request.app.state.templates.TemplateResponse(
        request=request, name=name, context=base)


def now_local() -> dt.datetime:
    return dt.datetime.now(config.CHINA_TZ)


def to_local(value: str | dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.UTC)
    return value.astimezone(config.CHINA_TZ)


def fmt_wan(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except Exception:
            return value
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value:,.2f}"


def fmt_money(value) -> str:
    text = fmt_wan(value)
    return text if text == "—" else f"{text} {config.MONEY_UNIT}"


def fmt_local_dt(value) -> str:
    local = to_local(value)
    return local.strftime("%Y-%m-%d %H:%M") if local else "—"


def fmt_local_date(value) -> str:
    local = to_local(value)
    return local.strftime("%Y-%m-%d") if local else "—"


def local_date_of(value) -> dt.date | None:
    local = to_local(value)
    return local.date() if local else None
