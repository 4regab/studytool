import pytest


@pytest.fixture(autouse=True)
def _own_settings_file(tmp_path, monkeypatch):
    """Tests never read or write the real data/settings.json (it may hold API keys)."""
    from studytool import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
