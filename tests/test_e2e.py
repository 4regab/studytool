"""End-to-end pipeline test with a fake AI layer (no network).

Monkeypatches studytool.ai.call_json with a fake that returns realistic SegmentResult
objects for fixtures/synthetic_exam.pdf (looking at its actual extracted text and at
segment.py's instructions to the model) and realistic card JSON for generation. Then runs
the real pipeline: documents.ingest_pdf -> jobs.create_deck -> jobs.submit_process_job ->
pipeline.run_job, directly in-process, and checks the PRD's end-to-end requirements.
"""
import json
from pathlib import Path

import pytest

from studytool import ai, cards, db, documents, export, generate, jobs, pipeline
from studytool.schemas import (
    AnswerKeyEntry,
    Option,
    PageExtract,
    SegmentResult,
    SourceItem,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    from studytool import config

    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(config, "PAGE_IMG_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(config, "MAX_AI_CONCURRENCY", 1)
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


# ---------- fake segmentation, matching what the real model would return for each page ----------

def _page1_segment() -> SegmentResult:
    matching_item = SourceItem(
        item_key="", type="matching", page_refs=[],
        raw_content=(
            "Q.1 Match List-I (Site) with List-II (Excavator) and select the correct answer "
            "using the codes given below:\nList-I (Site)\nA. Hastinapur\nB. Kalibangan\n"
            "C. Lothal\nD. Dholavira\nList-II (Excavator)\n1. S.R. Rao\n2. R.S. Bisht\n"
            "3. B.B. Lal (1950-52)\n4. B.K. Thapar\n"
            "A B C D\n(a) 1 2 3 4\n(b) 2 1 4 3\n(c) 3 4 1 2\n(d) 4 3 2 1"
        ),
        stem="Match List-I (Site) with List-II (Excavator) and select the correct answer using the codes given below:",
        section_key="A", question_number="1",
        list_i_title="Site", list_ii_title="Excavator",
        list_i=[
            Option(label="A", text="Hastinapur"), Option(label="B", text="Kalibangan"),
            Option(label="C", text="Lothal"), Option(label="D", text="Dholavira"),
        ],
        list_ii=[
            Option(label="1", text="S.R. Rao"), Option(label="2", text="R.S. Bisht"),
            Option(label="3", text="B.B. Lal (1950-52)"), Option(label="4", text="B.K. Thapar"),
        ],
        options=[
            Option(label="a", text="1 2 3 4"), Option(label="b", text="2 1 4 3"),
            Option(label="c", text="3 4 1 2"), Option(label="d", text="4 3 2 1"),
        ],
    )
    # Chronology item starts on page 1 but its options are only printed on page 2 (overlap).
    chronology_item = SourceItem(
        item_key="", type="chronology", page_refs=[],
        raw_content=(
            "Q.2 Arrange the following events in chronological order:\n1. Battle of Plassey\n"
            "2. Battle of Buxar\n3. Regulating Act, 1773\n4. Pitt's India Act, 1784\n"
            "(continuation of Q.2) Select the correct order using the codes below:\n"
            "(a) 2-1-3-4\n**(b) 1-2-3-4**\n(c) 1-3-2-4\n(d) 4-3-2-1"
        ),
        stem="Arrange the following events in chronological order:",
        section_key="A", question_number="2",
        events=[
            Option(label="1", text="Battle of Plassey"), Option(label="2", text="Battle of Buxar"),
            Option(label="3", text="Regulating Act, 1773"), Option(label="4", text="Pitt's India Act, 1784"),
        ],
        options=[
            Option(label="a", text="2-1-3-4"), Option(label="b", text="1-2-3-4", bold=True),
            Option(label="c", text="1-3-2-4"), Option(label="d", text="4-3-2-1"),
        ],
    )
    info_box_item = SourceItem(
        item_key="", type="info_box", page_refs=[],
        raw_content="The Regulating Act of 1773 provided for the establishment of a Supreme Court at "
                    "Calcutta. It also made the Governor of Bengal the Governor-General of British India.",
        section_key="A",
    )
    return SegmentResult(items=[info_box_item, matching_item, chronology_item])


def _page2_segment() -> SegmentResult:
    # Q.2's continuation and Q.3/Section B Q.1 start on page 2. The chronology item itself
    # was already emitted (with its continuation folded in) while processing page 1.
    q3_item = SourceItem(
        item_key="", type="mcq", page_refs=[],
        raw_content="Q.3 The Pitt's India Act was passed in which year?\n(a) 1773\n**(b) 1784**\n(c) 1793\n(d) 1858",
        stem="The Pitt's India Act was passed in which year?",
        section_key="A", question_number="3",
        options=[
            Option(label="a", text="1773"), Option(label="b", text="1784", bold=True),
            Option(label="c", text="1793"), Option(label="d", text="1858"),
        ],
    )
    section_b_q1 = SourceItem(
        item_key="", type="mcq", page_refs=[],
        raw_content="Q.1 Who was the first Governor-General of independent India?\n(a) Lord Mountbatten\n"
                    "(b) Jawaharlal Nehru\n**(c) C. Rajagopalachari**\n(d) Lord Canning",
        stem="Who was the first Governor-General of independent India?",
        section_key="B", question_number="1",
        options=[
            Option(label="a", text="Lord Mountbatten"), Option(label="b", text="Jawaharlal Nehru"),
            Option(label="c", text="C. Rajagopalachari", bold=True), Option(label="d", text="Lord Canning"),
        ],
    )
    # The live model also returned the tail of Q.2 as its own item and copied key lines from the
    # overlap page (page 3); the pipeline must drop both.
    q2_fragment = SourceItem(
        item_key="", type="chronology", page_refs=[],
        raw_content="(continuation of Q.2) Select the correct order using the codes below:\n"
                    "(a) 2-1-3-4\n**(b) 1-2-3-4**\n(c) 1-3-2-4\n(d) 4-3-2-1",
        stem="Select the correct order using the codes below:",
        section_key="main", question_number="2",
        options=[Option(label="a", text="2-1-3-4"), Option(label="b", text="1-2-3-4")],
    )
    overlap_keys = [
        AnswerKeyEntry(section_key="main", question_number="1", answer="(a)", page_number=3, excerpt="1-(c)"),
    ]
    return SegmentResult(items=[q2_fragment, q3_item, section_b_q1], key_entries=overlap_keys)


def _page3_segment() -> SegmentResult:
    # Answer-key page: only key_entries, no items (segmentation rules: don't fabricate study items).
    return SegmentResult(
        key_entries=[
            AnswerKeyEntry(section_key="A", question_number="1", answer="c", page_number=3, excerpt="1-(c)"),
            AnswerKeyEntry(section_key="A", question_number="2", answer="b", page_number=3, excerpt="2-(b)"),
            AnswerKeyEntry(section_key="A", question_number="3", answer="b", page_number=3, excerpt="3-(b)"),
            AnswerKeyEntry(
                section_key="A", question_number="1", answer="c", page_number=3,
                excerpt="1-(c)",
                explanation="Hastinapur-B.B. Lal (1950-52), Kalibangan-B.K. Thapar, Lothal-S.R. Rao, "
                            "Dholavira-R.S. Bisht.",
            ),
            AnswerKeyEntry(section_key="B", question_number="1", answer="c", page_number=3, excerpt="1-(c)"),
        ]
    )


_SEGMENT_BY_PAGE = {1: _page1_segment, 2: _page2_segment, 3: _page3_segment}


def _fake_segment_call(prompt: str):
    for page_num, builder in _SEGMENT_BY_PAGE.items():
        if f"<source page={page_num}>" in prompt:
            return builder()
    raise AssertionError(f"unexpected segment prompt (no <source page=N> match): {prompt[:200]}")


# ---------- fake generation: realistic card JSON per item ----------

def _fake_cards_for(prompt: str) -> list[dict]:
    if "Hastinapur" in prompt and "B.B. Lal" in prompt:
        return [
            {"front": "Who excavated Hastinapur in 1950-52?", "back": "B.B. Lal.", "evidence_indexes": []},
            {"front": "Who excavated Kalibangan?", "back": "B.K. Thapar.", "evidence_indexes": []},
            {"front": "Who excavated Lothal?", "back": "S.R. Rao.", "evidence_indexes": []},
            {"front": "Who excavated Dholavira?", "back": "R.S. Bisht.", "evidence_indexes": []},
        ]
    if "Battle of Plassey" in prompt:
        return [{
            "front": "Arrange these events in chronological order: Battle of Buxar; Pitt's India Act, 1784; "
                     "Battle of Plassey; Regulating Act, 1773.",
            "back": "Battle of Plassey -> Battle of Buxar -> Regulating Act, 1773 -> Pitt's India Act, 1784.",
            "evidence_indexes": [],
        }]
    if "Supreme Court at Calcutta" in prompt:
        return [{
            "front": "Which act provided for the establishment of a Supreme Court at Calcutta?",
            "back": "The Regulating Act of 1773.",
            "evidence_indexes": [],
        }]
    if "1784" in prompt and "Pitt" in prompt:
        return [{"front": "In which year was Pitt's India Act passed?", "back": "1784", "evidence_indexes": []}]
    if "C. Rajagopalachari" in prompt:
        return [{
            "front": "Who was the first Governor-General of independent India?",
            "back": "C. Rajagopalachari.", "evidence_indexes": [],
        }]
    raise AssertionError(f"unexpected generate prompt: {prompt[:200]}")


def _make_fake_call_json(call_log: list):
    def _fake_call_json(system, prompt, response_model, prompt_version, images=None):
        call_log.append(prompt)
        usage = ai.ModelUsage(provider="deepseek", model="fake", prompt_version=prompt_version,
                               input_tokens=10, output_tokens=10)
        if response_model is SegmentResult:
            return _fake_segment_call(prompt), usage
        if response_model is generate._GenResponse:
            cards_out = _fake_cards_for(prompt)
            return generate._GenResponse(cards=cards_out), usage
        raise AssertionError(f"unexpected response_model: {response_model}")

    return _fake_call_json


def _run_full_job(conn, monkeypatch, call_log):
    fake = _make_fake_call_json(call_log)
    # segment.py imported call_json by name, so patching studytool.ai.call_json alone would
    # not reach it; patch both module's own reference.
    monkeypatch.setattr(ai, "call_json", fake)
    import studytool.segment as segment_module
    monkeypatch.setattr(segment_module, "call_json", fake)

    data = (FIXTURES_DIR / "synthetic_exam.pdf").read_bytes()
    document_id = documents.ingest_pdf(conn, "synthetic_exam.pdf", data)
    deck_id = jobs.create_deck(conn, document_id, "Synthetic Exam", content_pages=[1, 2], key_pages=[3])
    job_id = jobs.submit_process_job(conn, deck_id)
    result = pipeline.run_job(conn, job_id, "test-worker")
    return document_id, deck_id, job_id, result


def _items_by_label(conn, deck_id):
    rows = conn.execute(
        "SELECT * FROM source_items WHERE deck_id = ? ORDER BY ordinal", (deck_id,)
    ).fetchall()
    return {(r["section_key"], r["question_number"]): r for r in rows}


def test_full_pipeline_matching_and_chronology_and_dispositions(conn, monkeypatch):
    call_log: list[str] = []
    document_id, deck_id, job_id, result = _run_full_job(conn, monkeypatch, call_log)

    assert result["status"] == "completed", result

    items = _items_by_label(conn, deck_id)

    # Section A Q1 (matching) and Section B Q1 (mcq) stay distinct despite sharing "1".
    a_q1 = items[("A", "1")]
    b_q1 = items[("B", "1")]
    assert a_q1["id"] != b_q1["id"]
    assert a_q1["type"] == "matching"
    assert b_q1["type"] == "mcq"
    assert b_q1["resolved_answer"] == "c"

    # Section A Q1 resolves from the distant key page (page 3) and normalizes to the four pairs.
    assert a_q1["resolved_answer"] == "c"
    assert a_q1["answer_source"] == "answer_key"
    norm_a_q1 = json.loads(a_q1["normalized_json"])
    pairs = {(r["left"], r["right"]) for r in norm_a_q1["relationships"]}
    assert pairs == {
        ("Hastinapur", "B.B. Lal (1950-52)"),
        ("Kalibangan", "B.K. Thapar"),
        ("Lothal", "S.R. Rao"),
        ("Dholavira", "R.S. Bisht"),
    }

    # The chronology item spans pages 1-2 as ONE item; its page-2 tail is not a second item.
    a_q2 = items[("A", "2")]
    assert json.loads(a_q2["page_refs_json"]) == [1, 2]
    assert conn.execute(
        "SELECT COUNT(*) FROM source_items WHERE deck_id = ? AND type = 'chronology'", (deck_id,)
    ).fetchone()[0] == 1

    # The info box converts (it has no answer to resolve, which is not a block).
    info = conn.execute("SELECT disposition FROM source_items WHERE deck_id = ? AND type = 'info_box'",
                        (deck_id,)).fetchone()
    assert info["disposition"] == "converted"

    # Key-page items (page 3) produce no source_items of their own (segmentation emits only
    # key_entries there); every content-page item has a final disposition, never 'pending'.
    for key, row in items.items():
        assert row["disposition"] != "pending", f"{key} still pending"

    # Cards are draft/needs_review, never approved, straight out of generation.
    all_cards = conn.execute("SELECT status FROM cards WHERE deck_id = ?", (deck_id,)).fetchall()
    assert all_cards, "expected cards to have been generated"
    assert all(c["status"] in ("draft", "needs_review") for c in all_cards)

    # Hastinapur matching item yields 4 independent cards (one per verified pair).
    matching_cards = conn.execute(
        "SELECT front, back FROM cards WHERE source_item_id = ? ORDER BY ordinal", (a_q1["id"],)
    ).fetchall()
    assert len(matching_cards) == 4
    fronts_backs = {(c["front"], c["back"]) for c in matching_cards}
    assert ("Who excavated Hastinapur in 1950-52?", "B.B. Lal.") in fronts_backs

    # A year is a real answer, not an option code.
    year_card = conn.execute("SELECT status, validation_flags_json FROM cards WHERE back = '1784'").fetchone()
    assert year_card["status"] == "draft", year_card["validation_flags_json"]


def test_rerun_is_idempotent_and_preserves_edits(conn, monkeypatch):
    call_log: list[str] = []
    document_id, deck_id, job_id, result = _run_full_job(conn, monkeypatch, call_log)
    assert result["status"] == "completed"

    cards_before = conn.execute(
        "SELECT id, front, back FROM cards WHERE deck_id = ? ORDER BY id", (deck_id,)
    ).fetchall()
    count_before = len(cards_before)
    assert count_before > 0

    # User edits one card.
    edited_card_id = cards_before[0]["id"]
    cards.edit_card(conn, edited_card_id, "Edited front", "Edited back")

    # Simulate a restart: clear 'running' leftovers like worker._recover_stale would, then
    # replay the whole job (job_steps already 'done' are skipped; nothing re-queries the AI).
    second_call_count_before = len(call_log)
    result2 = pipeline.run_job(conn, job_id, "test-worker-2")
    assert result2["status"] == "completed"
    assert len(call_log) == second_call_count_before, "rerun must not re-call the AI for completed steps"

    cards_after = conn.execute(
        "SELECT id, front, back FROM cards WHERE deck_id = ? ORDER BY id", (deck_id,)
    ).fetchall()
    assert len(cards_after) == count_before, "rerun must not create duplicate cards"

    edited = conn.execute("SELECT front, back FROM cards WHERE id = ?", (edited_card_id,)).fetchone()
    assert edited["front"] == "Edited front"
    assert edited["back"] == "Edited back"


def test_export_after_approval_round_trips(conn, monkeypatch):
    call_log: list[str] = []
    document_id, deck_id, job_id, result = _run_full_job(conn, monkeypatch, call_log)
    assert result["status"] == "completed"

    draft_cards = conn.execute(
        "SELECT id FROM cards WHERE deck_id = ? AND status = 'draft'", (deck_id,)
    ).fetchall()
    assert draft_cards, "expected at least one clean draft card to approve"
    for c in draft_cards:
        cards.set_status(conn, c["id"], "approved")

    path = export.export_csv(conn, deck_id)
    assert path.exists()

    import csv as csv_mod
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv_mod.reader(f))
    assert rows[0] == ["front", "back"]
    assert len(rows) - 1 == len(draft_cards)
    for front, back in rows[1:]:
        assert front.strip() and back.strip()
