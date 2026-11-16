"""采信服务：按来源选门户、造 fetcher、汇总页级诊断与运行日志。"""
from __future__ import annotations

from ... import config, errors
from . import paging, reader

SOURCE_ALIASES = {
    "all": "全部门户",
    "portal_a": "江阴区工信局",
    "portal_b": "江南市科技局",
    "portal_c": "园区企服中心",
}


def make_fetcher(settings, *, offline: bool = True):
    """容器内走 http://sample-site/；本地/测试走 sample_site 目录，同一套解析。"""
    if offline:
        return reader.FileFetcher(settings.sample_site_dir, base=settings.sample_site_base)
    return reader.HttpFetcher(timeout=settings.http_timeout)


def select_portals(source: str) -> tuple[dict, ...]:
    if source in (None, "", "all"):
        return config.PORTALS
    selected = tuple(p for p in config.PORTALS if p["id"] == source)
    if not selected:
        raise errors.BadRequest(
            f"未知来源 {source!r}；可选 all / " + " / ".join(p["id"] for p in config.PORTALS),
            code="E_BAD_SOURCE")
    return selected


def ingest(settings, *, source: str = "all", fetcher=None) -> paging.CrawlBundle:
    portals = select_portals(source)
    fetcher = fetcher or make_fetcher(settings)
    return paging.crawl_all(fetcher, portals, settings)


def page_diagnostics(bundle: paging.CrawlBundle) -> list[dict]:
    rows = []
    for page in bundle.pages:
        rows.append({
            "url": page.url, "portal": page.portal, "kind": page.kind,
            "status": page.status, "http_status": page.http_status,
            "encoding": page.encoding, "declared": page.declared_charset,
            "bytes": page.bytes, "sha256": page.sha256[:12],
            "rows": page.row_count, "code": page.code,
            "note": page.note,
        })
    for diag in bundle.diagnostics:
        rows.append({"url": diag["url"], "portal": "-", "kind": "paging",
                     "status": "warn", "code": diag["code"], "note": diag["note"],
                     "rows": 0, "bytes": 0, "sha256": "", "encoding": None,
                     "declared": None, "http_status": None})
    return rows


def log_lines(bundle: paging.CrawlBundle) -> list[str]:
    lines: list[str] = []
    for page in bundle.pages:
        flag = page.status.upper()
        lines.append(
            f"[{page.page_no:02d}] {flag:<7} {page.url} "
            f"编码={page.encoding or '-'} 字节={page.bytes} 行={page.row_count}"
            + (f" 码={page.code}" if page.code else "")
            + (f" 备注={page.note}" if page.note else ""))
    for diag in bundle.diagnostics:
        lines.append(f"[--] WARN    {diag['url']} 码={diag['code']} {diag['note']}")
    return lines


def rows_to_log(pages: list[dict], diagnostics: list[dict] | None = None) -> list[str]:
    """由已落库的页面行重建运行日志尾（页面与导出读同一份数据）。"""
    lines: list[str] = []
    for page in pages:
        lines.append(
            f"[{page.get('page_no', 0):02d}] {page['status'].upper():<7} {page['url']} "
            f"编码={page.get('encoding') or '-'} 字节={page.get('bytes') or 0} "
            f"行={page.get('row_count') or 0}"
            + (f" 码={page['code']}" if page.get("code") else "")
            + (f" 备注={page['note']}" if page.get("note") else ""))
    for diag in diagnostics or []:
        lines.append(f"[--] WARN    {diag['url']} 码={diag['code']} {diag['note']}")
    return lines


def page_diagnostics_rows(pages: list[dict], diagnostics: list[dict] | None = None) -> list[dict]:
    """已落库页面 → 诊断表行（页面诊断与日志共用同一份数据）。"""
    rows = []
    for page in pages:
        rows.append({
            "url": page["url"], "portal": page["portal"], "kind": page["kind"],
            "status": page["status"], "http_status": page.get("http_status"),
            "encoding": page.get("encoding"), "declared": page.get("declared_charset"),
            "bytes": page.get("bytes") or 0, "sha256": (page.get("sha256") or "")[:12],
            "rows": page.get("row_count") or 0, "code": page.get("code"),
            "note": page.get("note") or "",
        })
    for diag in diagnostics or []:
        rows.append({"url": diag["url"], "portal": "-", "kind": "paging",
                     "status": "warn", "code": diag["code"], "note": diag["note"],
                     "rows": 0, "bytes": 0, "sha256": "", "encoding": None,
                     "declared": None, "http_status": None})
    return rows
