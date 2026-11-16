"""M8 编排：事件收件箱 → 断点可续跑的处理 → 版本/回执/快照查询与导出数据。

处理顺序与到达顺序解耦：
- 事件先全部进入 quota_inbox（事实只进不改、不按到达顺序覆盖）；
- 每次 run 只取 status='pending' 的事件，按收件顺序逐条处理并即时落库；
- 中断后已确认的更正/受理已落库，续跑从剩余 pending 继续，不重复占用；
- 内容摘要命中已处理事件即幂等跳过（F_DUP_EVENT）。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, errors, manifest as manifest_mod
from . import events, ledger


def _new_run_id() -> str:
    return manifest_mod.new_run_id(prefix="FQ")


def _digest_seen(store, digest: str) -> bool:
    """摘要是否已落到任一事实表（版本或回执）。"""
    if store.quota_get_receipt_by_digest(digest) is not None:
        return True
    for pool in store.quota_list_pools():
        if any(v["digest"] == digest for v in store.quota_load_versions(pool["pool_id"])):
            return True
    return False


def ingest_events(store, events_in: list[dict]) -> dict:
    """把外部事件规范化后放入收件箱；同摘要事件只保留一条（幂等）。"""
    accepted = []
    duplicates = 0
    invalid = 0
    for raw in events_in:
        normalized = events.normalize_event_or_none(raw)
        if normalized is None:
            invalid += 1
            continue
        digest = events.event_digest(normalized["event_type"], normalized["payload"])
        in_inbox = any(item["digest"] == digest for item in store.quota_inbox_all())
        if _digest_seen(store, digest) or in_inbox:
            duplicates += 1
            continue
        store.quota_inbox_upsert({
            "digest": digest,
            "event_type": normalized["event_type"],
            "payload": normalized["payload"],
            "received_at": normalized["received_at"],
            "status": "pending",
        })
        accepted.append({"digest": digest, "event_type": normalized["event_type"]})
    return {"accepted": len(accepted), "duplicates": duplicates, "invalid": invalid,
            "items": accepted}


def _log(store, run_id: str, digest: str, event_type: str, action: str, code: str,
         detail: str = "") -> None:
    store.quota_add_event({
        "run_id": run_id, "digest": digest, "event_type": event_type,
        "action": action, "code": code, "detail": detail,
        "created_at": events.now_iso(),
    })


def _apply_version(store, run_id: str, digest: str, event: dict) -> dict:
    facts = event["payload"]
    pool_id = facts["pool_id"]
    pool = store.quota_get_pool(pool_id)
    existing = store.quota_load_versions(pool_id) if pool else []
    problem = ledger.validate_version(pool, existing, event)
    received_at = event["received_at"]
    if problem is not None:
        code, detail = problem
        if code == "F_DUP_EVENT":
            store.quota_save_version({
                **facts, "received_at": received_at, "digest": digest,
                "status": "applied", "note": detail})
            _log(store, run_id, digest, "quota_version", "skip", code, detail)
            return {"action": "skip", "code": code}
        # 切换失败：失败版本留 rejected 证据，池指针不动（保留上一完整额度版本）
        store.quota_upsert_pool({
            "pool_id": pool_id,
            "title": (pool or {}).get("title", pool_id),
            "status": (pool or {}).get("status", "ACTIVE"),
            "current_version_no": (pool or {}).get("current_version_no"),
            "created_at": (pool or {}).get("created_at", received_at),
        })
        store.quota_save_version({
            **facts, "received_at": received_at, "digest": digest,
            "status": "rejected", "reject_code": code, "reject_detail": detail,
            "applied_at": None})
        _log(store, run_id, digest, "quota_version", "reject", code, detail)
        return {"action": "reject", "code": code, "detail": detail}

    # 校验通过：建/更新池 + 落 applied 版本 + 移动当前指针，一次请求内顺序落库
    if pool is None:
        store.quota_upsert_pool({
            "pool_id": pool_id, "title": pool_id, "status": "ACTIVE",
            "current_version_no": None, "created_at": received_at})
        pool = store.quota_get_pool(pool_id)
    new_status = "VOIDED" if facts["kind"] == "撤销" else pool.get("status", "ACTIVE")
    store.quota_save_version({
        **facts, "received_at": received_at, "digest": digest,
        "status": "applied", "applied_at": events.now_iso()})
    store.quota_set_pool_pointer(pool_id, facts["version_no"], new_status)
    _log(store, run_id, digest, "quota_version", "apply", "F_VERSION_APPLIED",
         f"{facts['kind']} v{facts['version_no']} 总额 {facts['total_wan']} 万元")
    return {"action": "apply", "code": "F_VERSION_APPLIED"}


def _apply_receipt(store, run_id: str, digest: str, event: dict) -> dict:
    facts = event["payload"]
    pool_id = facts["pool_id"]
    received_at = event["received_at"]
    pool = store.quota_get_pool(pool_id)
    # 池不存在：挂起，等首发版本到达后续跑自动匹配
    if pool is None:
        store.quota_upsert_pool({
            "pool_id": pool_id, "title": pool_id, "status": "ACTIVE",
            "current_version_no": None, "created_at": received_at})
        pool = store.quota_get_pool(pool_id)
    if pool.get("status") == "VOIDED":
        store.quota_save_receipt({
            **facts, "received_at": received_at, "digest": digest,
            "status": "rejected", "pending_reason": "F_RECEIPT_VOID_POOL"})
        _log(store, run_id, digest, "quota_receipt", "reject", "F_RECEIPT_VOID_POOL",
             f"{facts['receipt_no']} 在资金池撤销后到达")
        return {"action": "reject", "code": "F_RECEIPT_VOID_POOL"}

    target = ledger.pegging_version(store.quota_load_versions(pool_id),
                                    facts["accepted_at"])
    if target is None:
        store.quota_save_receipt({
            **facts, "received_at": received_at, "digest": digest,
            "status": "pending", "pending_reason": "F_RECEIPT_PENDING"})
        _log(store, run_id, digest, "quota_receipt", "pending", "F_RECEIPT_PENDING",
             f"{facts['receipt_no']} 受理时刻 {facts['accepted_at']} 早于首发")
        return {"action": "pending", "code": "F_RECEIPT_PENDING"}

    amount = Decimal(str(facts["amount_wan"]))
    pegged = [r for r in store.quota_load_receipts(pool_id)
              if r["status"] == "applied"
              and r.get("pegged_version_no") == target["version_no"]]
    occupied = sum((r["amount_wan"] for r in pegged), Decimal("0.00"))
    overshoot = (occupied + amount) > target["total_wan"]
    code = "F_RECEIPT_OVERSHOOT" if overshoot else "F_RECEIPT_APPLIED"
    store.quota_save_receipt({
        **facts, "received_at": received_at, "digest": digest,
        "status": "applied", "pegged_version_no": target["version_no"],
        "overshoot": overshoot,
        "note": f"钉版 v{target['version_no']}" + ("｜受理即超占" if overshoot else "")})
    _log(store, run_id, digest, "quota_receipt", "apply", code,
         f"{facts['receipt_no']} 钉版 v{target['version_no']}，占用 {facts['amount_wan']} 万元")
    return {"action": "apply", "code": code, "pegged_version_no": target["version_no"]}


def _resolve_pending(store, run_id: str, pool_id: str) -> int:
    """版本切换后把挂起回执再匹配一次；按受理时刻钉版，重跑只动仍 pending 的行。"""
    resolved = 0
    for receipt in store.quota_pending_receipts(pool_id):
        target = ledger.pegging_version(store.quota_load_versions(pool_id),
                                        receipt["accepted_at"])
        if target is None:
            continue
        amount = receipt["amount_wan"]
        pegged = [r for r in store.quota_load_receipts(pool_id)
                  if r["status"] == "applied"
                  and r.get("pegged_version_no") == target["version_no"]]
        occupied = sum((r["amount_wan"] for r in pegged), Decimal("0.00"))
        overshoot = (occupied + amount) > target["total_wan"]
        store.quota_save_receipt({
            **receipt, "status": "applied", "pegged_version_no": target["version_no"],
            "overshoot": overshoot,
            "pending_reason": "",
            "note": f"续跑匹配钉版 v{target['version_no']}"
                    + ("｜受理即超占" if overshoot else "")})
        _log(store, run_id, receipt["digest"], "quota_receipt", "resolve",
             "F_RECEIPT_RESOLVED",
             f"{receipt['receipt_no']} 钉版 v{target['version_no']}")
        store.quota_inbox_mark(receipt["digest"], "done", run_id, "挂起已匹配")
        resolved += 1
    return resolved


def _sweep_all_pending(store, run_id: str) -> int:
    """运行末尾全局扫挂起：所有池各匹配一次，并把仍无版本可钉的收件箱行保持 pending。"""
    resolved = 0
    for pool in store.quota_list_pools():
        resolved += _resolve_pending(store, run_id, pool["pool_id"])
    return resolved


def process(store, *, hooks=None, resume: bool = False) -> dict:
    """处理收件箱 pending 事件；hooks 可在指定 digest 处理前注入中断（测试用）。"""
    hooks = hooks or {}
    run_id = _new_run_id()
    pending = store.quota_inbox_pending()
    input_digest = manifest_mod.digest_parts(sorted(e["digest"] for e in pending))
    started = events.now_iso()
    counters = {"processed": 0, "skipped": 0, "versions_applied": 0,
                "versions_rejected": 0, "receipts_applied": 0, "receipts_pending": 0,
                "resolved": 0}
    if not pending:
        # 没有待处理事实：不写运行流水、不产生留痕噪音，直接给空结果
        return {"run_id": None, "status": "nothing_to_do", "interrupted": False,
                "pending_left": 0, **counters, "note": "收件箱没有 pending 事件"}
    store.quota_save_run({
        "run_id": run_id, "status": "running", "started_at": started,
        "input_digest": input_digest, "note": "续跑" if resume else "全新处理"})

    interrupted = False
    last_digest = None
    fail_index = hooks.get("fail_at_index")
    for index, event in enumerate(pending):
        if fail_index is not None and index == fail_index:
            interrupted = True
            break
        if hooks.get("fail_on_digest") == event["digest"]:
            interrupted = True
            break
        last_digest = event["digest"]
        digest = event["digest"]
        # 幂等：摘要已落到版本/回执表则只标记跳过，不产生第二条占用
        if _digest_seen(store, digest):
            store.quota_inbox_mark(digest, "done", run_id, "幂等跳过")
            _log(store, run_id, digest, event["event_type"], "skip", "F_DUP_EVENT")
            counters["skipped"] += 1
            continue
        if event["event_type"] == "quota_version":
            outcome = _apply_version(store, run_id, digest, event)
            if outcome["action"] == "apply":
                counters["versions_applied"] += 1
            elif outcome["action"] == "reject":
                counters["versions_rejected"] += 1
            else:
                counters["skipped"] += 1
        else:
            outcome = _apply_receipt(store, run_id, digest, event)
            if outcome["action"] == "apply":
                counters["receipts_applied"] += 1
            elif outcome["action"] == "pending":
                counters["receipts_pending"] += 1
            else:
                counters["versions_rejected"] += 1
        # 挂起回执的收件箱行保持 pending；末尾全局扫挂起若匹配成功会转 done，
        # 仍无版本可钉则继续 pending，等下一次续跑，不丢事实。
        if outcome["action"] != "pending":
            store.quota_inbox_mark(digest, "done", run_id, outcome["code"])
        counters["processed"] += 1
        store.quota_save_run({
            "run_id": run_id, "status": "running", "started_at": started,
            "cursor_digest": digest, "input_digest": input_digest, **counters})

    # 运行末尾全局扫挂起：晚到首发/更正后把仍 pending 的事实补齐钉版。
    # 只动 status='pending' 的回执，已受理回执不受影响，重跑不产生第二条占用。
    # 中断时不扫：已确认事实已落库，留给续跑从剩余 pending 继续。
    if not interrupted:
        counters["resolved"] += _sweep_all_pending(store, run_id)

    status = "failed" if interrupted else "done"
    note = "中断：已确认更正/受理已落库，续跑只处理剩余 pending" if interrupted else "处理完成"
    if interrupted:
        _log(store, run_id, last_digest or "", "system", "interrupt",
             "F_RUN_INTERRUPTED", note)
    pending_left = len(store.quota_inbox_pending())
    store.quota_save_run({
        "run_id": run_id, "status": status, "started_at": started,
        "finished_at": events.now_iso(), "cursor_digest": last_digest,
        "input_digest": input_digest, "note": note, **counters})
    return {"run_id": run_id, "status": status, "interrupted": interrupted,
            "pending_left": pending_left, **counters}


def run(store, events_in: list[dict] | None = None, *, hooks=None) -> dict:
    """一步式：收件 + 处理；测试与 API 共用。

    没有新事实（全部重复/非法）且收件箱无剩余 pending 时不产生运行流水，
    保证幂等重跑不留第二条处理记录。
    """
    ingest = ingest_events(store, events_in or []) if events_in else None
    if ingest is not None and ingest["accepted"] == 0:
        result = {"run_id": None, "status": "nothing_to_do", "interrupted": False,
                  "pending_left": len(store.quota_inbox_pending()),
                  "processed": 0, "skipped": 0, "versions_applied": 0,
                  "versions_rejected": 0, "receipts_applied": 0,
                  "receipts_pending": 0, "resolved": 0,
                  "note": "没有新事件进入收件箱（全部重复或非法）"}
    elif not store.quota_inbox_pending():
        result = {"run_id": None, "status": "nothing_to_do", "interrupted": False,
                  "pending_left": 0, "processed": 0, "skipped": 0,
                  "versions_applied": 0, "versions_rejected": 0,
                  "receipts_applied": 0, "receipts_pending": 0, "resolved": 0,
                  "note": "没有新事件，收件箱也没有 pending"}
    else:
        result = process(store, hooks=hooks)
    if ingest is not None:
        result["ingest"] = ingest
    return result


def _has_processable_inbox(store) -> bool:
    """收件箱里是否还有可处理事件。

    挂起回执若在当前所有版本下仍钉不到版（受理时刻早于首发），空转处理不会改变
    任何事实——这类纯挂起收件箱不算可处理，等新版本事件进入后再随运行重试。
    """
    for event in store.quota_inbox_pending():
        if event["event_type"] == "quota_version":
            return True
        receipt = store.quota_get_receipt_by_digest(event["digest"])
        if receipt is None or receipt.get("status") != "pending":
            return True
        pool_id = event["payload"]["pool_id"]
        if ledger.pegging_version(store.quota_load_versions(pool_id),
                                  event["payload"]["accepted_at"]) is not None:
            return True
    return False


def resume(store, *, hooks=None) -> dict:
    """续跑：只处理收件箱剩余 pending，已处理事件不重复占用。"""
    if not _has_processable_inbox(store):
        return {"run_id": None, "status": "nothing_to_resume", "interrupted": False,
                "pending_left": len(store.quota_inbox_pending()), "processed": 0,
                "skipped": 0, "versions_applied": 0, "versions_rejected": 0,
                "receipts_applied": 0, "receipts_pending": 0, "resolved": 0,
                "note": "没有可推进的 pending 事件（挂起回执在等更早的首发版本）"}
    return process(store, hooks=hooks, resume=True)


# ---------- 查询 ----------

def pool_view(store, pool_id: str) -> dict:
    pool = store.quota_get_pool(pool_id)
    if pool is None:
        raise errors.NotFound(f"资金池 {pool_id} 不存在", code="E_NOT_FOUND")
    versions = store.quota_load_versions(pool_id)
    receipts = store.quota_load_receipts(pool_id)
    snap = ledger.pool_snapshot(pool, versions, receipts)
    digests = {v["digest"] for v in versions} | {r["digest"] for r in receipts}
    snap["events"] = [e for e in store.quota_load_events() if e["digest"] in digests]
    return snap


def dashboard(store) -> dict:
    snapshots = []
    for pool in store.quota_list_pools():
        versions = store.quota_load_versions(pool["pool_id"])
        receipts = store.quota_load_receipts(pool["pool_id"])
        snapshots.append(ledger.pool_snapshot(pool, versions, receipts))
    return {"pools": snapshots, "reconcile": ledger.reconcile(snapshots),
            "runs": store.quota_list_runs()}


def precheck(store, pool_id: str, amount_wan) -> dict:
    """新申报预检：按更正后当前总额看可用余额（旧受理不重算）。"""
    pool = store.quota_get_pool(pool_id)
    if pool is None:
        raise errors.NotFound(f"资金池 {pool_id} 不存在", code="E_NOT_FOUND")
    if pool["status"] == "VOIDED":
        raise errors.Conflict("资金池已撤销，不再受理新申报", code="F_RECEIPT_VOID_POOL")
    versions = store.quota_load_versions(pool_id)
    receipts = store.quota_load_receipts(pool_id)
    snap = ledger.pool_snapshot(pool, versions, receipts)
    amount = events.parse_amount(amount_wan, allow_zero=False)
    available = snap["available_wan"]
    return {
        "pool_id": pool_id,
        "current_version_no": snap["current_version_no"],
        "current_total_wan": str(snap["current_total_wan"]),
        "current_occupied_wan": str(snap["current_occupied_wan"]),
        "available_wan": str(available),
        "ask_wan": str(amount.quantize(config.DECIMAL_QUANT)),
        "fit": amount <= available,
        "projected_gap_wan": str(
            (amount - available).quantize(config.DECIMAL_QUANT)
            if amount > available else Decimal("0.00")),
    }
