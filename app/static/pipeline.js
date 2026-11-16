// pipeline.js —— 采集触发、阶段轮询、失败重试/续跑/幂等重跑、人工裁定提交。
// 只做请求与展示；阶段计数与判定全部来自服务端，前端不重算业务规则。

const state = {
  busy: false,
  batchId: document.body.dataset.batch || "",
  timer: null,
  polls: 0,
};

const el = (id) => document.getElementById(id);

function setHint(text, failed = false) {
  const hint = el("action-hint");
  if (!hint) return;
  hint.textContent = text;
  hint.classList.toggle("op-failed", Boolean(failed));
}

function stageSummary(payload) {
  const parts = (payload.stages || []).map(
    (s) => `${s.label} ${s.in_count}→${s.out_count}`,
  );
  const tail = payload.failed_stage ? `｜失败阶段：${payload.failed_stage}` : "";
  return parts.join(" · ") + tail;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || `HTTP ${response.status}`);
  }
  return data;
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function poll(batchId) {
  if (state.timer) clearTimeout(state.timer);
  state.polls += 1;
  state.timer = setTimeout(async () => {
    try {
      const data = await getJSON(`/api/crawl/${batchId}`);
      setHint(`运行中：${stageSummary(data)}`);
      if (data.status === "done" || data.status === "failed") {
        window.location.href = `/?batch_id=${batchId}`;
        return;
      }
    } catch (error) {
      setHint(`轮询失败：${error.message}`, true);
    }
    if (state.polls < 30) poll(batchId);
  }, 700);
}

async function startCrawl() {
  if (state.busy) return;
  state.busy = true;
  const button = el("btn-crawl");
  if (button) button.disabled = true;
  const source = el("source-select") ? el("source-select").value : "all";
  setHint(`已提交采集（${source}），正在读样例站页面…`);
  try {
    const data = await postJSON("/api/crawl", { source, mode: "full" });
    if (data.status === "done") {
      const replay = data.idempotent_replay
        ? `｜复用 ${data.batch_id}`
        : "";
      setHint(`${stageSummary(data)}${replay}`);
      window.location.href = `/?batch_id=${data.batch_id}`;
      return;
    }
    setHint(`失败：${data.error || "未知错误"}（可在批次历史续跑）`, true);
    window.location.href = `/?batch_id=${data.batch_id}`;
  } catch (error) {
    setHint(`采集失败：${error.message}`, true);
  } finally {
    state.busy = false;
    if (button) button.disabled = false;
  }
}

async function retryCrawl(batchId) {
  if (state.busy) return;
  state.busy = true;
  setHint("正在重试失败阶段…");
  try {
    const data = await postJSON(`/api/crawl/${batchId}/retry`, {});
    setHint(`重试结束：${stageSummary(data)}`, data.status === "failed");
    window.location.href = `/?batch_id=${data.batch_id}`;
  } catch (error) {
    setHint(`重试失败：${error.message}`, true);
  } finally {
    state.busy = false;
  }
}

async function rerun(batchId) {
  const hint = document.querySelector(".dialog-body pre");
  try {
    const data = await postJSON(`/api/batches/${batchId}/rerun`, {});
    const text = [
      `状态：${data.status}`,
      `批次：${data.batch_id}`,
      data.idempotent_replay ? "幂等命中：未新增任何行" : "新批次：第一批次键不同",
      `阶段：${stageSummary(data)}`,
    ].join("\n");
    if (hint) hint.textContent = text;
    setHint(data.idempotent_replay ? "复用" : "新批次");
  } catch (error) {
    setHint(`重跑失败：${error.message}`, true);
    if (hint) hint.textContent = `重跑失败：${error.message}`;
  }
}

async function resume(batchId) {
  try {
    const data = await postJSON(`/api/batches/${batchId}/resume`, {});
    surfaceResult(data, `续跑结束：${data.status}`);
    setHint(`续跑结束：${stageSummary(data)}`, data.status === "failed");
  } catch (error) {
    setHint(`续跑失败：${error.message}`, true);
  }
}

async function submitRuling() {
  const button = el("btn-ruling");
  const hint = el("ruling-hint");
  if (!button || !hint) return;
  const body = {
    doc_no: button.dataset.doc || "",
    seq: Number(el("ruling-seq") ? el("ruling-seq").value : 0),
    reviewer: el("ruling-reviewer") ? el("ruling-reviewer").value.trim() : "",
    basis: el("ruling-basis") ? el("ruling-basis").value.trim() : "",
  };
  hint.classList.remove("op-failed");
  hint.textContent = "提交中…";
  try {
    const data = await postJSON("/api/review/ruling", body);
    hint.textContent = `${data.code}：序号 ${data.seq} 已生效`;
    window.location.href = `/review?doc_no=${encodeURIComponent(body.doc_no)}`;
  } catch (error) {
    hint.textContent = `裁定被拒：${error.message}`;
    hint.classList.add("op-failed");
  }
}

function surfaceResult(payload, title) {
  const mask = el("op-result");
  const body = el("op-result-body");
  if (body) {
    body.textContent = [
      title,
      `批次：${payload.batch_id || "—"}`,
      `状态：${payload.status}`,
      payload.replay_of ? `复用自：${payload.replay_of}` : "",
      `阶段：${stageSummary(payload)}`,
      payload.error ? `错误：${payload.error}` : "",
    ]
      .filter(Boolean)
      .join("\n");
  }
  if (mask) mask.hidden = false;
}

function wire() {
  const crawl = el("btn-crawl");
  if (crawl) crawl.addEventListener("click", startCrawl);

  const rerunButton = el("btn-rerun");
  if (rerunButton) {
    rerunButton.addEventListener("click", () => {
      const batchId = rerunButton.dataset.batch;
      if (!batchId) return;
      const mask = el("op-result");
      if (mask) mask.hidden = false;
      rerun(batchId);
    });
  }

  document.querySelectorAll("[data-retry]").forEach((button) => {
    button.addEventListener("click", () => retryCrawl(button.dataset.retry));
  });
  document.querySelectorAll("[data-rerun-batch]").forEach((button) => {
    button.addEventListener("click", () => {
      const mask = el("op-result");
      if (mask) mask.hidden = false;
      rerun(button.dataset.rerunBatch);
    });
  });
  document.querySelectorAll("[data-resume]").forEach((button) => {
    button.addEventListener("click", () => resume(button.dataset.resume));
  });

  const rulingButton = el("btn-ruling");
  if (rulingButton) rulingButton.addEventListener("click", submitRuling);

  const closeOp = document.querySelector("[data-close-op]");
  if (closeOp) {
    closeOp.addEventListener("click", () => {
      const mask = el("op-result");
      if (mask) mask.hidden = true;
    });
  }

  if (state.batchId && document.body.dataset.page === "ingest") {
    setHint(`批次 ${state.batchId}`);
  }
}

wire();
