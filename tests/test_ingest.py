"""M1 采信与抓取：编码回退、BOM、引号内换行、列数、截断、分页。"""
from __future__ import annotations

from decimal import Decimal

from app import config
from app.modules.m1_ingest import decode, paging, reader, service
from fixtures import expected as X


def _page(run, suffix: str) -> dict:
    return next(p for p in run["pages"] if p["url"].endswith(suffix))


def test_charset_fallback_decodes_gb18030(site_dir):
    """TRAP-01：页面声明 utf-8 但字节是 GB18030，必须回退且中文列名可读。"""
    raw = (site_dir / "b/list_p1.html").read_bytes()
    decoded = decode.decode_bytes(raw, content_type="text/html")
    assert decoded.encoding == "gb18030"
    assert "E_CHARSET_MISMATCH" in decoded.codes
    doc = reader.parse_html(decoded.text, "http://sample-site/b/list_p1.html")
    table = reader.pick_table(doc)
    assert "公文号" in table.headers or "文号" in table.headers


def test_charset_alias_normalizes_gb2312(site_dir):
    """gb2312 是 gb18030 的子集，声明归一后与实际编码一致就不再报不匹配。"""
    raw = (site_dir / "b/list_p2.html").read_bytes()
    decoded = decode.decode_bytes(raw, content_type="text/html")
    assert decoded.declared == "gb18030"
    assert decoded.encoding == "gb18030"


def test_mismatch_code_recorded_on_page(run):
    """TRAP-01：页面级诊断保留原因码，页面上能直接看到这一页没有按声明解码。"""
    page = _page(run, X.GB18030_PAGE)
    assert page["code"] == "E_CHARSET_MISMATCH"
    assert page["encoding"] == "gb18030"
    assert page["declared_charset"] == "utf-8"


def test_bom_stripped_before_header_mapping(tmp_path):
    """TRAP-02：UTF-8 BOM 出现在文件头，剥离后首个列名不能带 U+FEFF。"""
    body = "\ufeff文号,标题,公告类型,公告序号,发布时间\n澄企服〔2026〕01号,示例,首次发布,1,2026-11-01\n"
    decoded = decode.decode_bytes(body.encode("utf-8"))
    assert decoded.bom_stripped is True
    assert "E_BOM_STRIPPED" in decoded.codes
    csv_doc = reader.parse_csv(decoded.text)
    assert csv_doc.headers[0] == "文号"
    assert not csv_doc.headers[0].startswith("\ufeff")


def test_self_exported_csv_reads_back(site_dir, tmp_path):
    """TRAP-02 回环：本系统导出的 CSV 带 BOM，导回同一解析路径必须可用。"""
    from app.modules.m6_export import csv_out
    text = csv_out.to_csv(["文号", "资助额度"], [["澄企服〔2026〕02号", "120.00"]])
    path = tmp_path / "roundtrip.csv"
    path.write_bytes(csv_out.encode_csv(text))
    decoded = decode.decode_bytes(path.read_bytes())
    assert decoded.bom_stripped is True
    assert decoded.text.splitlines()[0].strip("\ufeff").startswith("文号")


def test_quoted_newline_keeps_record_count(site_dir):
    """TRAP-03：引号内含换行，逻辑记录数必须是 7 条而不是 8 条。"""
    raw = (site_dir / "b/export.csv").read_bytes()
    decoded = decode.decode_bytes(raw)
    csv_doc = reader.parse_csv(decoded.text)
    assert len(csv_doc.rows) == X.QUOTED_NEWLINE_CSV_RECORDS
    quoted = next(r for r in csv_doc.rows if "产业基础再造" in r[1])
    assert quoted[5] == "自发布之日起 30个工作日"


def test_naive_splitlines_breaks_records(site_dir):
    """反例：按 splitlines 切行会把一条带换行的记录算成两条，与 csv 结果不同。"""
    text = (site_dir / "b/export.csv").read_text(encoding="utf-8")
    naive = [line for line in text.splitlines()[1:] if line.strip()]
    csv_rows = reader.parse_csv(text).rows
    assert len(naive) != len(csv_rows)


def test_truncated_page_detected(run):
    """TRAP-16：表格未闭合的页面被标记为截断，半行按列数不齐隔离。"""
    page = _page(run, X.TRUNCATED_PAGE)
    assert "E_PAGE_TRUNCATED" in page["codes"] or page["code"] == "E_PAGE_TRUNCATED"
    refs = [q["row_ref"] for q in run["quarantine_rows"] if q["code"] == "Q_COLUMN_COUNT"]
    assert any(ref.startswith("b/list_p2.html") for ref in refs)


def test_truncated_csv_detected(run, site_dir):
    """TRAP-16：附件声明 8 条但末行被截断，解析器要给出不完整结论而不是静默接受。"""
    page = _page(run, X.TRUNCATED_CSV)
    assert page["code"] == "E_PAGE_TRUNCATED"
    decoded = decode.decode_bytes((site_dir / "b/export.csv").read_bytes())
    csv_doc = reader.parse_csv(decoded.text)
    assert csv_doc.truncated is True
    assert csv_doc.declared_rows == 8
    assert len(csv_doc.rows) == 7
    assert "end of data" in (csv_doc.error or "")


def test_paging_follows_rel_next(site_dir, settings):
    """分页：门户 A 通过 rel=next 翻到第 2 页，并补抓 2 个详情页。"""
    result = paging.crawl_portal(reader.FileFetcher(settings.sample_site_dir), config.PORTALS[0],
                                 settings)
    urls = [p.url for p in result.pages]
    assert any("list_p2.html" in url for url in urls)
    assert sum(1 for p in result.pages if p.kind == "detail") == 2
    assert len(result.records) == 6


def test_missing_rel_next_is_recorded(site_dir, settings):
    """分页断点：门户 C 页脚声明 2 页却没有 rel=next，必须记录断点而不是假装抓完。"""
    result = paging.crawl_portal(reader.FileFetcher(settings.sample_site_dir), config.PORTALS[2],
                                 settings)
    codes = [d["code"] for d in result.diagnostics]
    assert "E_PAGING_NO_NEXT" in codes


def test_page_account_identity(run):
    """页账守恒：抓取页数 = 公告页 + 非公告跳过页 + 采集失败页。"""
    ok = sum(1 for p in run["pages"] if p["status"] == "ok")
    skipped = sum(1 for p in run["pages"] if p["status"] == "skipped")
    failed = sum(1 for p in run["pages"] if p["status"] == "failed")
    assert ok == X.PAGES_OK
    assert skipped == X.PAGES_SKIPPED
    assert failed == X.PAGES_FAILED
    assert len(run["pages"]) == X.PAGES_TOTAL == ok + skipped + failed


def test_log_lines_have_encoding_and_code(run):
    """证据层：运行日志每行带编码与原因码，可直接定位页面。"""
    lines = service.rows_to_log(run["pages"])
    assert any("gb18030" in line for line in lines)
    assert any("E_PAGE_TRUNCATED" in line for line in lines)
