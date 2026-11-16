"""对账（四账）：页账、行账、金额账、清单账。

每条账都给出「输入 = 各部分之和」，差额非 0 就 balanced=False，
页面与导出都显示差额，不允许四舍五入掩盖。
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0.00")


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def build(*, pages: list[dict], rows_seen: int, parsed: int, parse_rejected: int,
          merged: int, quality_rejected: int, accepted: list[dict],
          chains: list[dict], settings) -> dict:
    ok = sum(1 for p in pages if p["status"] == "ok")
    skipped = sum(1 for p in pages if p["status"] == "skipped")
    failed = sum(1 for p in pages if p["status"] == "failed")
    page_in = len(pages)
    page_diff = page_in - (ok + skipped + failed)

    row_in = rows_seen
    row_diff = row_in - (parsed + parse_rejected)
    parsed_diff = parsed - (len(accepted) + merged + quality_rejected)

    input_amount = sum((a["amount_wan"] for a in accepted if a.get("amount_wan") is not None),
                       ZERO)
    version_amount = ZERO
    final_amount = ZERO
    listed_amount = ZERO
    for chain in chains:
        for version in chain.get("version_rows", []):
            version_amount += _dec(version.get("amount_wan"))
        final = _dec(chain.get("final_amount_wan"))
        final_amount += final
        if chain.get("listed"):
            listed_amount += final
    unlisted_amount = final_amount - listed_amount
    correction_delta = version_amount - final_amount
    amount_diff = (input_amount - correction_delta) - final_amount
    list_diff = final_amount - (listed_amount + unlisted_amount)
    version_count = sum(c["versions"] for c in chains)

    return {
        "pages": {
            "label": "页账", "in": page_in,
            "parts": {"公告页": ok, "非公告跳过": skipped, "采集失败": failed},
            "diff": page_diff, "balanced": page_diff == 0,
            "note": "抓取页数 = 公告页 + 非公告跳过页 + 采集失败页",
        },
        "rows": {
            "label": "行账", "in": row_in,
            "parts": {"入链公告": len(accepted), "转载合并": merged,
                      "解析隔离": parse_rejected, "质量隔离": quality_rejected},
            "diff": row_diff + parsed_diff,
            "balanced": row_diff == 0 and parsed_diff == 0,
            "note": "表格数据行 = 解析成功 + 解析隔离；解析成功 = 入链 + 转载合并 + 质量隔离",
        },
        "amounts": {
            "label": "金额账", "in": str(input_amount.quantize(Decimal("0.01"))),
            "parts": {
                "清单合计": str(listed_amount.quantize(Decimal("0.01"))),
                "未入清单合计": str(unlisted_amount.quantize(Decimal("0.01"))),
                "更正覆盖调整": str(correction_delta.quantize(Decimal("0.01"))),
            },
            "diff": str(amount_diff.quantize(Decimal("0.01"))),
            "balanced": amount_diff == 0,
            "note": "公告金额合计 − 更正覆盖调整 = 链最终金额 = 清单 + 未入清单",
        },
        "chains": {
            "label": "清单账", "in": len(accepted),
            "parts": {"链内版本": version_count,
                      "链数": len(chains),
                      "入清单链": sum(1 for c in chains if c.get("listed"))},
            "diff": len(accepted) - version_count,
            "balanced": len(accepted) == version_count,
            "note": "入链公告数 = 每条链版本数之和（每条公告恰好属于一条链）",
        },
        "rule_version": settings.rule_version,
        "eval_date": settings.eval_date.isoformat(),
        "all_balanced": (page_diff == 0 and row_diff == 0 and parsed_diff == 0
                         and amount_diff == 0 and list_diff == 0
                         and len(accepted) == version_count),
    }


def rows_for_page(reconciliation: dict) -> list[dict]:
    """页面表格：一条账一行。"""
    out = []
    for key in ("pages", "rows", "amounts", "chains"):
        account = reconciliation[key]
        parts = "，".join(f"{k} {v}" for k, v in account["parts"].items())
        out.append({
            "key": key, "label": account["label"], "in": account["in"],
            "parts": parts, "diff": account["diff"], "balanced": account["balanced"],
            "note": account["note"],
        })
    return out
