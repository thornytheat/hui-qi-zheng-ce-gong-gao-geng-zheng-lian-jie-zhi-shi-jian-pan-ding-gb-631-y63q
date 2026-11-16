"""原因码唯一来源：跨模块共享，页面/导出/测试都从这里取文案。

一个原因码 = 级别 + 所属阶段 + 中文结论 + 复核建议。级别只有三档：
info（可忽略的采集提示）、warn（需人工看一眼）、error（已隔离，不能进清单）。
"""
from __future__ import annotations

from typing import NamedTuple

INFO = "info"
WARN = "warn"
ERROR = "error"
LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
LEVEL_LABELS = {ERROR: "隔离", WARN: "提示", INFO: "记录"}


class Reason(NamedTuple):
    code: str
    level: str
    stage: str
    title: str
    hint: str


def _r(code, level, stage, title, hint):
    return Reason(code, level, stage, title, hint)


REASONS = {
    # ---- M1 采信 ----
    "E_CHARSET_MISMATCH": _r(
        "E_CHARSET_MISMATCH", WARN, "ingest",
        "页面声明编码与实际字节不符", "按回退链重新解码，检查门户是否改版"),
    "E_BOM_STRIPPED": _r(
        "E_BOM_STRIPPED", INFO, "ingest",
        "页面带 UTF-8 BOM，已剥离", "导出给 Excel 时自身也会带 BOM，导入端需同样剥离"),
    "E_DECODE_FAILED": _r(
        "E_DECODE_FAILED", ERROR, "ingest",
        "回退链内所有编码都无法解码该页", "抓取原始字节人工确认编码后补进回退链"),
    "E_HTTP_STATUS": _r(
        "E_HTTP_STATUS", ERROR, "ingest",
        "页面返回非 200 状态", "确认样例站或门户是否仍在发布"),
    "E_PAGE_TRUNCATED": _r(
        "E_PAGE_TRUNCATED", ERROR, "ingest",
        "页面表格未闭合或声明条数与实际行数不符", "该页末行按隔离处理，重新抓取整页"),
    "E_PAGING_NO_NEXT": _r(
        "E_PAGING_NO_NEXT", WARN, "ingest",
        "页脚声明还有下一页但缺少 rel=next 链接", "记录断点，按已知页数上限人工补抓"),
    "E_PAGING_LOOP": _r(
        "E_PAGING_LOOP", ERROR, "ingest",
        "分页 next 指回已访问页面", "门户分页参数异常，停止翻页避免死循环"),
    "E_PAGING_LIMIT": _r(
        "E_PAGING_LIMIT", WARN, "ingest",
        "达到单门户翻页上限", "确认是否漏抓，必要时提高上限再跑"),
    "S_NO_ANNOUNCEMENT": _r(
        "S_NO_ANNOUNCEMENT", INFO, "ingest",
        "页面不含公告表格，跳过（非隔离）", "门户首页/导航页属正常跳过"),
    # ---- M2 解析 ----
    "Q_COLUMN_COUNT": _r(
        "Q_COLUMN_COUNT", ERROR, "parse",
        "列数与表头不一致（多列或少列）", "多为导出时末尾多一个分隔符或截断，需回源核对"),
    "Q_MISSING_REQUIRED": _r(
        "Q_MISSING_REQUIRED", ERROR, "parse",
        "缺少必需字段（公文号/公告序号/公告类型等）", "补齐后再入链，禁止用默认值顶替"),
    "Q_DOC_NO_INVALID": _r(
        "Q_DOC_NO_INVALID", ERROR, "parse",
        "公文号无法归一为「机关〔年〕号」格式", "检查全角括号与空格，人工确认后重录"),
    "Q_SEQ_INVALID": _r(
        "Q_SEQ_INVALID", ERROR, "parse",
        "公告序号不是正整数", "序号决定链内顺序，缺失不能猜"),
    "Q_BAD_TIME": _r(
        "Q_BAD_TIME", ERROR, "parse",
        "发布时间无法解析为时刻", "检查是否混入中文日期或时区缺偏移"),
    "Q_BAD_AMOUNT": _r(
        "Q_BAD_AMOUNT", ERROR, "parse",
        "资助额度无法归一为万元", "检查是否缺单位或混入说明文字"),
    "Q_BAD_DEADLINE_RULE": _r(
        "Q_BAD_DEADLINE_RULE", WARN, "parse",
        "截止说明无法识别为工作日/自然日/绝对日期", "补规则或人工登记绝对日期"),
    "Q_KIND_UNKNOWN": _r(
        "Q_KIND_UNKNOWN", ERROR, "parse",
        "公告类型不在 首次发布/更正/延期/撤销 之内", "新增类型需先扩展链合并规则"),
    # ---- M3 质量 ----
    "Q_DUP_ROW": _r(
        "Q_DUP_ROW", WARN, "quality",
        "完全重复的公告行，已合并", "同一版本被两个门户转载，保留首见行并记来源"),
    "Q_SENTINEL_ONLY": _r(
        "Q_SENTINEL_ONLY", ERROR, "quality",
        "关键字段整行为空值哨兵", "哨兵不是 0，不能参与合计"),
    "Q_AMOUNT_SUSPECT": _r(
        "Q_AMOUNT_SUSPECT", ERROR, "quality",
        "资助额度超过经验上限 50000 万元", "疑似单位写错（元当成万元），人工确认"),
    "Q_DATE_SUSPECT": _r(
        "Q_DATE_SUSPECT", ERROR, "quality",
        "发布时间早于采集窗口或晚于评估日 90 天", "疑似年份笔误"),
    # ---- M4 链判定 ----
    "C_DUP_SEQ_CONFLICT": _r(
        "C_DUP_SEQ_CONFLICT", ERROR, "chain",
        "同一公文号同一序号出现冲突取值", "整链隔离，不猜哪条为准，回源核实"),
    "C_NO_ORIGIN": _r(
        "C_NO_ORIGIN", ERROR, "chain",
        "链缺少首次发布（序号 1）", "父公告缺失，更正/延期无从谈起"),
    "C_TIME_INVERSION": _r(
        "C_TIME_INVERSION", ERROR, "chain",
        "链内发布时间倒挂", "序号大的版本发布时间更早，说明门户补发或序号错"),
    "C_AFTER_CANCEL": _r(
        "C_AFTER_CANCEL", WARN, "chain",
        "撤销公告之后仍有新版本", "链已作废，后续版本仅登记不影响判定"),
    "C_ORPHAN_CORRECTION": _r(
        "C_ORPHAN_CORRECTION", ERROR, "chain",
        "更正指向的字段不在可更正经度内", "只允许更正 截止说明/资助额度/标题"),
    "C_BAD_EXTEND": _r(
        "C_BAD_EXTEND", ERROR, "chain",
        "延期公告没有可用的新截止说明", "延期不给新截止等于没延期，需回源"),
    # ---- M5 批次 ----
    "B_ABORTED": _r(
        "B_ABORTED", ERROR, "batches",
        "批次在阶段中断", "用续跑从断点继续，已采信页面不重抓"),
    "B_RESUME_NO_STATE": _r(
        "B_RESUME_NO_STATE", WARN, "batches",
        "没有可用的断点状态，按全新批次重跑", "首次失败过早时会遇到"),
    "B_IDEMPOTENT_REPLAY": _r(
        "B_IDEMPOTENT_REPLAY", INFO, "batches",
        "同一输入摘要命中已完成批次，直接复用结果", "重跑幂等：不新增行、不重复出清单"),
    # ---- M6 导出 ----
    "X_EMPTY_SELECTION": _r(
        "X_EMPTY_SELECTION", WARN, "export",
        "导出选择为空", "先跑一次采集再导出"),
    # ---- M7 复核与回放 ----
    "M_NEED_REVIEW": _r(
        "M_NEED_REVIEW", WARN, "review",
        "冲突链待人工裁定", "看两版证据行，选定一版并写清依据"),
    "M_REPOST_LOST": _r(
        "M_REPOST_LOST", INFO, "review",
        "转载页的版本让位发文机关版本", "同一版本只在发文机关页取值，转载页仅作来源"),
    "M_RULING_RECORDED": _r(
        "M_RULING_RECORDED", INFO, "review",
        "人工裁定已记录，结论与自动判定一致", "留痕含裁定人、依据与时间"),
    "M_MANUAL_OVERRIDE": _r(
        "M_MANUAL_OVERRIDE", INFO, "review",
        "人工裁定覆盖自动判定，已按裁定重算", "自动值与人工值都留在留痕里，可复核"),
    "M_ILLEGAL_TRANSITION": _r(
        "M_ILLEGAL_TRANSITION", ERROR, "review",
        "撤销之后又更正/延期/发布，属非法迁移", "该版不能被选为有效版，回源确认后重新采集"),
    "M_LONG_CHAIN": _r(
        "M_LONG_CHAIN", WARN, "review",
        "版本数超过 20，回放按线性合并压缩", "未改变状态的连续版本压成一步，结论不变"),
    "M_BAD_RULING": _r(
        "M_BAD_RULING", ERROR, "review",
        "裁定参数不完整或不可复算", "裁定人与依据都要填，依据写清看了哪一页哪一行"),
    "M_NO_SUCH_SEQ": _r(
        "M_NO_SUCH_SEQ", ERROR, "review",
        "选定的公告序号不在该链内", "先看回放表里实际有哪些序号"),
}


def get(code: str) -> Reason:
    return REASONS.get(code, Reason(code, WARN, "unknown", code, "未登记的原因码"))


def level_of(code: str) -> str:
    return get(code).level


def label(code: str) -> str:
    return get(code).title


def stage_of(code: str) -> str:
    return get(code).stage


def catalog() -> list[dict]:
    """给 /api/contract 与页面用的原因码目录（按阶段、级别排序）。"""
    rows = sorted(REASONS.values(), key=lambda r: (r.stage, LEVEL_ORDER[r.level], r.code))
    return [
        {"code": r.code, "level": r.level, "stage": r.stage,
         "title": r.title, "hint": r.hint, "level_label": LEVEL_LABELS[r.level]}
        for r in rows
    ]


def is_error(code: str) -> bool:
    return level_of(code) == ERROR
