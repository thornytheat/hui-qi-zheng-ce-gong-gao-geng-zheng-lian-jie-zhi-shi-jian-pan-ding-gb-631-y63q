"""M4 链判定与截止：更正覆盖、延期、撤销、三种冲突、工作日推算与区间端点。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app import config
from app.modules.m4_chain import chain as chain_rules
from app.modules.m4_chain import service as chain_service, workdays
from fixtures import expected as X


def _chain(run, doc_no: str) -> dict:
    return next(c for c in run["chains"] if c["doc_no"] == doc_no)


def test_correction_overrides_amount(run):
    """更正链：序号 2 的更正覆盖资助额度，最终额度是 1500 而不是 1200。"""
    chain = _chain(run, "澄工信发〔2026〕37号")
    assert chain["status"] == X.CHAIN_37["status"]
    assert chain["final_amount_wan"] == Decimal(X.CHAIN_37["amount"])
    assert chain["versions"] == X.CHAIN_37["versions"]
    correction = [v for v in chain["version_rows"] if v["seq"] == 2][0]
    assert correction["target_field"] == "amount"
    assert correction["note"].startswith("更正资助额度")


def test_extension_moves_deadline(run):
    """延期链：序号 3 的延期把截止改成 2026-12-15，并进入可申报清单。"""
    chain = _chain(run, "澄工信发〔2026〕37号")
    assert chain["final_deadline"] == X.CHAIN_37["deadline"]
    assert chain["listed"] is True


def test_cancel_voids_chain(run):
    """撤销链：撤销后整链作废，即使原截止还没到也不进清单。"""
    chain = _chain(run, "澄工信发〔2026〕41号")
    assert chain["status"] == X.CHAIN_41["status"]
    assert chain["listed"] is False
    assert chain["final_deadline"] == X.CHAIN_41["deadline"]


def test_duplicate_seq_conflict_isolated(run):
    """TRAP-14：同一公文号同一序号出现两个额度（800 / 950），整链隔离且不猜。"""
    chain = _chain(run, X.CHAIN_21_CONFLICT)
    assert chain["status"] == "CONFLICT"
    assert chain["final_amount_wan"] is None
    refs = [c["refs"] for c in chain["conflicts"]]
    assert any(len(group) == 2 for group in refs)
    codes = [q["code"] for q in run["quarantine_rows"]]
    assert "C_DUP_SEQ_CONFLICT" in codes


def test_missing_origin_conflict_isolated(run):
    """缺父公告：链首就是序号 2 的更正，C_NO_ORIGIN 整链隔离。"""
    chain = _chain(run, X.CHAIN_08_NO_ORIGIN)
    assert chain["status"] == "CONFLICT"
    assert any(c["code"] == "C_NO_ORIGIN" for c in chain["conflicts"])


def test_time_inversion_conflict_isolated(run):
    """时间倒挂：序号 2 的发布时间早于序号 1，C_TIME_INVERSION 整链隔离。"""
    chain = _chain(run, X.CHAIN_11_INVERSION)
    assert chain["status"] == "CONFLICT"
    assert any(c["code"] == "C_TIME_INVERSION" for c in chain["conflicts"])


def test_workday_deadline_subtracts_holidays_and_adds_makeup():
    """TRAP-23：发布日 2026-09-28 + 30 个工作日 = 2026-11-13；
    期间扣除国庆 7 天、计入调休上班日 2026-10-10。"""
    trace = workdays.walk_workdays(dt.date(2026, 9, 28), 30)
    assert trace["deadline"].isoformat() == X.WORKDAY_30_DEADLINE
    assert len(trace["holiday_hits"]) == 7
    assert trace["makeup_hits"] == [X.MAKEUP_DAY]
    assert trace["first"] == dt.date(2026, 9, 29)


def test_workday_interval_is_half_open():
    """TRAP-23：计数区间左闭右开，区间内工作日数恰为 N，相邻区间不重不漏。"""
    base = dt.date(2026, 9, 28)
    trace = workdays.walk_workdays(base, 30)
    start = base + dt.timedelta(days=1)
    end = trace["deadline"] + dt.timedelta(days=1)
    assert workdays.count_workdays(start, end) == 30
    assert workdays.count_workdays(start, trace["deadline"]) == 29
    first_ten = workdays.walk_workdays(base, 10)["deadline"]
    assert workdays.count_workdays(start, first_ten + dt.timedelta(days=1)) == 10
    assert end.isoformat() == "2026-11-14"          # 右开：截止日次日不计数


def test_naive_weekday_count_differs():
    """反例：只数周一至周五（不扣节假日、不加调休）会得到更早的日期。"""
    base = dt.date(2026, 9, 28)
    naive = base
    counted = 0
    while counted < 30:
        naive += dt.timedelta(days=1)
        if naive.isoweekday() <= 5:
            counted += 1
    correct = workdays.walk_workdays(base, 30)["deadline"]
    assert naive != correct
    assert naive.isoformat() == "2026-11-09"
    assert correct.isoformat() == X.WORKDAY_30_DEADLINE


def test_expired_chain_not_listed(run):
    """过期链：截止 2026-10-20 早于评估日 2026-11-15，不进清单但仍可见。"""
    chain = _chain(run, "澄工信发〔2026〕29号")
    assert chain["final_deadline"] == X.CHAIN_29_EXPIRED["deadline"]
    assert chain["listed"] is False
    decorated = chain_service.decorate(chain, config.Settings())
    assert decorated["display_status"] == "EXPIRED"


def test_chain_invariants_and_trace(run, store, batch_id, settings):
    """链不变量：入链公告数 = 每条链版本数之和；推算依据能回溯到具体版本。"""
    assert sum(c["versions"] for c in run["chains"]) == X.ACCEPTED
    detail = chain_service.detail(store, batch_id, "澄科技发〔2026〕18号", settings)
    assert detail["trace"]["source_seq"] == 1
    assert any(line.startswith("第 20 个工作日") for line in detail["trace"]["lines"])
    extension = chain_service.detail(store, batch_id, "澄工信发〔2026〕37号", settings)
    assert extension["trace"]["source_seq"] == 3
    assert any("绝对日期" in line for line in extension["trace"]["lines"])
    assert detail["versions_detail"][0]["published_local"].utcoffset() == dt.timedelta(hours=8)


def _ann(seq, kind, *, doc_no="澄企服〔2026〕70号", published, amount=None, spec=None,
         target_field=None, target_value="", title="惠企服务券发放"):
    """构造一条入链公告行（与 M2 解析输出同形）。"""
    return {
        "doc_no": doc_no, "seq": seq, "kind": kind, "title": title,
        "published_at": published, "deadline_spec": spec,
        "amount_wan": None if amount is None else Decimal(str(amount)),
        "target_field": target_field, "target_value": target_value,
        "portal": "portal_c", "row_ref": f"c/list_p1.html#r{seq}",
        "source_url": "http://sample-site/c/list_p1.html",
    }


def _cancelled_chain(settings):
    announcements = [
        _ann(1, "ORIGINAL", published="2026-09-01T09:00:00+08:00", amount="300.00",
             spec={"type": "date", "date": "2026-12-01"}),
        _ann(2, "CANCEL", published="2026-09-20T09:00:00+08:00"),
        _ann(3, "CORRECT", published="2026-10-10T09:00:00+08:00",
             target_field="amount", target_value="500万元"),
    ]
    result = chain_rules.build_chains(announcements, settings)
    return result.chains[0], result.quarantine


def test_after_cancel_version_keeps_chain_voided(settings):
    """边界：撤销之后的更正只登记（C_AFTER_CANCEL），不能把作废链改回有效。"""
    row, quarantine = _cancelled_chain(settings)
    assert row["status"] == "VOIDED"
    assert row["final_amount_wan"] == Decimal("300.00")
    assert row["listed"] is False
    assert "撤销后仍有新版本" in row["note"]
    assert [item["code"] for item in quarantine] == ["C_AFTER_CANCEL"]


def test_naive_last_write_wins_would_revive_cancelled_chain(settings):
    """反例：朴素「后写覆盖」会把撤销后的更正套上去，额度 500、状态回到有效。"""
    row, _quarantine = _cancelled_chain(settings)
    naive_amount = Decimal("500.00")
    naive_status = "ACTIVE"
    assert row["final_amount_wan"] != naive_amount
    assert row["status"] != naive_status
    assert row["status"] == "VOIDED"
