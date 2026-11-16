// tables.js —— 表格排序与就地过滤。只影响展示顺序，不改动任何数据。

const state = { sort: { table: null, index: -1, dir: 1 } };

function cellValue(row, index) {
  const cell = row.children[index];
  if (!cell) return "";
  return cell.textContent.trim();
}

function numeric(text) {
  const cleaned = text.replace(/[,\s，万元%]/g, "");
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : null;
}

function sortTable(table, index, dir) {
  const body = table.tBodies[0];
  if (!body) return;
  const rows = Array.from(body.rows);
  const probe = numeric(cellValue(rows[0] || document.createElement("tr"), index));
  rows.sort((a, b) => {
    const left = cellValue(a, index);
    const right = cellValue(b, index);
    if (probe !== null) {
      const lv = numeric(left);
      const rv = numeric(right);
      if (lv !== null && rv !== null) return (lv - rv) * dir;
    }
    return left.localeCompare(right, "zh-Hans-CN") * dir;
  });
  rows.forEach((row) => body.appendChild(row));
}

function enableSort() {
  document.querySelectorAll("table.grid").forEach((table) => {
    const head = table.tHead;
    if (!head || !head.rows.length) return;
    Array.from(head.rows[0].cells).forEach((th, index) => {
      if (th.classList.contains("num") === false && index > 6) return;
      th.dataset.sort = "1";
      th.setAttribute("tabindex", "0");
      th.addEventListener("click", () => {
        const same = state.sort.table === table && state.sort.index === index;
        state.sort = { table, index, dir: same ? -state.sort.dir : 1 };
        sortTable(table, index, state.sort.dir);
      });
      th.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          th.click();
        }
      });
    });
  });
}

function enableQuickFilter() {
  const tables = document.querySelectorAll("table.grid");
  tables.forEach((table) => {
    if (table.rows.length > 22) {
      const input = document.createElement("input");
      input.type = "search";
      input.className = "quick-filter";
      input.placeholder = `在 ${table.rows.length - 1} 行里查找`;
      input.setAttribute("aria-label", "快速过滤");
      input.style.cssText =
        "margin:0 0 8px;padding:5px 9px;font:inherit;font-size:12px;border:1px solid var(--line-strong);border-radius:3px;min-width:240px";
      table.parentElement.parentElement.insertBefore(input, table.parentElement);
      input.addEventListener("input", () => {
        const needle = input.value.trim().toLowerCase();
        Array.from(table.tBodies[0].rows).forEach((row) => {
          row.hidden = needle !== "" && !row.textContent.toLowerCase().includes(needle);
        });
      });
    }
  });
}

enableSort();
enableQuickFilter();
