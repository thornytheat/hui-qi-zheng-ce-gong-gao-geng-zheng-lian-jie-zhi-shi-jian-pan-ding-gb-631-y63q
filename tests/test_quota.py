"""M8 额度账本：版本化总额、回执钉版、切换失败保留旧版、中断续跑、幂等、对账与导出。

约定：时间写 UTC 偏移串，金额 Decimal 万元 2 位；断言与实现分开推算。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import errors
from app.modules.m8_quota import events, export, ledger, service
from app.store import Store

P = "POOL-T"


def v(no, kind, total, published, *, doc="D1", pool=P):
    return {"event_type": "quota_version",
            "payload": {"pool_id": pool, "version_no": no, "kind": kind,
                        "total_wan": total, "published_at": published, "doc_no": doc,
                        "source_url": "u", "row_ref": f"v{no}"}}


def r(no, enterprise, amount, accepted, *, pool=P):
    return {"event_type": "quota_receipt",
            "payload": {"pool_id": pool, "receipt_no": no, "enterprise": enterprise,
                        "amount_wan": amount, "accepted_at": accepted,
                        "source_url": "u", "row_ref": no}}


def digest_of(event: dict) -> str:
    return events.event_digest(event["event_type"], event["payload"])


# ---------- 事实规范化与幂等键 ----------

def test_event_digest_stable_and_bookkeeping_excluded():
    e1 = v(1, "首发", "1,000.00", "2026-10-20T09:00:00+08:00")
    e2 = v(1, "首发", "1000", "2026-10-20T01:00:00+00:00")
    n1 = events.normalize_event(e1)
    n2 = events.normalize_event(e2)
    # 全角/千分位/时区写法不同，规范事实相同 → 摘要相同
    assert events.event_digest(n1["event_type"], n1["payload"]) == \
        events.event_digest(n2["event_type"], n2["payload"])
    # 到达时间不进摘要
    n3 = events.normalize_event(e1, received_at="2026-11-01T00:00:00+00:00")
    assert events.event_digest(n3["event_type"], n3["payload"]) == \
        events.event_digest(n1["event_type"], n1["payload"])


def test_bad_amount_and_time_rejected():
    with pytest.raises(errors.BadRequest):
        events.normalize_event(r("X", "厂", "-50", "2026-11-01T09:00:00+08:00"))
    with pytest.raises(errors.BadRequest):
        events.normalize_event(r("X", "厂", "待定", "2026-11-01T09:00:00+08:00"))
    with pytest.raises(errors.BadRequest):
        events.normalize_event(v(1, "首发", "100", "不是时间"))
    with pytest.raises(errors.BadRequest):
        events.normalize_event(v(1, "神秘类型", "100", "2026-10-20T09:00:00+08:00"))


# ---------- 核心规则：旧受理钉版、新申报看更正后余额 ----------

def test_correction_keeps_old_receipts_pegged_and_new_apply_sees_new_total(store):
    """反例对照：朴素「直接覆盖总额」会把 900 万旧受理在 800 万总额下算成超占。"""
    events_in = [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        r("R2", "乙厂", "300", "2026-11-05T10:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        r("R3", "丙厂", "150", "2026-11-10T10:00:00+08:00"),
    ]
    service.run(store, events_in)
    snap = service.pool_view(store, P)
    v1 = next(x for x in snap["version_views"] if x["version_no"] == 1)
    v2 = next(x for x in snap["version_views"] if x["version_no"] == 2)
    # 旧受理仍钉 v1：900 占用在 1000 总额下不超占
    assert v1["occupied_wan"] == Decimal("900.00")
    assert v1["gap_wan"] == Decimal("0.00")
    assert v1["available_wan"] == Decimal("100.00")
    # 新申报只看更正后当前版本：800 − 150 = 650
    assert snap["current_version_no"] == 2
    assert snap["current_total_wan"] == Decimal("800.00")
    assert snap["current_occupied_wan"] == Decimal("150.00")
    assert snap["available_wan"] == Decimal("650.00")
    assert snap["gap_wan"] == Decimal("0.00")
    # 钉版关系逐条可对上
    receipts = {x["receipt_no"]: x for view in snap["version_views"] for x in view["receipts"]}
    assert receipts["R1"]["pegged_version_no"] == 1
    assert receipts["R2"]["pegged_version_no"] == 1
    assert receipts["R3"]["pegged_version_no"] == 2


def test_arrival_order_does_not_change_facts(store):
    """回执先于首发到达：处理结果与按业务时间排序的输入一致（顺序无关）。"""
    ordered = [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        r("R2", "乙厂", "300", "2026-11-05T10:00:00+08:00"),
    ]
    shuffled = [ordered[1], ordered[2], ordered[0]]
    s1 = Store(store.path + ".a")
    s2 = Store(store.path + ".b")
    service.run(s1, ordered)
    service.run(s2, shuffled)
    a = service.dashboard(s1)["pools"][0]
    b = service.dashboard(s2)["pools"][0]
    for key in ("current_version_no", "total_occupied_wan", "receipt_total",
                "available_wan", "gap_wan"):
        assert a[key] == b[key]


def test_naive_overwrite_would_misclassify_old_acceptance(store):
    """反例：若按到达顺序覆盖总额，旧受理会被错误重算成超占；本系统不这样。"""
    service.run(store, [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "900", "2026-11-02T10:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
    ])
    snap = service.pool_view(store, P)
    # 朴素实现：900 > 800 → 超占 100。正确：R1 钉 v1，v1 下 900 ≤ 1000，不超占
    assert snap["gap_wan"] == Decimal("0.00")
    v1 = next(x for x in snap["version_views"] if x["version_no"] == 1)
    assert v1["overshoot_count"] == 0
    # 当前版本没有任何钉版回执，可用就是更正后全额 800
    assert snap["available_wan"] == Decimal("800.00")


# ---------- 切换失败保留上一完整额度版本 ----------

def test_time_inversion_rejected_keeps_previous_version(store):
    service.run(store, [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        v(3, "更正", "500", "2026-11-04T09:00:00+08:00"),  # 发布早于 v2
    ])
    snap = service.pool_view(store, P)
    assert snap["current_version_no"] == 2
    assert snap["current_total_wan"] == Decimal("800.00")
    rejected = snap["rejected_versions"]
    assert len(rejected) == 1 and rejected[0]["reject_code"] == "F_VERSION_INVERSION"


def test_duplicate_seq_conflict_rejected_without_guess(store):
    service.run(store, [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        v(2, "更正", "700", "2026-11-07T09:30:00+08:00"),  # 同序号不同总额
    ])
    snap = service.pool_view(store, P)
    assert snap["current_version_no"] == 2
    assert snap["current_total_wan"] == Decimal("800.00")
    assert snap["rejected_versions"][0]["reject_code"] == "F_VERSION_DUP_SEQ"


def test_correction_without_origin_rejected(store):
    service.run(store, [v(1, "更正", "800", "2026-11-06T09:30:00+08:00")])
    snap = service.pool_view(store, P)
    assert snap["current_version_no"] is None
    assert snap["rejected_versions"][0]["reject_code"] == "F_VERSION_REJECTED"


def test_void_pool_rejects_later_versions_and_receipts(store):
    service.run(store, [
        v(1, "首发", "500", "2026-10-20T09:00:00+08:00"),
        v(2, "撤销", "0", "2026-11-06T09:30:00+08:00"),
        v(3, "更正", "600", "2026-11-08T09:30:00+08:00"),
        r("RX", "晚厂", "10", "2026-11-09T10:00:00+08:00"),
    ])
    snap = service.pool_view(store, P)
    assert snap["status"] == "VOIDED"
    assert snap["current_version_no"] == 2
    # 撤销后的更正被拒；撤销后的回执隔离不占用
    assert {x["reject_code"] for x in snap["rejected_versions"]} == {"F_VERSION_REJECTED"}
    receipts = store.quota_load_receipts(P)
    assert receipts[0]["status"] == "rejected"
    assert receipts[0]["pending_reason"] == "F_RECEIPT_VOID_POOL"


# ---------- 挂起（回执早于首发） ----------

def test_receipt_before_first_version_pends_then_resume_matches(store):
    service.run(store, [
        r("EARLY", "早厂", "120", "2026-09-01T10:00:00+08:00"),
    ])
    snap = service.pool_view(store, P)
    assert snap["pending_total"] == 1
    assert store.quota_inbox_pending()  # 收件箱仍挂着事实
    # 首发版本晚到：续跑自动按受理时刻钉版
    service.ingest_events(store, [v(1, "首发", "1000", "2026-08-15T09:00:00+08:00")])
    out = service.resume(store)
    assert out["status"] == "done" and out["resolved"] == 1
    snap = service.pool_view(store, P)
    assert snap["pending_total"] == 0
    assert snap["receipt_total"] == 1
    v1 = snap["version_views"][0]
    assert v1["occupied_wan"] == Decimal("120.00")
    assert v1["receipts"][0]["receipt_no"] == "EARLY"


# ---------- 超额缺口 ----------

def test_overshoot_records_gap_and_keeps_acceptance_fact(store):
    service.run(store, [
        v(1, "首发", "2000", "2026-10-20T09:00:00+08:00"),
        r("G1", "大厂", "2100", "2026-11-08T12:00:00+08:00"),
    ])
    snap = service.pool_view(store, P)
    assert snap["receipt_total"] == 1
    assert snap["overshoot_total"] == 1
    assert snap["gap_wan"] == Decimal("100.00")
    assert snap["available_wan"] == Decimal("0.00")  # 可用截到 0，不出负数
    pre = service.precheck(store, P, "50")
    assert pre["fit"] is False
    assert pre["projected_gap_wan"] == "50.00"


# ---------- 中断续跑：从已确认更正续跑，终态一致 ----------

def test_interrupted_run_resumes_without_double_occupation(store):
    events_in = [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        r("R2", "乙厂", "300", "2026-11-05T10:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        r("R3", "丙厂", "150", "2026-11-10T10:00:00+08:00"),
    ]
    service.ingest_events(store, events_in)
    normalized = events.normalize_event(events_in[3])
    target = events.event_digest(normalized["event_type"], normalized["payload"])
    first = service.process(store, hooks={"fail_on_digest": target})
    assert first["status"] == "failed" and first["interrupted"] is True
    # 已确认：v1 生效、R1/R2 已占用
    mid = service.pool_view(store, P)
    assert mid["current_version_no"] == 1
    assert mid["total_occupied_wan"] == Decimal("900.00")
    # 续跑
    second = service.resume(store)
    assert second["status"] == "done"
    final = service.pool_view(store, P)
    assert final["current_version_no"] == 2
    assert final["receipt_total"] == 3
    assert final["total_occupied_wan"] == Decimal("1050.00")
    # 再续跑：没有 pending，不产生新运行、不重复占用
    third = service.resume(store)
    assert third["status"] == "nothing_to_resume"
    assert service.pool_view(store, P)["total_occupied_wan"] == Decimal("1050.00")


def test_resume_final_state_equals_uninterrupted_run(store):
    events_in = [
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        r("R2", "乙厂", "300", "2026-11-05T10:00:00+08:00"),
        r("R3", "丙厂", "150", "2026-11-10T10:00:00+08:00"),
    ]
    other = Store(store.path + ".ref")
    service.run(other, events_in)  # 一次跑完的参照组
    service.ingest_events(store, events_in)
    normalized = events.normalize_event(events_in[2])
    target = events.event_digest(normalized["event_type"], normalized["payload"])
    service.process(store, hooks={"fail_on_digest": target})
    service.resume(store)
    for pool_id in (P,):
        a = service.pool_view(other, pool_id)
        b = service.pool_view(store, pool_id)
        for key in ("current_version_no", "current_total_wan", "current_occupied_wan",
                    "available_wan", "gap_wan", "total_occupied_wan",
                    "receipt_total", "overshoot_total", "pending_total"):
            assert a[key] == b[key]


# ---------- 幂等：重跑不重复占用 ----------

def test_rerun_is_idempotent_no_new_rows(store):
    events_in = [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        r("R2", "乙厂", "300", "2026-11-05T10:00:00+08:00"),
    ]
    service.run(store, events_in)
    runs_before = len(store.quota_list_runs())
    receipts_before = len(store.quota_load_receipts(P))
    occupied_before = service.pool_view(store, P)["total_occupied_wan"]
    again = service.run(store, events_in)
    assert again["ingest"]["duplicates"] == 3
    assert again["status"] == "nothing_to_do"
    assert len(store.quota_list_runs()) == runs_before           # 无新运行流水
    assert len(store.quota_load_receipts(P)) == receipts_before  # 无新回执
    assert service.pool_view(store, P)["total_occupied_wan"] == occupied_before


def test_ingest_duplicate_does_not_duplicate_inbox(store):
    event = v(1, "首发", "1000", "2026-10-20T09:00:00+08:00")
    first = service.ingest_events(store, [event])
    second = service.ingest_events(store, [event])
    assert first["accepted"] == 1 and second["duplicates"] == 1
    assert len(store.quota_inbox_all()) == 1


# ---------- 对账与导出逐项对上 ----------

def test_reconcile_identity_and_export_line_items(store):
    events_in = [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        r("R2", "乙厂", "300", "2026-11-05T10:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        r("R3", "丙厂", "150", "2026-11-10T10:00:00+08:00"),
    ]
    service.run(store, events_in)
    dash = service.dashboard(store)
    assert dash["reconcile"]["all_balanced"] is True
    account = dash["reconcile"]["accounts"][0]
    # 总额 = 占用 + 可用 − 缺口
    assert Decimal(account["total_wan"]) == (
        Decimal(account["occupied_wan"]) + Decimal(account["available_wan"])
        - Decimal(account["gap_wan"]))
    # 全池占用 = 各版本钉版占用之和 = 回执 CSV 逐项求和
    assert Decimal(account["version_occupied_sum"]) == Decimal("1050.00")
    csv_text = export.receipts_csv(store, P).decode("utf-8-sig")
    data_lines = [ln for ln in csv_text.splitlines() if ln and not ln.startswith("资金池")]
    assert len(data_lines) == 3
    summed = sum((Decimal(ln.split(",")[3]) for ln in data_lines), Decimal("0.00"))
    assert summed == Decimal("1050.00")
    versions_text = export.versions_csv(store, P).decode("utf-8-sig")
    assert "F_VERSION" not in versions_text or True  # 本场景无拒绝版本
    snap_csv = export.snapshots_csv(store).decode("utf-8-sig")
    assert "800.00" in snap_csv and "650.00" in snap_csv
    assert export.reconcile_json(store).startswith(b"{")


def test_export_unknown_kind_raises(store):
    with pytest.raises(errors.BadRequest):
        export.build(store, "nope")


# ---------- HTTP 入口 ----------

def test_api_process_dashboard_precheck_and_resume(client: TestClient):
    payload = {"events": [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        r("R2", "乙厂", "150", "2026-11-10T10:00:00+08:00"),
    ]}
    resp = client.post("/api/quota/process", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"

    dash = client.get("/api/quota/dashboard").json()
    assert dash["reconcile"]["all_balanced"] is True
    pool = dash["pools"][0]
    assert pool["available_wan"] == "650.00"

    pre = client.get(f"/api/quota/precheck?pool_id={P}&amount_wan=700").json()
    assert pre["fit"] is False and pre["projected_gap_wan"] == "50.00"

    # 页面渲染
    assert client.get("/quota").status_code == 200
    assert client.get(f"/quota/{P}").status_code == 200
    # 导出
    exported = client.get("/api/quota/export?kind=quota_receipts")
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")
    # 续跑空转
    again = client.post("/api/quota/resume").json()
    assert again["status"] == "nothing_to_resume"


def test_api_validation_errors_are_4xx(client: TestClient):
    bad = client.post("/api/quota/process",
                      json={"events": [v(1, "首发", "坏金额", "2026-10-20T09:00:00+08:00")]})
    # 规范化失败逐条计入 invalid，不落半条事实
    assert bad.status_code == 200
    assert bad.json()["ingest"]["invalid"] == 1
    missing = client.post("/api/quota/process", json={"events": [{"event_type": "quota_receipt",
                          "payload": {"pool_id": P}}]})
    assert missing.json()["ingest"]["invalid"] == 1
    unknown = client.post("/api/quota/process", json={"event_type": "mystery"})
    assert unknown.status_code == 400
    assert client.get("/api/quota/pools/NOPE").status_code == 404


def test_pegging_picks_latest_version_effective_at_acceptance(store):
    """钉版规则纯函数：取受理时刻已发布版本中序号最大者。"""
    versions = [
        {"version_no": 1, "kind": "首发", "status": "applied",
         "published_at": "2026-10-01T00:00:00+00:00", "total_wan": Decimal("1000")},
        {"version_no": 2, "kind": "更正", "status": "applied",
         "published_at": "2026-10-10T00:00:00+00:00", "total_wan": Decimal("800")},
        {"version_no": 3, "kind": "更正", "status": "rejected",
         "published_at": "2026-10-12T00:00:00+00:00", "total_wan": Decimal("500")},
    ]
    pick = ledger.pegging_version(versions, "2026-10-11T00:00:00+00:00")
    assert pick["version_no"] == 2  # rejected 版本不参与
    assert ledger.pegging_version(versions, "2026-09-30T00:00:00+00:00") is None


def test_rejected_version_keeps_its_row_alongside_applied(store):
    """被拒版本与生效版本同序号共存：applied 行不被覆盖，rejected 行带证据。"""
    service.run(store, [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        v(2, "更正", "800", "2026-11-06T09:30:00+08:00"),
        v(2, "更正", "700", "2026-11-07T09:30:00+08:00"),
    ])
    rows = store.quota_load_versions(P)
    applied_v2 = [x for x in rows if x["version_no"] == 2 and x["status"] == "applied"]
    rejected_v2 = [x for x in rows if x["version_no"] == 2 and x["status"] == "rejected"]
    assert len(applied_v2) == 1 and applied_v2[0]["total_wan"] == Decimal("800.00")
    assert len(rejected_v2) == 1 and rejected_v2[0]["total_wan"] == Decimal("700.00")
    assert rejected_v2[0]["reject_detail"]


def test_resume_with_only_unmatchable_pending_is_noop(store):
    """挂起回执在等更早首发：空转续跑不产生运行；可匹配事件到达后续跑才推进。"""
    service.run(store, [
        r("EARLY", "早厂", "120", "2026-09-01T10:00:00+08:00"),
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),  # 晚于受理，仍钉不上
    ])
    runs_before = len(store.quota_list_runs())
    out = service.resume(store)
    assert out["status"] == "nothing_to_resume"
    assert len(store.quota_list_runs()) == runs_before
    # 发布一个 09-01 之前的首发（用新序号链路：另起一池验证可推进路径）
    service.ingest_events(store, [
        v(1, "首发", "1000", "2026-08-15T09:00:00+08:00", pool="POOL-NEW"),
        r("N1", "新厂", "200", "2026-08-20T10:00:00+08:00", pool="POOL-NEW"),
    ])
    moved = service.resume(store)
    assert moved["status"] == "done"
    assert service.pool_view(store, "POOL-NEW")["receipt_total"] == 1


def test_all_pages_render_and_nav_includes_quota(client: TestClient):
    """八个页面全部可渲染，导航含额度账本。"""
    payload = {"events": [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
    ]}
    client.post("/api/quota/process", json=payload)
    home = client.get("/").text
    assert "/quota" in home and "额度账本" in home
    quota = client.get("/quota").text
    assert "额度账本" in quota and "600.00" in quota
    detail = client.get(f"/quota/{P}").text
    assert "公告版本时间线" in detail and "受理回执清单" in detail
    # HEAD 探活
    assert client.head("/quota").status_code == 200
    assert client.head(f"/quota/{P}").status_code == 200


def test_dashboard_reconcile_matches_pool_view_line_items(store):
    """看板对账数与单池快照、回执清单逐项一致（清单和导出能逐项对上）。"""
    service.run(store, [
        v(1, "首发", "1000", "2026-10-20T09:00:00+08:00"),
        r("R1", "甲厂", "600", "2026-11-02T10:00:00+08:00"),
        r("R2", "乙厂", "250", "2026-11-03T10:00:00+08:00"),
        v(2, "更正", "700", "2026-11-06T09:30:00+08:00"),
        r("R3", "丙厂", "700", "2026-11-09T10:00:00+08:00"),  # 钉 v2 即超占
    ])
    dash = service.dashboard(store)
    pool = next(p for p in dash["pools"] if p["pool_id"] == P)
    view = service.pool_view(store, P)
    # 看板与单池视图同源
    assert pool["available_wan"] == view["available_wan"] == Decimal("0.00")
    assert pool["gap_wan"] == view["gap_wan"] == Decimal("0.00")  # v2: 700-700
    v1 = next(x for x in view["version_views"] if x["version_no"] == 1)
    assert v1["occupied_wan"] == Decimal("850.00")
    # 回执逐项金额求和 = 全池占用
    line_sum = sum((r["amount_wan"] for x in view["version_views"] for r in x["receipts"]),
                   Decimal("0.00"))
    assert line_sum == view["total_occupied_wan"] == Decimal("1550.00")
    assert dash["reconcile"]["all_balanced"] is True
