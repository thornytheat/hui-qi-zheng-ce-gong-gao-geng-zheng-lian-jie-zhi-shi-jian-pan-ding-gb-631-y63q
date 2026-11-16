"""编码探测与回退链（TRAP-01/02）。

原则：**不信任页面声明**。声明只用来排第一个候选，是否可用由严格解码决定；
GB18030 是 GBK/GB2312 的超集，统一归到它。UTF-8 BOM 在解码前剥离，
但保留 bom_stripped 标记，页面与导出都能看到这条信息。
"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

from ... import config

BOM_UTF8 = codecs.BOM_UTF8
_META_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
_CONTENT_TYPE_RE = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


@dataclass(frozen=True)
class Decoded:
    text: str
    encoding: str
    declared: str | None
    bom_stripped: bool
    attempts: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    detail: str = ""


def canon_charset(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().strip('"').strip("'").lower()
    if key in config.CHARSET_ALIASES:
        return config.CHARSET_ALIASES[key]
    if key.startswith("gb"):
        return "gb18030"
    if key.startswith("utf"):
        return "utf-8"
    return key


def extract_declared(raw: bytes, content_type: str | None = None) -> str | None:
    """HTTP 头优先，其次页面 meta；只在前 4KB 里找，避免扫大文件。"""
    if content_type:
        m = _CONTENT_TYPE_RE.search(content_type)
        if m:
            return canon_charset(m.group(1))
    m = _META_CHARSET_RE.search(raw[:4096])
    if m:
        return canon_charset(m.group(1).decode("ascii", "ignore"))
    return None


def candidate_encodings(declared: str | None) -> tuple[str, ...]:
    order: list[str] = []
    if declared:
        order.append(declared)
    for enc in config.ENCODING_CHAIN:
        if enc not in order:
            order.append(enc)
    return tuple(order)


def decode_bytes(raw: bytes, *, content_type: str | None = None,
                 declared: str | None = None) -> Decoded:
    declared = declared or extract_declared(raw, content_type)
    has_bom = raw.startswith(BOM_UTF8)
    payload = raw[len(BOM_UTF8):] if has_bom else raw
    attempts: list[str] = []
    for enc in candidate_encodings(declared):
        try:
            text = payload.decode(enc)
        except (UnicodeDecodeError, LookupError) as exc:
            attempts.append(f"{enc}=fail:{type(exc).__name__}")
            continue
        attempts.append(f"{enc}=ok")
        codes: list[str] = []
        if has_bom:
            codes.append("E_BOM_STRIPPED")
        if declared and canon_charset(declared) != canon_charset(enc):
            codes.append("E_CHARSET_MISMATCH")
        detail = ""
        if "E_CHARSET_MISMATCH" in codes:
            detail = f"声明 {declared} 解码失败或不可信，实际按 {enc} 解出"
        elif has_bom:
            detail = "首字节为 EF BB BF，已剥离后再解码"
        return Decoded(text=text, encoding=enc, declared=declared,
                       bom_stripped=has_bom, attempts=tuple(attempts),
                       codes=tuple(codes), detail=detail)
    return Decoded(
        text="", encoding="", declared=declared, bom_stripped=has_bom,
        attempts=tuple(attempts), codes=("E_DECODE_FAILED",),
        detail="回退链全部失败：" + ", ".join(attempts[-3:]),
    )
