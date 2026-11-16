"""CSV 输出：清单与隔离区，金额带 2 位小数与单位，行号与原因码一并导出。

统一写 UTF-8 BOM + CRLF（Excel 直接双击打开不乱码）；本项目自己的导入端
在采信层剥离 BOM，所以「自己导出的文件再导入」也走得通（TRAP-02 回环）。
"""
from __future__ import annotations

import csv
import io


def to_csv(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return buffer.getvalue()


def encode_csv(text: str) -> bytes:
    return text.encode("utf-8-sig")


POLICY_HEADER = ["公文号", "标题", "状态", "当前截止", "截止说明", "资助额度(万元)",
                 "版本数", "来源门户", "说明"]
QUARANTINE_HEADER = ["原因码", "级别", "阶段", "门户", "来源页面", "证据行", "字段", "原值", "说明"]
CHAIN_HEADER = ["公文号", "序号", "类型", "发布时间(+08:00)", "截止说明", "资助额度(万元)",
                "更正事项", "更正后", "门户", "证据行", "处理说明"]
