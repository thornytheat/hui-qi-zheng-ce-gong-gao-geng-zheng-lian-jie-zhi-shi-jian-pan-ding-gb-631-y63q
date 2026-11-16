"""M8 CSV 序列化：与 M6 同一套 BOM + CRLF 约定（Excel 直开不乱码、回环可导入）。"""
from __future__ import annotations

import csv
import io


def to_csv(header: list[str], rows: list[list]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return buffer.getvalue()


def encode_csv(text: str) -> bytes:
    return text.encode("utf-8-sig")
