"""Review actions on cards. OWNER: agent D. Every change is persisted in SQLite."""
import sqlite3

from studytool import db

VALID_STATUSES = {"draft", "needs_review", "approved", "rejected"}


def edit_card(conn: sqlite3.Connection, card_id: int, front: str, back: str) -> None:
    """Save an edit: revision+1, card_revisions(origin='edited'), approved -> draft."""
    front = front.strip()
    back = back.strip()
    if not front or not back:
        raise ValueError("Front and back cannot be blank.")

    row = conn.execute("SELECT front, back, status, revision FROM cards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise ValueError(f"Card {card_id} not found.")

    if row["front"] == front and row["back"] == back:
        return  # unchanged, nothing to do

    # approved cards go back to draft on edit; needs_review stays needs_review
    new_status = "draft" if row["status"] == "approved" else row["status"]

    with db.tx(conn):
        conn.execute(
            "UPDATE cards SET front = ?, back = ?, status = ?, revision = revision + 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (front, back, new_status, card_id),
        )
        conn.execute(
            "INSERT INTO card_revisions (card_id, front, back, origin) VALUES (?, ?, ?, 'edited')",
            (card_id, front, back),
        )


def set_status(conn: sqlite3.Connection, card_id: int, status: str, verified_note: str | None = None) -> None:
    """approve/reject/draft. Approving needs non-blank fields. Approving a flagged card is the
    user's own check; the warnings are shown on the review screen, no note is required."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}.")

    row = conn.execute("SELECT front, back FROM cards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise ValueError(f"Card {card_id} not found.")

    note = (verified_note or "").strip()

    if status == "approved" and (not row["front"].strip() or not row["back"].strip()):
        raise ValueError("Cannot approve a card with a blank front or back.")

    with db.tx(conn):
        conn.execute(
            "UPDATE cards SET status = ?, verified_note = ?, updated_at = datetime('now') WHERE id = ?",
            (status, note or None, card_id),
        )


def merge_duplicate(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Reject drop_id, set duplicate_of=keep_id, copy its evidence links to keep_id."""
    if keep_id == drop_id:
        raise ValueError("Cannot merge a card into itself.")

    keep = conn.execute("SELECT id FROM cards WHERE id = ?", (keep_id,)).fetchone()
    if keep is None:
        raise ValueError(f"Card {keep_id} not found.")
    drop = conn.execute("SELECT id FROM cards WHERE id = ?", (drop_id,)).fetchone()
    if drop is None:
        raise ValueError(f"Card {drop_id} not found.")

    with db.tx(conn):
        conn.execute(
            "UPDATE cards SET status = 'rejected', duplicate_of = ?, updated_at = datetime('now') WHERE id = ?",
            (keep_id, drop_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO card_evidence (card_id, evidence_id) "
            "SELECT ?, evidence_id FROM card_evidence WHERE card_id = ?",
            (keep_id, drop_id),
        )
