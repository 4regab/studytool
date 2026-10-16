"""Web UI: server-rendered HTML pages plus a few JSON actions. Run with `uv run python run.py`.

Like the old Streamlit screen, this only submits jobs and reads saved state; the worker does
the processing. It listens on 127.0.0.1 and has no login, so every request is checked:
- Host must be a loopback name (stops DNS-rebinding pages from reading the app).
- Every POST needs the per-process CSRF token (header or form field) and, if the browser sends
  an Origin, it must be this app. Without this any website open in the same browser could
  approve cards or start paid AI jobs.
"""
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from studytool import ai, cards, config, db, documents, export, extract, generate, jobs, settings

HERE = Path(__file__).parent
CSRF_TOKEN = secrets.token_urlsafe(32)
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", config.HOST}

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["csrf_token"] = CSRF_TOKEN


def display_name(name: str) -> str:
    """'Book (z-library.sk, 1lib.sk).pdf' -> 'Book'. Only for display; stored names are unchanged."""
    stem = re.sub(r"\.pdf$", "", name or "", flags=re.I)
    stem = re.sub(r"\s*\([^()]*\b[\w-]+\.(?:sk|org|com|net|io|ru|is|li|me)\b[^()]*\)", "", stem)
    return re.sub(r"\s+", " ", stem).strip() or (name or "")


templates.env.filters["display"] = display_name

STATUSES = ["draft", "needs_review", "approved", "rejected"]
ITEM_TYPES = ["info_box", "mcq", "matching", "chronology", "statement", "assertion_reason"]
STAGES = [  # (job_steps stage, label shown to the user)
    ("extract", "Reading pages"), ("segment", "Finding questions"), ("link", "Matching answers"),
    ("generate", "Writing cards"), ("dedupe", "Checking duplicates"),
]
FLAG_TEXT = {
    "blank_field": "A field is empty.",
    "answer_code_leak": "The answer looks like an option code (e.g. \"(c)\" or \"BAC\").",
    "source_dependent_wording": "The wording refers to the original layout (\"List-I\", \"the above\").",
    "not_in_source": "Most words of the answer are not in the source text.",
    "too_long": "A field is very long.",
    "long_chronology": "The event list is longer than 8 items.",
    "same_front": "Another card from this question has the same front.",
}


# ---------- helpers ----------

@contextmanager
def connection():
    """One SQLite connection per request (connections are not shared across threads)."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def page(request: Request, name: str, **ctx) -> Response:
    return templates.TemplateResponse(request, name, ctx)


def parse_pages(text: str, page_count: int) -> list[int]:
    """'7-12, 22' -> [7..12, 22]. Raises ValueError with a message the user can act on."""
    pages: set[int] = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)|(\d+)", part)
        if not m:
            raise ValueError(f"'{part}' is not a page or range. Use e.g. 7-12, 22.")
        lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(3) is None else (int(m.group(3)),) * 2
        lo, hi = min(lo, hi), max(lo, hi)
        if lo < 1 or hi > page_count:
            raise ValueError(f"Pages must be between 1 and {page_count}.")
        pages.update(range(lo, hi + 1))
    return sorted(pages)


async def check_post(request: Request) -> dict:
    """Verify the CSRF token and return the parsed form ({} for JSON bodies)."""
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        raise HTTPException(403, "Request from another site refused.")
    token = request.headers.get("x-csrf-token")
    form: dict = {}
    if token is None and request.headers.get("content-type", "").startswith(
            ("multipart/form-data", "application/x-www-form-urlencoded")):
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large (limit 300 MB).")
        form = dict(await request.form(max_part_size=MAX_UPLOAD_BYTES))
        token = form.get("csrf")
    if not token or not secrets.compare_digest(str(token), CSRF_TOKEN):
        raise HTTPException(403, "Missing or wrong security token. Reload the page and try again.")
    return form


async def json_body(request: Request) -> dict:
    await check_post(request)
    try:
        return await request.json()
    except ValueError:
        return {}


def error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def get_deck(conn, deck_id: int) -> sqlite3.Row:
    deck = conn.execute(
        "SELECT d.*, doc.filename, doc.page_count FROM decks d JOIN documents doc ON doc.id = d.document_id "
        "WHERE d.id = ?", (deck_id,)).fetchone()
    if deck is None:
        raise HTTPException(404, "Deck not found.")
    return deck


def card_counts(conn, deck_id: int) -> dict:
    counts = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) n FROM cards WHERE deck_id = ? GROUP BY status", (deck_id,)):
        counts[r["status"]] = r["n"]
    counts["to_review"] = counts["draft"] + counts["needs_review"]
    counts["total"] = sum(counts[s] for s in STATUSES)
    return counts


def worker_alive(conn) -> bool:
    row = conn.execute(
        "SELECT (julianday('now') - julianday(heartbeat)) * 86400.0 AS age FROM worker_lease WHERE id = 1").fetchone()
    return row is not None and row["age"] is not None and row["age"] < config.WORKER_STALE_SECONDS


def _worker_status() -> bool:
    with connection() as conn:
        return worker_alive(conn)


templates.env.globals["worker_status"] = _worker_status
templates.env.globals["ai_info"] = lambda: {k: v for k, v in settings.active().items() if k != "key"} | {
    "has_key": bool(settings.active()["key"])}  # provider label/model for pages; never the key itself


def progress(conn, deck: sqlite3.Row) -> dict:
    """Everything the progress panel shows: stage bars with real totals, item outcomes and
    the reason for every blocked or skipped item."""
    job = jobs.latest_job(conn, deck["id"])
    settings = json.loads(deck["settings_json"] or "{}")
    deck_pages = sorted(set(settings.get("content_pages", [])) | set(settings.get("key_pages", [])))
    out = {"job": dict(job) if job else None, "worker_alive": worker_alive(conn), "stages": [],
           "pages": deck_pages, "content_pages": settings.get("content_pages", []),
           "key_pages": settings.get("key_pages", [])}
    if job is None:
        return out
    p = jobs.job_progress(conn, job["id"])
    steps = p["stages"]

    def done(stage):
        return steps.get(stage, {}).get("done", 0) + steps.get(stage, {}).get("skipped", 0)

    marks = ",".join("?" * len(deck_pages)) or "NULL"
    extracted = conn.execute(
        f"SELECT COUNT(*) FROM pages WHERE document_id = ? AND page_number IN ({marks}) "
        f"AND extraction_status IN ('done', 'needs_vision') AND cache_key LIKE ?",
        (deck["document_id"], *deck_pages, f"%|{config.EXTRACTOR_VERSION}")).fetchone()[0]
    to_generate = conn.execute(
        "SELECT COUNT(*) FROM source_items WHERE deck_id = ? AND disposition != 'skipped'", (deck["id"],)).fetchone()[0]
    totals = {"extract": (extracted, len(deck_pages)), "segment": (done("segment"), len(deck_pages)),
              "link": (done("link"), 1), "generate": (done("generate"), to_generate), "dedupe": (done("dedupe"), 1)}
    if job["kind"] == "regenerate":
        totals = {"generate": (done("generate"), 1)}
    for stage, label in STAGES:
        if stage not in totals:
            continue
        n, total = totals[stage]
        running = steps.get(stage, {}).get("running", 0) > 0
        failed = steps.get(stage, {}).get("failed", 0)
        out["stages"].append({"label": label, "done": min(n, total), "total": total, "running": running,
                              "failed": failed, "pct": 100 if total == 0 and n else round(100 * n / total) if total else 0})
    # One bar and one line for the user: the first unfinished step, and the average of all steps.
    current = next((s for s in out["stages"] if s["pct"] < 100), out["stages"][-1] if out["stages"] else None)
    out["current"] = current
    out["step_no"] = out["stages"].index(current) + 1 if current else 0
    out["pct"] = round(sum(s["pct"] for s in out["stages"]) / len(out["stages"])) if out["stages"] else 0

    outcomes = {d: 0 for d in ("converted", "duplicate", "blocked", "skipped", "pending")}
    for r in conn.execute("SELECT disposition, COUNT(*) n FROM source_items WHERE deck_id = ? GROUP BY disposition",
                          (deck["id"],)):
        outcomes[r["disposition"]] = r["n"]
    problems = conn.execute(
        "SELECT id, item_key, question_number, type, disposition, issue_reason, page_refs_json, raw_content "
        "FROM source_items WHERE deck_id = ? AND disposition IN ('blocked', 'skipped') ORDER BY ordinal",
        (deck["id"],)).fetchall()
    out.update(
        progress=p, outcomes=outcomes, cards=card_counts(conn, deck["id"]),
        active=job["status"] in ("queued", "running"),
        blocked=[_item_summary(r) for r in problems if r["disposition"] == "blocked"],
        skipped=[_item_summary(r) for r in problems if r["disposition"] == "skipped"],
    )
    return out


def _item_summary(r: sqlite3.Row) -> dict:
    pages = json.loads(r["page_refs_json"] or "[]")
    text = re.sub(r"\s+", " ", (r["raw_content"] or "").replace("*", "")).strip()
    return {"id": r["id"], "label": f"Q{r['question_number']}" if r["question_number"] else r["type"].replace("_", " "),
            "type": r["type"], "reason": r["issue_reason"] or "No reason recorded.", "pages": pages,
            "excerpt": text[:180] + ("…" if len(text) > 180 else "")}


def review_reasons(conn, card: sqlite3.Row) -> list[str]:
    reasons = [FLAG_TEXT.get(f, f) for f in json.loads(card["validation_flags_json"] or "[]")]
    if card["duplicate_of"]:
        reasons.append(f"Possible duplicate of card #{card['duplicate_of']}.")
    if card["item_issue_reason"]:
        reasons.append(f"Source issue: {card['item_issue_reason']}")
    if card["answer_source"] == "conflict":
        reasons.append("The source gives conflicting answers.")
    return reasons


def card_filters(params) -> dict:
    status = params.get("status", "to_review")
    if status not in (*STATUSES, "to_review", "all"):
        status = "to_review"
    item_type = params.get("type", "")
    pg = params.get("page", "")
    return {"status": status, "type": item_type if item_type in ITEM_TYPES else "",
            "page": pg if pg.isdigit() else ""}


CARD_SQL = (
    "SELECT c.*, si.type AS item_type, si.page_refs_json, si.issue_reason AS item_issue_reason, "
    "si.answer_source, si.question_number, si.document_id "
    "FROM cards c JOIN source_items si ON si.id = c.source_item_id "
)
CARD_ORDER = " ORDER BY (SELECT MIN(value) FROM json_each(si.page_refs_json)), si.ordinal, c.ordinal, c.id"


def filtered_cards(conn, deck_id: int, f: dict) -> list[sqlite3.Row]:
    where, args = ["c.deck_id = ?"], [deck_id]
    if f["status"] == "to_review":
        where.append("c.status IN ('draft', 'needs_review')")
    elif f["status"] != "all":
        where.append("c.status = ?")
        args.append(f["status"])
    if f["type"]:
        where.append("si.type = ?")
        args.append(f["type"])
    if f["page"]:
        where.append("EXISTS (SELECT 1 FROM json_each(si.page_refs_json) WHERE value = ?)")
        args.append(int(f["page"]))
    return conn.execute(CARD_SQL + "WHERE " + " AND ".join(where) + CARD_ORDER, args).fetchall()


def card_view(conn, card: sqlite3.Row) -> dict:
    """A card plus what the review screen shows next to it."""
    evidence = conn.execute(
        "SELECT e.page_number, e.excerpt, e.kind FROM card_evidence ce JOIN evidence e ON e.id = ce.evidence_id "
        "WHERE ce.card_id = ? ORDER BY e.id", (card["id"],)).fetchall()
    if not evidence:  # the model often links no evidence; show the item's evidence instead
        evidence = conn.execute("SELECT page_number, excerpt, kind FROM evidence WHERE source_item_id = ? ORDER BY id",
                                (card["source_item_id"],)).fetchall()
    reasons = review_reasons(conn, card)
    dup = None
    if card["duplicate_of"]:
        dup = conn.execute("SELECT id, front, back, status FROM cards WHERE id = ?", (card["duplicate_of"],)).fetchone()
    return {"card": dict(card), "pages": json.loads(card["page_refs_json"] or "[]"), "reasons": reasons,
            "evidence": [dict(e) for e in evidence], "duplicate": dict(dup) if dup else None}


# ---------- pages ----------

async def home(request: Request):
    with connection() as conn:
        docs = [dict(d) for d in conn.execute("SELECT * FROM documents ORDER BY id DESC")]
        for d in docs:
            d["decks"] = []
            for deck in conn.execute("SELECT * FROM decks WHERE document_id = ? ORDER BY id DESC", (d["id"],)):
                job = jobs.latest_job(conn, deck["id"])
                d["decks"].append({**dict(deck), "job": dict(job) if job else None,
                                   "cards": card_counts(conn, deck["id"])})
        return page(request, "home.html", docs=docs, worker_alive=worker_alive(conn), nav="home",
                    upload_error=request.query_params.get("error"))


async def upload(request: Request):
    form = await check_post(request)
    file = form.get("pdf")
    if file is None or not getattr(file, "filename", ""):
        return RedirectResponse("/?error=Choose+a+PDF+file+first.", status_code=303)
    data = await file.read()
    try:
        with connection() as conn:
            doc_id = await run_in_threadpool(documents.ingest_pdf, conn, Path(file.filename).name, data)
    except documents.IngestError as exc:
        return RedirectResponse(f"/?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


async def document_page(request: Request):
    doc_id = request.path_params["doc_id"]
    with connection() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc is None:
            raise HTTPException(404, "Document not found.")
        decks = [dict(d) | {"cards": card_counts(conn, d["id"])}
                 for d in conn.execute("SELECT * FROM decks WHERE document_id = ? ORDER BY id DESC", (doc_id,))]
        return page(request, "document.html", doc=dict(doc), decks=decks, nav="home",
                    form={"title": display_name(doc["filename"])[:80], "content": f"1-{min(5, doc['page_count'])}", "key": ""})


async def create_deck(request: Request):
    form = await check_post(request)
    doc_id = request.path_params["doc_id"]
    with connection() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc is None:
            raise HTTPException(404, "Document not found.")
        values = {"title": str(form.get("title", "")).strip(), "content": str(form.get("content", "")),
                  "key": str(form.get("key", ""))}
        try:
            content = parse_pages(values["content"], doc["page_count"])
            key = parse_pages(values["key"], doc["page_count"])
            if not content:
                raise ValueError("Enter at least one content page.")
            if not values["title"]:
                raise ValueError("Enter a deck title.")
        except ValueError as exc:
            decks = [dict(d) | {"cards": card_counts(conn, d["id"])}
                     for d in conn.execute("SELECT * FROM decks WHERE document_id = ?", (doc_id,))]
            return templates.TemplateResponse(request, "document.html", {
                "doc": dict(doc), "decks": decks, "form": values, "form_error": str(exc), "nav": "home"}, status_code=400)
        deck_id = jobs.create_deck(conn, doc_id, values["title"], content, [p for p in key if p not in content])
        jobs.submit_process_job(conn, deck_id)
    return RedirectResponse(f"/decks/{deck_id}", status_code=303)


async def page_image(request: Request):
    doc_id, n = request.path_params["doc_id"], request.path_params["page"]
    with connection() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None or not 1 <= n <= doc["page_count"]:
        raise HTTPException(404, "Page not found.")
    path = await run_in_threadpool(extract.render_page, Path(doc["local_path"]), n,
                                   config.PAGE_IMG_DIR / doc["content_hash"], 1.5)
    # The file name includes the PDF's content hash, so the image never changes.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=31536000, immutable"})


async def deck_progress(request: Request):
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        return page(request, "progress.html", deck=dict(deck), p=progress(conn, deck), tab="progress")


async def progress_panel(request: Request):
    """Just the progress panel, polled by app.js every 2 s while a job runs."""
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        return page(request, "_progress_panel.html", deck=dict(deck), p=progress(conn, deck))


async def deck_review(request: Request):
    params = request.query_params
    f = card_filters(params)
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        rows = filtered_cards(conn, deck["id"], f)
        ctx = {"deck": dict(deck), "tab": "review", "f": f, "total": len(rows),
               "counts": card_counts(conn, deck["id"]), "statuses": STATUSES, "types": ITEM_TYPES}
        query = "&".join(f"{k}={v}" for k, v in f.items() if v)
        ctx["query"] = query
        per = 50
        pages = max(1, (len(rows) + per - 1) // per)
        pg = min(max(int(params.get("p", "1")) if params.get("p", "1").isdigit() else 1, 1), pages)
        ctx.update(rows=[card_view(conn, r) for r in rows[(pg - 1) * per: pg * per]], p=pg, pages=pages)
        return page(request, "review_table.html", **ctx)


async def deck_export(request: Request):
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        pv = export.export_preview(conn, deck["id"])
        return page(request, "export.html", deck=dict(deck), tab="export", pv=pv, cards=card_counts(conn, deck["id"]),
                    export_error=request.query_params.get("error"))


async def export_file(request: Request):
    await check_post(request)
    kind = request.path_params["kind"]
    if kind not in ("apkg", "csv"):
        raise HTTPException(404, "Unknown export type.")
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        fn = export.export_apkg if kind == "apkg" else export.export_csv
        try:
            path = await run_in_threadpool(fn, conn, deck["id"])
        except ValueError as exc:
            return RedirectResponse(f"/decks/{deck['id']}/export?error={quote(str(exc))}", status_code=303)
    safe = re.sub(r"[^\w\- ]+", "", deck["title"]).strip()[:60] or f"deck{deck['id']}"
    media = "application/octet-stream" if kind == "apkg" else "text/csv; charset=utf-8"
    return FileResponse(path, media_type=media, filename=f"{safe}.{kind}")


# ---------- JSON actions (called by app.js) ----------

async def api_progress(request: Request):
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        p = progress(conn, deck)
        return JSONResponse({"active": p.get("active", False), "status": (p["job"] or {}).get("status")})


async def api_job(request: Request):
    await check_post(request)
    job_id, action = request.path_params["job_id"], request.path_params["action"]
    if action not in ("cancel", "retry"):
        return error("Unknown action.", 404)
    with connection() as conn:
        if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            return error("Job not found.", 404)
        if action == "cancel":
            jobs.request_cancel(conn, job_id)
            return JSONResponse({"ok": True, "message": "Cancel requested. A call already running may still finish."})
        new_id = jobs.retry_failed(conn, job_id)
        return JSONResponse({"ok": True, "job_id": new_id, "message": "Retry queued."})


async def api_card(request: Request):
    body = await json_body(request)
    card_id, action = request.path_params["card_id"], request.path_params["action"]
    with connection() as conn:
        try:
            if action == "edit":
                cards.edit_card(conn, card_id, str(body.get("front", "")), str(body.get("back", "")))
            elif action in ("approve", "reject", "draft"):
                status = {"approve": "approved", "reject": "rejected", "draft": "draft"}[action]
                cards.set_status(conn, card_id, status)
            elif action == "merge":
                row = conn.execute("SELECT duplicate_of FROM cards WHERE id = ?", (card_id,)).fetchone()
                if row is None or row["duplicate_of"] is None:
                    return error("This card is not marked as a duplicate.")
                cards.merge_duplicate(conn, row["duplicate_of"], card_id)
            elif action == "regenerate":
                row = conn.execute("SELECT source_item_id FROM cards WHERE id = ?", (card_id,)).fetchone()
                if row is None:
                    return error("Card not found.", 404)
                jobs.submit_regenerate_job(conn, row["source_item_id"])
                return JSONResponse({"ok": True, "message": "New cards for this question are being written. "
                                                            "They will appear as drafts."})
            else:
                return error("Unknown action.", 404)
        except ValueError as exc:
            return error(str(exc))
        row = conn.execute("SELECT status, front, back, revision FROM cards WHERE id = ?", (card_id,)).fetchone()
        return JSONResponse({"ok": True, "card": dict(row)})


async def api_bulk_approve(request: Request):
    """Approve the given cards, or every card still to review ({"all": true})."""
    body = await json_body(request)
    with connection() as conn:
        deck = get_deck(conn, request.path_params["deck_id"])
        if body.get("all"):
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM cards WHERE deck_id = ? AND status IN ('draft', 'needs_review')", (deck["id"],))]
        else:
            ids = [int(i) for i in body.get("card_ids", []) if str(i).isdigit()]
        approved = 0
        for cid in ids:
            if conn.execute("SELECT 1 FROM cards WHERE id = ? AND deck_id = ?", (cid, deck["id"])).fetchone() is None:
                continue
            try:
                cards.set_status(conn, cid, "approved")
                approved += 1
            except ValueError:  # blank front or back
                pass
        msg = f"Approved {approved} card{'s' if approved != 1 else ''}."
        if approved < len(ids):
            msg += f" {len(ids) - approved} have an empty side and were skipped."
        return JSONResponse({"ok": True, "approved": approved, "message": msg})


async def health(request: Request):
    return PlainTextResponse("ok")


# ---------- settings ----------

def _settings_ctx(saved: bool = False, error_msg: str | None = None, form: dict | None = None) -> dict:
    s = settings.load()
    providers = [{"id": name, **p, "has_key": bool(s["keys"][name]), "key_hint": s["keys"][name][-4:] if s["keys"][name] else "",
                  "from_env": bool(s["keys"][name]) and s["keys"][name] == os.environ.get(p["env"], "") and
                              name not in (settings._raw().get("keys") or {}),
                  "model": s["models"][name], "price": s["prices"][name]}
                 for name, p in settings.PROVIDERS.items()]
    return {"provider": (form or {}).get("provider", s["provider"]), "providers": providers,
            "card_prompt": s["card_prompt"] or generate.DEFAULT_CARD_PROMPT,
            "custom_prompt": bool(s["card_prompt"].strip()) and s["card_prompt"].strip() != generate.DEFAULT_CARD_PROMPT.strip(),
            "saved": saved, "settings_error": error_msg, "nav": "settings"}


async def settings_page(request: Request):
    return page(request, "settings.html", **_settings_ctx(saved=request.query_params.get("saved") == "1"))


async def save_settings(request: Request):
    form = await check_post(request)
    provider = str(form.get("provider", ""))
    if provider not in settings.PROVIDERS:
        return templates.TemplateResponse(request, "settings.html", _settings_ctx(error_msg="Choose a provider."),
                                          status_code=400)
    values: dict = {"provider": provider, "keys": {}, "models": {}, "prices": {}}
    prompt = str(form.get("card_prompt", "")).replace("\r\n", "\n").strip()
    try:
        for name in settings.PROVIDERS:
            key = str(form.get(f"key_{name}", "")).strip()
            if form.get(f"remove_key_{name}"):
                values["keys"][name] = None
            elif key:
                if not key.isascii() or any(c.isspace() for c in key) or len(key) > 400:
                    raise SettingsError(f"The {settings.PROVIDERS[name]['label']} key looks wrong: it has spaces or unusual characters. Paste it again.")
                values["keys"][name] = key
            model = str(form.get(f"model_{name}", "")).strip()
            if model:
                values["models"][name] = model[:200]
            prices = {}
            for side in ("in", "out"):
                raw = str(form.get(f"price_{side}_{name}", "")).strip().replace(",", ".")
                if raw:
                    try:
                        v = float(raw)
                    except ValueError:
                        raise SettingsError("Prices must be numbers, e.g. 0.30.") from None
                    if not 0 <= v < 10_000:
                        raise SettingsError("Prices must be between 0 and 10000 dollars per million tokens.")
                    prices[side] = v
            if prices:
                values["prices"][name] = prices
    except SettingsError as exc:  # show the form again with what the user typed (keys are never echoed)
        ctx = _settings_ctx(error_msg=str(exc), form={"provider": provider})
        ctx["card_prompt"] = prompt or ctx["card_prompt"]
        for p in ctx["providers"]:
            p["model"] = str(form.get(f"model_{p['id']}", "")).strip() or p["model"]
            p["price"] = {side: form.get(f"price_{side}_{p['id']}", p["price"][side]) for side in ("in", "out")}
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)
    values["card_prompt"] = "" if form.get("reset_prompt") or prompt == generate.DEFAULT_CARD_PROMPT.strip() else prompt[:20000]
    settings.save(values)
    return RedirectResponse("/settings?saved=1", status_code=303)


class SettingsError(ValueError):
    """A settings form value the user must fix; the message is shown on the page."""


class _Ping(BaseModel):
    ok: bool


async def api_test_settings(request: Request):
    """Send one tiny request with the saved settings, so the user knows the key and model work."""
    await check_post(request)
    s = settings.active()
    try:
        await run_in_threadpool(ai.call_json, "You check that an API connection works.",
                                'Reply with {"ok": true}.', _Ping, "settings-test", None, 0)
    except ai.AIError as exc:
        return error(str(exc)[:400])
    return JSONResponse({"ok": True, "message": f"{s['label']} works with model {s['model']}."})


# ---------- app ----------

class HostCheck:
    """Refuse requests whose Host header is not this computer (DNS-rebinding protection)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if name not in LOOPBACK_HOSTS:
                await PlainTextResponse("Unknown host.", status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or request.method == "POST" and \
            "application/json" in request.headers.get("content-type", ""):
        return error(exc.detail, exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "message": exc.detail},
                                      status_code=exc.status_code)


@asynccontextmanager
async def lifespan(app):
    with connection() as conn:
        db.migrate(conn)
    yield


app = Starlette(
    routes=[
        Route("/", home),
        Route("/health", health),
        Route("/settings", settings_page),
        Route("/settings", save_settings, methods=["POST"]),
        Route("/api/settings/test", api_test_settings, methods=["POST"]),
        Route("/documents", upload, methods=["POST"]),
        Route("/documents/{doc_id:int}", document_page),
        Route("/documents/{doc_id:int}/decks", create_deck, methods=["POST"]),
        Route("/documents/{doc_id:int}/pages/{page:int}.png", page_image),
        Route("/decks/{deck_id:int}", deck_progress),
        Route("/decks/{deck_id:int}/progress-panel", progress_panel),
        Route("/decks/{deck_id:int}/review", deck_review),
        Route("/decks/{deck_id:int}/export", deck_export),
        Route("/decks/{deck_id:int}/export.{kind:str}", export_file, methods=["POST"]),
        Route("/api/decks/{deck_id:int}/progress", api_progress),
        Route("/api/decks/{deck_id:int}/approve", api_bulk_approve, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/{action:str}", api_job, methods=["POST"]),
        Route("/api/cards/{card_id:int}/{action:str}", api_card, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ],
    middleware=[Middleware(HostCheck)],
    exception_handlers={HTTPException: http_error},
    lifespan=lifespan,
)
