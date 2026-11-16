"""额度账本核心规则：版本切换、回执钉版、快照与对账。

三条不变量：
1. 受理回执钉在「受理时刻」有效的额度版本上（pegged_version_no），
   更正只换总额指针，绝不把旧受理重算成超占；
2. 新申报只看当前版本（更正后总额）的可用余额 = 当前总额 − 当前版本内钉版占用；
3. 版本切换是校验通过后的整体动作，任一校验失败保留上一完整额度版本，指针不动。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config
from . import events

ZERO = Decimal("0.00")


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q2(value: Decimal) -> Decimal:
    return value.quantize(config.DECIMAL_QUANT)


# ---------- 版本切换校验（返回 None 表示通过，否则返回 (原因码, 说明)） ----------

def validate_version(pool: dict | None, existing: list[dict], candidate: dict) -> tuple | None:
    """校验候选版本能否切换；existing 为该池全部版本（含 rejected），按序号升序。"""
    facts = candidate["payload"]
    kind = facts["kind"]
    current_no = pool.get("current_version_no") if pool else None
    applied = [v for v in existing if v["status"] == "applied"]

    if kind == "首发":
        if current_no is not None:
            return ("F_VERSION_REJECTED",
                    f"池 {facts['pool_id']} 已有生效版本 v{current_no}，首发不能再次切换")
        same_seq = next((v for v in existing if v["version_no"] == facts["version_no"]), None)
        if same_seq is not None:
            return ("F_VERSION_DUP_SEQ",
                    f"序号 v{facts['version_no']} 已存在（总额 {same_seq['total_wan']}），"
                    f"同序号不同总额不猜哪条为准")
        return None

    # 更正 / 撤销：池必须已首发
    if current_no is None:
        return ("F_VERSION_REJECTED",
                f"池 {facts['pool_id']} 缺少首发版本，{kind} 无从切换")
    current = next((v for v in applied if v["version_no"] == current_no), None)
    if current is None:
        return ("F_VERSION_REJECTED", "当前版本指针找不到已生效版本，拒绝切换")
    if current["kind"] == "撤销":
        return ("F_VERSION_REJECTED", "资金池已撤销，撤销后的公告版本不能再切换")
    same_seq = next((v for v in existing if v["version_no"] == facts["version_no"]), None)
    if same_seq is not None:
        if same_seq["status"] == "applied" and _dec(same_seq["total_wan"]) == _dec(
                facts["total_wan"]) and same_seq["kind"] == kind:
            return ("F_DUP_EVENT", "同序号同总额的重复版本，按幂等处理")
        return ("F_VERSION_DUP_SEQ",
                f"序号 v{facts['version_no']} 已存在（{same_seq['kind']} / "
                f"{same_seq['total_wan']}），同序号冲突不猜哪条为准")
    if facts["version_no"] <= current_no:
        return ("F_VERSION_DUP_SEQ",
                f"更正序号 v{facts['version_no']} 不大于当前 v{current_no}，不能回退覆盖")
    published = events.parse_instant(facts["published_at"])
    current_published = events.parse_instant(current["published_at"])
    if published < current_published:
        return ("F_VERSION_INVERSION",
                f"更正发布时间 {published.isoformat()} 早于当前版本 "
                f"{current_published.isoformat()}，时间倒挂拒绝切换")
    return None


# ---------- 回执钉版 ----------

def pegging_version(versions: list[dict], accepted_at: str) -> dict | None:
    """找受理时刻有效的版本：applied 且发布时间 ≤ 受理时刻，取序号最大者。

    受理时刻早于首发 → 返回 None（挂起）；晚于撤销 → 撤销版本身不承载总额，
    钉在撤销前最后一个承载版本上（pool 状态另判，撤销池的新回执在服务层拦截）。
    """
    accepted = events.parse_instant(accepted_at)
    candidates = []
    for version in versions:
        if version["status"] != "applied":
            continue
        if events.parse_instant(version["published_at"]) <= accepted:
            candidates.append(version)
    if not candidates:
        return None
    return max(candidates, key=lambda v: v["version_no"])


# ---------- 快照（总额 / 占用 / 可用 / 超额缺口，逐项可对上） ----------

def pool_snapshot(pool: dict, versions: list[dict], receipts: list[dict]) -> dict:
    """按版本核算占用，并给出当前版本视图（新申报看到的可用余额）。

    - 每个承载额度的 applied 版本（首发/更正）：钉版占用合计、受理笔数、超占笔数、
      版本视角的可用与缺口；撤销版不承载额度，只进时间线；
    - 当前版本：占用只统计钉在当前版本的回执，旧版本占用不重算进新余额；
    - 全池口径：占用 = 所有有效钉版回执之和，用于与回执清单逐项对账。
    """
    applied_versions = sorted(
        (v for v in versions if v["status"] == "applied"),
        key=lambda v: v["version_no"])
    occupancy_versions = [v for v in applied_versions if v["kind"] != "撤销"]
    void_event = next((v for v in applied_versions if v["kind"] == "撤销"), None)
    valid_receipts = [r for r in receipts if r["status"] in ("applied",)
                      and r.get("pegged_version_no") is not None]
    current_no = pool.get("current_version_no")

    version_views = []
    for version in occupancy_versions:
        pegged = [r for r in valid_receipts
                  if r["pegged_version_no"] == version["version_no"]]
        occupied = q2(sum((r["amount_wan"] for r in pegged), ZERO))
        total = q2(_dec(version["total_wan"]))
        overshoot = q2(max(ZERO, occupied - total))
        view = {
            "version_no": version["version_no"],
            "kind": version["kind"],
            "total_wan": total,
            "published_at": version["published_at"],
            "received_at": version["received_at"],
            "doc_no": version["doc_no"],
            "row_ref": version["row_ref"],
            "receipt_count": len(pegged),
            "overshoot_count": sum(1 for r in pegged if r["overshoot"]),
            "occupied_wan": occupied,
            "available_wan": q2(max(ZERO, total - occupied)),
            "gap_wan": overshoot,
            "is_current": version["version_no"] == current_no,
            "receipts": sorted(pegged, key=lambda r: (r["accepted_at"], r["receipt_no"])),
        }
        version_views.append(view)

    current_view = next((v for v in version_views if v["is_current"]), None)
    total_occupied = q2(sum((r["amount_wan"] for r in valid_receipts), ZERO))
    # 只有显式 VOIDED 池才把当前总额/占用归零；有效池当前指针必指向承载版本
    if pool.get("status") == "VOIDED" or current_view is None:
        current_total = ZERO
        current_occupied = ZERO
    else:
        current_total = current_view["total_wan"]
        current_occupied = current_view["occupied_wan"]
    # 可用余额截到 0，不向新申报展示负数；超占部分走超额缺口
    available = q2(max(ZERO, current_total - current_occupied))
    pending = [r for r in receipts if r["status"] == "pending"]

    return {
        "pool_id": pool["pool_id"],
        "title": pool.get("title"),
        "status": pool.get("status", "ACTIVE"),
        "current_version_no": current_no,
        "void_event": void_event,
        "current_total_wan": current_total,
        "current_occupied_wan": current_occupied,
        "available_wan": available,
        "gap_wan": current_view["gap_wan"] if current_view else ZERO,
        "total_occupied_wan": total_occupied,
        "receipt_total": len(valid_receipts),
        "overshoot_total": sum(1 for r in valid_receipts if r["overshoot"]),
        "pending_total": len(pending),
        "version_views": version_views,
        "pending": sorted(pending, key=lambda r: (r["accepted_at"], r["receipt_no"])),
        "rejected_versions": [v for v in versions if v["status"] == "rejected"],
    }


def reconcile(snapshots: list[dict]) -> dict:
    """额度账：当前总额 = 当前占用 + 可用 − 超额缺口；占用 = 各版本钉版占用之和。"""
    rows = []
    all_balanced = True
    for snap in snapshots:
        total = snap["current_total_wan"]
        occupied = snap["current_occupied_wan"]
        available = snap["available_wan"]
        gap = snap["gap_wan"]
        # total = occupied + available - gap  （超占时 available 已截到 0）
        diff = q2(total - (occupied + available - gap))
        version_sum = q2(sum((v["occupied_wan"] for v in snap["version_views"]), ZERO))
        occ_diff = q2(snap["total_occupied_wan"] - version_sum)
        balanced = diff == ZERO and occ_diff == ZERO
        all_balanced = all_balanced and balanced
        rows.append({
            "pool_id": snap["pool_id"],
            "title": snap["title"],
            "total_wan": str(total),
            "occupied_wan": str(occupied),
            "available_wan": str(available),
            "gap_wan": str(gap),
            "version_occupied_sum": str(version_sum),
            "diff": str(diff),
            "occupied_diff": str(occ_diff),
            "balanced": balanced,
            "note": "当前总额 = 当前占用 + 可用 − 超额缺口；全池占用 = 各版本钉版占用之和",
        })
    return {"accounts": rows, "all_balanced": all_balanced}
