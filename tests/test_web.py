"""Web UI tests: every page renders, card actions work, and requests from other sites are refused.
No network, no worker: data comes from the same fake pipeline run as test_e2e."""
import re

import pytest
from starlette.testclient import TestClient

from studytool import db
from tests import test_e2e


@pytest.fixture
def client(tmp_path, monkeypatch):
    from studytool import config
    db_path = tmp_path / "test.db"
    for name, value in (("DB_PATH", db_path), ("PDF_DIR", tmp_path / "pdfs"),
                        ("PAGE_IMG_DIR", tmp_path / "pages"), ("EXPORT_DIR", tmp_path / "exports")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "MAX_AI_CONCURRENCY", 1)
    conn = db.connect(db_path)
    db.migrate(conn)
    _, deck_id, job_id, result = test_e2e._run_full_job(conn, monkeypatch, [])
    assert result["status"] == "completed"
    with db.tx(conn):  # what worker._run_job does with the result
        conn.execute("UPDATE jobs SET status = 'completed' WHERE id = ?", (job_id,))
    conn.close()
    from studytool import web
    with TestClient(web.app, base_url="http://127.0.0.1:8501") as c:
        c.deck_id = deck_id
        c.csrf = web.CSRF_TOKEN
        yield c


def _ids(conn_path, sql, *args):
    conn = db.connect(conn_path)
    try:
        return [r[0] for r in conn.execute(sql, args)]
    finally:
        conn.close()


def test_every_page_renders(client):
    d = client.deck_id
    for url in ("/", "/documents/1", f"/decks/{d}", f"/decks/{d}/progress-panel", f"/decks/{d}/review",
                f"/decks/{d}/review?view=table", f"/decks/{d}/review?status=all&type=matching",
                f"/decks/{d}/export", "/documents/1/pages/1.png"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
    review = client.get(f"/decks/{d}/review?status=all&type=matching").text
    assert "Who excavated Hastinapur in 1950-52?" in review
    assert client.get("/decks/999").status_code == 404


def test_progress_shows_outcomes_and_reasons(client):
    html = client.get(f"/decks/{client.deck_id}").text
    assert "Done" in html and "cards written" in html and "questions turned into cards" in html


def test_card_actions_and_export(client):
    from studytool import config
    headers = {"X-CSRF-Token": client.csrf}
    card_id = _ids(config.DB_PATH, "SELECT id FROM cards WHERE status = 'draft' ORDER BY id")[0]

    r = client.post(f"/api/cards/{card_id}/edit", json={"front": "Edited?", "back": "Yes."}, headers=headers)
    assert r.status_code == 200 and r.json()["card"]["revision"] == 2
    r = client.post(f"/api/cards/{card_id}/edit", json={"front": "", "back": "x"}, headers=headers)
    assert r.status_code == 400 and "blank" in r.json()["error"]

    r = client.post(f"/api/decks/{client.deck_id}/approve", json={"all": True}, headers=headers)
    assert r.json()["approved"] >= 1
    # Flagged cards are approved too: no check note is required any more.
    assert _ids(config.DB_PATH, "SELECT COUNT(*) FROM cards WHERE status IN ('draft', 'needs_review')")[0] == 0

    r = client.post(f"/decks/{client.deck_id}/export.csv", data={"csrf": client.csrf})
    assert r.status_code == 200 and r.text.startswith('"front","back"') and "Edited?" in r.text
    r = client.post(f"/decks/{client.deck_id}/export.apkg", data={"csrf": client.csrf})
    assert r.status_code == 200 and r.content[:2] == b"PK"  # .apkg is a zip


def test_requests_from_other_sites_are_refused(client):
    from studytool import config
    card_id = _ids(config.DB_PATH, "SELECT id FROM cards ORDER BY id")[0]
    url = f"/api/cards/{card_id}/approve"
    assert client.post(url, json={}).status_code == 403  # no token
    assert client.post(url, json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(url, json={}, headers={"X-CSRF-Token": client.csrf,
                                              "Origin": "https://evil.example"}).status_code == 403
    # Upload form without the token, as a cross-site form post would send it.
    assert client.post("/documents", files={"pdf": ("x.pdf", b"%PDF")}).status_code == 403
    # DNS rebinding: a page on another host name cannot read the app.
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert _ids(config.DB_PATH, "SELECT COUNT(*) FROM cards WHERE status = 'approved'")[0] == 0


def test_new_deck_form_validates_pages(client):
    r = client.post("/documents/1/decks", data={"csrf": client.csrf, "title": "T", "content": "2-99", "key": ""},
                    follow_redirects=False)
    assert r.status_code == 400 and "between 1 and 3" in r.text
    r = client.post("/documents/1/decks", data={"csrf": client.csrf, "title": "Pilot", "content": "1-2", "key": "3"},
                    follow_redirects=False)
    assert r.status_code == 303 and re.fullmatch(r"/decks/\d+", r.headers["location"])
