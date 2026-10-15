"""SQLite access. Every process opens its own connection with connect().

Connections run in autocommit mode. Group writes with `with tx(conn):` and keep
those blocks short. Never hold a transaction open across an AI call.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from studytool import config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Short write transaction. BEGIN IMMEDIATE takes the write lock up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply unapplied migrations in file-name order. Returns the names applied."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name in done:
            continue
        # executescript commits on its own, so the migration and its record go in one script.
        try:
            conn.executescript(
                "BEGIN;\n" + f.read_text(encoding="utf-8")
                + f"\nINSERT INTO schema_migrations(name) VALUES ('{f.name}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(f.name)
    return applied


def backup(conn: sqlite3.Connection, dest: Path | str) -> None:
    """Consistent copy of a live database (safe while the worker is writing)."""
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
