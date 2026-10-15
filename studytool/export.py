"""CSV export. OWNER: agent D."""
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from studytool import config, db

STATUSES = ("draft", "needs_review", "approved", "rejected")
DISPOSITIONS = ("pending", "converted", "duplicate", "blocked", "skipped")

# Cards joined with their source item, ordered by: min page in page_refs_json,
# then source_items.ordinal, then cards.ordinal, then cards.id.
_ORDERED_CARDS_SQL = """
    SELECT c.id, c.front, c.back, c.revision, c.status, c.ordinal AS card_ordinal,
           si.page_refs_json, si.ordinal AS item_ordinal
    FROM cards c
    JOIN source_items si ON si.id = c.source_item_id
    WHERE c.deck_id = ? AND c.status = 'approved'
"""


def _min_page(page_refs_json: str) -> int:
    pages = json.loads(page_refs_json or "[]")
    return min(pages) if pages else 0


def _ordered_approved_cards(conn: sqlite3.Connection, deck_id: int) -> list[sqlite3.Row]:
    rows = conn.execute(_ORDERED_CARDS_SQL, (deck_id,)).fetchall()
    return sorted(rows, key=lambda r: (_min_page(r["page_refs_json"]), r["item_ordinal"], r["card_ordinal"], r["id"]))


def export_csv(conn: sqlite3.Connection, deck_id: int) -> Path:
    """Write approved cards only to config.EXPORT_DIR, UTF-8, header 'front,back', csv module
    quoting (QUOTE_ALL). Order: first source page, item ordinal, card ordinal. Refuses blank fields.
    Records an exports row with card ids + revisions. Returns the file path."""
    rows = _ordered_approved_cards(conn, deck_id)
    if not rows:
        raise ValueError("No approved cards to export for this deck.")
    for r in rows:
        if not r["front"].strip() or not r["back"].strip():
            raise ValueError(f"Card {r['id']} has a blank front or back; fix it before exporting.")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.EXPORT_DIR / f"deck{deck_id}_{timestamp}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(["front", "back"])
        for r in rows:
            writer.writerow([r["front"], r["back"]])

    card_ids_and_revisions = [{"id": r["id"], "revision": r["revision"]} for r in rows]
    with db.tx(conn):
        conn.execute(
            "INSERT INTO exports (deck_id, local_path, card_ids_and_revisions_json) VALUES (?, ?, ?)",
            (deck_id, str(out_path), json.dumps(card_ids_and_revisions)),
        )

    return out_path


def export_preview(conn: sqlite3.Connection, deck_id: int) -> dict:
    """Counts for the Export screen: approved/excluded cards, status breakdown, and
    source-item disposition breakdown, so the user can see what will be left out."""
    all_cards = conn.execute(
        "SELECT id, front, back, status FROM cards WHERE deck_id = ?", (deck_id,)
    ).fetchall()

    by_status = {s: 0 for s in STATUSES}
    for c in all_cards:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1

    approved = [dict(c) for c in all_cards if c["status"] == "approved"]
    excluded = [dict(c) for c in all_cards if c["status"] != "approved"]

    item_rows = conn.execute(
        "SELECT disposition FROM source_items WHERE deck_id = ?", (deck_id,)
    ).fetchall()
    by_disposition = {d: 0 for d in DISPOSITIONS}
    for r in item_rows:
        by_disposition[r["disposition"]] = by_disposition.get(r["disposition"], 0) + 1

    return {
        "approved": approved,
        "excluded": excluded,
        "by_status": by_status,
        "by_disposition": by_disposition,
    }
