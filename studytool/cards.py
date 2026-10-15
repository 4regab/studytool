"""Review actions on cards. OWNER: agent D. Every change is persisted in SQLite."""
import sqlite3


def edit_card(conn: sqlite3.Connection, card_id: int, front: str, back: str) -> None:
    """Save an edit: revision+1, card_revisions(origin='edited'), approved -> draft."""
    raise NotImplementedError


def set_status(conn: sqlite3.Connection, card_id: int, status: str, verified_note: str | None = None) -> None:
    """approve/reject/draft. Approving needs non-blank fields, and a verified_note when the
    card's source item has an issue_reason or the card has validation flags."""
    raise NotImplementedError


def merge_duplicate(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Reject drop_id, set duplicate_of=keep_id, copy its evidence links to keep_id."""
    raise NotImplementedError
