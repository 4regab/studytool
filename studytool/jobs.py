"""Durable job queue in SQLite. OWNER: agent A. Called by the UI (submit/cancel/read) and the worker."""
import json
import sqlite3

from studytool import config, db


class JobExists(Exception):
    """An equivalent job is already queued or running (jobs_one_active index)."""


def create_deck(conn: sqlite3.Connection, document_id: int, title: str,
                 content_pages: list[int], key_pages: list[int]) -> int:
    """Insert a deck with settings_json {"content_pages": [...], "key_pages": [...]}."""
    settings = json.dumps({"content_pages": content_pages, "key_pages": key_pages})
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO decks (document_id, title, settings_json) VALUES (?, ?, ?)",
            (document_id, title, settings),
        )
        return cur.lastrowid


def _existing_active_job(conn: sqlite3.Connection, dedupe_key: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued','running')",
        (dedupe_key,),
    ).fetchone()
    return row["id"] if row else None


def submit_process_job(conn: sqlite3.Connection, deck_id: int) -> int:
    """Queue processing for a deck (dedupe_key 'process:<deck_id>').
    Returns the existing active job id if one is already queued/running."""
    row = conn.execute("SELECT document_id FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if row is None:
        raise ValueError(f"deck {deck_id} not found")
    document_id = row["document_id"]
    dedupe_key = f"process:{deck_id}"
    try:
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO jobs (document_id, deck_id, kind, dedupe_key, status) "
                "VALUES (?, ?, 'process', ?, 'queued')",
                (document_id, deck_id, dedupe_key),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        # jobs_one_active partial unique index: an active job already exists for this deck.
        existing = _existing_active_job(conn, dedupe_key)
        if existing is not None:
            return existing
        raise  # not actually a dedupe collision; surface the real error


def submit_regenerate_job(conn: sqlite3.Connection, source_item_id: int) -> int:
    """Queue regeneration of one item's cards (dedupe_key 'regen:<id>'). New cards are drafts
    alongside existing ones; approved or edited cards are never replaced."""
    row = conn.execute(
        "SELECT document_id, deck_id FROM source_items WHERE id = ?", (source_item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"source_item {source_item_id} not found")
    dedupe_key = f"regen:{source_item_id}"
    settings = json.dumps({"source_item_id": source_item_id})
    try:
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO jobs (document_id, deck_id, kind, dedupe_key, status, settings_json) "
                "VALUES (?, ?, 'regenerate', ?, 'queued', ?)",
                (row["document_id"], row["deck_id"], dedupe_key, settings),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        existing = _existing_active_job(conn, dedupe_key)
        if existing is not None:
            return existing
        raise


def request_cancel(conn: sqlite3.Connection, job_id: int) -> None:
    """Set cancel_requested. The worker stops scheduling new AI calls for this job."""
    with db.tx(conn):
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1, updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )


def cancel_requested(conn: sqlite3.Connection, job_id: int) -> bool:
    """True if the job's cancel flag is set. Used by the pipeline before each AI call."""
    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def retry_failed(conn: sqlite3.Connection, job_id: int) -> int:
    """Reset failed steps to pending and requeue the job (or a new job for the same deck).
    Returns the job id that will run."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError(f"job {job_id} not found")

    if job["status"] in ("queued", "running"):
        # Already active: nothing to retry on top of, just hand back the same job.
        return job_id

    if job["status"] not in ("failed", "cancelled"):
        return job_id

    existing = _existing_active_job(conn, job["dedupe_key"])
    if existing is not None and existing != job_id:
        return existing

    with db.tx(conn):
        conn.execute(
            "UPDATE job_steps SET status = 'pending', error = NULL, updated_at = datetime('now') "
            "WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        conn.execute(
            "UPDATE jobs SET status = 'queued', cancel_requested = 0, error = NULL, "
            "worker_id = NULL, heartbeat = NULL, updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
    return job_id


def job_progress(conn: sqlite3.Connection, job_id: int) -> dict:
    """{'status', 'cancel_requested', 'error', 'stages': {stage: {'pending','running','done','failed','skipped'}},
    'failed_steps': [{'stage','item_key','error'}], 'tokens_in', 'tokens_out', 'estimated_cost',
    'worker_alive': bool}."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError(f"job {job_id} not found")

    stages: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT stage, status, COUNT(*) AS n FROM job_steps WHERE job_id = ? GROUP BY stage, status",
        (job_id,),
    ):
        stages.setdefault(row["stage"], {}).setdefault(row["status"], row["n"])

    failed_steps = [
        {"stage": r["stage"], "item_key": r["item_key"], "error": r["error"]}
        for r in conn.execute(
            "SELECT stage, item_key, error FROM job_steps WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
    ]

    tokens_in = 0
    tokens_out = 0
    estimated_cost = 0.0
    for r in conn.execute(
        "SELECT mc.token_usage_json, mc.estimated_cost FROM model_calls mc "
        "JOIN job_steps js ON js.id = mc.job_step_id WHERE js.job_id = ?",
        (job_id,),
    ):
        usage = json.loads(r["token_usage_json"] or "{}")
        tokens_in += usage.get("input_tokens") or 0
        tokens_out += usage.get("output_tokens") or 0
        estimated_cost += r["estimated_cost"] or 0.0

    lease = conn.execute("SELECT heartbeat FROM worker_lease WHERE id = 1").fetchone()
    worker_alive = False
    if lease is not None:
        age_row = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 86400.0 AS age", (lease["heartbeat"],)
        ).fetchone()
        worker_alive = age_row["age"] is not None and age_row["age"] < config.WORKER_STALE_SECONDS

    return {
        "status": job["status"],
        "cancel_requested": bool(job["cancel_requested"]),
        "error": job["error"],
        "stages": stages,
        "failed_steps": failed_steps,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "estimated_cost": estimated_cost,
        "worker_alive": worker_alive,
    }


def latest_job(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE deck_id = ? ORDER BY id DESC LIMIT 1", (deck_id,)
    ).fetchone()
