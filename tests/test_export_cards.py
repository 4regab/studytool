"""Tests for cards.py and export.py. Uses a temp SQLite DB, no network."""
import csv
import io
import json
import sqlite3

import pytest

from studytool import cards, db, export


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


def _make_document(conn: sqlite3.Connection) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO documents (filename, local_path, content_hash, page_count) VALUES (?, ?, ?, ?)",
            ("doc.pdf", "/tmp/doc.pdf", "hash1", 10),
        )
        return cur.lastrowid


def _make_deck(conn: sqlite3.Connection, document_id: int) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO decks (document_id, title) VALUES (?, ?)", (document_id, "Test Deck")
        )
        return cur.lastrowid


def _make_item(conn: sqlite3.Connection, document_id: int, deck_id: int, item_key: str,
                ordinal: int, page_refs: list[int], issue_reason: str | None = None,
                answer_source: str | None = None) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO source_items (document_id, deck_id, item_key, ordinal, type, "
            "page_refs_json, raw_content, item_json, disposition, issue_reason, answer_source) "
            "VALUES (?, ?, ?, ?, 'info_box', ?, 'raw', '{}', 'converted', ?, ?)",
            (document_id, deck_id, item_key, ordinal, json.dumps(page_refs), issue_reason, answer_source),
        )
        return cur.lastrowid


def _make_card(conn: sqlite3.Connection, deck_id: int, source_item_id: int, card_key: str,
                ordinal: int, front: str, back: str, status: str = "approved",
                flags: list[str] | None = None, review_reason: str | None = None) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO cards (deck_id, source_item_id, card_key, ordinal, front, back, status, "
            "validation_flags_json, review_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (deck_id, source_item_id, card_key, ordinal, front, back, status,
             json.dumps(flags or []), review_reason),
        )
        return cur.lastrowid


# ---------- export.export_csv ----------

def test_export_only_approved_cards(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item1 = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    item2 = _make_item(conn, doc_id, deck_id, "main|2|p2", 2, [2])

    _make_card(conn, deck_id, item1, "k1|generated|0", 0, "Q1", "A1", status="approved")
    _make_card(conn, deck_id, item2, "k2|generated|0", 0, "Q2", "A2", status="draft")  # excluded

    path = export.export_csv(conn, deck_id)
    assert path.exists()

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert rows[0] == ["front", "back"]
    assert len(rows) == 2  # header + 1 approved card
    assert rows[1] == ["Q1", "A1"]


def test_export_round_trips_special_characters(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])

    tricky_cases = [
        ("Who excavated Hastinapur, dated 1950-52?", "B.B. Lal (1950–52)"),
        ('Field with "comma, and quotes"', 'naïve "quotes"'),
        ("Line one\nLine two", "हस्तिनापुर"),
    ]
    for i, (front, back) in enumerate(tricky_cases):
        _make_card(conn, deck_id, item, f"k{i}|generated|0", i, front, back, status="approved")

    path = export.export_csv(conn, deck_id)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert rows[0] == ["front", "back"]
    data_rows = rows[1:]
    assert len(data_rows) == len(tricky_cases)
    for (front, back), row in zip(tricky_cases, data_rows):
        assert row == [front, back]

    # no BOM, encoded as plain UTF-8
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_export_ordering_by_page_then_ordinal(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    # item on page 5 inserted before item on page 2, to prove ordering is by page, not insert order
    item_p5 = _make_item(conn, doc_id, deck_id, "main|1|p5", 2, [5])
    item_p2 = _make_item(conn, doc_id, deck_id, "main|2|p2", 1, [2])

    _make_card(conn, deck_id, item_p5, "k1|generated|0", 0, "FrontOnPage5", "BackOnPage5", status="approved")
    _make_card(conn, deck_id, item_p2, "k2|generated|0", 0, "FrontOnPage2", "BackOnPage2", status="approved")

    path = export.export_csv(conn, deck_id)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    # page 2 item should come first even though its card was inserted second
    assert rows[1] == ["FrontOnPage2", "BackOnPage2"]
    assert rows[2] == ["FrontOnPage5", "BackOnPage5"]


def test_export_zero_approved_cards_raises(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    _make_card(conn, deck_id, item, "k1|generated|0", 0, "Q1", "A1", status="draft")

    with pytest.raises(ValueError):
        export.export_csv(conn, deck_id)


def test_export_blank_field_raises(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    _make_card(conn, deck_id, item, "k1|generated|0", 0, "", "A1", status="approved")

    with pytest.raises(ValueError):
        export.export_csv(conn, deck_id)


def test_export_records_exports_row_with_ids_and_revisions(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Q1", "A1", status="approved")

    export.export_csv(conn, deck_id)

    row = conn.execute("SELECT card_ids_and_revisions_json FROM exports WHERE deck_id = ?", (deck_id,)).fetchone()
    assert row is not None
    recorded = json.loads(row["card_ids_and_revisions_json"])
    assert recorded == [{"id": card_id, "revision": 1}]


def test_export_preview_counts(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item1 = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    item2 = _make_item(conn, doc_id, deck_id, "main|2|p2", 2, [2])
    _make_card(conn, deck_id, item1, "k1|generated|0", 0, "Q1", "A1", status="approved")
    _make_card(conn, deck_id, item2, "k2|generated|0", 0, "Q2", "A2", status="draft")

    preview = export.export_preview(conn, deck_id)
    assert len(preview["approved"]) == 1
    assert len(preview["excluded"]) == 1
    assert preview["by_status"]["approved"] == 1
    assert preview["by_status"]["draft"] == 1


# ---------- cards.edit_card ----------

def test_edit_card_approved_moves_to_draft_and_writes_revision(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Old front", "Old back", status="approved")

    cards.edit_card(conn, card_id, "New front", "New back")

    row = conn.execute("SELECT front, back, status, revision FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert row["front"] == "New front"
    assert row["back"] == "New back"
    assert row["status"] == "draft"
    assert row["revision"] == 2

    revisions = conn.execute(
        "SELECT front, back, origin FROM card_revisions WHERE card_id = ? ORDER BY id", (card_id,)
    ).fetchall()
    assert revisions[-1]["origin"] == "edited"
    assert revisions[-1]["front"] == "New front"


def test_edit_card_needs_review_stays_needs_review(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Old front", "Old back", status="needs_review")

    cards.edit_card(conn, card_id, "New front", "New back")

    row = conn.execute("SELECT status FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert row["status"] == "needs_review"


def test_edit_card_rejects_blank(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Front", "Back", status="draft")

    with pytest.raises(ValueError):
        cards.edit_card(conn, card_id, "", "Back")
    with pytest.raises(ValueError):
        cards.edit_card(conn, card_id, "Front", "   ")


def test_edit_card_unchanged_does_nothing(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Front", "Back", status="draft")

    cards.edit_card(conn, card_id, "Front", "Back")

    row = conn.execute("SELECT revision FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert row["revision"] == 1
    revisions = conn.execute("SELECT COUNT(*) AS n FROM card_revisions WHERE card_id = ?", (card_id,)).fetchone()
    assert revisions["n"] == 0


# ---------- cards.set_status ----------

def test_approving_flagged_card_needs_no_note(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1], issue_reason="conflicting answer key")
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Front", "Back",
                          status="needs_review", flags=["answer_code_leak"])

    cards.set_status(conn, card_id, "approved")
    row = conn.execute("SELECT status FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert row["status"] == "approved"


def test_approving_clean_card_without_note_succeeds(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Front", "Back", status="draft")

    cards.set_status(conn, card_id, "approved")
    row = conn.execute("SELECT status FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert row["status"] == "approved"


def test_approving_blank_card_raises(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "", "Back", status="draft")

    with pytest.raises(ValueError):
        cards.set_status(conn, card_id, "approved")


def test_set_status_invalid_status_raises(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    card_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Front", "Back", status="draft")

    with pytest.raises(ValueError):
        cards.set_status(conn, card_id, "bogus_status")


# ---------- cards.merge_duplicate ----------

def test_merge_duplicate_rejects_drop_and_copies_evidence(conn):
    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    keep_id = _make_card(conn, deck_id, item, "k1|generated|0", 0, "Front1", "Back1", status="approved")
    drop_id = _make_card(conn, deck_id, item, "k2|generated|0", 1, "Front2", "Back2", status="draft")

    with db.tx(conn):
        ev = conn.execute(
            "INSERT INTO evidence (source_item_id, page_number, excerpt, kind) VALUES (?, 1, 'excerpt', 'fact')",
            (item,),
        )
        evidence_id = ev.lastrowid
        conn.execute("INSERT INTO card_evidence (card_id, evidence_id) VALUES (?, ?)", (drop_id, evidence_id))

    cards.merge_duplicate(conn, keep_id, drop_id)

    drop_row = conn.execute("SELECT status, duplicate_of FROM cards WHERE id = ?", (drop_id,)).fetchone()
    assert drop_row["status"] == "rejected"
    assert drop_row["duplicate_of"] == keep_id

    keep_evidence = conn.execute("SELECT evidence_id FROM card_evidence WHERE card_id = ?", (keep_id,)).fetchall()
    assert {r["evidence_id"] for r in keep_evidence} == {evidence_id}


# ---------- export.export_apkg ----------

def _apkg_notes(path) -> list[tuple[str, str, str]]:
    """(guid, front, back) of every note in an .apkg (a zip holding an Anki SQLite collection)."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        db_bytes = z.read("collection.anki2")
    tmp = path.with_suffix(".anki2")
    tmp.write_bytes(db_bytes)
    c = sqlite3.connect(tmp)
    try:
        return [(guid, *flds.split("\x1f")) for guid, flds in c.execute("SELECT guid, flds FROM notes ORDER BY id")]
    finally:
        c.close()


def test_export_apkg_holds_approved_cards_with_stable_ids(conn, tmp_path, monkeypatch):
    from studytool import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")

    doc_id = _make_document(conn)
    deck_id = _make_deck(conn, doc_id)
    item = _make_item(conn, doc_id, deck_id, "main|1|p1", 1, [1])
    _make_card(conn, deck_id, item, "k1|g1|0", 0, 'Who wrote "A & B"?', "Line one\nLine two", status="approved")
    _make_card(conn, deck_id, item, "k2|g1|0", 1, "Draft front", "Draft back", status="draft")

    notes = _apkg_notes(export.export_apkg(conn, deck_id))
    assert [(f, b) for _, f, b in notes] == [('Who wrote "A &amp; B"?', "Line one<br>Line two")]

    # A second export of the same deck gives the same note id, so Anki updates instead of duplicating.
    assert _apkg_notes(export.export_apkg(conn, deck_id))[0][0] == notes[0][0]
