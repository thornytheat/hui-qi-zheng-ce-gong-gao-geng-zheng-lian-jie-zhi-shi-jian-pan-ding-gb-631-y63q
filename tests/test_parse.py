"""M2 契约与解析：千分位/全角、单位、哨兵、时区、列数、公文号保形。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app import config
from app.modules.m2_parse import coerce, contract, fields, service


def test_amount_thousands_and_fullwidth():
    """TRAP-06：半角千分位、全角逗号、全角数字、带空格与单位后缀都要归一。"""
    cases = {
        "1,200万元": Decimal("1200.00"),
        "1，200万元": Decimal("1200.00"),
        "１２３４万元": Decimal("1234.00"),
        " 1,200 万元 ": Decimal("1200.00"),
    }
    for raw, want in cases.items():
        value, unit, error = coerce.norm_amount(raw)
        assert error is None, raw
        assert unit == "万元"
        assert value == want, raw


def test_amount_unit_yuan_converted_to_wan():
    """TRAP-15：元与万元混用，先归一再合计，1 元 = 0.0001 万元。"""
    value, _unit, error = coerce.norm_amount("12,000,000元")
    assert error is None
    assert value == Decimal("1200.0000")
    assert value == config.UNIT_TO_WAN["元"] * Decimal("12000000")


def test_sentinel_amount_is_three_state():
    """TRAP-07：哨兵不是 0；None / 0 / 具体值三态必须分开。"""
    for raw in ("-", "N/A", "未知", "暂无", "", "—"):
        value, _unit, error = coerce.norm_amount(raw)
        assert error is None and value is None, raw
    zero, _u, error = coerce.norm_amount("0万元")
    assert error is None and zero == Decimal("0.00")


def test_bad_amount_gives_reason_not_zero():
    """坏额度给原因码，不能静默变成 0。"""
    value, _unit, error = coerce.norm_amount("详见正文")
    assert value is None
    assert error is None  # 「详见正文」是转引写法，按未登记处理
    value, _unit, error = coerce.norm_amount("约一千万")
    assert error == "Q_BAD_AMOUNT"
    assert coerce.norm_amount("壹仟万元")[2] == "Q_BAD_AMOUNT"


def test_instant_timezone_offsets_collapse():
    """TRAP-11：Z 与 +08:00 写同一时刻，归一后必须完全相同。"""
    utc = coerce.norm_instant("2026-11-03T09:00:00Z")
    local = coerce.norm_instant("2026-11-03T17:00:00+08:00")
    assert utc[0] == local[0] == dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)
    assert utc[0].isoformat() == local[0].isoformat()


def test_date_only_uses_shanghai_midnight():
    """只有日期的公告按 Asia/Shanghai 当日 00:00 解释（规则写进契约）。"""
    moment, error = coerce.norm_instant("2026-11-03")
    assert error is None
    assert moment == dt.datetime(2026, 11, 2, 16, 0, tzinfo=dt.timezone.utc)
    chinese, error = coerce.norm_instant("2026年11月12日")
    assert error is None
    assert chinese.astimezone(config.CHINA_TZ).date() == dt.date(2026, 11, 12)


def test_deadline_rule_variants():
    """截止说明三种写法：工作日、自然日、绝对日期；中文数字不支持并给原因码。"""
    assert coerce.norm_deadline("自发布之日起30个工作日")[0] == {"type": "workdays", "n": 30}
    assert coerce.norm_deadline("自发布之日起30个自然日")[0] == {"type": "days", "n": 30}
    assert coerce.norm_deadline("2026-12-15")[0] == {"type": "date", "date": "2026-12-15"}
    spec, error, note = coerce.norm_deadline("自发布之日起三十个工作日")
    assert spec is None and error == "Q_BAD_DEADLINE_RULE"
    assert "三十" in note


def test_doc_no_keeps_leading_zero_and_brackets():
    """公文号保形：全角方括号归一为〔〕，前导零不丢，整串按字符串保存。"""
    assert coerce.norm_doc_no("澄科技发〔2026〕07号")[0] == "澄科技发〔2026〕07号"
    assert coerce.norm_doc_no("澄科技发[2026]07号")[0] == "澄科技发〔2026〕07号"
    assert coerce.norm_doc_no("澄科技发【2026】7号")[0] == "澄科技发〔2026〕7号"
    assert coerce.norm_doc_no("无编号公告")[1] == "Q_DOC_NO_INVALID"


def test_column_count_mismatch_quarantined():
    """TRAP-05：列数与表头不一致一律隔离，不补空也不截断。"""
    record = {
        "portal": "portal_b", "source_url": "http://sample-site/b/export.csv",
        "row_ref": "b/export.csv#L9", "header_count": 9, "cell_count": 10,
        "fields": {"文号": "澄科技发〔2026〕52号"}, "cells": ["x"] * 10,
    }
    result = service.parse_records([record])
    assert result.announcements == []
    assert result.rejected_records == 1
    assert result.quarantine[0]["code"] == "Q_COLUMN_COUNT"
    assert "9" in result.quarantine[0]["detail"]


def test_alias_priority_prefers_explicit_column():
    """列名演进：同一页既有「序号」又有「公告序号」时，取更精确的公告序号。"""
    canon, unknown = fields.canonicalize({"序号": "9", "公告序号": "2", "文号": "澄企服〔2026〕13号"})
    assert canon["seq"] == "2"
    assert unknown.get("序号") is None or canon["seq"] == "2"


def test_portal_aliases_map_to_same_fields():
    """三个门户列名不同，契约映射到同一批规范字段。"""
    cases = [
        {"文号": "澄科技发〔2026〕07号", "序号": "1", "类型": "首次发布", "标题": "t",
         "发布时间": "2026-10-22"},
        {"文件编号": "澄科技发〔2026〕07号", "公告序号": "1", "公告性质": "首次发布",
         "名称": "t", "日期": "2026-10-22"},
    ]
    for raw in cases:
        canon, _unknown = fields.canonicalize(raw)
        assert canon["doc_no"] == "澄科技发〔2026〕07号"
        assert canon["seq"] == "1"
        assert canon["kind"] == "首次发布"


def test_naive_float_parse_would_raise():
    """反例：朴素 float() 直接解析「1,200万元」会抛错，本项目走归一函数。"""
    with pytest.raises(ValueError):
        float("1,200万元")
    value, _unit, error = coerce.norm_amount("1,200万元")
    assert error is None and value == Decimal("1200.00")


def test_preview_reasons_for_unknown_kind():
    """异常路径：公告类型不在四类里，预览要给出原因码而不是猜。"""
    announcement, errors = service.parse_one({
        "文号": "澄科技发〔2026〕48号", "公告序号": "1", "公告类型": "补充说明",
        "标题": "中小企业公共服务平台奖励", "发布时间": "2026-11-12",
    })
    assert announcement is None
    assert errors[0]["code"] == "Q_KIND_UNKNOWN"


def test_contract_document_declares_decimals_and_interval():
    """字段契约：金额 Decimal、区间左闭右开、去重键都写在契约里，可被页面复核。"""
    doc = contract.document()
    assert doc["decimals"]["amount_unit"] == "万元"
    assert doc["decimals"]["quantize"] == "0.01"
    assert "[start, end)" in doc["interval"]["endpoint"] or "左闭右开" in doc["interval"]["endpoint"]
    assert doc["dedupe_key"].startswith("doc_no + seq")
    assert len(doc["columns"]) == len(fields.FIELDS)


def test_deadline_workdays_upper_bound_rejected():
    """边界：工作日上限 200，201 直接给原因码；200 照常接受，不静默截断。"""
    spec, error, note = coerce.norm_deadline("自发布之日起201个工作日")
    assert spec is None
    assert error == "Q_BAD_DEADLINE_RULE"
    assert "201" in note
    ok, error, _note = coerce.norm_deadline("自发布之日起200个工作日")
    assert error is None
    assert ok == {"type": "workdays", "n": 200}


def test_correction_amount_negative_would_shrink_total():
    """反例：负额度若被当成有效值会直接冲减合计；归一后仍保留符号，交给链判定隔离。"""
    value, _unit, error = coerce.norm_amount("-1200万元")
    assert error is None
    assert value == Decimal("-1200.00")
    assert value < 0          # 归一不掩符号，是否入账由上层规则决定
