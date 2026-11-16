"""工作日推算：周一至周五 − 法定节假日 + 调休上班日。

区间规则（TRAP-23）：计数区间左闭右开 [发布日次日, 截止日次日)。
发布日不计入，截止日 = 计数区间内第 N 个工作日，因此相邻区间不重不漏。
"""
from __future__ import annotations

import datetime as dt

from ... import config


def _holidays(holidays) -> frozenset[str]:
    return holidays if holidays is not None else config.HOLIDAYS_2026


def _makeup(makeup) -> frozenset[str]:
    return makeup if makeup is not None else config.MAKEUP_WORKDAYS_2026


def is_workday(day: dt.date, holidays=None, makeup=None) -> bool:
    key = day.isoformat()
    holiday_set = _holidays(holidays)
    makeup_set = _makeup(makeup)
    if key in holiday_set:
        return False
    if key in makeup_set:
        return True
    return day.isoweekday() <= 5


def count_workdays(start: dt.date, end: dt.date, holidays=None, makeup=None) -> int:
    """[start, end) 内的工作日数。"""
    total = 0
    cursor = start
    while cursor < end:
        if is_workday(cursor, holidays, makeup):
            total += 1
        cursor += dt.timedelta(days=1)
    return total


def walk_workdays(start: dt.date, n: int, holidays=None, makeup=None) -> dict:
    """从 start 的次日开始数 n 个工作日，返回截止日与推算痕迹。"""
    cursor = start
    count = 0
    holiday_hits: list[str] = []
    makeup_hits: list[str] = []
    first: dt.date | None = None
    while count < n:
        cursor += dt.timedelta(days=1)
        key = cursor.isoformat()
        if key in _holidays(holidays):
            holiday_hits.append(key)
            continue
        if key in _makeup(makeup) or cursor.isoweekday() <= 5:
            count += 1
            if cursor.isoweekday() > 5:
                makeup_hits.append(key)
            if first is None:
                first = cursor
    return {
        "deadline": cursor,
        "first": first,
        "last": cursor,
        "n": n,
        "holiday_hits": holiday_hits,
        "makeup_hits": makeup_hits,
        "interval": f"[{(start + dt.timedelta(days=1)).isoformat()}, "
                    f"{(cursor + dt.timedelta(days=1)).isoformat()})",
    }


def add_natural_days(start: dt.date, n: int) -> dict:
    deadline = start + dt.timedelta(days=n)
    return {
        "deadline": deadline,
        "first": start + dt.timedelta(days=1),
        "last": deadline,
        "n": n,
        "holiday_hits": [],
        "makeup_hits": [],
        "interval": f"[{(start + dt.timedelta(days=1)).isoformat()}, "
                    f"{(deadline + dt.timedelta(days=1)).isoformat()})",
    }


def deadline_for_spec(spec: dict | None, base: dt.date, holidays=None, makeup=None) -> dict | None:
    if not spec:
        return None
    if spec["type"] == "workdays":
        trace = walk_workdays(base, int(spec["n"]), holidays, makeup)
        trace["mode"] = "workdays"
        return trace
    if spec["type"] == "days":
        trace = add_natural_days(base, int(spec["n"]))
        trace["mode"] = "days"
        return trace
    if spec["type"] == "date":
        day = dt.date.fromisoformat(spec["date"])
        return {"deadline": day, "first": None, "last": day, "n": 0,
                "holiday_hits": [], "makeup_hits": [], "mode": "date",
                "interval": f"[{day.isoformat()}, {(day + dt.timedelta(days=1)).isoformat()})"}
    return None


def explain(spec: dict | None, base: dt.date, trace: dict | None) -> list[str]:
    """给人读的推算依据：起点、规则、扣除的节假日、计入的调休、结果。"""
    if trace is None:
        return ["截止说明未登记，按「待确认」展示"]
    lines = [f"起点：发布日 {base.isoformat()}（+08:00，发布日不计入）"]
    if trace["mode"] == "date":
        lines.append("规则：公告直接给出绝对日期，不推算")
        lines.append(f"结果：{trace['deadline'].isoformat()}")
        return lines
    if trace["mode"] == "days":
        lines.append(f"规则：自然日 {trace['n']} 天")
        lines.append(f"结果：{trace['deadline'].isoformat()}")
        return lines
    lines.append(f"规则：工作日 {trace['n']} 个（周一至周五 − 法定节假日 + 调休上班日）")
    if trace["first"]:
        lines.append(f"第 1 个工作日：{trace['first'].isoformat()}")
    if trace["holiday_hits"]:
        lines.append("扣除法定节假日：" + "、".join(trace["holiday_hits"]))
    if trace["makeup_hits"]:
        lines.append("计入调休上班日：" + "、".join(trace["makeup_hits"]))
    lines.append(f"第 {trace['n']} 个工作日（截止日）：{trace['deadline'].isoformat()}")
    lines.append("计数区间：" + trace["interval"] + "，右开，不含次日")
    return lines
