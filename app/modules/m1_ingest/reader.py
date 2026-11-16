"""采信层读写：HTTP/文件取数、HTML 表格与分页解析、附件 CSV 流式解析。

三种输入形态共用同一套记录结构（RawRec）：
- 列表页表格（含详情链接）→ 再抓详情页补字段；
- 密集表格页（一页含全部字段）；
- 附件 CSV（导出表格）。
解析只做「结构」，不做业务归一：字段名到契约列的映射是 M2 的事。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ... import config
from . import decode

_WS_RE = re.compile(r"[\s\u00a0\u3000]+")
_DECLARED_ROWS_RE = re.compile(r"共\s*(\d+)\s*条")
_DECLARED_PAGE_ROWS_RE = re.compile(r"本页\s*(\d+)\s*条")
_DECLARED_PAGES_RE = re.compile(r"共\s*(\d+)\s*页")
_MIME = {".html": "text/html", ".htm": "text/html", ".csv": "text/csv",
         ".json": "application/json", ".txt": "text/plain"}


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


class FileFetcher:
    """把样例站 URL 映射到仓库里的 sample_site 目录，测试与本地演示用。"""

    def __init__(self, root: str | Path, base: str = "http://sample-site"):
        self.root = Path(root)
        self.base = base.rstrip("/")

    def get(self, url: str) -> Fetched:
        path = urlsplit(url).path.lstrip("/")
        target = (self.root / path)
        if not target.is_file():
            return Fetched(url=url, status=404, body=b"", error=f"文件不存在：{path}")
        body = target.read_bytes()
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        return Fetched(url=url, status=200, body=body, headers={"content-type": ctype})


class HttpFetcher:
    """容器内访问样例站：只允许 sample-site 主机，禁止宿主机地址。"""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def get(self, url: str) -> Fetched:
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            return Fetched(url=url, status=0, body=b"", error=f"{type(exc).__name__}: {exc}")
        return Fetched(url=url, status=resp.status_code, body=resp.content,
                       headers={k.lower(): v for k, v in resp.headers.items()})


class FailingFetcher:
    """测试用：指定 URL 返回 500，其余委托给内部 fetcher。"""

    def __init__(self, inner, failing: set[str]):
        self.inner = inner
        self.failing = set(failing)

    def get(self, url: str) -> Fetched:
        if url in self.failing:
            return Fetched(url=url, status=500, body=b"", error="注入的采信失败")
        return self.inner.get(url)


def collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


@dataclass
class Table:
    index: int
    headers: list[str]
    rows: list[list[str]]
    row_links: list[list[str]]
    has_th: bool

    @property
    def header_count(self) -> int:
        return len(self.headers)


@dataclass
class PageDoc:
    tables: list[Table]
    next_href: str | None
    meta_charset: str | None
    declared_rows: int | None
    declared_page_rows: int | None
    declared_pages: int | None
    unclosed_table: bool
    title: str
    text: str


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self.next_href: str | None = None
        self.unclosed_table = False
        self.title = ""
        self.text_parts: list[str] = []
        self._depth = 0
        self._cur: list[list[str]] | None = None
        self._cur_links: list[list[str]] | None = None
        self._th_flags: list[bool] = []
        self._row: list[str] | None = None
        self._row_links: list[list[str]] | None = None
        self._cell: list[str] | None = None
        self._in_title = False

    # -- 结构 --
    def handle_starttag(self, tag, attrs):
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            if self._depth == 0:
                self._cur, self._cur_links, self._th_flags = [], [], []
            self._depth += 1
        elif tag == "tr" and self._depth == 1:
            self._row, self._row_links = [], []
        elif tag in ("td", "th") and self._depth == 1 and self._row is not None:
            self._cell = []
            self._th_flags.append(tag == "th")
        elif tag == "a":
            href = attributes.get("href", "")
            if attributes.get("rel", "").lower() == "next":
                self.next_href = self.next_href or href
            if href and self._row_links is not None:
                self._row_links.append(href)
        elif tag == "link":
            if "next" in attributes.get("rel", "").lower().split():
                self.next_href = self.next_href or attributes.get("href", "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1 and self._cur is not None:
                self.tables.append(self._close_table())
            self._depth = max(0, self._depth - 1)
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(collapse("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cur is not None:
                self._cur.append(self._row)
                self._cur_links.append(list(self._row_links or []))
            self._row = None
            self._row_links = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._cell is not None:
            self._cell.append(data)
        self.text_parts.append(data)

    def close(self):  # type: ignore[override]
        super().close()
        if self._depth > 0:
            self.unclosed_table = True
            if self._cell is not None and self._row is not None:
                self._row.append(collapse("".join(self._cell)))
                self._cell = None
            if self._row is not None and self._cur is not None:
                self._cur.append(self._row)
                self._cur_links.append(list(self._row_links or []))
                self._row = None
            if self._cur is not None:
                self.tables.append(self._close_table())
                self._depth = 0

    def _close_table(self) -> Table:
        rows = self._cur or []
        links = self._cur_links or []
        th_rows = [i for i, flag in enumerate(self._th_flags) if flag]
        has_th = bool(th_rows)
        if has_th:
            headers = rows[0]
            body = rows[1:]
            body_links = links[1:]
        else:
            headers = rows[0] if rows else []
            body = rows[1:] if rows else []
            body_links = links[1:] if links else []
        table = Table(index=len(self.tables), headers=headers, rows=body,
                      row_links=body_links, has_th=has_th)
        self._th_flags = []
        return table


def parse_html(text: str, base_url: str) -> PageDoc:
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    from urllib.parse import urljoin
    next_href = urljoin(base_url, parser.next_href) if parser.next_href else None
    body_text = collapse(" ".join(parser.text_parts))
    declared_rows = _DECLARED_ROWS_RE.search(body_text)
    declared_page_rows = _DECLARED_PAGE_ROWS_RE.search(body_text)
    declared_pages = _DECLARED_PAGES_RE.search(body_text)
    return PageDoc(
        tables=parser.tables,
        next_href=next_href,
        meta_charset=decode.extract_declared(text.encode("utf-8", "ignore")),
        declared_rows=int(declared_rows.group(1)) if declared_rows else None,
        declared_page_rows=(int(declared_page_rows.group(1)) if declared_page_rows else None),
        declared_pages=int(declared_pages.group(1)) if declared_pages else None,
        unclosed_table=parser.unclosed_table,
        title=collapse(parser.title),
        text=body_text,
    )


def pick_table(doc: PageDoc) -> Table | None:
    """挑表格：优先有 th 表头且行数最多的那张。"""
    if not doc.tables:
        return None
    candidates = [t for t in doc.tables if t.headers and t.rows]
    if not candidates:
        return None
    return max(candidates, key=lambda t: (t.has_th, len(t.rows), len(t.headers)))


def field_value_pairs(doc: PageDoc) -> dict[str, str]:
    """详情页：两列「字段/值」表 → dict。取行数最多的两列表。"""
    best: dict[str, str] = {}
    for table in doc.tables:
        pairs: dict[str, str] = {}
        two_col = 0
        for row in table.rows:
            if len(row) == 2:
                two_col += 1
                if row[0]:
                    pairs[row[0]] = row[1]
        if len(table.rows) and two_col / len(table.rows) >= 0.6 and len(pairs) > len(best):
            best = pairs
    return best


@dataclass
class CsvDoc:
    headers: list[str]
    rows: list[list[str]]
    line_numbers: list[int]
    declared_rows: int | None
    truncated: bool
    error: str | None = None


def parse_csv(text: str) -> CsvDoc:
    """流式解析附件 CSV：引号内换行（TRAP-03）交给 csv 模块，不 splitlines。"""
    declared: int | None = None
    buffer = io.StringIO()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            m = _DECLARED_ROWS_RE.search(stripped)
            if m:
                declared = int(m.group(1))
            continue
        buffer.write(line + "\n")
    buffer.seek(0)
    reader = csv.reader(buffer, strict=True)
    headers: list[str] = []
    rows: list[list[str]] = []
    line_numbers: list[int] = []
    truncated = False
    error: str | None = None
    try:
        for record in reader:
            if not headers:
                headers = [collapse(c) for c in record]
                continue
            if not any(cell.strip() for cell in record):
                continue
            rows.append([collapse(c) for c in record])
            line_numbers.append(reader.line_num)
    except csv.Error as exc:
        truncated = True
        error = f"csv.Error at line {reader.line_num}: {exc}"
    if declared is not None and len(rows) < declared:
        truncated = True
        error = error or f"声明 {declared} 条，实得 {len(rows)} 条"
    return CsvDoc(headers=headers, rows=rows, line_numbers=line_numbers,
                  declared_rows=declared, truncated=truncated, error=error)
