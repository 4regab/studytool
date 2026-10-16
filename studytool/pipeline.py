"""Run one job's stages. OWNER: agent A.

Stages, each step committed before the next (job_steps row per stage+item_key):
  extract  item_key 'page:<n>'   -> pages row (skip if cache_key already matches)
  segment  item_key 'page:<n>'   -> segment.segment_page; upsert source_items + evidence
  link     item_key 'all'        -> link.link_answers over all items + key entries of the deck
  generate item_key <item_key>   -> generate.generate_cards; insert cards (card_key unique),
                                    card_evidence, card_revisions(origin='generated'), model_calls
  dedupe   item_key 'all'        -> same fingerprint as an earlier card -> duplicate_of set,
                                    status needs_review with reason; item disposition 'duplicate'
                                    when all its cards are duplicates
Only content pages produce cards; key pages are extracted/segmented for evidence only.
AI calls run outside transactions, with at most config.MAX_AI_CONCURRENCY at a time.
Cancellation is checked before each new AI call.
"""
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from studytool import ai, config, db, extract, generate, jobs, link, segment, validate
from studytool.schemas import AnswerKeyEntry, ModelUsage, PageExtract, SegmentResult, SourceItem

log = logging.getLogger("studytool.pipeline")


# ---------- small step-tracking helpers ----------

def _get_step(conn, job_id: int, stage: str, item_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM job_steps WHERE job_id = ? AND stage = ? AND item_key = ?",
        (job_id, stage, item_key),
    ).fetchone()


def _start_step(conn, job_id: int, stage: str, item_key: str) -> int:
    """Insert the step row if missing, mark it running, bump attempt_count. Returns step id."""
    with db.tx(conn):
        row = conn.execute(
            "SELECT id FROM job_steps WHERE job_id = ? AND stage = ? AND item_key = ?",
            (job_id, stage, item_key),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO job_steps (job_id, item_key, stage, status, attempt_count) "
                "VALUES (?, ?, ?, 'running', 1)",
                (job_id, item_key, stage),
            )
            return cur.lastrowid
        conn.execute(
            "UPDATE job_steps SET status = 'running', attempt_count = attempt_count + 1, "
            "error = NULL, updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        return row["id"]


def _finish_step(conn, step_id: int, status: str, cache_key: str | None = None,
                  result_ref: str | None = None, error: str | None = None) -> None:
    with db.tx(conn):
        conn.execute(
            "UPDATE job_steps SET status = ?, cache_key = COALESCE(?, cache_key), "
            "result_ref = COALESCE(?, result_ref), error = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (status, cache_key, result_ref, error, step_id),
        )


def _deck_settings(conn, deck_id: int) -> tuple[list[int], list[int]]:
    row = conn.execute("SELECT settings_json FROM decks WHERE id = ?", (deck_id,)).fetchone()
    settings = json.loads(row["settings_json"] or "{}")
    return sorted(settings.get("content_pages", [])), sorted(settings.get("key_pages", []))


# ---------- extract stage ----------

def _run_extract(conn, job_id: int, document_id: int, pdf_path: Path, content_hash: str,
                  pages: list[int]) -> str | None:
    """Returns an error message if a step failed unrecoverably, else None."""
    for page_number in pages:
        if jobs.cancel_requested(conn, job_id):
            return None
        cache_key = f"{content_hash}|{page_number}|{config.EXTRACTOR_VERSION}"
        page_row = conn.execute(
            "SELECT * FROM pages WHERE document_id = ? AND page_number = ?",
            (document_id, page_number),
        ).fetchone()
        if page_row is not None and page_row["cache_key"] == cache_key and \
                page_row["extraction_status"] in ("done", "needs_vision"):
            continue  # already extracted with this exact logic/version

        item_key = f"page:{page_number}"
        step_id = _start_step(conn, job_id, "extract", item_key)
        try:
            result: PageExtract = extract.extract_page(pdf_path, page_number)
            rendered_path = None
            if result.needs_vision:
                rendered_path = str(extract.render_page(pdf_path, page_number, config.PAGE_IMG_DIR / content_hash))
            with db.tx(conn):
                conn.execute(
                    "UPDATE pages SET printed_label = ?, text = ?, blocks_json = ?, "
                    "rendered_path = COALESCE(?, rendered_path), "
                    "extraction_status = ?, cache_key = ? "
                    "WHERE document_id = ? AND page_number = ?",
                    (
                        result.printed_label,
                        result.text,
                        json.dumps([b.model_dump() for b in result.blocks]),
                        rendered_path,
                        "needs_vision" if result.needs_vision else "done",
                        cache_key,
                        document_id,
                        page_number,
                    ),
                )
            _finish_step(conn, step_id, "done", cache_key=cache_key)
        except Exception as exc:
            log.exception("extract failed for page %s", page_number)
            _finish_step(conn, step_id, "failed", error=str(exc))
            with db.tx(conn):
                conn.execute(
                    "UPDATE pages SET extraction_status = 'failed' WHERE document_id = ? AND page_number = ?",
                    (document_id, page_number),
                )
    return None


# ---------- segment stage ----------

def _load_page_extract(conn, document_id: int, page_number: int) -> PageExtract | None:
    row = conn.execute(
        "SELECT * FROM pages WHERE document_id = ? AND page_number = ?",
        (document_id, page_number),
    ).fetchone()
    if row is None or row["text"] is None:
        return None
    return PageExtract(
        page_number=page_number,
        printed_label=row["printed_label"],
        text=row["text"],
        blocks=json.loads(row["blocks_json"] or "[]"),
        needs_vision=row["extraction_status"] == "needs_vision",
    )


def _run_segment(conn, job_id: int, document_id: int, pages: list[int],
                  content_pages: set[int], key_pages: set[int]) -> list[ModelUsage]:
    """Returns all usage recorded. Mutates job_steps/result_ref as cache."""
    usage_all: list[ModelUsage] = []
    prev_section_key: str | None = None
    seen_text: dict[str, int] = {}  # page text -> first page with that text
    # Saved results are reused only if they came from the same extraction and prompt version.
    segment_version = f"x{config.EXTRACTOR_VERSION}|s{config.SEGMENT_PROMPT_VERSION}"
    for i, page_number in enumerate(pages):
        if jobs.cancel_requested(conn, job_id):
            break
        item_key = f"page:{page_number}"
        existing = _get_step(conn, job_id, "segment", item_key)
        page = _load_page_extract(conn, document_id, page_number)
        # Some books print the same page twice. Reading it again would only make duplicate cards.
        if page is not None and page.text.strip():
            first = seen_text.setdefault(page.text, page_number)
            if first != page_number:
                if existing is None or existing["status"] != "skipped":
                    step_id = _start_step(conn, job_id, "segment", item_key)
                    _finish_step(conn, step_id, "skipped", error=f"same text as page {first}")
                continue
        if (existing is not None and existing["status"] == "done" and existing["result_ref"]
                and existing["cache_key"] == segment_version):
            result = SegmentResult.model_validate_json(existing["result_ref"])
            if result.items:
                prev_section_key = result.items[-1].section_key
            continue

        if page is None:
            continue  # extraction failed for this page; nothing to segment
        # The overlap must be the physically next page, and not a repeat of this one.
        next_page = None
        if i + 1 < len(pages) and pages[i + 1] == page_number + 1:
            next_page = _load_page_extract(conn, document_id, pages[i + 1])
            if next_page is not None and next_page.text == page.text:
                next_page = None
        image_path = None
        if page.needs_vision:
            page_row = conn.execute(
                "SELECT rendered_path FROM pages WHERE document_id = ? AND page_number = ?",
                (document_id, page_number),
            ).fetchone()
            if page_row and page_row["rendered_path"]:
                image_path = Path(page_row["rendered_path"])

        is_key_page = page_number in key_pages and page_number not in content_pages
        step_id = _start_step(conn, job_id, "segment", item_key)
        try:
            result, usage = segment.segment_page(
                page, next_page, prev_section_key, is_key_page=is_key_page, image_path=image_path
            )
            usage_all.extend(usage)
            if result.items:
                prev_section_key = result.items[-1].section_key
            with db.tx(conn):
                _record_usage(conn, step_id, usage)
            _finish_step(conn, step_id, "done", cache_key=segment_version, result_ref=result.model_dump_json())
            # Pages were (re)read, so the link and dedupe results built from the old ones are stale.
            with db.tx(conn):
                conn.execute(
                    "UPDATE job_steps SET status = 'pending' WHERE job_id = ? AND stage IN ('link', 'dedupe')",
                    (job_id,),
                )
        except ai.AIPermanentError as exc:
            # Bad key, no balance, etc.: every later page would fail the same way. Stop the job.
            _finish_step(conn, step_id, "failed", error=str(exc))
            raise
        except Exception as exc:
            log.exception("segment failed for page %s", page_number)
            _finish_step(conn, step_id, "failed", error=str(exc))
    return usage_all


def _record_usage(conn, step_id: int, usage: list[ModelUsage]) -> None:
    """One model_calls row per AI call. Call inside the caller's transaction."""
    for u in usage:
        cost = u.estimated_cost
        if cost is None and u.input_tokens is not None and u.output_tokens is not None:
            cost = (u.input_tokens / 1_000_000) * config.PRICE_IN_PER_M + \
                   (u.output_tokens / 1_000_000) * config.PRICE_OUT_PER_M
        conn.execute(
            "INSERT INTO model_calls (job_step_id, provider, model, prompt_version, "
            "token_usage_json, estimated_cost) VALUES (?, ?, ?, ?, ?, ?)",
            (
                step_id, u.provider, u.model, u.prompt_version,
                json.dumps({"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}),
                cost,
            ),
        )

# ---------- link stage ----------

def _run_link(conn, job_id: int, document_id: int, deck_id: int, pages: list[int],
              content_pages: set[int]) -> None:
    item_key = "all"
    existing = _get_step(conn, job_id, "link", item_key)
    if existing is not None and existing["status"] == "done":
        return

    all_items: list[SourceItem] = []
    key_entries: list[AnswerKeyEntry] = []
    for page_number in pages:
        step = _get_step(conn, job_id, "segment", f"page:{page_number}")
        if step is None or step["status"] != "done" or not step["result_ref"]:
            continue
        result = SegmentResult.model_validate_json(step["result_ref"])
        for item in result.items:
            # The tail of a question that began on the previous page is already part of that
            # item (it was given the page as overlap). Drop the fragment instead of keeping a
            # second, incomplete copy of the same question. Match on the question number, or,
            # when the model gave the fragment no number, on its first line of text.
            first_line = next((ln.replace("*", "").strip() for ln in (item.raw_content or "").splitlines()
                               if len(ln.replace("*", "").strip()) >= 15), "")
            if any(
                item.page_refs[0] in prev.page_refs[1:] and (
                    (item.question_number and prev.section_key == item.section_key
                     and prev.question_number == item.question_number)
                    or (first_line and first_line in prev.raw_content.replace("*", ""))
                )
                for prev in all_items
            ):
                log.info("dropping continuation fragment %s", item.item_key)
                continue
            all_items.append(item)
        key_entries.extend(result.key_entries)

    step_id = _start_step(conn, job_id, "link", item_key)
    try:
        linked = link.link_answers(all_items, key_entries)
        with db.tx(conn):
            for ordinal, item in enumerate(linked):
                if item.from_key_pages_only:
                    item.disposition = "skipped"
                    item.issue_reason = "answer-key page: evidence only"
                page_refs_json = json.dumps(item.page_refs)
                cur = conn.execute(
                    "INSERT INTO source_items "
                    "(document_id, deck_id, item_key, ordinal, section_key, question_number, type, "
                    " page_refs_json, raw_content, item_json, resolved_answer, answer_source, "
                    " disposition, issue_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (deck_id, item_key) DO UPDATE SET "
                    " ordinal = excluded.ordinal, section_key = excluded.section_key, "
                    " question_number = excluded.question_number, type = excluded.type, "
                    " page_refs_json = excluded.page_refs_json, raw_content = excluded.raw_content, "
                    " item_json = excluded.item_json, resolved_answer = excluded.resolved_answer, "
                    " answer_source = excluded.answer_source, disposition = excluded.disposition, "
                    " issue_reason = excluded.issue_reason "
                    "RETURNING id",
                    (
                        document_id, deck_id, item.item_key, ordinal, item.section_key,
                        item.question_number, item.type, page_refs_json, item.raw_content,
                        item.model_dump_json(), item.resolved_answer, item.answer_source,
                        item.disposition, item.issue_reason,
                    ),
                )
                source_item_id = cur.fetchone()["id"]
                conn.execute("DELETE FROM evidence WHERE source_item_id = ?", (source_item_id,))
                for ev in item.evidence:
                    conn.execute(
                        "INSERT INTO evidence (source_item_id, page_number, excerpt, bounding_box_json, kind) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            source_item_id, ev.page_number, ev.excerpt,
                            json.dumps(ev.bbox) if ev.bbox else None, ev.kind,
                        ),
                    )
        _finish_step(conn, step_id, "done")
    except Exception as exc:
        log.exception("link failed")
        _finish_step(conn, step_id, "failed", error=str(exc))
        raise


# ---------- generate stage ----------

def _load_source_item(conn, source_item_id: int) -> SourceItem:
    row = conn.execute("SELECT item_json FROM source_items WHERE id = ?", (source_item_id,)).fetchone()
    return SourceItem.model_validate_json(row["item_json"])


def _insert_cards_for_item(conn, deck_id: int, source_item_id: int, item_key: str,
                            step_id: int, gen) -> None:
    """One transaction: cards, card_evidence, card_revisions, model_calls, source_items update."""
    evidence_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM evidence WHERE source_item_id = ? ORDER BY id", (source_item_id,)
        )
    ]
    with db.tx(conn):
        for i, card in enumerate(gen.cards):
            card_key = f"{item_key}|g1|{i}"
            flags = validate.check_card(card, _load_source_item(conn, source_item_id))
            status = "needs_review" if (card.review_reason or flags) else "draft"
            fp = validate.fingerprint(card.front, card.back)
            cur = conn.execute(
                "INSERT OR IGNORE INTO cards "
                "(deck_id, source_item_id, card_key, ordinal, front, back, status, "
                " validation_flags_json, review_reason, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    deck_id, source_item_id, card_key, i, card.front, card.back, status,
                    json.dumps(flags), card.review_reason, fp,
                ),
            )
            if cur.rowcount == 0:
                continue  # already inserted by a previous (replayed) run; skip the rest for it
            card_id = cur.lastrowid
            for idx in card.evidence_indexes:
                if 0 <= idx < len(evidence_ids):
                    conn.execute(
                        "INSERT OR IGNORE INTO card_evidence (card_id, evidence_id) VALUES (?, ?)",
                        (card_id, evidence_ids[idx]),
                    )
            conn.execute(
                "INSERT INTO card_revisions (card_id, front, back, origin) VALUES (?, ?, ?, 'generated')",
                (card_id, card.front, card.back),
            )
        _record_usage(conn, step_id, gen.usage)
        conn.execute(
            "UPDATE source_items SET normalized_json = ?, disposition = ?, issue_reason = ? WHERE id = ?",
            (gen.normalized.model_dump_json(), gen.disposition, gen.issue_reason, source_item_id),
        )


def _run_generate(conn_main, job_id: int, deck_id: int) -> tuple[int, bool]:
    """Returns (failed_step_count, was_cancelled)."""
    db_path = conn_main.execute("PRAGMA database_list").fetchone()["file"]
    rows = conn_main.execute(
        "SELECT id, item_key FROM source_items "
        "WHERE deck_id = ? AND disposition != 'skipped' ORDER BY ordinal",
        (deck_id,),
    ).fetchall()
    todo = []
    for row in rows:
        existing = _get_step(conn_main, job_id, "generate", row["item_key"])
        if existing is not None and existing["status"] == "done":
            continue
        todo.append((row["id"], row["item_key"]))

    failed = 0
    cancelled = False

    def _work(source_item_id: int, item_key: str) -> tuple[str, int | None, str | None]:
        conn = db.connect(db_path)
        step_id = None
        try:
            step_id = _start_step(conn, job_id, "generate", item_key)
            item = _load_source_item(conn, source_item_id)
            gen = generate.generate_cards(item)  # AI call: no transaction held
            _insert_cards_for_item(conn, deck_id, source_item_id, item_key, step_id, gen)
            _finish_step(conn, step_id, "done")
            return ("done", step_id, None)
        except ai.AIPermanentError as exc:
            if step_id is not None:
                _finish_step(conn, step_id, "failed", error=str(exc))
            return ("permanent", step_id, str(exc))
        except Exception as exc:
            log.exception("generate failed for %s", item_key)
            if step_id is not None:
                _finish_step(conn, step_id, "failed", error=str(exc))
            return ("transient", step_id, str(exc))
        finally:
            conn.close()

    permanent_error: str | None = None
    with ThreadPoolExecutor(max_workers=max(1, config.MAX_AI_CONCURRENCY)) as pool:
        futures = {}
        pending = list(todo)
        while pending and permanent_error is None:
            if jobs.cancel_requested(conn_main, job_id):
                cancelled = True
                break
            while pending and len(futures) < config.MAX_AI_CONCURRENCY:
                if jobs.cancel_requested(conn_main, job_id):
                    cancelled = True
                    break
                source_item_id, item_key = pending.pop(0)
                fut = pool.submit(_work, source_item_id, item_key)
                futures[fut] = item_key
            if not futures:
                break
            done_set = set()
            for fut in as_completed(list(futures.keys()), timeout=None):
                outcome, _step_id, err = fut.result()
                done_set.add(fut)
                if outcome == "transient":
                    failed += 1
                elif outcome == "permanent":
                    permanent_error = err
                break  # re-check cancel/pending after each completion
            for fut in done_set:
                futures.pop(fut, None)
        # drain any still-running futures (let in-flight calls finish)
        for fut in list(futures.keys()):
            outcome, _step_id, err = fut.result()
            if outcome == "transient":
                failed += 1
            elif outcome == "permanent":
                permanent_error = permanent_error or err

    if permanent_error:
        raise ai.AIPermanentError(permanent_error)
    return failed, cancelled


# ---------- dedupe stage ----------

def _run_dedupe(conn, job_id: int, deck_id: int) -> None:
    item_key = "all"
    existing = _get_step(conn, job_id, "dedupe", item_key)
    if existing is not None and existing["status"] == "done":
        return
    step_id = _start_step(conn, job_id, "dedupe", item_key)
    try:
        rows = conn.execute(
            "SELECT c.id, c.fingerprint, c.status, c.duplicate_of, c.front, c.back, si.id AS source_item_id, "
            "si.ordinal AS item_ordinal, c.ordinal AS card_ordinal "
            "FROM cards c JOIN source_items si ON si.id = c.source_item_id "
            "WHERE c.deck_id = ? AND c.fingerprint IS NOT NULL "
            "ORDER BY si.ordinal, c.ordinal",
            (deck_id,),
        ).fetchall()

        seen: dict[str, int] = {}  # fingerprint -> first card id
        by_back: dict[str, list[tuple[int, set[str]]]] = {}  # same answer -> (card id, front words)
        item_cards: dict[int, list[sqlite3.Row]] = {}
        with db.tx(conn):
            for row in rows:
                item_cards.setdefault(row["source_item_id"], []).append(row)
                first_id = seen.get(row["fingerprint"])
                # Exam books ask the same fact many times in different words; same answer and
                # mostly the same question words counts as a possible duplicate too.
                back_key = validate.fingerprint("", row["back"])
                words = validate.content_words(row["front"])
                if first_id is None:
                    first_id = next((cid for cid, w in by_back.get(back_key, [])
                                     if validate.mostly_same(words, w)), None)
                if first_id is None:
                    seen[row["fingerprint"]] = row["id"]
                    by_back.setdefault(back_key, []).append((row["id"], words))
                    continue
                if row["status"] == "rejected" or row["duplicate_of"] is not None:
                    continue
                conn.execute(
                    "UPDATE cards SET duplicate_of = ?, status = 'needs_review', "
                    "review_reason = ?, updated_at = datetime('now') WHERE id = ?",
                    (first_id, f"possible duplicate of card #{first_id}", row["id"]),
                )

            for source_item_id, cards_rows in item_cards.items():
                refreshed = conn.execute(
                    "SELECT duplicate_of FROM cards WHERE source_item_id = ?", (source_item_id,)
                ).fetchall()
                if not refreshed:
                    continue
                all_dup = all(r["duplicate_of"] is not None for r in refreshed)
                disposition = "duplicate" if all_dup else "converted"
                conn.execute(
                    "UPDATE source_items SET disposition = ? WHERE id = ? AND disposition NOT IN ('skipped','blocked')",
                    (disposition, source_item_id),
                )
        _finish_step(conn, step_id, "done")
    except Exception as exc:
        log.exception("dedupe failed")
        _finish_step(conn, step_id, "failed", error=str(exc))
        raise


# ---------- entry points ----------

def run_job(conn: sqlite3.Connection, job_id: int, worker_id: str) -> dict:
    """Run the full process pipeline for a job. Returns {'status', 'error'}."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    document = conn.execute("SELECT * FROM documents WHERE id = ?", (job["document_id"],)).fetchone()
    pdf_path = Path(document["local_path"])
    content_hash = document["content_hash"]
    deck_id = job["deck_id"]

    content_pages, key_pages = _deck_settings(conn, deck_id)
    all_pages = sorted(set(content_pages) | set(key_pages))

    if jobs.cancel_requested(conn, job_id):
        return {"status": "cancelled", "error": None}

    _run_extract(conn, job_id, job["document_id"], pdf_path, content_hash, all_pages)
    if jobs.cancel_requested(conn, job_id):
        return {"status": "cancelled", "error": None}

    try:
        _run_segment(conn, job_id, job["document_id"], all_pages, set(content_pages), set(key_pages))
    except ai.AIPermanentError as exc:
        return {"status": "failed", "error": str(exc)}
    if jobs.cancel_requested(conn, job_id):
        return {"status": "cancelled", "error": None}

    _run_link(conn, job_id, job["document_id"], deck_id, all_pages, set(content_pages))
    if jobs.cancel_requested(conn, job_id):
        return {"status": "cancelled", "error": None}

    try:
        failed, cancelled = _run_generate(conn, job_id, deck_id)
    except ai.AIPermanentError as exc:
        return {"status": "failed", "error": str(exc)}

    if cancelled:
        return {"status": "cancelled", "error": None}

    _run_dedupe(conn, job_id, deck_id)

    if failed:
        return {"status": "failed", "error": f"{failed} steps failed"}
    return {"status": "completed", "error": None}


def run_regenerate(conn: sqlite3.Connection, job_id: int, worker_id: str) -> dict:
    """Regenerate cards for one source item (settings_json {"source_item_id": id}).
    Inserts new cards under the next generation number; never touches existing cards."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    settings = json.loads(job["settings_json"] or "{}")
    source_item_id = settings["source_item_id"]
    deck_id = job["deck_id"]

    item_key_row = conn.execute("SELECT item_key FROM source_items WHERE id = ?", (source_item_id,)).fetchone()
    item_key = item_key_row["item_key"]
    step_item_key = f"regen:{source_item_id}"

    if jobs.cancel_requested(conn, job_id):
        return {"status": "cancelled", "error": None}

    step_id = _start_step(conn, job_id, "generate", step_item_key)
    try:
        item = _load_source_item(conn, source_item_id)
        gen = generate.generate_cards(item)  # AI call outside any transaction

        existing_gens = conn.execute(
            "SELECT card_key FROM cards WHERE source_item_id = ?", (source_item_id,)
        ).fetchall()
        max_gen = 1
        for r in existing_gens:
            parts = r["card_key"].split("|")
            if len(parts) == 3 and parts[1].startswith("g"):
                try:
                    max_gen = max(max_gen, int(parts[1][1:]))
                except ValueError:
                    pass
        next_gen = max_gen + 1

        evidence_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM evidence WHERE source_item_id = ? ORDER BY id", (source_item_id,)
            )
        ]
        with db.tx(conn):
            for i, card in enumerate(gen.cards):
                card_key = f"{item_key}|g{next_gen}|{i}"
                flags = validate.check_card(card, item)
                status = "needs_review" if (card.review_reason or flags) else "draft"
                fp = validate.fingerprint(card.front, card.back)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO cards "
                    "(deck_id, source_item_id, card_key, ordinal, front, back, status, "
                    " validation_flags_json, review_reason, fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        deck_id, source_item_id, card_key, i, card.front, card.back, status,
                        json.dumps(flags), card.review_reason, fp,
                    ),
                )
                if cur.rowcount == 0:
                    continue
                card_id = cur.lastrowid
                for idx in card.evidence_indexes:
                    if 0 <= idx < len(evidence_ids):
                        conn.execute(
                            "INSERT OR IGNORE INTO card_evidence (card_id, evidence_id) VALUES (?, ?)",
                            (card_id, evidence_ids[idx]),
                        )
                conn.execute(
                    "INSERT INTO card_revisions (card_id, front, back, origin) VALUES (?, ?, ?, 'regenerated')",
                    (card_id, card.front, card.back),
                )
            _record_usage(conn, step_id, gen.usage)
        _finish_step(conn, step_id, "done")
        return {"status": "completed", "error": None}
    except ai.AIPermanentError as exc:
        _finish_step(conn, step_id, "failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        log.exception("regenerate failed for item %s", source_item_id)
        _finish_step(conn, step_id, "failed", error=str(exc))
        return {"status": "failed", "error": str(exc)}
