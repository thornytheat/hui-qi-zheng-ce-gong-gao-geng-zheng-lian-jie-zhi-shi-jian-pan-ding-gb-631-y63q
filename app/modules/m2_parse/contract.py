"""字段契约：列定义、必需字段、去重键、区间声明、原因码目录。

对外通过 GET /api/contract 暴露，页面上的「规则」面板读同一份文档，
保证文档与代码不会各写一套。
"""
from __future__ import annotations

from ... import config, reasons
from . import fields

CONTRACT_VERSION = fields.CONTRACT_VERSION


def document() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "rule_version": config.RULE_VERSION,
        "decimals": {
            "amount_unit": config.MONEY_UNIT,
            "quantize": str(config.DECIMAL_QUANT),
            "storage": "SQLite 中以 TEXT 存 Decimal 字符串，不落 float",
            "unit_normalization": {k: str(v) for k, v in config.UNIT_TO_WAN.items()},
        },
        "time": {
            "storage": "UTC，ISO 8601 带偏移",
            "display": "Asia/Shanghai(+08:00)",
            "date_only_rule": "只有日期的公告按当日 00:00(+08:00) 解释",
        },
        "interval": {
            "workdays": "[发布日次日, 截止日] 内取第 N 个工作日；发布日不计入",
            "natural_days": "截止日 = 发布日 + N 天，区间 [发布日, 发布日+N+1)",
            "endpoint": "相邻区间左闭右开，不重不漏",
        },
        "columns": [
            {
                "name": f.name, "zh": f.zh, "aliases": list(f.aliases),
                "kind": f.kind, "required": f.required, "note": f.note,
            }
            for f in fields.FIELDS
        ],
        "kinds": fields.KIND_LABELS,
        "target_fields": {k: list(v) for k, v in fields.TARGET_FIELDS.items()},
        "dedupe_key": "doc_no + seq（链内版本位）；全行哈希仅用于识别转载重复",
        "dedupe_reason": "同一版本被两个门户转载时，业务键相同但取值一致，可合并；"
                         "取值不同则视为冲突，整链隔离而不是覆盖",
        "sentinels": sorted(config.SENTINELS),
        "stages": [
            {"key": s, "label": config.STAGE_LABELS[s]} for s in config.STAGES
        ],
        "reason_codes": reasons.catalog(),
    }


def missing_required(canon: dict[str, str]) -> list[str]:
    return [name for name in fields.REQUIRED_NAMES
            if not (canon.get(name) or "").strip()]


def column_error(header_count: int, cell_count: int) -> str | None:
    """TRAP-05：列数与表头不一致一律隔离，不静默补空也不截断。"""
    if header_count <= 0:
        return "Q_MISSING_REQUIRED"
    if cell_count != header_count:
        return "Q_COLUMN_COUNT"
    return None
