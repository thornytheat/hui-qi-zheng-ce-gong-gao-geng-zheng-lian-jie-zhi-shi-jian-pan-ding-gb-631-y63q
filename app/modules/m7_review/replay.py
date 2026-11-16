"""M7 回放规则：按公告号把版本线性合并一遍，超长链给压缩视图。

- 单次顺序遍历（O(n)，不递归、不回填），每一步都对应到一条证据行；
- 超过 20 版时把「没有改变状态」的连续版本压成一步，避免回放表刷屏，结论不变；
- 回放结果要与 M4 的链判定逐字段对得上，否则说明两条路算得不一样。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config
from ..m2_parse import coerce, service as parse_service
from ..m4_chain import workdays

LINEAR_LIMIT = 20
MOVE_LABELS = {"ORIGINAL": "首次发布", "CORRECT": "更正", "EXTEND": "延期", "CANCEL": "撤销"}
CORRECT_LABELS = {"deadline": "更正截止说明", "amount": "更正资助额度", "title": "更正标题"}

# 原文列名（三门户写法不同，按别名命中后原样展示，不做二次归一）
RAW_FIELDS = {
    "资助额度": ("资助额度", "支持金额", "额度", "资助金额"),
    "截止说明": ("截止说明", "截止日期", "截止时间", "申报截止"),
    "公告类型": ("公告类型", "类型", "公告性质", "文种"),
}


def ordered(versions: list[dict]) -> list[dict]:
    """按 (序号, 发布时间) 定序：与采集顺序无关，重放可复现。"""
    return sorted(versions, key=lambda v: (int(v["seq"]), v.get("published_at") or ""))


def local_date(published_iso: str | None) -> dt.date | None:
    if not published_iso:
        return None
    try:
        moment = dt.datetime.fromisoformat(published_iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.UTC)
    return moment.astimezone(config.CHINA_TZ).date()


def _deadline(spec: dict | None, version: dict) -> dt.date | None:
    base = local_date(version.get("published_at"))
    if not spec or base is None:
        return None
    trace = workdays.deadline_for_spec(spec, base)
    return trace["deadline"] if trace else None


def _snapshot(state: dict) -> dict:
    deadline = state["deadline"]
    amount = state["amount"]
    return {
        "title": state["title"],
        "amount": None if amount is None else str(amount),
        "spec": coerce.spec_label(state["deadline_spec"]),
        "deadline": deadline.isoformat() if deadline else None,
        "voided": state["voided"],
    }


def _apply(state: dict, version: dict) -> tuple[str, str]:
    """把一个版本合并进状态；返回 (动作名, 结果说明)。"""
    kind = version["kind"]
    if kind == "ORIGINAL":
        state["title"] = version.get("title")
        state["amount"] = version.get("amount_wan")
        state["deadline_spec"] = version.get("deadline_spec")
        state["deadline"] = _deadline(state["deadline_spec"], version)
        return MOVE_LABELS["ORIGINAL"], "建立基线"
    if state["voided"]:
        return MOVE_LABELS.get(kind, kind), "链已撤销，本版不影响判定"
    if kind == "CORRECT":
        target = parse_service.resolve_target(version)
        if target is None:
            return MOVE_LABELS["CORRECT"], "更正事项无法解释，状态不变"
        key, value = target
        if key == "deadline":
            state["deadline_spec"] = value
            state["deadline"] = _deadline(value, version)
            return CORRECT_LABELS[key], f"→ {coerce.spec_label(value)}"
        if key == "amount":
            state["amount"] = value
            return CORRECT_LABELS[key], f"→ {value} 万元"
        state["title"] = value
        return CORRECT_LABELS[key], "→ 标题改写"
    if kind == "EXTEND":
        spec = version.get("deadline_spec")
        if spec is None:
            return MOVE_LABELS["EXTEND"], "延期未给新截止说明，状态不变"
        state["deadline_spec"] = spec
        state["deadline"] = _deadline(spec, version)
        return MOVE_LABELS["EXTEND"], f"→ {coerce.spec_label(spec)}"
    if kind == "CANCEL":
        state["voided"] = True
        return MOVE_LABELS["CANCEL"], "链作废"
    return MOVE_LABELS.get(kind, kind), "未登记的类型，状态不变"


def fold(versions: list[dict]) -> dict:
    """线性合并：一遍扫完，返回最终状态与逐步回放。"""
    state = {"title": None, "amount": None, "deadline_spec": None,
             "deadline": None, "voided": False}
    steps: list[dict] = []
    for index, version in enumerate(ordered(versions), start=1):
        before = _snapshot(state)
        action, note = _apply(state, version)
        after = _snapshot(state)
        steps.append({
            "index": index,
            "seq": int(version["seq"]),
            "kind": version["kind"],
            "action": action,
            "note": note,
            "changed": before != after,
            "row_ref": version.get("row_ref"),
            "portal": version.get("portal"),
            "before": before,
            "after": after,
        })
    return {"state": state, "steps": steps, "versions": len(steps), "mode": "linear"}


def compress(steps: list[dict]) -> list[dict]:
    """把连续的「没改变状态」的版本压成一步；首步永远保留。"""
    out: list[dict] = []
    for step in steps:
        if not step["changed"] and out and not out[-1]["changed"]:
            last = out[-1]
            last["merged_seqs"].append(step["seq"])
            last["merged_to"] = step["seq"]
            last["merged"] = len(last["merged_seqs"])
            last["note"] = f"{last['note']}（{last['merged']} 版同结论）"
            continue
        item = dict(step)
        item["merged_seqs"] = [step["seq"]]
        item["merged_to"] = step["seq"]
        item["merged"] = 1
        out.append(item)
    return out


def long_view(folded: dict) -> dict:
    """回放视图：普通链一版一步；超长链给压缩视图并标 M_LONG_CHAIN。"""
    steps = folded["steps"]
    long_chain = len(steps) > LINEAR_LIMIT
    if long_chain:
        rows = compress(steps)
    else:
        rows = []
        for step in steps:
            item = dict(step)
            item["merged_seqs"] = [step["seq"]]
            item["merged_to"] = step["seq"]
            item["merged"] = 1
            rows.append(item)
    return {
        "long": long_chain,
        "versions": len(steps),
        "limit": LINEAR_LIMIT,
        "rows": len(rows),
        "steps": rows,
        "code": "M_LONG_CHAIN" if long_chain else None,
    }


def consistency(chain_row: dict, state: dict) -> dict:
    """回放结果 vs 链判定：冲突链已整链隔离，不比对；其余必须逐字段相等。"""
    if chain_row.get("status") == "CONFLICT":
        return {"checked": False, "consistent": None, "diffs": [],
                "reason": "冲突链已整链隔离，回放只用于复核"}
    diffs: list[dict] = []
    replay_deadline = state["deadline"].isoformat() if state["deadline"] else None
    if replay_deadline != chain_row.get("final_deadline"):
        diffs.append({"field": "deadline", "chain": chain_row.get("final_deadline"),
                      "replay": replay_deadline})
    chain_amount = chain_row.get("final_amount_wan")
    chain_amount = (None if chain_amount is None
                    else str(Decimal(chain_amount).quantize(config.DECIMAL_QUANT)))
    replay_amount = (None if state["amount"] is None
                     else str(state["amount"].quantize(config.DECIMAL_QUANT)))
    if chain_amount != replay_amount:
        diffs.append({"field": "amount", "chain": chain_amount, "replay": replay_amount})
    replay_status = "VOIDED" if state["voided"] else "ACTIVE"
    if replay_status != chain_row.get("status"):
        diffs.append({"field": "status", "chain": chain_row.get("status"),
                      "replay": replay_status})
    return {"checked": True, "consistent": not diffs, "diffs": diffs, "reason": ""}


def raw_excerpt(raw: dict | None) -> list[dict]:
    """原文列摘录：把「资助额度/截止说明/公告类型」的原始写法原样带出来。"""
    if not raw:
        return []
    out: list[dict] = []
    for label, aliases in RAW_FIELDS.items():
        for key, value in raw.items():
            if str(key).strip() in aliases:
                out.append({"label": label, "value": str(value)[:40]})
                break
    return out
