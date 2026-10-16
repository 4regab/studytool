"""Streamlit UI for PDF to Flashcards. OWNER: agent D.

Four screens: Documents, Processing, Review, Export. The UI only submits jobs and
reads persisted state; it never runs generation itself and never starts a worker.
Start the worker separately with `uv run python run.py`.
"""
import json
import re
import sqlite3
from pathlib import Path

import streamlit as st

from studytool import cards, config, db, documents, export, extract, jobs

st.set_page_config(page_title="PDF to Flashcards", layout="wide")


# ---------- shared helpers ----------

@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    db.migrate(conn)
    return conn


def worker_banner(conn: sqlite3.Connection) -> None:
    """Warn if no live worker is heartbeating, per config.WORKER_STALE_SECONDS."""
    row = conn.execute(
        "SELECT (julianday('now') - julianday(heartbeat)) * 86400.0 AS age FROM worker_lease WHERE id = 1"
    ).fetchone()
    alive = row is not None and row["age"] is not None and row["age"] < config.WORKER_STALE_SECONDS
    if not alive:
        st.warning(
            "No worker appears to be running, so queued jobs will not process. "
            "Start one with `uv run python run.py` in a separate terminal."
        )


def safe_call(fn, *args, **kwargs):
    """Call a module function; show a visible error instead of crashing the app.
    Other agents' modules are still stubs (NotImplementedError) in places."""
    try:
        return True, fn(*args, **kwargs)
    except NotImplementedError:
        st.error(f"'{fn.__module__}.{fn.__name__}' is not implemented yet.")
        return False, None
    except Exception as exc:
        st.error(f"{fn.__module__}.{fn.__name__} failed: {exc}")
        return False, None


def parse_page_list(text: str) -> list[int]:
    """Parse '25-27, 30' into [25, 26, 27, 30]. Blank input -> []."""
    text = (text or "").strip()
    if not text:
        return []
    pages: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            pages.extend(range(lo, hi + 1))
        elif part.isdigit():
            pages.append(int(part))
        else:
            raise ValueError(f"Could not parse page range '{part}'. Use e.g. '25-27, 30'.")
    return sorted(set(pages))


def list_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, filename, page_count, created_at FROM documents ORDER BY created_at DESC"
    ).fetchall()


def list_decks(conn: sqlite3.Connection, document_id: int | None = None) -> list[sqlite3.Row]:
    if document_id is None:
        return conn.execute("SELECT id, title, document_id FROM decks ORDER BY id DESC").fetchall()
    return conn.execute(
        "SELECT id, title, document_id FROM decks WHERE document_id = ? ORDER BY id DESC", (document_id,)
    ).fetchall()


# ---------- Documents screen ----------

def screen_documents(conn: sqlite3.Connection) -> None:
    st.header("Documents")
    worker_banner(conn)

    st.subheader("Upload a PDF")
    uploaded = st.file_uploader("Choose a study PDF", type="pdf", key="pdf_uploader")
    if uploaded is not None and st.button("Ingest this PDF", key="ingest_button"):
        ok, doc_id = safe_call(documents.ingest_pdf, conn, uploaded.name, uploaded.getvalue())
        if ok:
            st.success(f"Stored '{uploaded.name}' as document {doc_id}.")
            st.rerun()

    st.subheader("Document history")
    docs = list_documents(conn)
    if not docs:
        st.info("No documents uploaded yet.")
        return

    doc_labels = {d["id"]: f"#{d['id']} {d['filename']} ({d['page_count']} pages, {d['created_at']})" for d in docs}
    selected_doc_id = st.selectbox(
        "Select a document to process",
        options=list(doc_labels.keys()),
        format_func=lambda i: doc_labels[i],
        key="doc_select",
    )
    doc = next(d for d in docs if d["id"] == selected_doc_id)

    st.subheader("Pages to process")
    # Default to a small pilot range: processing a whole book costs real money (PRD 11).
    all_pages = st.checkbox("Use all pages as content", value=False, key="use_all_pages")
    if all_pages:
        content_pages = list(range(1, doc["page_count"] + 1))
        st.caption(f"Content pages: 1-{doc['page_count']}")
    else:
        col1, col2 = st.columns(2)
        from_page = col1.number_input("From page", min_value=1, max_value=doc["page_count"], value=1, key="from_page")
        to_page = col2.number_input("To page", min_value=1, max_value=doc["page_count"], value=min(doc["page_count"], 5), key="to_page")
        content_pages = list(range(min(from_page, to_page), max(from_page, to_page) + 1))

    key_pages_text = st.text_input(
        "Answer-key pages (optional), e.g. '25-27, 30'",
        key="key_pages_text",
        help="Pages with answers outside the content range. These supply evidence only; they never generate their own cards.",
    )
    try:
        key_pages = parse_page_list(key_pages_text)
    except ValueError as exc:
        st.error(str(exc))
        return

    deck_title = st.text_input("Deck title", value=doc["filename"].rsplit(".", 1)[0], key="deck_title")

    st.info(
        "The text (and page images for scanned pages) of the selected pages will be sent to "
        "DeepSeek for processing. Consider a small pilot range first to check quality and cost."
    )

    # Guard against double submit: disable while a job for this deck's most recent run is active.
    decks_for_doc = list_decks(conn, selected_doc_id)
    active_job = None
    for d in decks_for_doc:
        j = jobs.latest_job(conn, d["id"])
        if j is not None and j["status"] in ("queued", "running"):
            active_job = j
            break

    disabled = active_job is not None
    if disabled:
        st.caption("A job for this document is already queued or running. Check the Processing screen.")

    if st.button("Process", key="process_button", disabled=disabled):
        if not content_pages:
            st.error("Select at least one content page.")
            return
        if not deck_title.strip():
            st.error("Enter a deck title.")
            return
        ok, deck_id = safe_call(jobs.create_deck, conn, selected_doc_id, deck_title.strip(), content_pages, key_pages)
        if ok:
            ok2, job_id = safe_call(jobs.submit_process_job, conn, deck_id)
            if ok2:
                st.success(f"Queued processing job {job_id} for deck {deck_id}. See the Processing screen.")
                st.rerun()


# ---------- Processing screen ----------

@st.fragment(run_every=2)
def _progress_panel(conn: sqlite3.Connection, job_id: int) -> None:
    ok, progress = safe_call(jobs.job_progress, conn, job_id)
    if not ok:
        return

    st.write(f"Status: **{progress['status']}**" + (" (cancel requested)" if progress["cancel_requested"] else ""))
    if not progress["worker_alive"]:
        st.warning("No live worker detected. Start one with `uv run python run.py`.")
    if progress["error"]:
        st.error(f"Job error: {progress['error']}")

    st.subheader("Stage progress")
    for stage, counts in progress["stages"].items():
        total = sum(counts.values())
        done = counts.get("done", 0) + counts.get("skipped", 0)
        st.caption(f"{stage}: {done}/{total} ({counts})")
        if total:
            st.progress(done / total)

    if progress["failed_steps"]:
        st.subheader("Failed steps")
        for step in progress["failed_steps"]:
            st.error(f"{step['stage']} · {step['item_key']}: {step['error']}")

    st.caption(
        f"Tokens in: {progress['tokens_in']} · Tokens out: {progress['tokens_out']} · "
        f"Estimated cost: ${progress['estimated_cost']:.4f} (estimate, not a guaranteed total)"
    )

    col1, col2 = st.columns(2)
    if col1.button("Cancel job", key=f"cancel_{job_id}", disabled=progress["status"] not in ("queued", "running")):
        ok, _ = safe_call(jobs.request_cancel, conn, job_id)
        if ok:
            st.info("Cancel requested. A call already in flight may still finish and incur cost.")
            st.rerun()
    if col2.button("Retry failed", key=f"retry_{job_id}", disabled=not progress["failed_steps"] and progress["status"] not in ("failed", "cancelled")):
        ok, new_job_id = safe_call(jobs.retry_failed, conn, job_id)
        if ok:
            st.info(f"Retrying as job {new_job_id}.")
            st.rerun()


def screen_processing(conn: sqlite3.Connection) -> None:
    st.header("Processing")
    worker_banner(conn)

    decks = list_decks(conn)
    if not decks:
        st.info("No decks yet. Create one from the Documents screen.")
        return

    deck_labels = {d["id"]: f"#{d['id']} {d['title']}" for d in decks}
    selected_deck_id = st.selectbox(
        "Select a deck", options=list(deck_labels.keys()), format_func=lambda i: deck_labels[i], key="proc_deck_select"
    )

    ok, job = safe_call(jobs.latest_job, conn, selected_deck_id)
    if not ok:
        return
    if job is None:
        st.info("No job has been submitted for this deck yet.")
        return

    _progress_panel(conn, job["id"])


# ---------- Review screen ----------

def _card_review_reasons(conn: sqlite3.Connection, card: sqlite3.Row) -> list[str]:
    reasons = []
    flags = json.loads(card["validation_flags_json"] or "[]")
    if flags:
        reasons.append(f"Validation flags: {', '.join(flags)}")
    if card["review_reason"]:
        reasons.append(f"Review reason: {card['review_reason']}")
    if card["duplicate_of"]:
        reasons.append(f"Possible duplicate of card {card['duplicate_of']}")
    item = conn.execute(
        "SELECT issue_reason, answer_source FROM source_items WHERE id = ?", (card["source_item_id"],)
    ).fetchone()
    if item and item["issue_reason"]:
        reasons.append(f"Source issue: {item['issue_reason']}")
    if item and item["answer_source"] == "conflict":
        reasons.append("Source answer is conflicting across evidence.")
    return reasons


def _render_evidence(conn: sqlite3.Connection, card_id: int) -> None:
    rows = conn.execute(
        "SELECT e.page_number, e.excerpt, e.kind FROM card_evidence ce "
        "JOIN evidence e ON e.id = ce.evidence_id WHERE ce.card_id = ? ORDER BY e.page_number",
        (card_id,),
    ).fetchall()
    if not rows:
        st.caption("No evidence linked.")
        return
    for r in rows:
        st.caption(f"p.{r['page_number']} ({r['kind']}): {r['excerpt']}")


def _render_source_page_image(conn: sqlite3.Connection, source_item_id: int) -> None:
    item = conn.execute(
        "SELECT document_id, page_refs_json FROM source_items WHERE id = ?", (source_item_id,)
    ).fetchone()
    if item is None:
        return
    pages = json.loads(item["page_refs_json"] or "[]")
    if not pages:
        st.caption("No source page recorded.")
        return
    doc = conn.execute("SELECT local_path, content_hash FROM documents WHERE id = ?", (item["document_id"],)).fetchone()
    if doc is None:
        return
    page_number = pages[0]
    out_dir = config.PAGE_IMG_DIR / doc["content_hash"]
    ok, img_path = safe_call(extract.render_page, Path(doc["local_path"]), page_number, out_dir)
    if ok and img_path is not None:
        st.image(str(img_path), caption=f"Page {page_number}")


def screen_review(conn: sqlite3.Connection) -> None:
    st.header("Review")

    decks = list_decks(conn)
    if not decks:
        st.info("No decks yet. Create one from the Documents screen.")
        return

    deck_labels = {d["id"]: f"#{d['id']} {d['title']}" for d in decks}
    selected_deck_id = st.selectbox(
        "Select a deck", options=list(deck_labels.keys()), format_func=lambda i: deck_labels[i], key="rev_deck_select"
    )

    st.subheader("Filters")
    col1, col2, col3 = st.columns(3)
    type_filter = col1.selectbox(
        "Question type", options=["all", "info_box", "mcq", "matching", "chronology", "statement", "assertion_reason"],
        key="rev_type_filter",
    )
    page_filter = col2.text_input("Source page (optional)", key="rev_page_filter")
    status_filter = col3.selectbox(
        "Card status", options=["all", "draft", "needs_review", "approved", "rejected"], key="rev_status_filter"
    )

    where = ["c.deck_id = ?"]
    params: list = [selected_deck_id]
    if type_filter != "all":
        where.append("si.type = ?")
        params.append(type_filter)
    if status_filter != "all":
        where.append("c.status = ?")
        params.append(status_filter)
    if page_filter.strip():
        where.append("si.page_refs_json LIKE ?")
        params.append(f"%{page_filter.strip()}%")

    sql = (
        "SELECT c.*, si.type AS item_type, si.page_refs_json, si.issue_reason AS item_issue_reason "
        "FROM cards c JOIN source_items si ON si.id = c.source_item_id "
        f"WHERE {' AND '.join(where)} ORDER BY c.id"
    )
    all_cards = conn.execute(sql, params).fetchall()

    if st.button("Approve all clean drafts", key="bulk_approve"):
        approved_count = 0
        for c in all_cards:
            if c["status"] != "draft":
                continue
            flags = json.loads(c["validation_flags_json"] or "[]")
            if flags or c["review_reason"] or c["duplicate_of"] or c["item_issue_reason"]:
                continue
            ok, _ = safe_call(cards.set_status, conn, c["id"], "approved")
            if ok:
                approved_count += 1
        st.success(f"Approved {approved_count} clean drafts.")
        st.rerun()

    st.subheader(f"Cards ({len(all_cards)})")
    page_size = 20
    total_pages = max(1, (len(all_cards) + page_size - 1) // page_size)
    page_num = st.number_input("Page", min_value=1, max_value=total_pages, value=1, key="rev_page_num")
    page_cards = all_cards[(page_num - 1) * page_size : page_num * page_size]

    for card in page_cards:
        with st.container(border=True):
            st.caption(f"Card {card['id']} · {card['item_type']} · status: {card['status']}")
            col_text, col_evidence = st.columns(2)

            with col_text:
                front = st.text_area("Front", value=card["front"], key=f"front_{card['id']}")
                back = st.text_area("Back", value=card["back"], key=f"back_{card['id']}")

                reasons = _card_review_reasons(conn, card)
                if reasons:
                    st.warning("Needs review:\n" + "\n".join(f"- {r}" for r in reasons))

                btn_col1, btn_col2, btn_col3, btn_col4 = st.columns(4)
                if btn_col1.button("Save", key=f"save_{card['id']}"):
                    ok, _ = safe_call(cards.edit_card, conn, card["id"], front, back)
                    if ok:
                        st.success("Saved.")
                        st.rerun()

                verified_note = None
                if reasons:
                    verified_note = st.text_input(
                        "Verified note (required to approve this card)", key=f"note_{card['id']}"
                    )
                if btn_col2.button("Approve", key=f"approve_{card['id']}"):
                    ok, _ = safe_call(cards.set_status, conn, card["id"], "approved", verified_note)
                    if ok:
                        st.success("Approved.")
                        st.rerun()
                if btn_col3.button("Reject", key=f"reject_{card['id']}"):
                    ok, _ = safe_call(cards.set_status, conn, card["id"], "rejected")
                    if ok:
                        st.success("Rejected.")
                        st.rerun()
                if btn_col4.button("Regenerate item", key=f"regen_{card['id']}"):
                    ok, _ = safe_call(jobs.submit_regenerate_job, conn, card["source_item_id"])
                    if ok:
                        st.info("Regeneration queued.")

                if card["duplicate_of"]:
                    if st.button(f"Merge into card {card['duplicate_of']}", key=f"merge_{card['id']}"):
                        ok, _ = safe_call(cards.merge_duplicate, conn, card["duplicate_of"], card["id"])
                        if ok:
                            st.success("Merged.")
                            st.rerun()

            with col_evidence:
                _render_source_page_image(conn, card["source_item_id"])
                st.caption("Evidence excerpts")
                _render_evidence(conn, card["id"])

    st.subheader("Blocked / skipped items")
    issue_rows = conn.execute(
        "SELECT id, item_key, type, disposition, issue_reason FROM source_items "
        "WHERE deck_id = ? AND disposition IN ('blocked', 'skipped') ORDER BY ordinal",
        (selected_deck_id,),
    ).fetchall()
    if not issue_rows:
        st.caption("None.")
    else:
        for r in issue_rows:
            st.caption(f"{r['item_key']} ({r['type']}, {r['disposition']}): {r['issue_reason']}")


# ---------- Export screen ----------

def screen_export(conn: sqlite3.Connection) -> None:
    st.header("Export")

    decks = list_decks(conn)
    if not decks:
        st.info("No decks yet. Create one from the Documents screen.")
        return

    deck_labels = {d["id"]: f"#{d['id']} {d['title']}" for d in decks}
    selected_deck_id = st.selectbox(
        "Select a deck", options=list(deck_labels.keys()), format_func=lambda i: deck_labels[i], key="exp_deck_select"
    )

    ok, preview = safe_call(export.export_preview, conn, selected_deck_id)
    if not ok:
        return

    st.subheader("Preview")
    st.metric("Approved cards", len(preview["approved"]))
    st.metric("Excluded cards", len(preview["excluded"]))

    col1, col2 = st.columns(2)
    with col1:
        st.caption("Cards by status")
        st.json(preview["by_status"])
    with col2:
        st.caption("Source items by disposition")
        st.json(preview["by_disposition"])

    if st.button("Export Anki deck (.apkg)", key="export_apkg_button", type="primary"):
        ok3, path = safe_call(export.export_apkg, conn, selected_deck_id)
        if ok3:
            st.success(f"Exported to {path}")
            st.download_button(
                "Download Anki deck", data=Path(path).read_bytes(), file_name=Path(path).name,
                mime="application/octet-stream", key="download_apkg",
            )

    if st.button("Export CSV", key="export_button"):
        ok2, path = safe_call(export.export_csv, conn, selected_deck_id)
        if ok2:
            data = Path(path).read_bytes()
            st.success(f"Exported to {path}")
            st.download_button(
                "Download CSV", data=data, file_name=Path(path).name, mime="text/csv", key="download_csv"
            )

    st.info(
        "Anki deck: double-click the .apkg file (or File > Import in Anki). Importing a newer export "
        "of the same deck updates its cards instead of duplicating them.\n\n"
        "CSV: File > Import, choose the CSV, note type Basic, field separator Comma, "
        "map field 1 to Front and field 2 to Back, and skip the first row (the header). "
        "Turn 'Allow HTML' off."
    )


# ---------- app entry ----------

def main() -> None:
    conn = get_conn()
    screen = st.sidebar.radio("Screen", ["Documents", "Processing", "Review", "Export"], key="screen_nav")
    if screen == "Documents":
        screen_documents(conn)
    elif screen == "Processing":
        screen_processing(conn)
    elif screen == "Review":
        screen_review(conn)
    elif screen == "Export":
        screen_export(conn)


main()
