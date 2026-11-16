"""批次流水线：六个阶段顺序执行，任何阶段失败都留证据并可续跑。

三条硬约束：
- 幂等：同一批次键（规则版本 + 来源 + 页面摘要集合）命中已完成批次即复用结果，
  不新增任何行，也不产生第二份清单；
- 原子：一次成功运行的全部行在同一事务里先删后插；失败只写「尝试」记录，不动旧批次；
- 可续跑：链判定之后的失败可复用已解析公告，不重抓页面；采信/解析/质量失败须整体重跑。
"""
from __future__ import annotations

import json
import time

from ... import config, errors, manifest as manifest_mod
from ..m1_ingest import service as m1_service
from ..m2_parse import service as m2_service
from ..m3_quality import reconcile as reconcile_mod
from ..m3_quality import service as m3_service
from ..m4_chain import service as m4_service
from . import resume, state


def _record_dict(record) -> dict:
    return {
        "portal": record.portal, "source_url": record.source_url,
        "row_ref": record.row_ref, "header_count": record.header_count,
        "cell_count": record.cell_count, "fields": record.fields,
        "cells": record.cells, "page_url": record.page_url, "note": record.note,
    }


def _page_dict(page) -> dict:
    return {
        "url": page.url, "portal": page.portal, "kind": page.kind,
        "page_no": page.page_no, "status": page.status,
        "http_status": page.http_status, "encoding": page.encoding,
        "declared_charset": page.declared_charset, "bytes": page.bytes,
        "sha256": page.sha256, "row_count": page.row_count,
        "code": page.code, "note": page.note, "codes": list(page.codes),
    }


def run(*, settings, store, source: str = "all", fetcher=None, hooks=None,
        resume_of: str | None = None, mode: str = "full") -> dict:
    portals = m1_service.select_portals(source)          # 未知来源直接 400
    fetcher = fetcher or m1_service.make_fetcher(settings)
    hooks = hooks or {}
    started_at = state.now_iso()
    clock = time.perf_counter()
    stages: list[dict] = []
    pages: list[dict] = []
    digests: list[str] = []
    key = ""
    input_digest = ""
    batch_id = state.new_batch_id()
    run_id = manifest_mod.new_run_id()
    resume_plan = resume.plan(store, resume_of) if resume_of else None
    reuse_ingest = bool(resume_plan and resume_plan["can_resume"]
                        and "ingest" in resume_plan["reusable_stages"])
    previous_stages = store.load_stages(resume_of) if reuse_ingest else []
    previous = {s["stage"]: s for s in previous_stages}

    def elapsed() -> int:
        return int((time.perf_counter() - clock) * 1000)

    def fire(stage_key: str) -> None:
        fn = hooks.get(stage_key)
        if fn is not None:
            fn()

    def fail(stage_key: str, message: str) -> dict:
        stages.append(state.failed_stage(stage_key, message=message, elapsed_ms=elapsed()))
        note = f"失败阶段：{config.STAGE_LABELS[stage_key]}｜{message}"
        batch = {
            "batch_id": batch_id, "batch_key": key, "run_id": run_id,
            "source": source, "status": "failed", "created_at": started_at,
            "finished_at": state.now_iso(), "input_digest": input_digest,
            "resume_of": resume_of,
            "params": {"source": source, "mode": mode, "resume_of": resume_of},
            "note": note,
        }
        store.save_attempt(batch=batch, pages=pages, stages=stages, note=note)
        return {
            "status": "failed", "batch": batch, "stages": stages,
            "reconciliation": None, "totals": {}, "idempotent_replay": False,
            "error": message, "failed_stage": stage_key, "pages": pages,
            "resume_hint": "采信/解析/质量阶段失败需整体重跑；"
                           "链判定之后的失败可直接续跑复用已解析公告",
        }

    # ---- 阶段 1：采信 ----
    try:
        fire("ingest")
        if reuse_ingest:
            pages = [{k: p[k] for k in
                      ("url", "portal", "kind", "page_no", "status", "http_status", "encoding",
                       "declared_charset", "bytes", "sha256", "row_count", "code", "note")}
                     for p in store.load_pages(resume_of)]
            digests = state.page_digests(pages)
            input_digest = manifest_mod.digest_parts(digests)
            prior = previous.get("ingest", {})
            stages.append(state.stage(
                "ingest", in_count=prior.get("in_count", len(pages)),
                out_count=prior.get("out_count", 0),
                quarantine_count=prior.get("quarantine_count", 0), elapsed_ms=0,
                status="reused", note=f"复用批次 {resume_of} 的采信结果，页面不重抓"))
        else:
            bundle = m1_service.ingest(settings, source=source, fetcher=fetcher)
            pages = [_page_dict(p) for p in bundle.pages]
            digests = bundle.digests
            input_digest = manifest_mod.digest_parts(digests)
            counters = bundle.counters()
            if counters["pages_failed"] > 0:
                raise errors.UpstreamError(
                    f"{counters['pages_failed']} 个页面采信失败，整批不进入解析"
                    f"（避免出半个清单）；失败页面见页面诊断，可续跑重试")
            stages.append(state.stage(
                "ingest", in_count=counters["pages"], out_count=counters["records"],
                quarantine_count=counters["pages_failed"] + counters["pages_skipped"],
                elapsed_ms=elapsed(),
                note=f"页面 {counters['pages']}（成功 {counters['pages_ok']}，"
                     f"跳过 {counters['pages_skipped']}，失败 {counters['pages_failed']}）"
                     f"｜表格行 {counters['records']}"))
    except Exception as exc:  # noqa: BLE001
        return fail("ingest", f"{type(exc).__name__}: {exc}")

    key = state.batch_key(rule_version=settings.rule_version, source=source, digests=digests)

    if mode == "full" and not resume_of and not hooks:
        existing = store.find_done_by_key(key)
        if existing is not None:
            stored = store.load_stages(existing["batch_id"])
            totals = store.load_totals(existing["batch_id"])
            recon = json.loads(totals["reconcile_json"]) if "reconcile_json" in totals else None
            return {
                "status": "done", "batch": existing, "stages": stored,
                "reconciliation": recon, "totals": totals, "idempotent_replay": True,
                "error": None, "failed_stage": None, "pages": pages,
                "replay_of": existing["batch_id"],
                "reconcile_note": "同输入摘要命中已完成批次，复用结果，未新增任何行",
            }

    # ---- 阶段 2：解析 ----
    if reuse_ingest:
        try:
            announcements = store.load_announcements(resume_of)
            accepted = announcements
            prior_parse = previous.get("parse", {})
            prior_quality = previous.get("quality", {})
            parse_rejected = prior_parse.get("quarantine_count", 0)
            quality_rejected = prior_quality.get("quarantine_count", 0)
            # 已解析公告数（含转载合并与质量隔离）在解析阶段 out_count 里留有记录，
            # 续跑时据此反推「转载合并」条数，保证行账在复用路径上依然闭合。
            parsed_total = prior_parse.get("out_count", len(announcements))
            merged = max(0, parsed_total - len(accepted) - quality_rejected)
            rows_seen = prior_parse.get("in_count", len(announcements))
            quarantine_rows = [q for q in store.load_quarantine(resume_of)
                               if q["stage"] in ("parse", "quality")]
            stages.append(state.stage(
                "parse", in_count=rows_seen, out_count=parsed_total,
                quarantine_count=parse_rejected, elapsed_ms=0, status="reused",
                note=f"复用批次 {resume_of} 已解析公告，不重读页面"))
            stages.append(state.stage(
                "quality", in_count=prior_quality.get("in_count", len(announcements)),
                out_count=len(accepted), quarantine_count=quality_rejected,
                elapsed_ms=0, status="reused", note="复用质量判定结果"))
        except Exception as exc:  # noqa: BLE001
            return fail("parse", f"{type(exc).__name__}: {exc}")
    else:
        try:
            fire("parse")
            records = [_record_dict(r) for r in bundle.records]
            parsed = m2_service.parse_records(records)
            stages.append(state.stage(
                "parse", in_count=len(records), out_count=len(parsed.announcements),
                quarantine_count=parsed.rejected_records, elapsed_ms=elapsed(),
                note=f"解析成功 {len(parsed.announcements)} 条，隔离 "
                     f"{parsed.rejected_records} 条，原因明细 {len(parsed.quarantine)} 条"))
            fire("quality")
            quality = m3_service.evaluate(parsed.announcements, settings)
            accepted = quality.accepted
            merged = quality.merged
            quality_rejected = quality.rejected_records
            parse_rejected = parsed.rejected_records
            quarantine_rows = parsed.quarantine + quality.quarantine
            rows_seen = len(records)
            stages.append(state.stage(
                "quality", in_count=len(parsed.announcements), out_count=len(accepted),
                quarantine_count=quality_rejected, elapsed_ms=elapsed(),
                note=f"转载合并 {merged} 条，质量隔离 {quality_rejected} 条"))
        except Exception as exc:  # noqa: BLE001
            stage_key = "quality" if any(s["stage"] == "parse" for s in stages) else "parse"
            return fail(stage_key, f"{type(exc).__name__}: {exc}")

    # ---- 阶段 3：链判定 ----
    try:
        fire("chain")
        chain_result = m4_service.compute(accepted, settings)
        quarantine_rows = quarantine_rows + chain_result.quarantine
        conflict_chains = [c for c in chain_result.chains if c["status"] == "CONFLICT"]
        stages.append(state.stage(
            "chain", in_count=len(accepted), out_count=len(chain_result.chains),
            quarantine_count=len(chain_result.quarantine), elapsed_ms=elapsed(),
            note=f"链 {len(chain_result.chains)} 条，冲突隔离 {len(conflict_chains)} 条"))
    except Exception as exc:  # noqa: BLE001
        return fail("chain", f"{type(exc).__name__}: {exc}")

    # ---- 阶段 4：清单 ----
    chains = chain_result.chains
    listed = [c for c in chains if c["listed"]]
    try:
        fire("list")
        stages.append(state.stage(
            "list", in_count=len(chains), out_count=len(listed),
            quarantine_count=sum(1 for c in chains if c["status"] == "CONFLICT"),
            elapsed_ms=elapsed(),
            note=f"可申报 {len(listed)} 条，已撤销 "
                 f"{sum(1 for c in chains if c['status'] == 'VOIDED')} 条，已过期 "
                 f"{sum(1 for c in chains if c.get('expired'))} 条"))
    except Exception as exc:  # noqa: BLE001
        return fail("list", f"{type(exc).__name__}: {exc}")

    # ---- 阶段 5：对账与导出 ----
    try:
        fire("export")
        parsed_rows = len(accepted) + merged + quality_rejected
        reconciliation = reconcile_mod.build(
            pages=pages, rows_seen=rows_seen, parsed=parsed_rows,
            parse_rejected=parse_rejected, merged=merged,
            quality_rejected=quality_rejected, accepted=accepted, chains=chains,
            settings=settings)
        listed_amount = reconciliation["amounts"]["parts"]["清单合计"]
        stages.append(state.stage(
            "export", in_count=len(listed), out_count=len(quarantine_rows),
            quarantine_count=0, elapsed_ms=elapsed(),
            note=f"四账{'平衡' if reconciliation['all_balanced'] else '不平衡'}，"
                 f"清单合计 {listed_amount} 万元"))
    except Exception as exc:  # noqa: BLE001
        return fail("export", f"{type(exc).__name__}: {exc}")

    finished_at = state.now_iso()
    versions = [v for chain in chains for v in chain["version_rows"]]
    totals = {
        "pages": str(len(pages)),
        "pages_ok": str(sum(1 for p in pages if p["status"] == "ok")),
        "rows": str(rows_seen),
        "parsed": str(len(accepted) + merged + quality_rejected),
        "accepted": str(len(accepted)),
        "merged": str(merged),
        "quarantine_rows": str(len(quarantine_rows)),
        "chains": str(len(chains)),
        "listed": str(len(listed)),
        "amount_total": listed_amount,
        "batch_key": key,
        "input_digest": input_digest,
        "elapsed_ms": str(elapsed()),
        "reconcile_json": json.dumps(reconciliation, ensure_ascii=False),
    }
    batch = {
        "batch_id": batch_id, "batch_key": key, "run_id": run_id, "source": source,
        "status": "done", "created_at": started_at, "finished_at": finished_at,
        "input_digest": input_digest, "resume_of": resume_of,
        "params": {"source": source, "mode": mode, "resume_of": resume_of},
        "note": ("续跑完成" if resume_of else "首次运行"),
    }
    counters = {k: v for k, v in totals.items() if k != "reconcile_json"}
    manifest = state.new_run_manifest(
        run_id=run_id, batch_id=batch_id, batch_key_value=key, settings=settings,
        source=source, input_digest=input_digest, pages=pages, params=batch["params"],
        started_at=started_at, finished_at=finished_at, elapsed_ms=elapsed(),
        stages=stages, counters=counters)
    try:
        store.save_run(batch=batch, pages=pages, announcements=accepted,
                       quarantine=quarantine_rows, chains=chains, versions=versions,
                       stages=stages, totals=totals, manifest=manifest)
    except Exception as exc:  # noqa: BLE001
        return fail("export", f"落库失败：{type(exc).__name__}: {exc}")
    return {
        "status": "done", "batch": batch, "stages": stages,
        "reconciliation": reconciliation, "totals": totals,
        "idempotent_replay": False, "error": None, "failed_stage": None,
        "pages": pages, "quarantine_rows": quarantine_rows, "chains": chains,
        "resume_plan": resume_plan,
    }


def rerun(*, settings, store, batch_id: str) -> dict:
    """幂等重跑：同来源再跑一次，命中批次键则返回原结果、不新增行。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_NOT_FOUND")
    params = json.loads(batch.get("params_json") or "{}")
    result = run(settings=settings, store=store, source=params.get("source", "all"), mode="full")
    result["rerun_of"] = batch_id
    return result


def resume_batch(*, settings, store, batch_id: str, fetcher=None) -> dict:
    plan = resume.plan(store, batch_id)
    if not plan["can_resume"]:
        raise errors.Conflict(plan["note"], code=plan["code"] or "B_RESUME_NO_STATE")
    batch = store.get_batch(batch_id)
    params = json.loads(batch.get("params_json") or "{}")
    result = run(settings=settings, store=store, source=params.get("source", "all"),
                 fetcher=fetcher, resume_of=batch_id, mode="resume")
    result["resume_plan"] = plan
    return result


def history(store, limit: int = 30) -> list[dict]:
    rows = store.list_batches(limit=limit)
    out = []
    for row in rows:
        stages = store.load_stages(row["batch_id"])
        counts = store.count_rows(row["batch_id"])
        out.append({
            **row,
            "stages": stages,
            "stage_status": {s["stage"]: s["status"] for s in stages},
            "failed_stage": state.interrupted_stage(stages),
            "rows": counts,
            "params": json.loads(row.get("params_json") or "{}"),
            "resumable": row["status"] == "failed",
        })
    return out
