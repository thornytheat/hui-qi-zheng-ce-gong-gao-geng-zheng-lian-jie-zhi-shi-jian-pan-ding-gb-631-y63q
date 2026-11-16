"""M7 裁定规则：转载优先级、迁移合法性、裁定校验、按裁定重算与改判差异。

四条规则，与采集顺序、页面顺序无关：
1. 候选优先级：发文机关的版本 > 转载页的版本；同来源多值时优先级定不了，只能人工裁定；
2. 迁移合法性：撤销之后再更正/延期/发布属非法迁移，这样的版本不能被选为有效版；
3. 人工裁定：指定某版为有效 → 以该版及之前的版本重算截止、额度与是否进清单；
4. 改判留痕：人工结果与自动判定不同就记 M_MANUAL_OVERRIDE，自动值与人工值都留着。
"""
from __future__ import annotations

from ... import config, errors
from ..m2_parse.fields import KIND_LABELS
from . import replay

# 公文号前缀 → 发文机关；门户 → 该门户所属机关。两者相同才算发文机关页。
DOC_NO_AGENCY = {
    "澄工信发": "industry_bureau",
    "澄科技发": "science_bureau",
    "澄企服": "service_center",
}
PORTAL_AGENCY = {
    "portal_a": "industry_bureau",
    "portal_b": "science_bureau",
    "portal_c": "service_center",
}
AGENCY_LABELS = {
    "industry_bureau": "江阴区工信局",
    "science_bureau": "江南市科技局",
    "service_center": "园区企服中心",
}

ROLE_ISSUER = "issuer"
ROLE_REPOST = "repost"
ROLE_UNKNOWN = "unknown"
ROLE_LABELS = {
    ROLE_ISSUER: "发文机关页",
    ROLE_REPOST: "转载页",
    ROLE_UNKNOWN: "来源未登记",
}
ROLE_RANK = {ROLE_ISSUER: 0, ROLE_REPOST: 1, ROLE_UNKNOWN: 2}

# 撤销之后不允许再出现的版本类型（TRAP-28）
AFTER_CANCEL_ILLEGAL = ("ORIGINAL", "CORRECT", "EXTEND")
MIN_REVIEWER = 2
MIN_BASIS = 6

COMPARE_FIELDS = (
    ("status", "判定"),
    ("final_deadline", "当前截止"),
    ("final_amount_wan", "资助额度(万元)"),
    ("listed", "在清单内"),
)
MANUAL_KEYS = {
    "status": "status",
    "final_deadline": "deadline",
    "final_amount_wan": "amount",
    "listed": "listed",
}


def agency_of_doc(doc_no: str | None) -> str | None:
    text = doc_no or ""
    for prefix in sorted(DOC_NO_AGENCY, key=len, reverse=True):
        if text.startswith(prefix):
            return DOC_NO_AGENCY[prefix]
    return None


def role_of(portal: str | None, doc_no: str | None) -> tuple[str, str | None]:
    """→ (角色, 该门户所属机关名)。角色决定同一版本取哪一页的值。"""
    agency = PORTAL_AGENCY.get(portal or "")
    issuer = agency_of_doc(doc_no)
    if agency is None or issuer is None:
        return ROLE_UNKNOWN, None
    if agency == issuer:
        return ROLE_ISSUER, AGENCY_LABELS.get(agency)
    return ROLE_REPOST, AGENCY_LABELS.get(agency)


def _priority_key(row: dict, doc_no: str | None) -> tuple:
    role, _owner = role_of(row.get("portal"), doc_no)
    return (ROLE_RANK[role], row.get("portal") or "", row.get("row_ref") or "")


def rank_candidates(versions: list[dict], doc_no: str | None) -> dict[int, list[dict]]:
    """按序号分组并按优先级排序；winner 是取值的那一版，tie 表示同来源多值。"""
    grouped: dict[int, list[dict]] = {}
    for version in versions:
        grouped.setdefault(int(version["seq"]), []).append(version)
    out: dict[int, list[dict]] = {}
    for seq in sorted(grouped):
        ordered = sorted(grouped[seq], key=lambda row: _priority_key(row, doc_no))
        rows: list[dict] = []
        for index, row in enumerate(ordered):
            role, owner = role_of(row.get("portal"), doc_no)
            item = dict(row)
            item.update({
                "role": role, "role_label": ROLE_LABELS[role], "role_owner": owner,
                "winner": index == 0,
                "kind_label": KIND_LABELS.get(row.get("kind"), row.get("kind")),
            })
            rows.append(item)
        tie = len(rows) > 1 and rows[0]["role"] == rows[1]["role"]
        for item in rows:
            item["priority_tie"] = tie
        out[seq] = rows
    return out


def priority_winner(candidates: dict[int, list[dict]], seq: int) -> dict | None:
    rows = candidates.get(seq) or []
    return rows[0] if rows else None


def effective_versions(versions: list[dict], doc_no: str | None = None) -> list[dict]:
    """同一序号只留优先级最高的一版：发文机关页 > 转载页 > 来源未登记。"""
    target = doc_no or (versions[0].get("doc_no") if versions else None)
    candidates = rank_candidates(versions, target)
    return [candidates[seq][0] for seq in sorted(candidates)]


def transition_problems(versions: list[dict]) -> list[dict]:
    """撤销之后又出现的新版本：非法迁移，逐条列出证据行（TRAP-28）。"""
    problems: list[dict] = []
    cancelled: dict | None = None
    for version in sorted(versions, key=lambda v: (v["seq"], v.get("published_at") or "")):
        if cancelled is not None and version["kind"] in AFTER_CANCEL_ILLEGAL:
            problems.append({
                "code": "M_ILLEGAL_TRANSITION",
                "seq": version["seq"], "kind": version["kind"],
                "kind_label": KIND_LABELS.get(version["kind"], version["kind"]),
                "after_seq": cancelled["seq"],
                "row_ref": version.get("row_ref"),
                "detail": (f"序号 {cancelled['seq']} 已撤销，"
                           f"序号 {version['seq']} 又给出"
                           f"{KIND_LABELS.get(version['kind'], version['kind'])}"),
            })
        if version["kind"] == "CANCEL" and cancelled is None:
            cancelled = version
    return problems


def validate_ruling(versions: list[dict], *, seq: int, reviewer: str,
                    basis: str) -> list[dict]:
    """裁定入参校验；不合法直接抛领域异常（4xx），不写库。"""
    if not versions:
        raise errors.NotFound("该公文号没有任何版本，无法裁定", code="E_NOT_FOUND")
    reviewer = (reviewer or "").strip()
    basis = (basis or "").strip()
    if len(reviewer) < MIN_REVIEWER:
        raise errors.BadRequest(f"裁定人至少 {MIN_REVIEWER} 个字",
                                code="M_BAD_RULING", detail={"field": "reviewer"})
    if len(basis) < MIN_BASIS:
        raise errors.BadRequest(
            f"裁定依据至少 {MIN_BASIS} 个字：写清看了哪一页、哪一行",
            code="M_BAD_RULING", detail={"field": "basis", "min": MIN_BASIS})
    chosen = [v for v in versions if int(v["seq"]) == int(seq)]
    if not chosen:
        seqs = sorted({int(v["seq"]) for v in versions})
        raise errors.BadRequest(f"序号 {seq} 不在该链内；可选 {seqs}",
                                code="M_NO_SUCH_SEQ", detail={"seq": seq, "seqs": seqs})
    problems = [p for p in transition_problems(versions) if int(p["seq"]) == int(seq)]
    if problems:
        raise errors.Conflict(problems[0]["detail"],
                              code="M_ILLEGAL_TRANSITION", detail=problems[0])
    return chosen


def recompute(versions: list[dict], seq: int, settings, doc_no: str | None = None) -> dict:
    """以「该版及其之前」的版本重算有效状态；与链判定同源规则，可复算。

    同一序号先按转载优先级收敛成一条，再合并：人工选的是有效版，不是某一行。
    """
    upto = [v for v in effective_versions(versions, doc_no) if int(v["seq"]) <= int(seq)]
    state = replay.fold(upto)["state"]
    deadline = state["deadline"]
    if state["voided"]:
        status, listed = "VOIDED", False
    elif deadline is not None and deadline < settings.eval_date:
        status, listed = "EXPIRED", False
    else:
        status, listed = "ACTIVE", True
    amount = state["amount"]
    return {
        "status": status,
        "status_label": config.CHAIN_STATUS_LABELS.get(status, status),
        "deadline": deadline.isoformat() if deadline else None,
        "amount": None if amount is None else str(amount.quantize(config.DECIMAL_QUANT)),
        "listed": listed,
        "title": state["title"],
        "days_left": None if deadline is None else (deadline - settings.eval_date).days,
    }


def _auto_value(chain_row: dict, key: str):
    if key == "final_amount_wan":
        value = chain_row.get("final_amount_wan")
        return None if value is None else str(value.quantize(config.DECIMAL_QUANT))
    if key == "listed":
        return bool(chain_row.get("listed"))
    return chain_row.get(key)


def compare(chain_row: dict, manual: dict) -> list[dict]:
    """自动判定 vs 人工裁定，逐字段列出差异（相同字段不出现在结果里）。"""
    diffs: list[dict] = []
    for key, label in COMPARE_FIELDS:
        auto = _auto_value(chain_row, key)
        after = manual.get(MANUAL_KEYS[key])
        if auto != after:
            diffs.append({"field": key, "label": label, "auto": auto, "manual": after})
    return diffs


def ruling_code(diffs: list[dict]) -> str:
    """人工结果与自动判定不同 → 改判（人工优先，但两条值都留痕）。"""
    return "M_MANUAL_OVERRIDE" if diffs else "M_RULING_RECORDED"


def needs_review(chain_row: dict) -> bool:
    return chain_row.get("status") == "CONFLICT"
