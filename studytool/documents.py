"""PDF import. OWNER: agent B."""
import sqlite3


class IngestError(Exception):
    """User-facing message: what is wrong with the file and what to do."""


def ingest_pdf(conn: sqlite3.Connection, filename: str, data: bytes) -> int:
    """Validate and store a PDF; return documents.id.

    - Reject empty, non-PDF, encrypted/password-protected, zero-page or unreadable files
      with an IngestError that says what to do.
    - sha256 of the bytes is content_hash. Same hash already stored -> return that id.
    - Save to config.PDF_DIR / f"{hash}.pdf"; insert documents row and one pages row
      per page (extraction_status 'pending').
    """
    raise NotImplementedError
