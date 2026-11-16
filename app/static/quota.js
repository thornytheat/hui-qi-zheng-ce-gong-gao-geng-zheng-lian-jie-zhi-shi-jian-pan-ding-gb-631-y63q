// quota.js —— 额度收件箱续跑按钮；只发请求并展示结果，判定全部在服务端。

async function postJSON(url) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
  return data;
}

function wire() {
  const button = document.querySelector("[data-quota-resume]");
  if (!button) return;
  button.addEventListener("click", async () => {
    const mask = document.getElementById("op-result");
    const body = document.getElementById("op-result-body");
    button.disabled = true;
    try {
      const data = await postJSON("/api/quota/resume");
      if (body) {
        body.textContent = [
          `状态：${data.status}`,
          `运行：${data.run_id || "—"}`,
          `处理事件：${data.processed || 0}（幂等跳过 ${data.skipped || 0}）`,
          `版本通过/拒绝：${data.versions_applied || 0} / ${data.versions_rejected || 0}`,
          `受理/挂起：${data.receipts_applied || 0} / ${data.receipts_pending || 0}`,
          `挂起续匹配：${data.resolved || 0}`,
          `剩余 pending：${data.pending_left}`,
          data.note || "",
        ].join("\n");
      }
      if (mask) mask.hidden = false;
      setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      const hint = document.getElementById("quota-hint");
      if (hint) hint.textContent = `续跑失败：${error.message}`;
    } finally {
      button.disabled = false;
    }
  });

  const closeOp = document.querySelector("[data-close-op]");
  if (closeOp) {
    closeOp.addEventListener("click", () => {
      const mask = document.getElementById("op-result");
      if (mask) mask.hidden = true;
    });
  }
}

wire();
