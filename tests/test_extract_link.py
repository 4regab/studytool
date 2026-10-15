"""Tests for documents.ingest_pdf, extract.extract_page, and link.link_answers.

No network calls. Run with: uv run pytest tests/test_extract_link.py -q
"""
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studytool import db
from studytool.documents import IngestError, ingest_pdf
from studytool.extract import extract_page
from studytool.link import link_answers
from studytool.schemas import AnswerKeyEntry, Option, SourceItem
from tests.make_fixture import build as build_fixture

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
MMLU_PDF = FIXTURES_DIR / "mmlu_2009.03300.pdf"


@pytest.fixture(scope="session", autouse=True)
def synthetic_pdf():
    """Build the synthetic fixture once for the whole test session."""
    return build_fixture()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A fresh migrated DB, with PDF storage pointed at a temp dir."""
    from studytool import config

    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


# ---------------- documents.ingest_pdf ----------------


def test_ingest_rejects_non_pdf(conn):
    with pytest.raises(IngestError):
        ingest_pdf(conn, "not_a_pdf.txt", b"this is just text, not a pdf")


def test_ingest_rejects_encrypted_pdf(conn, tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "secret content")
    enc_path = tmp_path / "encrypted.pdf"
    doc.save(enc_path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    doc.close()

    with pytest.raises(IngestError, match="password"):
        ingest_pdf(conn, "encrypted.pdf", enc_path.read_bytes())


def test_ingest_dedupes_same_bytes(conn):
    data = (FIXTURES_DIR / "synthetic_exam.pdf").read_bytes()
    id1 = ingest_pdf(conn, "exam.pdf", data)
    id2 = ingest_pdf(conn, "exam_again.pdf", data)
    assert id1 == id2

    rows = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
    assert rows["n"] == 1

    pages = conn.execute("SELECT COUNT(*) AS n FROM pages WHERE document_id = ?", (id1,)).fetchone()
    assert pages["n"] == 3  # synthetic_exam.pdf has 3 pages


# ---------------- extract.extract_page ----------------


def test_extract_mmlu_page16_bold_and_text():
    pe = extract_page(MMLU_PDF, 16)
    assert "hyoid bone" in pe.text
    assert "**(B) 1**" in pe.text  # the correct option for the abstract algebra question is bold
    assert pe.needs_vision is False


def test_extract_synthetic_page3_answer_key():
    pe = extract_page(FIXTURES_DIR / "synthetic_exam.pdf", 3)
    assert "Answer Key" in pe.text
    assert "1-(c)" in pe.text


# ---------------- link.link_answers ----------------


def _mcq_item(section_key, question_number, options, page=1, inline_answer=None):
    return SourceItem(
        item_key=f"{section_key}|{question_number}|p{page}",
        section_key=section_key,
        question_number=question_number,
        type="mcq",
        page_refs=[page],
        raw_content="dummy",
        options=[Option(label=lab, text=txt) for lab, txt in options],
        inline_answer=inline_answer,
    )


def test_link_resolves_repeated_numbers_across_sections():
    items = [
        _mcq_item("A", "1", [("a", "x"), ("b", "y"), ("c", "z"), ("d", "w")]),
        _mcq_item("B", "1", [("a", "x"), ("b", "y"), ("c", "z"), ("d", "w")]),
    ]
    key_entries = [
        AnswerKeyEntry(section_key="A", question_number="1", answer="(c)", page_number=3, excerpt="1-(c)"),
        AnswerKeyEntry(section_key="B", question_number="1", answer="(d)", page_number=3, excerpt="1-(d)"),
    ]
    resolved = link_answers(items, key_entries)
    by_section = {item.section_key: item for item in resolved}
    assert by_section["A"].resolved_answer == "c"
    assert by_section["A"].answer_source == "answer_key"
    assert by_section["B"].resolved_answer == "d"
    assert by_section["B"].answer_source == "answer_key"


def test_link_flags_conflict_between_inline_and_key():
    items = [_mcq_item("A", "2", [("a", "x"), ("b", "y")], inline_answer="a")]
    key_entries = [AnswerKeyEntry(section_key="A", question_number="2", answer="b", page_number=3, excerpt="2-(b)")]
    resolved = link_answers(items, key_entries)
    assert resolved[0].answer_source == "conflict"
    assert resolved[0].issue_reason is not None


def test_link_flags_ambiguous_duplicate_key_entries():
    items = [_mcq_item("A", "3", [("a", "x"), ("b", "y")])]
    key_entries = [
        AnswerKeyEntry(section_key="A", question_number="3", answer="a", page_number=3, excerpt="3-(a)"),
        AnswerKeyEntry(section_key="A", question_number="3", answer="b", page_number=3, excerpt="3-(b) [dup]"),
    ]
    resolved = link_answers(items, key_entries)
    assert resolved[0].resolved_answer is None
    assert resolved[0].issue_reason is not None
    assert "disagree" in resolved[0].issue_reason


def test_link_flags_missing_answer():
    items = [_mcq_item("A", "4", [("a", "x"), ("b", "y")])]
    resolved = link_answers(items, [])
    assert resolved[0].answer_source == "none"
    assert resolved[0].issue_reason is not None


def test_link_does_not_mutate_inputs():
    items = [_mcq_item("A", "1", [("a", "x"), ("b", "y")])]
    key_entries = [AnswerKeyEntry(section_key="A", question_number="1", answer="a", page_number=3, excerpt="1-(a)")]
    link_answers(items, key_entries)
    assert items[0].resolved_answer is None  # original untouched
