"""M7 编排：复核队列、按公告号回放、人工裁定与裁定后重算清单。

页面与 API 都只读 store，规则全在 review.py / replay.py 里；裁定写库前先校验，
校验失败抛领域异常（4xx），不留半条裁定。
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from ... import config, errors
from . import replay, review


def _versions(chain_row: dict) -> list[dict]:
    rows: list[dict] = []
    for version in chain_row.get("version_rows") or []:
        item = dict(version)
        spec = item.get("deadline_spec")
        if isinstance(spec, str):
            try:
                item["deadline_spec"] = json.loads(spec)
            except ValueError:
                item["deadline_spec"] = None
        rows.append(item)
    return rows


def _auto(chain_row: dict, settings) -> dict:
    status = chain_row.get("status")
    deadline = chain_row.get("final_deadline")
    listed = bool(chain_row.get("listed"))
    if status == "ACTIVE" and deadline:
        expired = dt.date.fromisoformat(deadline) < settings.eval_date
        if expired:
            status = "EXPIRED"
    return {
        "status": status,
        "status_label": config.CHAIN_STATUS_LABELS.get(status, status),
        "deadline": deadline,
        "amount": chain_row.get("final_amount_wan"),
        "listed": listed,
        "note": chain_row.get("note") or "",
        "conflicts": chain_row.get("conflicts") or [],
    }


def _snapshot(ruling: dict) -> dict:
    try:
        return json.loads(ruling.get("manual_json") or "{}")
    except ValueError:
        return {}


def replay_doc(store, batch_id: str, doc_no: str, settings) -> dict | None:
    """按公告号回放：候选优先级 + 逐版合并 + 非法迁移 + 与链判定的比对。"""
    chain_row = store.load_chain(batch_id, doc_no)
    if chain_row is None:
        return None
    versions = _versions(chain_row)
    candidates = review.rank_candidates(versions, doc_no)
    folded = replay.fold(review.effective_versions(versions, doc_no))
    view = replay.long_view(folded)
    raw_by_ref = {a.get("row_ref"): a.get("raw")
                  for a in store.load_announcements(batch_id) if a["doc_no"] == doc_no}
    rows: list[dict] = []
    for seq in sorted(candidates):
        for item in candidates[seq]:
            rows.append({
                "seq": item["seq"],
                "kind": item["kind"],
                "kind_label": item.get("kind_label"),
                "published_at": item.get("published_at"),
                "portal": item.get("portal"),
                "role_label": item["role_label"],
                "role_owner": item["role_owner"],
                "winner": item["winner"],
                "priority_tie": item["priority_tie"],
                "row_ref": item.get("row_ref"),
                "source_url": item.get("source_url"),
                "amount_wan": item.get("amount_wan"),
                "deadline_spec": item.get("deadline_spec"),
                "target_field": item.get("target_field"),
                "target_value": item.get("target_value"),
                "chain_note": item.get("note") or "",
                "raw": replay.raw_excerpt(raw_by_ref.get(item.get("row_ref"))),
            })
    ruling = store.load_ruling(batch_id, doc_no)
    manual = None
    if ruling is not None:
        manual = dict(_snapshot(ruling))
        manual["seq"] = ruling["seq"]
        manual["reviewer"] = ruling["reviewer"]
        manual["basis"] = ruling["basis"]
        manual["created_at"] = ruling["created_at"]
        manual["code"] = ruling["code"]
    return {
        "doc_no": doc_no,
        "title": chain_row.get("title"),
        "auto": _auto(chain_row, settings),
        "versions": rows,
        "seqs": sorted(candidates),
        "steps": view,
        "illegal": review.transition_problems(versions),
        "checks": replay.consistency(chain_row, folded["state"]),
        "replay_state": {
            "deadline": folded["state"]["deadline"].isoformat()
            if folded["state"]["deadline"] else None,
            "amount": None if folded["state"]["amount"] is None
            else str(folded["state"]["amount"].quantize(config.DECIMAL_QUANT)),
            "voided": folded["state"]["voided"],
        },
        "ruling": manual,
    }


def review_view(store, batch_id: str, settings, *, doc_no: str | None = None) -> dict:
    """复核页数据：待复核队列、已裁定列表、裁定后清单、当前回放。"""
    chains = store.load_chains(batch_id)
    rulings = {row["doc_no"]: row for row in store.load_rulings(batch_id)}
    queue: list[dict] = []
    for chain in chains:
        if not review.needs_review(chain):
            continue
        conflicts = chain.get("conflicts") or []
        queue.append({
            "doc_no": chain["doc_no"],
            "title": chain.get("title"),
            "versions": chain.get("versions"),
            "codes": sorted({c.get("code") for c in conflicts if c.get("code")}),
            "refs": sorted({ref for c in conflicts for ref in (c.get("refs") or [])}),
            "ruled": chain["doc_no"] in rulings,
        })
    reviewed: list[dict] = []
    listed_count = 0
    listed_amount = Decimal("0")
    for key in sorted(rulings):
        ruling = rulings[key]
        snapshot = _snapshot(ruling)
        reviewed.append({
            "doc_no": key,
            "seq": ruling["seq"],
            "reviewer": ruling["reviewer"],
            "basis": ruling["basis"],
            "created_at": ruling["created_at"],
            "code": ruling["code"],
            "status": snapshot.get("status"),
            "status_label": config.CHAIN_STATUS_LABELS.get(snapshot.get("status"),
                                                            snapshot.get("status")),
            "deadline": snapshot.get("deadline"),
            "amount": snapshot.get("amount"),
            "listed": bool(snapshot.get("listed")),
            "diffs": snapshot.get("diffs") or [],
            "auto_status": snapshot.get("auto_status"),
            "auto_deadline": snapshot.get("auto_deadline"),
            "auto_amount": snapshot.get("auto_amount"),
            "auto_listed": bool(snapshot.get("auto_listed")),
        })
        if snapshot.get("listed"):
            listed_count += 1
            if snapshot.get("amount"):
                listed_amount += Decimal(str(snapshot["amount"]))
    target = doc_no
    if target is None:
        target = queue[0]["doc_no"] if queue else (reviewed[0]["doc_no"] if reviewed else None)
    selected = replay_doc(store, batch_id, target, settings) if target else None
    if doc_no is not None and selected is None:
        raise errors.NotFound(f"批次 {batch_id} 中没有公文号 {doc_no}", code="E_NOT_FOUND")
    return {
        "queue": queue,
        "reviewed": reviewed,
        "selected": selected,
        "chains": [{"doc_no": c["doc_no"], "title": c.get("title"),
                    "status": c.get("status")} for c in chains],
        "counts": {
            "queue": len(queue),
            "ruled": len(reviewed),
            "overridden": sum(1 for r in reviewed if r["code"] == "M_MANUAL_OVERRIDE"),
            "long_chains": sum(1 for c in chains
                               if (c.get("versions") or 0) > replay.LINEAR_LIMIT),
        },
        "manual_list": {"count": listed_count,
                        "amount": listed_amount.quantize(config.DECIMAL_QUANT)},
        "filters": {"doc_no": target or "", "reviewer": "", "basis": ""},
    }


def submit_ruling(store, batch_id: str, doc_no: str, payload, settings) -> dict:
    """人工裁定：校验 → 重算 → 留痕（自动值 + 人工值）→ 落库。"""
    chain_row = store.load_chain(batch_id, doc_no)
    if chain_row is None:
        raise errors.NotFound(f"批次 {batch_id} 中没有公文号 {doc_no}", code="E_NOT_FOUND")
    versions = _versions(chain_row)
    seq = int(payload.seq)
    chosen = review.validate_ruling(versions, seq=seq, reviewer=payload.reviewer,
                                    basis=payload.basis)
    candidates = review.rank_candidates(versions, doc_no)
    winner = review.priority_winner(candidates, seq) or chosen[0]
    manual = review.recompute(versions, seq, settings, doc_no=doc_no)
    diffs = review.compare(chain_row, manual)
    code = review.ruling_code(diffs)
    snapshot = {
        "seq": seq,
        "status": manual["status"],
        "status_label": manual["status_label"],
        "deadline": manual["deadline"],
        "amount": manual["amount"],
        "listed": manual["listed"],
        "days_left": manual["days_left"],
        "auto_status": chain_row.get("status"),
        "auto_deadline": chain_row.get("final_deadline"),
        "auto_amount": None if chain_row.get("final_amount_wan") is None
        else str(chain_row["final_amount_wan"].quantize(config.DECIMAL_QUANT)),
        "auto_listed": bool(chain_row.get("listed")),
        "diffs": diffs,
        "code": code,
        "winner_portal": winner.get("portal"),
        "winner_role": winner.get("role"),
        "candidates": len(chosen),
    }
    ruling = {
        "batch_id": batch_id,
        "doc_no": doc_no,
        "seq": seq,
        "reviewer": (payload.reviewer or "").strip(),
        "basis": (payload.basis or "").strip(),
        "code": code,
        "created_at": dt.datetime.now(config.CHINA_TZ).isoformat(timespec="seconds"),
        "manual_json": json.dumps(snapshot, ensure_ascii=False),
        "note": manual["status_label"],
    }
    store.save_ruling(ruling)
    return {
        "batch_id": batch_id,
        "doc_no": doc_no,
        "seq": seq,
        "code": code,
        "reviewer": ruling["reviewer"],
        "basis": ruling["basis"],
        "created_at": ruling["created_at"],
        "manual": manual,
        "diffs": diffs,
        "snapshot": snapshot,
    }
