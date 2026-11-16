"""质量判定：完全重复合并、哨兵整行隔离、金额与日期范围校验。

顺序固定：先按 (门户, 页面, 行号) 排序再做任何判定，
保证换一次采集顺序结果不变（TRAP-22 等价类：顺序无关）。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ... import config, reasons
from ..m2_parse import coerce


@dataclass
class QualityResult:
    accepted: list[dict] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)
    merged: int = 0
    rejected_records: int = 0

    def counters(self) -> dict:
        return {"in": len(self.accepted) + self.rejected_records, "out": len(self.accepted),
                "merged": self.merged, "rejected": self.rejected_records}


def _q(code: str, row: dict, *, field_name: str | None = None,
       raw_value: str | None = None, detail: str = "") -> dict:
    reason = reasons.get(code)
    return {"code": code, "stage": reason.stage, "level": reason.level,
            "portal": row.get("portal"), "source_url": row.get("source_url"),
            "row_ref": row.get("row_ref"), "field": field_name, "raw_value": raw_value,
            "detail": detail or reason.title}


def signature(row: dict) -> tuple:
    """转载重复判定用全行签名；业务键冲突留给链判定。"""
    spec = row.get("deadline_spec")
    return (
        row["doc_no"], row["seq"], row["kind"], row["published_at"],
        (spec or {}).get("type"), (spec or {}).get("n"), (spec or {}).get("date"),
        None if row.get("amount_wan") is None else str(row["amount_wan"]),
        row["title"], row.get("target_field"), row.get("target_value"),
    )


def is_sentinel_only(row: dict) -> bool:
    return (coerce.is_sentinel(row.get("title"))
            and row.get("amount_wan") is None
            and not row.get("deadline_spec"))


def amount_too_large(row: dict, limit: dt.decimal.Decimal | None = None) -> bool:
    limit = limit or config.AMOUNT_SUSPECT_LIMIT
    amount = row.get("amount_wan")
    return amount is not None and amount > limit


def date_out_of_window(row: dict, settings) -> bool:
    published = dt.datetime.fromisoformat(row["published_at"])
    local = published.astimezone(config.CHINA_TZ).date()
    upper = settings.eval_date + dt.timedelta(days=90)
    return local < config.MIN_PUBLISHED or local > upper


def _ordered(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r.get("portal", ""), r.get("source_url", ""),
                                       r.get("row_ref", "")))


def apply(announcements: list[dict], settings) -> QualityResult:
    result = QualityResult()
    seen: dict[tuple, dict] = {}
    for row in _ordered(announcements):
        sig = signature(row)
        if sig in seen:
            first = seen[sig]
            result.merged += 1
            result.quarantine.append(_q(
                "Q_DUP_ROW", row, field_name="row",
                raw_value=f"{row['doc_no']}#{row['seq']}",
                detail=f"与 {first['row_ref']} 取值完全一致（转载重复），保留首见行"))
            continue
        seen[sig] = row
        if is_sentinel_only(row):
            result.rejected_records += 1
            result.quarantine.append(_q("Q_SENTINEL_ONLY", row, field_name="title",
                                        raw_value=row.get("title")))
            continue
        if amount_too_large(row):
            result.rejected_records += 1
            result.quarantine.append(_q("Q_AMOUNT_SUSPECT", row, field_name="amount_wan",
                                        raw_value=str(row.get("amount_wan"))))
            continue
        if date_out_of_window(row, settings):
            result.rejected_records += 1
            result.quarantine.append(_q("Q_DATE_SUSPECT", row, field_name="published_at",
                                        raw_value=row["published_at"]))
            continue
        result.accepted.append(row)
    return result
