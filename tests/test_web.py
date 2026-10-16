"""Web UI tests: every page renders, card actions work, and requests from other sites are refused.
No network, no worker: data comes from the same fake pipeline run as test_e2e."""
import re

import pytest
from starlette.testclient import TestClient

from studytool import ai as _ai, db
from tests import test_e2e

REAL_CALL_JSON = _ai.call_json  # the client fixture replaces ai.call_json with a fake


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



def test_settings_save_keeps_key_secret_and_switches_provider(client):
    from studytool import ai, config, settings
    form = {"csrf": client.csrf, "provider": "openrouter", "key_openrouter": "sk-or-test-1234",
            "model_openrouter": "some/model", "price_in_openrouter": "0.5", "price_out_openrouter": "1.5",
            "card_prompt": "Write short cards."}
    r = client.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303
    page_html = client.get("/settings").text
    assert "sk-or-test-1234" not in page_html and "ending in 1234" in page_html  # the key is never sent back
    s = settings.active()
    assert (s["provider"], s["key"], s["model"], s["price_in"]) == ("openrouter", "sk-or-test-1234", "some/model", 0.5)
    # Saving again with a blank key keeps the saved one.
    client.post("/settings", data={**form, "key_openrouter": ""}, follow_redirects=False)
    assert settings.active()["key"] == "sk-or-test-1234"
    # The custom prompt is used, and the PDF-is-data rule is added back.
    from studytool import generate
    prompt, version = generate.card_prompt()
    assert prompt.startswith("Write short cards.") and "<source>" in prompt and "custom" in version
    assert client.post("/settings", data={**form, "price_in_openrouter": "abc"}).status_code == 400
    assert client.post("/settings", data={**form, "key_openrouter": "sk-or\u200b-x"}).status_code == 400  # zero-width space
    # A broken hand-edited file falls back to defaults instead of breaking every page.
    config.SETTINGS_PATH.write_text('{"keys": "x", "prices": {"gemini": 5}, "card_prompt": 5}', encoding="utf-8")
    assert client.get("/settings").status_code == 200 and client.get("/").status_code == 200
    settings.save({"provider": "openrouter", "keys": {"openrouter": "sk-or-test-1234"}, "models": {"openrouter": "some/model"}})
    # A request goes to OpenRouter with the saved key and model.
    sent = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices": [{"message": {"content": "{\\"ok\\": true}"}}], "usage": {}}'

    def _urlopen(req, timeout):
        sent.update(url=req.full_url, auth=req.headers["Authorization"], body=req.data)
        return _Resp()

    import urllib.request
    monkey = pytest.MonkeyPatch()
    monkey.setattr(urllib.request, "urlopen", _urlopen)
    monkey.setattr(ai, "call_json", REAL_CALL_JSON)
    try:
        r = client.post("/api/settings/test", json={}, headers={"X-CSRF-Token": client.csrf})
    finally:
        monkey.undo()
    assert r.status_code == 200, r.text
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["auth"] == "Bearer sk-or-test-1234" and b'"some/model"' in sent["body"] and b"thinking" not in sent["body"]
