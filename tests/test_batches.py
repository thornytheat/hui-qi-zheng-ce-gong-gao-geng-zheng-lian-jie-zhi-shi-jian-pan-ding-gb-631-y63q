"""M5 批次与续跑：幂等重跑、批次键、失败续跑、阶段计数、运行清单。"""
from __future__ import annotations

import pytest

from app import errors
from app.modules.m1_ingest import reader
from app.modules.m3_quality import reconcile as reconcile_mod
from app.modules.m5_batches import resume as resume_mod
from app.modules.m5_batches import service as batch_service, state
from fixtures import expected as X


def test_idempotent_rerun_adds_no_rows(settings, store, run):
    """不变量（幂等）：同输入第二次运行命中批次键，公告与页面行数完全不变。"""
    before = store.count_rows(run["batch"]["batch_id"])
    second = batch_service.run(settings=settings, store=store, source="all")
    assert second["idempotent_replay"] is True
    assert second["batch"]["batch_id"] == run["batch"]["batch_id"]
    assert store.count_rows(second["batch"]["batch_id"]) == before
    done = [b for b in store.list_batches() if b["status"] == "done"]
    assert len(done) == 1


def test_batch_key_stable_for_same_input(settings, run, store):
    """批次键 = 规则版本 + 来源 + 页面摘要集合；同一输入必然同键。"""
    digests = state.page_digests(run["pages"])
    key1 = state.batch_key(rule_version=settings.rule_version, source="all", digests=digests)
    key2 = state.batch_key(rule_version=settings.rule_version, source="all",
                           digests=list(reversed(digests)))
    assert key1 == key2 == run["batch"]["batch_key"]


def test_ingest_failure_then_resume(settings, store, fetcher):
    """失败与续跑：某页返回 500 时批次失败并留证据；恢复后续跑成功。"""
    bad = reader.FailingFetcher(fetcher, {"http://sample-site/b/list_p1.html"})
    failed = batch_service.run(settings=settings, store=store, source="all", fetcher=bad)
    assert failed["status"] == "failed"
    assert failed["failed_stage"] == "chain" or failed["failed_stage"] == "ingest"
    plan = resume_mod.plan(store, failed["batch"]["batch_id"])
    assert plan["can_resume"] is True
    resumed = batch_service.run(settings=settings, store=store, source="all",
                                fetcher=fetcher, resume_of=failed["batch"]["batch_id"])
    assert resumed["status"] == "done"
    assert resumed["batch"]["resume_of"] == failed["batch"]["batch_id"]


def test_chain_stage_failure_reuses_pages(settings, store, run):
    """断点续跑：链判定阶段失败后重跑，采信与解析阶段标记为 reused，页面不重抓。"""
    def boom():
        raise RuntimeError("注入的链判定失败")

    failed = batch_service.run(settings=settings, store=store, source="all", hooks={"chain": boom})
    assert failed["status"] == "failed"
    assert failed["failed_stage"] == "chain"
    plan = resume_mod.plan(store, failed["batch"]["batch_id"])
    assert plan["can_resume"] is True
    assert plan["reused_pages"] == X.PAGES_OK
    resumed = batch_service.run(settings=settings, store=store, source="all",
                                resume_of=failed["batch"]["batch_id"])
    assert resumed["status"] == "done"
    statuses = {s["stage"]: s["status"] for s in resumed["stages"]}
    assert statuses["ingest"] == "reused"
    assert statuses["parse"] == "reused"
    assert statuses["chain"] == "ok"
    assert resumed["reconciliation"]["all_balanced"] is True


def test_resume_rejects_done_batch(settings, store, run):
    """续跑只对失败批次开放；已完成批次续跑返回冲突状态码语义。"""
    plan = resume_mod.plan(store, run["batch"]["batch_id"])
    assert plan["can_resume"] is False
    assert plan["code"] == "B_RESUME_NO_STATE"
    with pytest.raises(errors.Conflict):
        batch_service.resume_batch(settings=settings, store=store,
                                   batch_id=run["batch"]["batch_id"])


def test_stage_sequence_and_counters(run):
    """阶段化：六个阶段按固定顺序产出，计数非负且入 ≥ 出。"""
    stages = run["stages"]
    assert [s["stage"] for s in stages] == ["ingest", "parse", "quality", "chain",
                                            "list", "export"]
    for stage in stages:
        assert stage["in_count"] >= 0 and stage["out_count"] >= 0
        assert stage["status"] == "ok"
    assert stages[1]["in_count"] == X.ROWS_SEEN


def test_manifest_carries_run_id_and_digests(store, run, settings):
    """运行清单：run_id、批次键、输入页面 sha256、规则版本、参数都可复算。"""
    manifest = store.load_manifest(run["batch"]["batch_id"])
    assert manifest["run_id"].startswith("NC-")
    assert len(manifest["input_digest"]) == 64
    assert len(manifest["inputs"]) == X.PAGES_TOTAL
    assert all(len(item["sha256"]) == 64 for item in manifest["inputs"])
    assert manifest["rule_version"] == settings.rule_version
    counters = manifest["counters"]
    assert counters["rows"] == str(X.ROWS_SEEN)


def test_unknown_source_is_rejected(settings, store):
    """失败输入：未知来源返回 400 语义错误，不产生半批数据。"""
    with pytest.raises(errors.BadRequest):
        batch_service.run(settings=settings, store=store, source="portal_x")
    assert store.list_batches() == []


def test_failed_attempt_does_not_destroy_previous_batch(settings, store, run):
    """原子性：新的一次失败尝试不会删掉上一次成功批次的数据。"""
    def boom():
        raise RuntimeError("注入失败")

    before = store.count_rows(run["batch"]["batch_id"])
    batch_service.run(settings=settings, store=store, source="all", hooks={"chain": boom})
    after = store.count_rows(run["batch"]["batch_id"])
    assert after == before


def test_history_reports_stage_status(settings, store, run):
    """批次历史：每个批次带阶段状态、失败阶段与行数统计。"""
    def boom():
        raise RuntimeError("注入失败")

    batch_service.run(settings=settings, store=store, source="portal_a", hooks={"quality": boom})
    rows = batch_service.history(store)
    assert rows[0]["status"] == "failed"
    assert rows[0]["failed_stage"] in ("parse", "quality")
    assert any(r["status"] == "done" for r in rows)
    assert rows[0]["resumable"] is True
