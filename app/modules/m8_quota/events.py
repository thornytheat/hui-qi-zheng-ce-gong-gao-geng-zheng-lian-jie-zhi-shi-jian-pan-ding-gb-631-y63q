"""事件规范化：外部输入 → 规范事件；内容摘要（幂等键）；字段校验。

事实字段全部进摘要，落库时间/运行号等簿记字段不进摘要——
同一条公告或回执重放，摘要必然相同，命中即幂等跳过，不产生第二条占用。
金额一律 Decimal（万元，2 位），时间统一转带时区的 ISO 串后再比较。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal, InvalidOperation

from ... import config, errors

VERSION_KINDS = frozenset({"首发", "更正", "撤销"})
VERSION_KIND_ALIASES = {
    "首发": "首发", "首次发布": "首发", "首次": "首发", "initial": "首发",
    "更正": "更正", "更正公告": "更正", "correction": "更正",
    "撤销": "撤销", "作废": "撤销", "cancel": "撤销", "void": "撤销",
}

# 规范字段（进摘要）；received_at/run_id 等簿记字段不进摘要
VERSION_FACTS = ("pool_id", "version_no", "kind", "total_wan", "published_at",
                 "doc_no", "source_url", "row_ref")
RECEIPT_FACTS = ("pool_id", "receipt_no", "enterprise", "amount_wan",
                 "accepted_at", "source_url", "row_ref")


def now_iso() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def parse_instant(value) -> dt.datetime:
    """把多种时间写法归一为带时区的 datetime（纯日期按 +08:00 当日 00:00）。"""
    if isinstance(value, dt.datetime):
        instant = value
    elif isinstance(value, dt.date):
        instant = dt.datetime.combine(value, dt.time.min)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("时间为空")
        try:
            instant = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"时间无法解析：{value!r}") from exc
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=config.CHINA_TZ)
    return instant.astimezone(config.UTC)


def parse_amount(value, *, allow_zero: bool = True) -> Decimal:
    """归一金额：全角转半角、去千分位，返回万元 Decimal（不量化，量化在展示层）。"""
    if value is None:
        raise ValueError("金额为空")
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip().translate(_FULLWIDTH_TABLE)
        text = text.replace(",", "").replace("，", "").replace("万元", "").replace("万", "")
        text = text.replace("元", "").strip()
        if text in {"", "-", "—", "/", "N/A", "未知", "待定"}:
            raise ValueError(f"金额是空值哨兵：{value!r}")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"金额无法归一：{value!r}") from exc
    if amount < 0 or (not allow_zero and amount <= 0):
        raise ValueError(f"金额必须为正数：{value!r}")
    return amount


_FULLWIDTH_TABLE = {ord(ch): ord(ch) - 0xFEE0 for ch in
                    "".join(chr(c) for c in range(0xFF10, 0xFF1A))}
_FULLWIDTH_TABLE[ord("．")] = ord(".")
_FULLWIDTH_TABLE[ord("－")] = ord("-")


def _require(payload: dict, key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise errors.BadRequest(f"事件缺少必需字段 {key}", code="F_BAD_EVENT")
    return str(value).strip()


def normalize_version(payload: dict, *, received_at: str | None = None) -> dict:
    """额度公告版本事件：校验并规范化，失败抛 BadRequest（4xx），不留半条。"""
    pool_id = _require(payload, "pool_id")
    try:
        version_no = int(payload.get("version_no"))
        if version_no <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise errors.BadRequest("version_no 必须是正整数", code="F_BAD_EVENT") from exc
    kind_raw = _require(payload, "kind")
    kind = VERSION_KIND_ALIASES.get(kind_raw)
    if kind is None:
        raise errors.BadRequest(
            f"额度公告类型只支持 首发/更正/撤销，收到 {kind_raw!r}",
            code="F_VERSION_BAD_KIND")
    try:
        total = parse_amount(payload.get("total_wan"), allow_zero=True)
        published = parse_instant(payload.get("published_at"))
    except ValueError as exc:
        raise errors.BadRequest(str(exc), code="F_VERSION_BAD_TOTAL") from exc
    return {
        "event_type": "quota_version",
        "received_at": received_at or now_iso(),
        "payload": {
            "pool_id": pool_id,
            "version_no": version_no,
            "kind": kind,
            "total_wan": str(total.quantize(config.DECIMAL_QUANT)),
            "published_at": published.isoformat(timespec="seconds"),
            "doc_no": str(payload.get("doc_no") or "").strip(),
            "source_url": str(payload.get("source_url") or "").strip(),
            "row_ref": str(payload.get("row_ref") or "").strip(),
        },
    }


def normalize_receipt(payload: dict, *, received_at: str | None = None) -> dict:
    """受理回执事件：受理金额必须为正，受理时间必须可解析。"""
    pool_id = _require(payload, "pool_id")
    receipt_no = _require(payload, "receipt_no")
    enterprise = _require(payload, "enterprise")
    try:
        amount = parse_amount(payload.get("amount_wan"), allow_zero=False)
        accepted = parse_instant(payload.get("accepted_at"))
    except ValueError as exc:
        raise errors.BadRequest(str(exc), code="F_RECEIPT_BAD_AMOUNT") from exc
    return {
        "event_type": "quota_receipt",
        "received_at": received_at or now_iso(),
        "payload": {
            "pool_id": pool_id,
            "receipt_no": receipt_no,
            "enterprise": enterprise,
            "amount_wan": str(amount.quantize(config.DECIMAL_QUANT)),
            "accepted_at": accepted.isoformat(timespec="seconds"),
            "source_url": str(payload.get("source_url") or "").strip(),
            "row_ref": str(payload.get("row_ref") or "").strip(),
        },
    }


def normalize_event(payload: dict, *, received_at: str | None = None) -> dict:
    event_type = str(payload.get("event_type") or "").strip()
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    if event_type == "quota_version":
        return normalize_version(body, received_at=received_at)
    if event_type == "quota_receipt":
        return normalize_receipt(body, received_at=received_at)
    raise errors.BadRequest(
        f"event_type 只支持 quota_version / quota_receipt，收到 {event_type!r}",
        code="F_BAD_EVENT")


def normalize_event_or_none(payload: dict, *, received_at: str | None = None) -> dict | None:
    """批量收件用：任何规范化错误（含 ValueError）都转成 None，由收件箱计入 invalid。"""
    try:
        return normalize_event(payload, received_at=received_at)
    except (ValueError, errors.NoticechainError):
        return None


def event_digest(event_type: str, facts: dict) -> str:
    """幂等键：sha256(类型 + 排序后的规范事实)。簿记字段不参与。"""
    keys = VERSION_FACTS if event_type == "quota_version" else RECEIPT_FACTS
    canonical = {key: facts[key] for key in keys if key in facts}
    raw = json.dumps({"event_type": event_type, "facts": canonical},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
