"""采信编排：站点体检 → 列表页翻页 → 详情页补字段 → 附件 CSV。

边界与失败都是显式的：
- 分页：只认 rel=next；页脚声明多页但缺链接时记录 E_PAGING_NO_NEXT 断点；
  next 指回已访问页面记 E_PAGING_LOOP 并停止；
- 截断：表格未闭合或「本页 N 条」与实际行数不符记 E_PAGE_TRUNCATED；
- 详情页先补字段再合并，同一字段以详情页为准（列表页常写「详见正文」）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from ... import config
from . import decode, reader

DETAIL_LIMIT = 8


@dataclass
class PageRec:
    url: str
    portal: str
    kind: str
    page_no: int
    status: str            # ok | skipped | failed
    http_status: int | None = None
    encoding: str | None = None
    declared_charset: str | None = None
    bytes: int = 0
    sha256: str = ""
    row_count: int = 0
    code: str | None = None
    note: str = ""
    codes: list[str] = field(default_factory=list)


@dataclass
class RawRec:
    portal: str
    source_url: str
    row_ref: str
    header_count: int
    cell_count: int
    fields: dict[str, str]
    cells: list[str]
    page_url: str
    note: str = ""


@dataclass
class PortalCrawl:
    pages: list[PageRec] = field(default_factory=list)
    records: list[RawRec] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


@dataclass
class CrawlBundle:
    pages: list[PageRec] = field(default_factory=list)
    records: list[RawRec] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    digests: list[str] = field(default_factory=list)

    def counters(self) -> dict:
        ok = sum(1 for p in self.pages if p.status == "ok")
        skipped = sum(1 for p in self.pages if p.status == "skipped")
        failed = sum(1 for p in self.pages if p.status == "failed")
        return {
            "pages": len(self.pages),
            "pages_ok": ok,
            "pages_skipped": skipped,
            "pages_failed": failed,
            "records": len(self.records),
            "bytes": sum(p.bytes for p in self.pages),
        }


def _abs(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _row_ref(url: str, index: int) -> str:
    return f"{urlsplit(url).path.lstrip('/')}#r{index}"


def _page_rec(url: str, portal: str, kind: str, page_no: int, fetched,
              decoded=None, **kwargs) -> PageRec:
    return PageRec(
        url=url, portal=portal, kind=kind, page_no=page_no,
        status=kwargs.pop("status", "ok"),
        http_status=fetched.status if fetched else None,
        encoding=(decoded.encoding if decoded else None),
        declared_charset=(decoded.declared if decoded else None),
        bytes=len(fetched.body) if fetched else 0,
        sha256=hashlib.sha256(fetched.body).hexdigest() if fetched else "",
        **kwargs,
    )


def crawl_portal(fetcher, portal: dict, settings, page_limit: int = config.MAX_PAGES_PER_PORTAL,
                 skip_urls: set[str] | None = None) -> PortalCrawl:
    base = settings.sample_site_base
    out = PortalCrawl()
    visited: set[str] = set()
    detail_cache: dict[str, dict[str, str]] = {}
    detail_pages = 0
    skip_urls = skip_urls or set()

    # 站点体检：门户首页不含公告表格，正常跳过并计入「非公告跳过」
    home = portal.get("home")
    if home:
        url = _abs(base, home)
        visited.add(url)
        fetched = fetcher.get(url)
        if fetched.ok:
            decoded = decode.decode_bytes(fetched.body,
                                          content_type=fetched.headers.get("content-type"))
            out.pages.append(_page_rec(url, portal["id"], "home", 0, fetched, decoded,
                                       status="skipped", code="S_NO_ANNOUNCEMENT",
                                       note="门户首页，无公告表格"))
        else:
            out.pages.append(_page_rec(url, portal["id"], "home", 0, fetched,
                                       status="failed", code="E_HTTP_STATUS",
                                       note=fetched.error or f"HTTP {fetched.status}"))

    queue = [(_abs(base, seed), 1) for seed in portal["seeds"]]
    page_no = 0
    while queue:
        url, declared_page = queue.pop(0)
        if url in visited:
            out.diagnostics.append({"code": "E_PAGING_LOOP", "url": url,
                                    "note": "next 指回已访问页面，停止翻页"})
            continue
        visited.add(url)
        page_no += 1
        if page_no > page_limit:
            out.diagnostics.append({"code": "E_PAGING_LIMIT", "url": url,
                                    "note": f"单门户翻页上限 {page_limit}"})
            break
        fetched = fetcher.get(url)
        if not fetched.ok:
            out.pages.append(_page_rec(url, portal["id"], "list", page_no, fetched,
                                       status="failed", code="E_HTTP_STATUS",
                                       note=fetched.error or f"HTTP {fetched.status}"))
            continue
        decoded = decode.decode_bytes(fetched.body,
                                      content_type=fetched.headers.get("content-type"))
        if "E_DECODE_FAILED" in decoded.codes:
            out.pages.append(_page_rec(url, portal["id"], "list", page_no, fetched, decoded,
                                       status="failed", code="E_DECODE_FAILED",
                                       note=decoded.detail))
            continue
        doc = reader.parse_html(decoded.text, url)
        table = reader.pick_table(doc)
        page_codes = list(decoded.codes)
        page_code = page_codes[0] if page_codes else None
        if table is None:
            out.pages.append(_page_rec(url, portal["id"], "list", page_no, fetched, decoded,
                                       status="skipped", code="S_NO_ANNOUNCEMENT",
                                       codes=page_codes, note="列表页无公告表格"))
            continue
        note_bits: list[str] = []
        if doc.unclosed_table:
            page_codes.append("E_PAGE_TRUNCATED")
            note_bits.append("表格未闭合")
        if doc.declared_page_rows is not None and len(table.rows) < doc.declared_page_rows:
            page_codes.append("E_PAGE_TRUNCATED")
            note_bits.append(f"本页声明 {doc.declared_page_rows} 条，实得 {len(table.rows)} 条")
        row_count = 0
        for index, row in enumerate(table.rows, start=1):
            if not any(cell.strip() for cell in row):
                continue
            row_count += 1
            cells = list(row)
            fields = {table.headers[i]: cells[i]
                      for i in range(min(len(table.headers), len(cells)))}
            detail_fields: dict[str, str] = {}
            links = table.row_links[index - 1] if index - 1 < len(table.row_links) else []
            detail_url = _pick_detail(links, url, portal)
            if detail_url and detail_pages < DETAIL_LIMIT:
                if detail_url not in detail_cache:
                    detail_pages += 1
                    rec = fetcher.get(detail_url)
                    if rec.ok:
                        ddec = decode.decode_bytes(
                            rec.body, content_type=rec.headers.get("content-type"))
                        ddoc = reader.parse_html(ddec.text, detail_url)
                        pairs = reader.field_value_pairs(ddoc)
                        detail_cache[detail_url] = pairs
                        out.pages.append(_page_rec(
                            detail_url, portal["id"], "detail", page_no, rec, ddec,
                            row_count=len(pairs), codes=list(ddec.codes),
                            note=f"详情页补 {len(pairs)} 个字段"))
                    else:
                        detail_cache[detail_url] = {}
                        out.pages.append(_page_rec(
                            detail_url, portal["id"], "detail", page_no, rec,
                            status="failed", code="E_HTTP_STATUS",
                            note=rec.error or f"HTTP {rec.status}"))
                detail_fields = detail_cache[detail_url]
            merged = {**fields, **{k: v for k, v in detail_fields.items() if v.strip()}}
            if detail_fields:
                note_bits.append(f"详情补字段 {len(detail_fields)}")
            out.records.append(RawRec(
                portal=portal["id"], source_url=url, row_ref=_row_ref(url, index),
                header_count=len(table.headers), cell_count=len(cells),
                fields=merged, cells=cells, page_url=url))
        out.pages.append(_page_rec(url, portal["id"], "list", page_no, fetched, decoded,
                                   row_count=row_count, code=page_code,
                                   codes=page_codes, note="；".join(note_bits)))
        # 分页推进
        if doc.declared_pages and doc.declared_pages > declared_page:
            if not doc.next_href:
                out.diagnostics.append({
                    "code": "E_PAGING_NO_NEXT", "url": url,
                    "note": f"页脚声明共 {doc.declared_pages} 页、当前第 {declared_page} 页，"
                            f"但缺少 rel=next，第 {declared_page + 1} 页无法自动续抓"})
            elif doc.next_href not in visited:
                queue.append((doc.next_href, declared_page + 1))

    attachment = portal.get("attachment")
    if attachment:
        url = _abs(base, attachment)
        fetched = fetcher.get(url)
        if not fetched.ok:
            out.pages.append(_page_rec(url, portal["id"], "attachment", page_no, fetched,
                                       status="failed", code="E_HTTP_STATUS",
                                       note=fetched.error or f"HTTP {fetched.status}"))
        else:
            decoded = decode.decode_bytes(fetched.body,
                                          content_type=fetched.headers.get("content-type"))
            csv_doc = reader.parse_csv(decoded.text)
            codes = list(decoded.codes)
            note = f"附件 {len(csv_doc.rows)} 条"
            if csv_doc.truncated:
                codes.append("E_PAGE_TRUNCATED")
                note += f"；{csv_doc.error or '末行被截断'}"
            for index, row in enumerate(csv_doc.rows):
                line = csv_doc.line_numbers[index] if index < len(csv_doc.line_numbers) else 0
                cells = list(row)
                fields = {csv_doc.headers[i]: cells[i]
                          for i in range(min(len(csv_doc.headers), len(cells)))}
                out.records.append(RawRec(
                    portal=portal["id"], source_url=url,
                    row_ref=f"{urlsplit(url).path.lstrip('/')}#L{line}",
                    header_count=len(csv_doc.headers), cell_count=len(cells),
                    fields=fields, cells=cells, page_url=url, note="附件 CSV"))
            out.pages.append(_page_rec(url, portal["id"], "attachment", page_no, fetched,
                                       decoded, row_count=len(csv_doc.rows),
                                       code=codes[0] if codes else None, codes=codes,
                                       note=note))
    return out


def _pick_detail(links: list[str], base_url: str, portal: dict) -> str | None:
    for link in links:
        target = urljoin(base_url, link)
        path = urlsplit(target).path
        if not path.endswith((".html", ".htm")):
            continue
        if "/detail" not in path:
            continue
        return target
    return None


def crawl_all(fetcher, portals, settings) -> CrawlBundle:
    bundle = CrawlBundle()
    for portal in portals:
        result = crawl_portal(fetcher, portal, settings)
        bundle.pages.extend(result.pages)
        bundle.records.extend(result.records)
        bundle.diagnostics.extend(result.diagnostics)
    bundle.digests = sorted(p.sha256 for p in bundle.pages if p.sha256)
    return bundle
