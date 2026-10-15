"""Behavior-level tests for jobs.py / worker.py pipeline wiring. No network calls:
extract/segment/link/generate are monkeypatched with fakes.
"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from studytool import db, jobs, pipeline, worker
from studytool.schemas import (
    CardDraft,
    GenerationResult,
    Normalized,
    PageExtract,
    SegmentResult,
    SourceItem,
)


# ---------- fixtures ----------

@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "studytool.db"
    c = db.connect(db_path)
    db.migrate(c)
    yield c
    c.close()


def _make_document(conn, path: Path, page_count: int = 2) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO documents (filename, local_path, content_hash, page_count) "
            "VALUES (?, ?, ?, ?)",
            ("doc.pdf", str(path), "hash123", page_count),
        )
        document_id = cur.lastrowid
        for p in range(1, page_count + 1):
            conn.execute(
                "INSERT INTO pages (document_id, page_number, extraction_status) VALUES (?, ?, 'pending')",
                (document_id, p),
            )
    return document_id


# ---------- (a) idempotent submission ----------

def test_submit_process_job_is_idempotent(conn, tmp_path):
    document_id = _make_document(conn, tmp_path / "a.pdf")
    deck_id = jobs.create_deck(conn, document_id, "Deck", [1], [2])

    job_id_1 = jobs.submit_process_job(conn, deck_id)
    job_id_2 = jobs.submit_process_job(conn, deck_id)

    assert job_id_1 == job_id_2
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE deck_id = ? AND status IN ('queued','running')",
        (deck_id,),
    ).fetchone()
    assert rows["n"] == 1


# ---------- (b) worker lease ----------

def test_second_worker_cannot_take_live_lease_but_can_take_stale_one(conn):
    assert worker._acquire_lease(conn, "worker-A") is True
    assert worker._acquire_lease(conn, "worker-B") is False

    # Make the lease stale, then worker-B should be able to take it.
    with db.tx(conn):
        conn.execute(
            "UPDATE worker_lease SET heartbeat = datetime('now', '-999 seconds') WHERE id = 1"
        )
    assert worker._acquire_lease(conn, "worker-B") is True
    row = conn.execute("SELECT worker_id FROM worker_lease WHERE id = 1").fetchone()
    assert row["worker_id"] == "worker-B"


# ---------- (c) stale recovery ----------

def test_recovery_resets_stale_running_job_and_steps(conn, tmp_path):
    document_id = _make_document(conn, tmp_path / "a.pdf")
    deck_id = jobs.create_deck(conn, document_id, "Deck", [1], [])
    job_id = jobs.submit_process_job(conn, deck_id)

    with db.tx(conn):
        conn.execute(
            "UPDATE jobs SET status = 'running', worker_id = 'dead-worker', "
            "heartbeat = datetime('now', '-999 seconds') WHERE id = ?",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO job_steps (job_id, item_key, stage, status) VALUES (?, 'page:1', 'extract', 'running')",
            (job_id,),
        )

    worker._recover_stale(conn, "new-worker")

    job = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "queued"
    step = conn.execute("SELECT status FROM job_steps WHERE job_id = ?", (job_id,)).fetchone()
    assert step["status"] == "pending"


# ---------- (d) idempotent generate replay ----------

def _fake_source_item(item_key="main|1|p1") -> SourceItem:
    return SourceItem(
        item_key=item_key,
        type="info_box",
        page_refs=[1],
        raw_content="Some fact.",
    )


def _insert_source_item(conn, document_id, deck_id, item: SourceItem) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO source_items (document_id, deck_id, item_key, ordinal, section_key, "
            "question_number, type, page_refs_json, raw_content, item_json, disposition) "
            "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                document_id, deck_id, item.item_key, item.section_key, item.question_number,
                item.type, json.dumps(item.page_refs), item.raw_content, item.model_dump_json(),
            ),
        )
        return cur.lastrowid


def test_generate_stage_replay_is_idempotent(conn, tmp_path, monkeypatch):
    document_id = _make_document(conn, tmp_path / "a.pdf")
    deck_id = jobs.create_deck(conn, document_id, "Deck", [1], [])
    item = _fake_source_item()
    source_item_id = _insert_source_item(conn, document_id, deck_id, item)

    def fake_generate_cards(source_item):
        return GenerationResult(
            normalized=Normalized(type="info_box", facts=["Some fact."]),
            cards=[CardDraft(front="Q?", back="A.")],
            disposition="converted",
        )

    monkeypatch.setattr(pipeline.generate, "generate_cards", fake_generate_cards)
    monkeypatch.setattr(pipeline.validate, "check_card", lambda card, item: [])
    monkeypatch.setattr(pipeline.validate, "fingerprint", lambda f, b: f"{f}|{b}")

    job_id = jobs.submit_process_job(conn, deck_id)

    failed1, cancelled1 = pipeline._run_generate(conn, job_id, deck_id)
    assert failed1 == 0 and cancelled1 is False

    # Clear the step so the stage re-runs the same item (simulating a worker restart replay).
    with db.tx(conn):
        conn.execute("UPDATE job_steps SET status = 'pending' WHERE job_id = ? AND stage = 'generate'", (job_id,))

    failed2, cancelled2 = pipeline._run_generate(conn, job_id, deck_id)
    assert failed2 == 0 and cancelled2 is False

    cards = conn.execute("SELECT COUNT(*) AS n FROM cards WHERE source_item_id = ?", (source_item_id,)).fetchall()
    assert cards[0]["n"] == 1  # INSERT OR IGNORE on card_key prevented a duplicate


# ---------- (e) cancellation ----------

def test_cancel_stops_before_further_ai_calls(conn, tmp_path, monkeypatch):
    document_id = _make_document(conn, tmp_path / "a.pdf")
    deck_id = jobs.create_deck(conn, document_id, "Deck", [1], [])
    item1 = _fake_source_item("main|1|p1")
    item2 = _fake_source_item("main|2|p1")
    _insert_source_item(conn, document_id, deck_id, item1)
    _insert_source_item(conn, document_id, deck_id, item2)

    job_id = jobs.submit_process_job(conn, deck_id)
    calls = []

    def fake_generate_cards(source_item):
        calls.append(source_item.item_key)
        # Cancel after the first call so the second item should never be scheduled.
        jobs.request_cancel(conn, job_id)
        return GenerationResult(
            normalized=Normalized(type="info_box", facts=["x"]),
            cards=[],
            disposition="converted",
        )

    monkeypatch.setattr(pipeline.generate, "generate_cards", fake_generate_cards)
    monkeypatch.setattr(pipeline, "config", pipeline.config)
    monkeypatch.setattr(pipeline.config, "MAX_AI_CONCURRENCY", 1)  # force serial scheduling

    failed, cancelled = pipeline._run_generate(conn, job_id, deck_id)

    assert cancelled is True
    assert len(calls) == 1  # only the first item's AI call happened
