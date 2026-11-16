"""样例站的期望值：由人工按公文逐条推算后写死，测试断言与实现无关。"""

# 采信：门户 A 首页(跳过) + 列表 2 页 + 详情 2 页 + 门户 B 列表 2 页 + 附件 1 + 门户 C 列表 1 页
PAGES_TOTAL = 9
PAGES_OK = 8
PAGES_SKIPPED = 1
PAGES_FAILED = 0

# 表格数据行：A 6 + B 列表 5+5（含截断半行）+ B 附件 7 + C 10
ROWS_SEEN = 33
PARSED_OK = 28
PARSE_REJECTED = 5          # 截断半行 1 + 附件 4（坏时间/未知类型/中文数字截止/多一列）
QUALITY_MERGED = 1          # 门户 C 完整重复行
QUALITY_REJECTED = 3        # 哨兵整行 1 + 额度异常 1 + 日期越界 1
ACCEPTED = 24
CHAINS = 19
LISTED = 13
AMOUNT_TOTAL = "5890.00"    # 可申报清单合计（万元，Decimal 2 位）

# 逐个公文号的链判定（状态 / 当前截止 / 最终额度 万元）
CHAIN_37 = {"status": "ACTIVE", "deadline": "2026-12-15", "amount": "1500.00", "versions": 3}
CHAIN_41 = {"status": "VOIDED", "deadline": "2026-11-05", "amount": "350.00", "versions": 2}
CHAIN_29_EXPIRED = {"status": "ACTIVE", "deadline": "2026-10-20", "amount": "300.00"}
CHAIN_12 = {"status": "ACTIVE", "deadline": "2026-11-20", "amount": "1200.00"}
CHAIN_18 = {"status": "ACTIVE", "deadline": "2026-12-01", "amount": "860.00"}
CHAIN_21_CONFLICT = "澄科技发〔2026〕21号"
CHAIN_08_NO_ORIGIN = "澄企服〔2026〕08号"
CHAIN_11_INVERSION = "澄企服〔2026〕11号"

# 工作日推算：发布日 2026-09-28 + 30 个工作日 = 2026-11-13
# 期间扣除国庆 7 天（10-01..10-07），计入调休上班日 10-10（周六）
WORKDAY_30_DEADLINE = "2026-11-13"
WORKDAY_15_DEADLINE = "2026-11-05"      # 发布日 2026-10-15 + 15 个工作日
WORKDAY_30_FROM_0901 = "2026-10-20"     # 发布日 2026-09-01 + 30 个工作日（跨中秋国庆）
MAKEUP_DAY = "2026-10-10"

# 编码与截断
GB18030_PAGE = "/b/list_p1.html"
BOM_PAGE_DECLARED = "utf-8"
TRUNCATED_PAGE = "/b/list_p2.html"
TRUNCATED_CSV = "/b/export.csv"
QUOTED_NEWLINE_CSV_RECORDS = 7          # 引号内换行不能把一条记录切成两条

REASON_CODES_EXPECTED = {
    "E_CHARSET_MISMATCH", "Q_COLUMN_COUNT", "Q_BAD_TIME", "Q_KIND_UNKNOWN",
    "Q_BAD_DEADLINE_RULE", "Q_SENTINEL_ONLY", "Q_AMOUNT_SUSPECT", "Q_DATE_SUSPECT",
    "Q_DUP_ROW", "C_DUP_SEQ_CONFLICT", "C_NO_ORIGIN", "C_TIME_INVERSION",
}
