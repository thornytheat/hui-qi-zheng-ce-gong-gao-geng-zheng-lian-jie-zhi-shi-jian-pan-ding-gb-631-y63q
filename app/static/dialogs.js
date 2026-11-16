// dialogs.js —— 证据对话框、打印、键盘可达性。Esc 关闭由 <dialog> 原生行为保证。

const state = { dialog: null };

function fillEvidence(button) {
  const dialog = document.getElementById("evidence-dialog");
  if (!dialog) return;
  const map = {
    "ev-code": button.dataset.code,
    "ev-level": button.dataset.level,
    "ev-ref": button.dataset.ref,
    "ev-value": button.dataset.value || "—",
    "ev-detail": button.dataset.detail,
  };
  Object.entries(map).forEach(([id, value]) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value || "—";
  });
  dialog.showModal();
}

function wireDialogs() {
  document.querySelectorAll("[data-evidence]").forEach((button) => {
    button.addEventListener("click", () => fillEvidence(button));
  });
  document.querySelectorAll("[data-print]").forEach((button) => {
    button.addEventListener("click", () => window.print());
  });
  const mask = document.getElementById("op-result");
  if (mask) {
    mask.addEventListener("click", (event) => {
      if (event.target === mask) mask.hidden = true;
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      const open = document.getElementById("op-result");
      if (open && !open.hidden) open.hidden = true;
    }
  });
}

wireDialogs();
