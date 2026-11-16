"""隔离区：统一构造隔离行、原因分布、可导出 CSV。

隔离行永远带 code + stage + 证据行号 + 原值；页面与导出读同一份数据。
"""
from __future__ import annotations

import csv
import io

from ... import reasons

CSV_HEADER = ("原因码", "级别", "阶段", "门户", "来源页面", "证据行", "字段", "原值", "说明")


def distribution(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["code"]] = counts.get(row["code"], 0) + 1
    total = len(rows) or 1
    out = []
    for code, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        reason = reasons.get(code)
        out.append({"code": code, "count": count, "share": round(count / total * 100, 1),
                    "level": reason.level, "level_label": reasons.LEVEL_LABELS[reason.level],
                    "stage": reason.stage, "title": reason.title, "hint": reason.hint})
    return out


def bar_width(count: int, rows: list[dict]) -> int:
    peak = max((r["count"] for r in rows), default=0) or 1
    return max(6, round(count / peak * 100))


def to_csv_text(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        reason = reasons.get(row["code"])
        writer.writerow([
            row["code"], reasons.LEVEL_LABELS[reason.level], reason.stage,
            row.get("portal") or "", row.get("source_url") or "",
            row.get("row_ref") or "", row.get("field") or "",
            row.get("raw_value") or "", row.get("detail") or "",
        ])
    return buffer.getvalue()


def filter_rows(rows: list[dict], *, code: str | None = None,
                stage: str | None = None, level: str | None = None) -> list[dict]:
    out = rows
    if code:
        out = [r for r in out if r["code"] == code]
    if stage:
        out = [r for r in out if r["stage"] == stage]
    if level:
        out = [r for r in out if r["level"] == level]
    return out
