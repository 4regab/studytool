"""Durable job queue in SQLite. OWNER: agent A. Called by the UI (submit/cancel/read) and the worker."""
import sqlite3


class JobExists(Exception):
    """An equivalent job is already queued or running (jobs_one_active index)."""


def create_deck(conn: sqlite3.Connection, document_id: int, title: str,
                content_pages: list[int], key_pages: list[int]) -> int:
    """Insert a deck with settings_json {"content_pages": [...], "key_pages": [...]}."""
    raise NotImplementedError


def submit_process_job(conn: sqlite3.Connection, deck_id: int) -> int:
    """Queue processing for a deck (dedupe_key 'process:<deck_id>').
    Returns the existing active job id if one is already queued/running."""
    raise NotImplementedError


def submit_regenerate_job(conn: sqlite3.Connection, source_item_id: int) -> int:
    """Queue regeneration of one item's cards (dedupe_key 'regen:<id>'). New cards are drafts
    alongside existing ones; approved or edited cards are never replaced."""
    raise NotImplementedError


def request_cancel(conn: sqlite3.Connection, job_id: int) -> None:
    """Set cancel_requested. The worker stops scheduling new AI calls for this job."""
    raise NotImplementedError


def retry_failed(conn: sqlite3.Connection, job_id: int) -> int:
    """Reset failed steps to pending and requeue the job (or a new job for the same deck).
    Returns the job id that will run."""
    raise NotImplementedError


def job_progress(conn: sqlite3.Connection, job_id: int) -> dict:
    """{'status', 'cancel_requested', 'error', 'stages': {stage: {'pending','running','done','failed','skipped'}},
    'failed_steps': [{'stage','item_key','error'}], 'tokens_in', 'tokens_out', 'estimated_cost',
    'worker_alive': bool}."""
    raise NotImplementedError


def latest_job(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row | None:
    raise NotImplementedError
