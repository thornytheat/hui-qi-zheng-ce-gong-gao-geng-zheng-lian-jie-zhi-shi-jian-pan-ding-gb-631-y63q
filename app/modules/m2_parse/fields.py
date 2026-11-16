"""列契约（键名层）：三个门户的列名各不相同，统一映射到 9 个规范字段。

契约是唯一映射来源：新增门户只加别名，不改下游。别名归一包括
UTF-8 BOM（TRAP-02）、全角括号、多余空格、末尾冒号与星号。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CONTRACT_VERSION = "contract/1.4"

KIND_LABELS = {
    "ORIGINAL": "首次发布",
    "CORRECT": "更正",
    "EXTEND": "延期",
    "CANCEL": "撤销",
}
TARGET_FIELDS = {
    "deadline": ("截止说明", "截止日期", "截止时间", "申报截止"),
    "amount": ("资助额度", "支持金额", "额度", "资助金额"),
    "title": ("标题", "公告标题"),
}
TARGET_LABELS = {"deadline": "截止说明", "amount": "资助额度", "title": "标题"}


@dataclass(frozen=True)
class Field:
    name: str
    zh: str
    aliases: tuple[str, ...]
    kind: str          # docno|int|enum|str|instant|deadline|amount
    required: bool
    note: str


FIELDS: tuple[Field, ...] = (
    Field("doc_no", "公文号", ("文号", "文件编号", "编号", "发文字号", "文件号"),
          "docno", True, "机关〔年〕序号号；字符串保形，前导零保留"),
    Field("seq", "公告序号", ("序号", "版本序号", "版本号", "公告号序"),
          "int", True, "链内顺序，从 1 起；序号决定合并次序"),
    Field("kind", "公告类型", ("类型", "公告性质", "文种", "公告种类"),
          "enum", True, "首次发布/更正/延期/撤销"),
    Field("title", "标题", ("公告标题", "名称", "项目名称"),
          "str", True, "原文标题，不做去停用词"),
    Field("published_at", "发布日期", ("发布时间", "发文时间", "日期", "公布日期"),
          "instant", True, "允许 Z / +08:00 / 中文日期；先归 UTC"),
    Field("deadline", "截止说明", ("截止日期", "截止时间", "申报截止", "申报截止时间", "截止"),
          "deadline", False, "N个工作日 / N个自然日 / 绝对日期；无法识别给原因码"),
    Field("amount", "资助额度", ("支持金额", "额度", "资助金额", "支持额度", "金额"),
          "amount", False, "统一归一到万元 Decimal，最小刻度 0.01"),
    Field("target_field", "更正事项", ("更正内容", "调整事项", "修正事项"),
          "str", False, "仅更正公告：截止说明/资助额度/标题"),
    Field("target_value", "更正后", ("更正后内容", "调整后", "更正为", "修正后"),
          "str", False, "按更正事项解释类型"),
)

FIELD_NAMES = tuple(f.name for f in FIELDS)
REQUIRED_NAMES = tuple(f.name for f in FIELDS if f.required)
FIELD_BY_NAME = {f.name: f for f in FIELDS}

_HEADER_NOISE_RE = re.compile(r"[\s\u00a0\u3000\ufeff:：*＊]")


def norm_header(raw: str) -> str:
    """列名归一：剥 BOM、去空白与全角冒号、去末尾星号。"""
    if raw is None:
        return ""
    text = raw.replace("\ufeff", "").replace("\u200b", "")
    text = _HEADER_NOISE_RE.sub("", text)
    return text


# 别名打分：字段中文名 3 分 > 显式别名 2 分 > 兜底 1 分。
# 这样「公告序号」不会被同页的「序号」列抢走（TRAP-17 列名演进的一类）。
_ALIAS_SCORE: dict[str, tuple[str, int]] = {}
for _f in FIELDS:
    _ALIAS_SCORE[norm_header(_f.zh)] = (_f.name, 3)
for _f in FIELDS:
    for _i, _alias in enumerate(_f.aliases):
        _key = norm_header(_alias)
        _score = 2 - min(_i, 1)
        if _key not in _ALIAS_SCORE or _ALIAS_SCORE[_key][1] < _score:
            _ALIAS_SCORE[_key] = (_f.name, _score)


def resolve(header: str) -> str | None:
    hit = _ALIAS_SCORE.get(norm_header(header))
    return hit[0] if hit else None


def alias_score(header: str) -> int:
    hit = _ALIAS_SCORE.get(norm_header(header))
    return hit[1] if hit else 0


def canonicalize(raw_fields: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """原始列名 → 规范字段；同名列按别名精度取最高分，未能识别的列留备查。"""
    canon: dict[str, str] = {}
    unknown: dict[str, str] = {}
    chosen: dict[str, int] = {}
    for key, value in raw_fields.items():
        name = resolve(key)
        if name is None:
            unknown[key] = value
            continue
        score = alias_score(key)
        if name not in chosen or score > chosen[name]:
            canon[name] = value
            chosen[name] = score
    return canon, unknown


def target_field_name(raw: str | None) -> str | None:
    if not raw:
        return None
    text = norm_header(raw)
    for name, aliases in TARGET_FIELDS.items():
        labels = TARGET_FIELDS[name]
        if any(norm_header(a) in text or text in norm_header(a) for a in labels):
            return name
        if text == target_label_short(name):
            return name
    return None


def target_label_short(name: str) -> str:
    return TARGET_LABELS.get(name, name)
