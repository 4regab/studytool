"""PDF import. OWNER: agent B."""
import hashlib
import sqlite3

import pymupdf

from studytool import config, db


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
    if not data:
        raise IngestError("The file is empty. Choose a PDF to upload.")

    content_hash = hashlib.sha256(data).hexdigest()

    # Dedupe: same bytes already stored -> return the existing document.
    existing = conn.execute(
        "SELECT id FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing is not None:
        return existing["id"]

    # Open from bytes so a bad file is caught before anything is written to disk.
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except pymupdf.FileDataError as exc:
        raise IngestError("This file is not a valid PDF. Choose a PDF to upload.") from exc
    except Exception as exc:  # pragma: no cover - pymupdf raises various RuntimeErrors
        raise IngestError("This PDF could not be read. The file may be damaged.") from exc

    try:
        if doc.is_encrypted:
            raise IngestError("This PDF is password-protected. Remove the password and upload again.")

        page_count = doc.page_count
        if page_count == 0:
            raise IngestError("This PDF has no pages.")

        # Touch every page once to catch a damaged/truncated file early.
        try:
            for i in range(page_count):
                doc[i].get_text()
        except Exception as exc:
            raise IngestError("This PDF could not be read. The file may be damaged.") from exc
    finally:
        doc.close()

    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
    local_path = config.PDF_DIR / f"{content_hash}.pdf"
    local_path.write_bytes(data)

    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO documents (filename, local_path, content_hash, page_count) VALUES (?, ?, ?, ?)",
            (filename, str(local_path), content_hash, page_count),
        )
        document_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO pages (document_id, page_number, extraction_status) VALUES (?, ?, 'pending')",
            [(document_id, n) for n in range(1, page_count + 1)],
        )

    return document_id
