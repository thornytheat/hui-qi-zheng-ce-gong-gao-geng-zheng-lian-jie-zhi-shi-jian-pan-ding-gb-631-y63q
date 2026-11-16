"""质量服务：执行规则并给出隔离区视图数据（供页面与导出共用）。"""
from __future__ import annotations

from . import quarantine, rules


def evaluate(announcements: list[dict], settings) -> rules.QualityResult:
    return rules.apply(announcements, settings)


def view(rows: list[dict], *, code: str | None = None, stage: str | None = None,
         level: str | None = None) -> dict:
    filtered = quarantine.filter_rows(rows, code=code, stage=stage, level=level)
    distribution = quarantine.distribution(rows)
    for item in distribution:
        item["width"] = quarantine.bar_width(item["count"], distribution)
    return {
        "rows": filtered,
        "total": len(rows),
        "filtered": len(filtered),
        "distribution": distribution,
        "filters": {"code": code or "", "stage": stage or "", "level": level or ""},
        "by_level": {lvl: sum(1 for r in rows if r["level"] == lvl)
                     for lvl in ("error", "warn", "info")},
    }
