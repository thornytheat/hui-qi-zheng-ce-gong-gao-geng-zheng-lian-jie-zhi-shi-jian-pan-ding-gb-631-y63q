"""noticechain 全局配置：编码回退链、节假日表、门户清单、Decimal 规则。

规则唯一来源，其他模块只读不写：
- 金额一律 Decimal，单位为「万元」，最小刻度 0.01；
- 时间入库一律 UTC，展示按 Asia/Shanghai（+08:00）；
- 工作日 = 周一至周五 − 法定节假日 + 调休上班日。
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHINA_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
UTC = dt.timezone.utc

RULE_VERSION = "noticechain/1.4.0"
DECIMAL_QUANT = Decimal("0.01")
MONEY_UNIT = "万元"
AMOUNT_SUSPECT_LIMIT = Decimal("50000")

STAGES = ("ingest", "parse", "quality", "chain", "list", "export")
STAGE_LABELS = {
    "ingest": "采信",
    "parse": "解析",
    "quality": "质量",
    "chain": "链判定",
    "list": "清单",
    "export": "导出",
}
STAGE_FLOW = "采信 → 解析 → 质量 → 链判定 → 清单 → 导出"

# 编码回退链：先按声明，再按严格顺序逐个尝试；gb18030 是 gbk/gb2312 的超集
ENCODING_CHAIN = ("utf-8-sig", "utf-8", "gb18030")
CHARSET_ALIASES = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
}

# 空值哨兵：集中定义，三个语义分开（None 哨兵 / 0 真值 / 空串）
SENTINELS = frozenset(
    {"", "-", "--", "—", "——", "/", "n/a", "na", "null", "none", "nil",
     "未知", "无", "暂无", "待定", "不适用", "未公布"}
)

# 资助额度单位 → 万元换算系数
UNIT_TO_WAN = {
    "万元": Decimal("1"),
    "万": Decimal("1"),
    "元": Decimal("0.0001"),   # 1 元 = 0.0001 万元
    "亿元": Decimal("10000"),
    "亿": Decimal("10000"),
}

# 2026 年度法定节假日（样例站所在年度的维护表，来源：园区企服组节假日台账）
HOLIDAYS_2026 = frozenset(
    {
        "2026-01-01", "2026-01-02", "2026-01-03",          # 元旦
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-21", "2026-02-22",          # 春节
        "2026-04-04", "2026-04-05", "2026-04-06",          # 清明
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
        "2026-06-19", "2026-06-20", "2026-06-21",          # 端午
        "2026-09-25", "2026-09-26", "2026-09-27",          # 中秋
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
        "2026-10-05", "2026-10-06", "2026-10-07",          # 国庆
    }
)
# 调休上班日（周末但算工作日）
MAKEUP_WORKDAYS_2026 = frozenset(
    {"2026-02-14", "2026-02-28", "2026-04-26", "2026-05-09", "2026-10-10"}
)

# 三个门户：列表页种子 + 附件 CSV（b 门户提供导出附件）
PORTALS = (
    {
        "id": "portal_a",
        "name": "江阴区工信局",
        "seeds": ("/a/list_p1.html",),
        "home": "/index.html",
        "attachment": None,
        "note": "列表页 + 详情页两段采集，含 rel=next 分页",
    },
    {
        "id": "portal_b",
        "name": "江南市科技局",
        "seeds": ("/b/list_p1.html",),
        "attachment": "/b/export.csv",
        "note": "GB18030 页面 + 导出附件 CSV（引号内换行/列数不齐/截断）",
    },
    {
        "id": "portal_c",
        "name": "园区企服中心",
        "seeds": ("/c/list_p1.html",),
        "attachment": None,
        "note": "分页缺 rel=next + 详情页时区混写",
    },
)

MAX_PAGES_PER_PORTAL = 6
MIN_PUBLISHED = dt.date(2025, 1, 1)
LISTED_STATUS = ("ACTIVE",)
CHAIN_STATUS_LABELS = {
    "ACTIVE": "有效",
    "VOIDED": "已撤销",
    "CONFLICT": "冲突隔离",
    "EXPIRED": "已过期",
}


@dataclass(frozen=True)
class Settings:
    """运行参数；测试用 replace() 覆盖，不读环境变量的分支。"""

    db_path: str = str(PROJECT_ROOT / "var" / "noticechain.sqlite")
    sample_site_base: str = "http://sample-site"
    sample_site_dir: str = str(PROJECT_ROOT / "sample_site")
    eval_date: dt.date = dt.date(2026, 11, 15)
    rule_version: str = RULE_VERSION
    http_timeout: float = 5.0
    export_dir: str = str(PROJECT_ROOT / "var" / "export")

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        db_path = os.getenv("NOTICECHAIN_DB") or base.db_path
        site = os.getenv("SAMPLE_SITE_BASE") or base.sample_site_base
        site_dir = os.getenv("SAMPLE_SITE_DIR") or base.sample_site_dir
        eval_raw = os.getenv("EVAL_DATE") or ""
        eval_date = base.eval_date
        if eval_raw:
            eval_date = dt.date.fromisoformat(eval_raw)
        return replace(cls(), db_path=db_path, sample_site_base=site,
                       sample_site_dir=site_dir, eval_date=eval_date)

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).resolve().parent
