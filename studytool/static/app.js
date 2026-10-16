// Small progressive enhancements. Pages render on the server; this adds live progress,
// instant card actions, auto-saving edits, the upload drop zone and the settings page.
"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;

async function post(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
  return data;
}

// Toasts rise in (CSS @starting-style) and fade out before removal. Timers pause while the tab is hidden.
function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " toast-error" : "");
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  const text = document.createElement("span");
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "Close";
  close.setAttribute("aria-label", "Dismiss");
  el.append(text, close);
  const box = document.getElementById("toasts");
  while (box.children.length >= 3) box.firstElementChild.remove();
  box.append(el);

  let left = kind === "error" ? 6000 : 3000, started = Date.now(), timer;
  const dismiss = () => {
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVis);
    el.dataset.leaving = "";
    el.addEventListener("transitionend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 300); // in case no transition runs (reduced motion)
  };
  const run = () => { started = Date.now(); timer = setTimeout(dismiss, left); };
  const onVis = () => {
    if (document.hidden) { clearTimeout(timer); left -= Date.now() - started; } else run();
  };
  document.addEventListener("visibilitychange", onVis);
  close.addEventListener("click", dismiss);
  run();
}

const TAGS = {
  draft: ["tag-gray", "To review"], needs_review: ["tag-orange", "Check"],
  approved: ["tag-green", "Approved"], rejected: ["tag-red", "Rejected"],
};
function tag(status) {
  const [cls, name] = TAGS[status] || TAGS.draft;
  const el = document.createElement("span");
  el.className = `tag ${cls}`;
  el.textContent = name;
  return el;
}

// Text boxes grow with their content, like text on a page.
function autosize(el) { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; }
document.querySelectorAll("[data-autosize]").forEach((el) => {
  autosize(el);
  el.addEventListener("input", () => autosize(el));
});
window.addEventListener("resize", () => document.querySelectorAll("[data-autosize]").forEach(autosize));

// ---------- forms ----------

document.querySelectorAll("form[data-busy]").forEach((form) => {
  form.addEventListener("submit", () => {
    const btn = form.querySelector("button:not([type=button])");
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner" aria-hidden="true"></span>${form.dataset.busy}`; }
  });
});

// Downloads keep the page; say what happens next.
document.querySelectorAll("form[data-download]").forEach((form) => {
  form.addEventListener("submit", () => toast(form.action.endsWith(".apkg")
    ? "Download started. Open the .apkg file to add the deck to Anki."
    : "Download started. Import it in Anki with File › Import."));
});

// Upload starts as soon as a file is chosen, or dropped anywhere on the page.
const upload = document.querySelector("form[data-upload]");
if (upload) {
  const input = upload.querySelector("input[type=file]");
  const send = () => {
    if (!input.files.length) return;
    const label = upload.querySelector("[data-upload-label]");
    label.textContent = `Uploading ${input.files[0].name}…`;
    label.closest("label").classList.add("pointer-events-none", "opacity-80");
    upload.submit();
  };
  input.addEventListener("change", send);
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  let depth = 0; // dragenter/dragleave fire for every child element
  const dragging = (on) => document.documentElement.toggleAttribute("data-dragging", on);
  document.addEventListener("dragenter", (e) => { if (hasFiles(e)) { depth++; dragging(true); } });
  document.addEventListener("dragleave", (e) => { if (hasFiles(e) && --depth <= 0) { depth = 0; dragging(false); } });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0; dragging(false);
    const file = [...e.dataTransfer.files].find((f) => f.name.toLowerCase().endsWith(".pdf"));
    if (!file) { toast("Drop a PDF file.", "error"); return; }
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    send();
  });
}

document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
  form.querySelectorAll("select, input").forEach((el) => el.addEventListener("change", () => form.requestSubmit()));
});

// "7-12, 22" -> live count of selected pages on the new-deck form.
const pagesInput = document.querySelector("[data-page-count]");
if (pagesInput) {
  const max = Number(pagesInput.dataset.pageCount);
  const out = document.querySelector("[data-page-summary]");
  const update = () => {
    const pages = new Set();
    const bad = (msg) => { out.textContent = msg; out.classList.add("text-bad"); pagesInput.setAttribute("aria-invalid", "true"); };
    for (const part of pagesInput.value.split(",").map((s) => s.trim()).filter(Boolean)) {
      const m = part.match(/^(\d+)\s*[-–]\s*(\d+)$|^(\d+)$/);
      if (!m) { bad(`"${part}" is not a page or range. Use e.g. 7-12, 22.`); return; }
      let [lo, hi] = m[3] ? [+m[3], +m[3]] : [+m[1], +m[2]];
      if (lo > hi) [lo, hi] = [hi, lo];
      if (lo < 1 || hi > max) { bad(`Pages go from 1 to ${max}.`); return; }
      for (let p = lo; p <= hi; p++) pages.add(p);
    }
    out.classList.remove("text-bad");
    pagesInput.removeAttribute("aria-invalid");
    out.textContent = pages.size
      ? `${pages.size} page${pages.size === 1 ? "" : "s"} selected${pages.size > 30 ? " · consider a smaller test first" : ""}`
      : "Ranges and single pages, e.g. 7-16, 22. Start small to check quality.";
  };
  pagesInput.addEventListener("input", update);
  update();
}

// Page browser on the document page. "Add page" appends the page shown to the Pages field.
const pager = document.querySelector("[data-pager]");
if (pager) {
  const input = pager.querySelector("input");
  const img = pager.querySelector("[data-preview]");
  const link = pager.querySelector("[data-preview-link]");
  const go = (n) => {
    n = Math.min(Math.max(1, n || 1), Number(pager.dataset.max));
    input.value = n;
    const src = `/documents/${pager.dataset.doc}/pages/${n}.png`;
    img.src = src; link.href = src;
    img.alt = `Page ${n}`;
    pager.querySelector("[data-add-page-n]").textContent = n;
  };
  input.addEventListener("change", () => go(Number(input.value)));
  pager.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => go(Number(input.value) + Number(b.dataset.step))));
  pager.querySelector("[data-add-page]")?.addEventListener("click", () => {
    if (!pagesInput) return;
    const n = input.value;
    const parts = pagesInput.value.split(",").map((s) => s.trim()).filter(Boolean);
    if (!parts.includes(n)) parts.push(n);
    pagesInput.value = parts.join(", ");
    pagesInput.dispatchEvent(new Event("input"));
    toast(`Page ${n} added to Pages.`);
  });
}

// ---------- progress ----------

const panel = document.getElementById("progress-panel");
async function refreshPanel() {
  const res = await fetch(panel.dataset.poll);
  if (!res.ok) return false;
  panel.innerHTML = await res.text();
  return panel.querySelector("[data-panel-active]")?.dataset.panelActive === "1";
}
if (panel && panel.dataset.active === "1") {
  const timer = setInterval(async () => {
    try { if (!(await refreshPanel())) clearInterval(timer); } catch { /* try again next tick */ }
  }, 2000);
}
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-post]");
  if (!btn) return;
  btn.disabled = true;
  try {
    const data = await post(btn.dataset.post);
    toast(data.message || "Done.");
    if (btn.hasAttribute("data-reload")) setTimeout(() => location.reload(), 500);
    else if (panel) await refreshPanel();
  } catch (err) { toast(err.message, "error"); btn.disabled = false; }
});

// ---------- settings ----------

const settingsForm = document.querySelector("form[data-settings]");
if (settingsForm) {
  // Show only the chosen provider's key and model fields.
  const show = () => {
    const chosen = settingsForm.querySelector("[data-provider-radio]:checked")?.value;
    settingsForm.querySelectorAll("[data-provider-panel]").forEach((p) => { p.hidden = p.dataset.providerPanel !== chosen; });
  };
  settingsForm.querySelectorAll("[data-provider-radio]").forEach((r) => r.addEventListener("change", show));
  settingsForm.querySelector("[data-test-settings]").addEventListener("click", async (e) => {
    const btn = e.currentTarget, label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>Testing…';
    try { toast((await post("/api/settings/test")).message); } catch (err) { toast(err.message, "error"); }
    btn.disabled = false; btn.innerHTML = label;
  });
}

// ---------- review: table ----------

const table = document.querySelector("[data-table]");
if (table) {
  const bulkSelected = document.querySelector('[data-bulk="selected"]');
  const countLabel = document.querySelector("[data-selected-count]");
  const totalText = countLabel.textContent;
  const selected = () => [...table.querySelectorAll("[data-select]:checked")].map((c) => c.closest("[data-row]").dataset.cardId);
  const syncBulk = () => {
    const n = selected().length;
    bulkSelected.classList.toggle("hidden", !n);
    countLabel.textContent = n ? `${n} selected` : totalText;
  };

  // Cell edits save when the cell loses focus.
  table.addEventListener("focusout", async (e) => {
    if (!e.target.matches("[data-field]") || e.target.value === e.target.defaultValue) return;
    const row = e.target.closest("[data-row]");
    const f = row.querySelector('[data-field="front"]'), b = row.querySelector('[data-field="back"]');
    try {
      const data = await post(`/api/cards/${row.dataset.cardId}/edit`, { front: f.value, back: b.value });
      f.defaultValue = f.value; b.defaultValue = b.value;
      row.querySelector("[data-status]").replaceChildren(tag(data.card.status));
    } catch (err) { toast(err.message, "error"); }
  });
  table.addEventListener("change", (e) => { if (e.target.matches("[data-select], [data-select-all]")) {
    if (e.target.matches("[data-select-all]")) table.querySelectorAll("[data-select]").forEach((c) => { c.checked = e.target.checked; });
    syncBulk();
  } });

  table.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const row = btn.closest("[data-row]");
    try {
      const data = await post(`/api/cards/${row.dataset.cardId}/${btn.dataset.action}`);
      row.querySelector("[data-status]").replaceChildren(tag(data.card.status));
    } catch (err) { toast(err.message, "error"); }
  });

  document.querySelectorAll("[data-bulk]").forEach((btn) => btn.addEventListener("click", async () => {
    const body = btn.dataset.bulk === "all" ? { all: true } : { card_ids: selected() };
    btn.disabled = true;
    try {
      const data = await post(`/api/decks/${table.dataset.deck}/approve`, body);
      toast(data.message);
      setTimeout(() => location.reload(), 700);
    } catch (err) { toast(err.message, "error"); btn.disabled = false; }
  }));
}
