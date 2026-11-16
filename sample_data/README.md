# sample_data

- `announcements_dirty.csv`：与 `sample_site/b/export.csv` 内容一致的脏数据副本，供离线核对与人工复现；两者走同一条解析路径（不存在“脏样例特判”）。
- 主要脏点：引号内换行、列数不齐、`2026-13-45` 坏时间、未知公告类型、中文数字截止、末行截断。
- 门户页面的原始字节（含 GB18030 页、截断页）在 `sample_site/` 下。
