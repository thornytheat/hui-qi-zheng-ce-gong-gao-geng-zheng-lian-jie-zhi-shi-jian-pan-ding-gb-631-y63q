"""M7 复核与回放：转载优先级、人工裁定留痕、非法迁移、超长链合并与回放对账。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import config, errors
from app.modules.m7_review import replay, review, service
from fixtures import expected as X

D37 = "澄工信发〔2026〕37号"          # 首次发布 → 更正 → 延期，自动判定为可申报
D21 = "澄科技发〔2026〕21号"          # 同一序号两个额度，整链冲突隔离
D08 = "澄企服〔2026〕08号"            # 缺父公告
D11 = "澄企服〔2026〕11号"            # 时间倒挂
GOOD_BASIS = "看 b/list_p2 两行额度不同，回源确认后取 800 万那版"


def _v(seq, kind, *, doc_no=D37, portal="portal_a",
       published="2026-10-01T09:00:00+08:00", amount=None, spec=None,
       target_field=None, target_value="", row_ref=None, title="智能车间改造"):
    """构造一个链版本用的公告行（与 store 里的版本行同形）。"""
    return {
        "doc_no": doc_no, "seq": seq, "kind": kind, "title": title,
        "published_at": published, "deadline_spec": spec,
        "amount_wan": None if amount is None else Decimal(str(amount)),
        "target_field": target_field, "target_value": target_value,
        "portal": portal, "row_ref": row_ref or f"{portal}/list_p1.html#r{seq}",
        "source_url": f"http://sample-site/{portal}/list_p1.html",
    }


def test_review_queue_lists_only_conflict_chains(client_with_run):
    """复核队列：只收冲突链，三条冲突各带自己的原因码与证据行。"""
    data = client_with_run.get("/api/review").json()
    assert data["counts"]["queue"] == 3
    assert {row["doc_no"] for row in data["queue"]} == {D08, D11, D21}
    codes = {code for row in data["queue"] for code in row["codes"]}
    assert codes == {"C_NO_ORIGIN", "C_TIME_INVERSION", "C_DUP_SEQ_CONFLICT"}
    assert all(row["ruled"] is False for row in data["queue"])
    assert data["manual_list"] == {"count": 0, "amount": "0.00"}


def test_review_api_without_batch_is_404(client):
    """失败输入：还没采信过就来复核，返回 404 与 B_EMPTY，不伪造空队列。"""
    response = client.get("/api/review")
    assert response.status_code == 404
    assert response.json()["error"] == "B_EMPTY"


def test_replay_unknown_doc_is_404(client_with_run):
    """失败输入：回放一个不在本批次的公文号，404 而不是空回放。"""
    response = client_with_run.get("/api/review/replay",
                                   params={"doc_no": "澄工信发〔2026〕99号"})
    assert response.status_code == 404
    assert response.json()["error"] == "E_NOT_FOUND"


def test_ruling_needs_reviewer_and_basis(client_with_run):
    """裁定入参：裁定人至少 2 字、依据至少 6 字，缺一边就 400 且指明字段。"""
    short_basis = client_with_run.post("/api/review/ruling", json={
        "doc_no": D21, "seq": 1, "reviewer": "王工", "basis": "短"})
    assert short_basis.status_code == 400
    assert short_basis.json()["error"] == "M_BAD_RULING"
    assert short_basis.json()["detail"]["field"] == "basis"

    short_reviewer = client_with_run.post("/api/review/ruling", json={
        "doc_no": D21, "seq": 1, "reviewer": "王", "basis": GOOD_BASIS})
    assert short_reviewer.status_code == 400
    assert short_reviewer.json()["detail"]["field"] == "reviewer"


def test_ruling_unknown_seq_rejected(client_with_run):
    """失败输入：序号不在链内时把可选序号列出来，不让裁定落到不存在的版本。"""
    response = client_with_run.post("/api/review/ruling", json={
        "doc_no": D21, "seq": 9, "reviewer": "王工", "basis": GOOD_BASIS})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "M_NO_SUCH_SEQ"
    assert body["detail"]["seqs"] == [1]


def test_manual_ruling_overrides_conflict_and_keeps_both_values(client_with_run):
    """TRAP-27：人工裁定与自动判定冲突时以人工为准，但自动值与人工值都留在留痕里。"""
    response = client_with_run.post("/api/review/ruling", json={
        "doc_no": D21, "seq": 1, "reviewer": "王工", "basis": GOOD_BASIS})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "M_MANUAL_OVERRIDE"
    assert {d["field"] for d in body["diffs"]} == {
        "status", "final_deadline", "final_amount_wan", "listed"}
    snapshot = body["snapshot"]
    assert snapshot["auto_status"] == "CONFLICT"
    assert snapshot["auto_amount"] is None
    assert snapshot["status"] == "ACTIVE"
    assert snapshot["amount"] == "800.00"
    assert snapshot["deadline"] == "2026-12-31"

    data = client_with_run.get("/api/review").json()
    assert data["counts"]["overridden"] == 1
    assert data["manual_list"] == {"count": 1, "amount": "800.00"}
    assert data["rulings"][0]["reviewer"] == "王工"
    assert data["rulings"][0]["code"] == "M_MANUAL_OVERRIDE"


def test_ruling_matching_auto_verdict_is_recorded_not_override(client_with_run):
    """人工结论与自动判定一致时只记 M_RULING_RECORDED，不算改判。"""
    response = client_with_run.post("/api/review/ruling", json={
        "doc_no": D37, "seq": 3, "reviewer": "李工",
        "basis": "a/list_p1 第 3 行延期到 12-15，与回放一致"})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "M_RULING_RECORDED"
    assert body["diffs"] == []
    assert body["manual"]["deadline"] == "2026-12-15"
    assert body["manual"]["amount"] == "1500.00"
    assert body["manual"]["listed"] is True
    assert body["snapshot"]["auto_amount"] == "1500.00"

    data = client_with_run.get("/api/review").json()
    assert data["counts"]["ruled"] == 1
    assert data["counts"]["overridden"] == 0


def test_ruling_rerun_keeps_single_trail(client_with_run):
    """不变量（幂等）：同一公文号重复裁定只留一条留痕，不重复进裁定后清单。"""
    payload = {"doc_no": D21, "seq": 1, "reviewer": "王工", "basis": GOOD_BASIS}
    for _ in range(2):
        assert client_with_run.post("/api/review/ruling", json=payload).status_code == 200
    data = client_with_run.get("/api/review").json()
    assert data["counts"]["ruled"] == 1
    assert len(data["rulings"]) == 1
    assert data["manual_list"] == {"count": 1, "amount": "800.00"}


def test_replay_state_matches_chain_for_every_chain(store, settings, batch_id, run):
    """不变量（一致）：非冲突链的回放结论必须与链判定逐字段相等，冲突链只做复核。"""
    chains = store.load_chains(batch_id)
    checked = 0
    for chain in chains:
        item = service.replay_doc(store, batch_id, chain["doc_no"], settings)
        assert item is not None
        state = item["replay_state"]
        if chain["status"] == "CONFLICT":
            assert item["checks"]["checked"] is False
            continue
        assert item["checks"]["consistent"] is True
        assert item["checks"]["diffs"] == []
        assert state["deadline"] == chain["final_deadline"]
        amount = chain["final_amount_wan"]
        expected = None if amount is None else str(amount.quantize(config.DECIMAL_QUANT))
        assert state["amount"] == expected
        assert state["voided"] is (chain["status"] == "VOIDED")
        checked += 1
    assert checked == X.CHAINS - 3


def test_issuer_page_beats_repost_for_same_seq():
    """转载优先级：同一序号既有发文机关页又有转载页时，取发文机关页的值。"""
    doc = "澄工信发〔2026〕60号"
    versions = [
        _v(1, "ORIGINAL", doc_no=doc, portal="portal_b",
           published="2026-10-01T09:00:00+08:00", amount="950.00",
           spec={"type": "date", "date": "2026-12-31"},
           row_ref="b/list_p1.html#r9"),
        _v(1, "ORIGINAL", doc_no=doc, portal="portal_a",
           published="2026-10-03T09:00:00+08:00", amount="800.00",
           spec={"type": "date", "date": "2026-12-31"},
           row_ref="a/list_p1.html#r2"),
    ]
    rows = review.rank_candidates(versions, doc)[1]
    assert rows[0]["portal"] == "portal_a"
    assert rows[0]["role_label"] == "发文机关页"
    assert rows[0]["winner"] is True
    assert rows[1]["role_label"] == "转载页"
    assert rows[1]["winner"] is False
    assert rows[0]["priority_tie"] is False

    effective = review.effective_versions(versions, doc)
    assert [row["portal"] for row in effective] == ["portal_a"]
    folded = replay.fold(effective)
    assert folded["state"]["amount"] == Decimal("800.00")


def test_correction_after_cancel_is_illegal_transition():
    """TRAP-28：撤销之后再更正属非法迁移，该版不能被选为有效版（请求 409）。"""
    versions = [
        _v(1, "ORIGINAL", amount="900.00", spec={"type": "date", "date": "2026-12-31"}),
        _v(2, "CANCEL", published="2026-10-20T09:00:00+08:00"),
        _v(3, "CORRECT", published="2026-11-01T09:00:00+08:00",
           target_field="amount", target_value="1200万元"),
    ]
    problems = review.transition_problems(versions)
    assert [p["code"] for p in problems] == ["M_ILLEGAL_TRANSITION"]
    assert problems[0]["seq"] == 3
    assert problems[0]["after_seq"] == 2

    with pytest.raises(errors.Conflict) as caught:
        review.validate_ruling(versions, seq=3, reviewer="王工",
                               basis="撤销后仍有更正，回源确认")
    assert caught.value.code == "M_ILLEGAL_TRANSITION"

    chosen = review.validate_ruling(versions, seq=2, reviewer="王工",
                                    basis="以撤销公告为准，整链作废")
    assert chosen[0]["kind"] == "CANCEL"


def test_long_chain_compresses_without_changing_conclusion():
    """超长链（>20 版）：撤销后连续不改变状态的版本压成一步，结论仍为作废。"""
    versions = [_v(1, "ORIGINAL", amount="900.00",
                   spec={"type": "date", "date": "2026-12-31"})]
    versions.append(_v(2, "CANCEL", published="2026-10-02T09:00:00+08:00"))
    for seq in range(3, 26):
        versions.append(_v(seq, "CORRECT",
                           published=f"2026-10-{seq:02d}T09:00:00+08:00",
                           target_field="amount", target_value=f"{100 + seq}万元"))
    folded = replay.fold(versions)
    assert folded["versions"] == 25
    state = folded["state"]
    assert state["voided"] is True
    assert state["amount"] == Decimal("900.00")

    view = replay.long_view(folded)
    assert view["long"] is True
    assert view["code"] == "M_LONG_CHAIN"
    assert view["rows"] < view["versions"]
    assert view["steps"][-1]["merged"] == 23
    assert view["steps"][-1]["merged_to"] == 25
    # 反例：逐版应用更正会把作废链复活成最后一版金额。
    assert state["amount"] != Decimal("125.00")


def test_review_page_renders_queue_and_replay(client_with_run):
    """复核页：默认可看冲突链队列与回放，未知公文号按 404 处理。"""
    response = client_with_run.get("/review")
    assert response.status_code == 200
    text = response.text
    assert "复核与回放" in text
    assert "待复核冲突链" in text
    assert "复核回放" in text          # 导航项
    assert D08 in text

    missing = client_with_run.get("/review", params={"doc_no": "澄工信发〔2026〕99号"})
    assert missing.status_code == 404
