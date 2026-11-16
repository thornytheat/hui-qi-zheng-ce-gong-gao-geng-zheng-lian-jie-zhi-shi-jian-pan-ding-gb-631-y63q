"""值归一层：全角/千分位（TRAP-06）、空值哨兵（TRAP-07）、时区（TRAP-11）、
公文号保形、序号、截止说明。

每个函数都返回 (值, 错误码) 形式，错误码为 None 表示成功；不做静默兜底。
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from ... import config

_FULLWIDTH = {ord("０") + i: ord("0") + i for i in range(10)}
_FULLWIDTH.update({ord("Ａ") + i: ord("A") + i for i in range(26)})
_FULLWIDTH.update({ord("ａ") + i: ord("a") + i for i in range(26)})
_FULLWIDTH.update({
    ord("，"): ",", ord("．"): ".", ord("："): ":",
    ord("（"): "(", ord("）"): ")", ord("－"): "-", ord("％"): "%",
})

_WORKDAY_RE = re.compile(r"(\d{1,3})\s*个?\s*工作日")
_NATURAL_RE = re.compile(r"(\d{1,3})\s*个?\s*自然日")
_SENTINEL_NORMAL = {s.lower() for s in config.SENTINELS}
# 「详见正文」不是错误，是列表页的转引写法：字段未登记
_DEFERRED = {"详见正文", "详见附件", "详见通知", "见正文", "详见公告", "详见文件"}
_UNIT_PATTERNS = ("亿元", "万元", "亿", "万", "元")
_DOCNO_TAIL_RE = re.compile(
    r"^(?P<org>[\u4e00-\u9fffA-Za-z0-9]{2,20})〔(?P<year>\d{4})〕(?P<num>\d{1,4})号$")
_BRACKETS = {"[": "〔", "【": "〔", "（": "〔", "(": "〔",
             "]": "〕", "】": "〕", "）": "〕", ")": "〕"}
_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?$"), "iso"),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$"), "ymd"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"),
)


def to_halfwidth(text: str) -> str:
    return text.translate(_FULLWIDTH)


def is_sentinel(text: str | None) -> bool:
    if text is None:
        return True
    return text.strip().lower() in _SENTINEL_NORMAL


def is_deferred(text: str | None) -> bool:
    return (text or "").strip() in _DEFERRED


def clean(text: str | None) -> str:
    return (text or "").replace("\u200b", "").replace("\ufeff", "").strip()


def norm_doc_no(text: str | None) -> tuple[str | None, str | None]:
    raw = clean(text)
    if not raw:
        return None, "Q_MISSING_REQUIRED"
    value = to_halfwidth(raw).replace(" ", "").replace("\u3000", "")
    for src, dst in _BRACKETS.items():
        value = value.replace(src, dst)
    m = _DOCNO_TAIL_RE.match(value)
    if not m:
        return None, "Q_DOC_NO_INVALID"
    return value, None


def norm_seq(text: str | None) -> tuple[int | None, str | None]:
    raw = to_halfwidth(clean(text))
    if not raw:
        return None, "Q_MISSING_REQUIRED"
    if not re.fullmatch(r"\d{1,3}", raw):
        return None, "Q_SEQ_INVALID"
    value = int(raw)
    if value < 1:
        return None, "Q_SEQ_INVALID"
    return value, None


_KIND_ALIASES = {
    "ORIGINAL": ("首次发布", "首次", "新发", "公告", "通知", "发布", "首次公告"),
    "CORRECT": ("更正", "更正公告", "变更", "修改", "补正", "更正通知"),
    "EXTEND": ("延期", "延长", "顺延", "延期公告", "延长申报"),
    "CANCEL": ("撤销", "撤回", "作废", "废止", "取消", "撤销公告"),
}


def norm_kind(text: str | None) -> tuple[str | None, str | None]:
    raw = clean(text)
    if not raw:
        return None, "Q_MISSING_REQUIRED"
    for kind, aliases in _KIND_ALIASES.items():
        if raw in aliases:
            return kind, None
    for kind, aliases in _KIND_ALIASES.items():
        if any(alias in raw for alias in aliases):
            return kind, None
    return None, "Q_KIND_UNKNOWN"


def norm_amount(text: str | None) -> tuple[Decimal | None, str | None, str | None]:
    """→ (万元 Decimal | None, 单位, 错误码)。哨兵返回 (None, '万元', None)。"""
    raw = clean(text)
    if is_sentinel(raw):
        return None, config.MONEY_UNIT, None
    if is_deferred(raw):
        return None, config.MONEY_UNIT, None
    value = to_halfwidth(raw)
    for symbol in ("￥", "¥", "RMB", "人民币", "约"):
        value = value.replace(symbol, "")
    value = value.replace(",", "").replace("_", "").replace(" ", "")
    unit = config.MONEY_UNIT
    for candidate in _UNIT_PATTERNS:
        if value.endswith(candidate):
            unit = candidate
            value = value[: -len(candidate)]
            break
    if not value:
        return None, config.MONEY_UNIT, "Q_BAD_AMOUNT"
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None, config.MONEY_UNIT, "Q_BAD_AMOUNT"
    factor = config.UNIT_TO_WAN.get(unit)
    if factor is None:
        return None, config.MONEY_UNIT, "Q_BAD_AMOUNT"
    return (number * factor).quantize(config.DECIMAL_QUANT), config.MONEY_UNIT, None


def norm_instant(text: str | None) -> tuple[dt.datetime | None, str | None]:
    """→ (UTC aware datetime, 错误码)。日期型按 Asia/Shanghai 当日 00:00 处理。"""
    raw = clean(text)
    if is_sentinel(raw) or is_deferred(raw):
        return None, "Q_BAD_TIME"
    value = to_halfwidth(raw)
    iso = value.replace("Z", "+00:00")
    if "T" in iso or re.search(r"\d{4}-\d{1,2}-\d{1,2}\s\d", iso):
        try:
            parsed = dt.datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=config.CHINA_TZ)
            return parsed.astimezone(config.UTC), None
        except ValueError:
            pass
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.match(value)
        if not m:
            continue
        parts = [int(g) for g in m.groups() if g is not None]
        try:
            if kind == "iso":
                year, month, day, hour, minute = parts[:5]
                second = parts[5] if len(parts) > 5 else 0
                parsed = dt.datetime(year, month, day, hour, minute, second,
                                     tzinfo=config.CHINA_TZ)
            else:
                year, month, day = parts[:3]
                parsed = dt.datetime(year, month, day, 0, 0, 0, tzinfo=config.CHINA_TZ)
        except ValueError:
            return None, "Q_BAD_TIME"
        return parsed.astimezone(config.UTC), None
    return None, "Q_BAD_TIME"


def norm_deadline(text: str | None) -> tuple[dict | None, str | None, str | None]:
    """→ (spec, 错误码, 说明)。spec = {type: workdays|days|date, n?, date?}。"""
    raw = clean(text)
    if is_sentinel(raw):
        return None, None, "截止说明为空值哨兵，按未登记处理"
    if is_deferred(raw):
        return None, None, "列表页写「详见正文」，详情页未补齐，按未登记处理"
    value = to_halfwidth(raw)
    m = _WORKDAY_RE.search(value)
    if m:
        n = int(m.group(1))
        if n <= 0 or n > 200:
            return None, "Q_BAD_DEADLINE_RULE", f"工作日数量异常：{n}"
        return {"type": "workdays", "n": n}, None, "工作日截止"
    m = _NATURAL_RE.search(value)
    if m:
        n = int(m.group(1))
        if n <= 0 or n > 400:
            return None, "Q_BAD_DEADLINE_RULE", f"自然日数量异常：{n}"
        return {"type": "days", "n": n}, None, "自然日截止"
    instant, error = norm_instant(value)
    if instant is not None:
        local = instant.astimezone(config.CHINA_TZ).date()
        return {"type": "date", "date": local.isoformat()}, None, "绝对日期截止"
    return None, "Q_BAD_DEADLINE_RULE", f"无法识别的截止说明：{raw[:40]}"


def spec_label(spec: dict | None) -> str:
    if not spec:
        return "未登记"
    if spec["type"] == "workdays":
        return f"{spec['n']} 个工作日"
    if spec["type"] == "days":
        return f"{spec['n']} 个自然日"
    return f"{spec['date']} 到期"
