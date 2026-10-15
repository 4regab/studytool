"""CSV export. OWNER: agent D."""
import sqlite3
from pathlib import Path


def export_csv(conn: sqlite3.Connection, deck_id: int) -> Path:
    """Write approved cards only to config.EXPORT_DIR, UTF-8, header 'front,back', csv module
    quoting (QUOTE_ALL). Order: first source page, item ordinal, card ordinal. Refuses blank fields.
    Records an exports row with card ids + revisions. Returns the file path."""
    raise NotImplementedError
