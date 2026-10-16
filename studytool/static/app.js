// Small progressive enhancements. Pages render on the server; this adds live progress,
// instant card actions, auto-saving edits and keyboard shortcuts.
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

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = "pointer-events-auto max-w-md rounded-md px-3 py-2 text-sm text-white shadow-lg " +
    (kind === "error" ? "bg-[#c4554d]" : "bg-[#37352f]");
  el.textContent = message;
  if (kind === "error") el.setAttribute("role", "alert");
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 2500);
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

const typing = (el) => el && (["TEXTAREA", "INPUT", "SELECT"].includes(el.tagName) || el.isContentEditable);

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
    if (btn) { btn.disabled = true; btn.textContent = form.dataset.busy; }
  });
});

// Upload starts as soon as a file is chosen, or dropped anywhere on the page.
const upload = document.querySelector("form[data-upload]");
if (upload) {
  const input = upload.querySelector("input[type=file]");
  const send = () => {
    if (!input.files.length) return;
    upload.querySelector("[data-upload-label]").textContent = `Uploading ${input.files[0].name}…`;
    upload.submit();
  };
  input.addEventListener("change", send);
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => {
    e.preventDefault();
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
    for (const part of pagesInput.value.split(",").map((s) => s.trim()).filter(Boolean)) {
      const m = part.match(/^(\d+)\s*[-–]\s*(\d+)$|^(\d+)$/);
      if (!m) { out.textContent = `"${part}" is not a page or range. Use e.g. 7-12, 22.`; out.classList.add("text-[#c4554d]"); return; }
      let [lo, hi] = m[3] ? [+m[3], +m[3]] : [+m[1], +m[2]];
      if (lo > hi) [lo, hi] = [hi, lo];
      if (lo < 1 || hi > max) { out.textContent = `Pages go from 1 to ${max}.`; out.classList.add("text-[#c4554d]"); return; }
      for (let p = lo; p <= hi; p++) pages.add(p);
    }
    out.classList.remove("text-[#c4554d]");
    out.textContent = pages.size
      ? `${pages.size} page${pages.size === 1 ? "" : "s"}${pages.size > 30 ? " · consider a smaller test first" : ""}`
      : "Start with a few pages to check quality.";
  };
  pagesInput.addEventListener("input", update);
  update();
}

// Page browser on the document page; the image loads only when opened.
const pager = document.querySelector("[data-pager]");
if (pager) {
  const input = pager.querySelector("input");
  const img = pager.querySelector("[data-preview]");
  const go = (n) => {
    n = Math.min(Math.max(1, n || 1), Number(pager.dataset.max));
    input.value = n;
    img.src = `/documents/${pager.dataset.doc}/pages/${n}.png`;
    img.alt = `Page ${n}`;
  };
  pager.closest("details").addEventListener("toggle", (e) => { if (e.target.open && !img.src) go(1); });
  input.addEventListener("change", () => go(Number(input.value)));
  pager.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => go(Number(input.value) + Number(b.dataset.step))));
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

// ---------- review: one card at a time ----------

const card = document.querySelector("[data-review-card]");
if (card) {
  const id = card.dataset.cardId;
  const front = card.querySelector('[data-field="front"]');
  const back = card.querySelector('[data-field="back"]');
  const saved = { front: front.value, back: back.value };
  let saving = Promise.resolve();

  // Edits save on their own: when a box loses focus, and before any action or move.
  function save() {
    if (front.value === saved.front && back.value === saved.back) return saving;
    const values = { front: front.value, back: back.value };
    saving = saving.then(() => post(`/api/cards/${id}/edit`, values)).then(() => {
      Object.assign(saved, values);
    });
    saving.catch((err) => toast(err.message, "error"));
    return saving;
  }
  [front, back].forEach((el) => el.addEventListener("blur", save));

  async function go(url) {
    if (!url) return;
    try { await save(); location.href = url; } catch { /* error already shown */ }
  }

  async function act(action) {
    try {
      await save();
      const data = await post(`/api/cards/${id}/${action}`);
      if (action === "regenerate") { toast(data.message); return; }
      location.href = card.dataset.next;
    } catch (err) { toast(err.message, "error"); }
  }

  card.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => act(b.dataset.action)));
  card.querySelectorAll("a[data-nav]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    if (!a.classList.contains("pointer-events-none")) go(a.href);
  }));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && typing(document.activeElement)) { document.activeElement.blur(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); act("approve"); return; }
    if (typing(document.activeElement) || e.ctrlKey || e.metaKey || e.altKey) return;
    const keys = {
      a: () => act("approve"), r: () => act("reject"), e: () => front.focus(),
      j: () => go(card.dataset.next), ArrowRight: () => go(card.dataset.next),
      k: () => go(card.dataset.prev), ArrowLeft: () => go(card.dataset.prev),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
  });

  card.querySelectorAll("[data-show-page]").forEach((b) => b.addEventListener("click", () => {
    card.querySelector("[data-page-img]").src = b.dataset.showPage;
    card.querySelector("[data-page-link]").href = b.dataset.showPage;
    card.querySelectorAll("[data-show-page]").forEach((x) => x.classList.toggle("bg-hover", x === b));
  }));
  // On phones the page image is long; start with it folded so the buttons stay near the card.
  if (window.matchMedia("(max-width: 1023px)").matches) card.querySelector("[data-page-details]")?.removeAttribute("open");
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
