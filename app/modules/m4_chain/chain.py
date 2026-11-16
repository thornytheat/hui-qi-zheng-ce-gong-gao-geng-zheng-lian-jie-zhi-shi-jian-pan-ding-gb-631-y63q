"""公告链合并：同一公文号的版本按序号合并，判定当前状态与真实截止。

规则（顺序固定，与采集顺序无关）：
1. 序号唯一：同一序号多条且取值不同 → C_DUP_SEQ_CONFLICT，整链隔离，不猜哪条为准；
2. 起点唯一：链首必须是序号 1 的首次发布，否则 C_NO_ORIGIN；
3. 时间单调：序号递增则发布时间必须递增，否则 C_TIME_INVERSION；
4. 合并：更正覆盖指定字段、延期顺延截止、撤销作废；撤销后再出现的版本记 C_AFTER_CANCEL；
5. 状态：冲突链不进清单；已撤销不进清单；截止日早于评估日记为过期，不进清单。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ... import config, reasons
from ..m2_parse import coerce, service as parse_service
from . import workdays

TARGET_ALLOWED = ("deadline", "amount", "title")


@dataclass
class ChainResult:
    chains: list[dict] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)

    def counters(self) -> dict:
        return {"in": sum(c["versions"] for c in self.chains), "out": len(self.chains),
                "conflicts": len(self.quarantine)}


def _q(code: str, row: dict, *, field_name: str | None = None,
       raw_value: str | None = None, detail: str = "") -> dict:
    reason = reasons.get(code)
    return {"code": code, "stage": reason.stage, "level": reason.level,
            "portal": row.get("portal"), "source_url": row.get("source_url"),
            "row_ref": row.get("row_ref"), "field": field_name, "raw_value": raw_value,
            "detail": detail or reason.title}


def _sig(row: dict) -> tuple:
    spec = row.get("deadline_spec") or {}
    return (row["kind"], row["title"], row["published_at"], spec.get("type"),
            spec.get("n"), spec.get("date"),
            None if row.get("amount_wan") is None else str(row["amount_wan"]),
            row.get("target_field"), row.get("target_value"))


def _local_date(published_iso: str) -> dt.date:
    moment = dt.datetime.fromisoformat(published_iso)
    return moment.astimezone(config.CHINA_TZ).date()


def _version_row(version: dict, note: str = "") -> dict:
    return {
        "doc_no": version["doc_no"], "seq": version["seq"], "kind": version["kind"],
        "title": version["title"], "published_at": version["published_at"],
        "deadline_spec": version.get("deadline_spec"),
        "amount_wan": version.get("amount_wan"),
        "target_field": version.get("target_field"),
        "target_value": version.get("target_value"),
        "portal": version.get("portal"), "row_ref": version.get("row_ref"),
        "source_url": version.get("source_url"), "note": note,
    }


def build_chains(announcements: list[dict], settings) -> ChainResult:
    result = ChainResult()
    groups: dict[str, list[dict]] = {}
    for row in announcements:
        groups.setdefault(row["doc_no"], []).append(row)
    for doc_no in sorted(groups):
        versions = sorted(groups[doc_no], key=lambda v: (v["seq"], v["published_at"]))
        chain, conflicts = _build_one(doc_no, versions, settings)
        result.chains.append(chain)
        result.quarantine.extend(conflicts)
    return result


def _build_one(doc_no: str, versions: list[dict], settings) -> tuple[dict, list[dict]]:
    quarantine: list[dict] = []
    conflicts: list[dict] = []
    version_rows = []
    state = {"title": versions[0]["title"], "deadline_spec": None, "deadline": None,
             "amount": None, "voided": False}

    def add_conflict(code: str, seq: int, refs: list[str], detail: str, level=None):
        conflicts.append({"code": code, "seq": seq, "refs": refs, "detail": detail})

    # 1. 序号唯一
    by_seq: dict[int, list[dict]] = {}
    for version in versions:
        by_seq.setdefault(version["seq"], []).append(version)
    duplicated = {seq: group for seq, group in by_seq.items()
                  if len({_sig(g) for g in group}) > 1}
    if duplicated:
        for seq, group in sorted(duplicated.items()):
            refs = [g["row_ref"] for g in group]
            detail = (f"序号 {seq} 出现 {len(group)} 条不同取值："
                      + "；".join(f"{g['row_ref']} 金额={g.get('amount_wan')}"
                                  f" 截止={coerce.spec_label(g.get('deadline_spec'))}"
                                  for g in group))
            add_conflict("C_DUP_SEQ_CONFLICT", seq, refs, detail)
            quarantine.append(_q("C_DUP_SEQ_CONFLICT", group[0], field_name="seq",
                                 raw_value=str(seq), detail=detail))
        for version in versions:
            version_rows.append(_version_row(version, "冲突链，版本仅登记"))
        return _chain(doc_no, versions, version_rows, "CONFLICT", None, None,
                      conflicts, "整链隔离：同一序号多值，不猜哪条为准"), quarantine

    # 2. 起点唯一
    origin_problem = None
    if versions[0]["seq"] != 1:
        origin_problem = (f"链首序号为 {versions[0]['seq']}，缺少序号 1 的首次发布")
    elif versions[0]["kind"] != "ORIGINAL":
        origin_problem = (f"链首序号 1 的类型是 {config.CHAIN_STATUS_LABELS.get(versions[0]['kind'], versions[0]['kind'])}"
                          f"，不是首次发布")
    originals = [v for v in versions if v["kind"] == "ORIGINAL"]
    if origin_problem is None and len(originals) > 1:
        origin_problem = f"链内出现 {len(originals)} 条首次发布"
    if origin_problem:
        add_conflict("C_NO_ORIGIN", versions[0]["seq"],
                     [v["row_ref"] for v in versions], origin_problem)
        quarantine.append(_q("C_NO_ORIGIN", versions[0], field_name="kind",
                             raw_value=str(versions[0]["seq"]), detail=origin_problem))
        for version in versions:
            version_rows.append(_version_row(version, "缺父公告，版本仅登记"))
        return _chain(doc_no, versions, version_rows, "CONFLICT", None, None,
                      conflicts, "整链隔离：缺少首次发布，更正/延期无依据"), quarantine

    # 3. 时间单调
    inversion = False
    previous = None
    for version in versions:
        if previous is not None and version["published_at"] < previous["published_at"]:
            inversion = True
            detail = (f"序号 {version['seq']} 的发布时间 {version['published_at']} "
                      f"早于序号 {previous['seq']} 的 {previous['published_at']}")
            add_conflict("C_TIME_INVERSION", version["seq"],
                         [previous["row_ref"], version["row_ref"]], detail)
            quarantine.append(_q("C_TIME_INVERSION", version, field_name="published_at",
                                 raw_value=version["published_at"], detail=detail))
        previous = version
    if inversion:
        for version in versions:
            version_rows.append(_version_row(version, "时间倒挂，版本仅登记"))
        return _chain(doc_no, versions, version_rows, "CONFLICT", None, None,
                      conflicts, "整链隔离：版本发布时间倒挂"), quarantine

    # 4. 逐版本合并
    orphan = False
    bad_extend = False
    after_cancel = False
    evaluate_date = settings.eval_date
    for version in versions:
        note = ""
        if version["kind"] == "ORIGINAL":
            state["title"] = version["title"]
            state["deadline_spec"] = version.get("deadline_spec")
            state["amount"] = version.get("amount_wan")
            base = _local_date(version["published_at"])
            trace = workdays.deadline_for_spec(state["deadline_spec"], base)
            state["deadline"] = trace["deadline"] if trace else None
            note = "首次发布：建立基线"
        elif state["voided"]:
            after_cancel = True
            note = "撤销后的新版本，仅登记"
            add_conflict("C_AFTER_CANCEL", version["seq"], [version["row_ref"]],
                         f"链已于此前撤销，序号 {version['seq']} 的新版本不影响判定")
            quarantine.append(_q("C_AFTER_CANCEL", version, field_name="kind",
                                 raw_value=version["kind"],
                                 detail="撤销后仍有新版本，链状态仍为已撤销"))
        elif version["kind"] == "CORRECT":
            target = parse_service.resolve_target(version)
            if target is None:
                orphan = True
                note = "更正字段无法解释"
                add_conflict("C_ORPHAN_CORRECTION", version["seq"], [version["row_ref"]],
                             f"更正事项「{version.get('target_field')}」不在允许范围内")
                quarantine.append(_q("C_ORPHAN_CORRECTION", version,
                                     field_name="target_field",
                                     raw_value=str(version.get("target_field"))))
            else:
                key, value = target
                if key == "deadline":
                    state["deadline_spec"] = value
                    base = _local_date(version["published_at"])
                    trace = workdays.deadline_for_spec(value, base)
                    state["deadline"] = trace["deadline"] if trace else None
                    note = f"更正截止说明为 {coerce.spec_label(value)}"
                elif key == "amount":
                    state["amount"] = value
                    note = f"更正资助额度为 {value} 万元"
                elif key == "title":
                    state["title"] = value
                    note = f"更正标题为 {value}"
        elif version["kind"] == "EXTEND":
            spec = version.get("deadline_spec")
            if spec is None:
                bad_extend = True
                note = "延期未给新截止说明"
                add_conflict("C_BAD_EXTEND", version["seq"], [version["row_ref"]],
                             "延期公告没有可用的新截止说明")
                quarantine.append(_q("C_BAD_EXTEND", version, field_name="deadline",
                                     raw_value=str(version.get("deadline_spec"))))
            else:
                state["deadline_spec"] = spec
                base = _local_date(version["published_at"])
                trace = workdays.deadline_for_spec(spec, base)
                state["deadline"] = trace["deadline"] if trace else None
                note = f"延期至 {state['deadline'].isoformat() if state['deadline'] else '待确认'}"
        elif version["kind"] == "CANCEL":
            state["voided"] = True
            note = "撤销：链作废"
        version_rows.append(_version_row(version, note))

    if orphan or bad_extend:
        status, listed = "CONFLICT", False
        note = "整链隔离：更正/延期依据不完整"
    elif state["voided"]:
        status, listed = "VOIDED", False
        note = "已撤销，不得申报"
    else:
        status = "ACTIVE"
        deadline = state["deadline"]
        expired = deadline is not None and deadline < evaluate_date
        listed = not expired
        note = ("可申报" if listed else
                f"已过期（截止 {deadline.isoformat()} < 评估日 {evaluate_date.isoformat()}）")
        if deadline is None:
            note = "可申报但截止未登记，需人工确认"
    if after_cancel:
        note += "；撤销后仍有新版本"
    deadline_value = state["deadline"]
    expired = bool(deadline_value is not None and deadline_value < settings.eval_date
                   and status == "ACTIVE")
    if status == "ACTIVE" and expired:
        listed = False
    return _chain(doc_no, versions, version_rows, status, deadline_value,
                  state["amount"], conflicts, note, title=state["title"],
                  listed=listed, expired=expired), quarantine


def _chain(doc_no: str, versions: list[dict], version_rows: list[dict], status: str,
           deadline: dt.date | None, amount: Decimal | None, conflicts: list[dict],
           note: str, *, title: str | None = None, listed: bool = False,
           expired: bool = False) -> dict:
    return {
        "doc_no": doc_no,
        "status": status,
        "title": title or versions[0]["title"],
        "versions": len(versions),
        "final_deadline": deadline.isoformat() if deadline else None,
        "final_amount_wan": amount,
        "listed": listed,
        "expired": expired,
        "note": note,
        "conflicts": conflicts,
        "version_rows": version_rows,
    }
