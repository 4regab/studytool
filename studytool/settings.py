"""AI settings chosen in the app: provider, API keys, model, prices and the card-writing prompt.

Saved as JSON in the data folder (config.SETTINGS_PATH), which is never committed. The web app
writes it and the worker reads it before every AI call, so changes apply without a restart.
Keys are stored as plain text on this computer; environment variables still work as a fallback.
"""
import json
import os
import time
from pathlib import Path

from studytool import config

# All three speak the OpenAI chat-completions format.
PROVIDERS = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "env": "DEEPSEEK_API_KEY",
                 "model": config.DEEPSEEK_MODEL, "keys_url": "https://platform.deepseek.com/api_keys",
                 "price_in": config.PRICE_IN_PER_M, "price_out": config.PRICE_OUT_PER_M},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "env": "GEMINI_API_KEY", "model": "gemini-3.8-flash", "keys_url": "https://aistudio.google.com/apikey",
               "price_in": 0.0, "price_out": 0.0},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY",
                   "model": "openrouter/auto", "keys_url": "https://openrouter.ai/keys",
                   "price_in": 0.0, "price_out": 0.0},
}


def _path() -> Path:
    return config.SETTINGS_PATH


def _raw() -> dict:
    """The saved file, or {} if it is missing, unreadable or not a JSON object."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(raw: dict, name: str) -> dict:
    v = raw.get(name)
    return v if isinstance(v, dict) else {}


def _price(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if 0 <= v < 10_000 else default


def load() -> dict:
    """Current settings with defaults filled in. `keys` holds the real keys: never send it to a page.
    A hand-edited file with wrong types falls back to defaults instead of breaking every page."""
    raw = _raw()
    provider = raw.get("provider") if raw.get("provider") in PROVIDERS else "deepseek"
    prompt = raw.get("card_prompt")
    out = {"provider": provider, "keys": {}, "models": {}, "prices": {},
           "card_prompt": prompt if isinstance(prompt, str) else ""}
    keys, models, prices = _section(raw, "keys"), _section(raw, "models"), _section(raw, "prices")
    for name, p in PROVIDERS.items():
        key, model = keys.get(name), models.get(name)
        out["keys"][name] = key if isinstance(key, str) and key else os.environ.get(p["env"], "")
        out["models"][name] = model if isinstance(model, str) and model else p["model"]
        price = prices.get(name) if isinstance(prices.get(name), dict) else {}
        out["prices"][name] = {"in": _price(price.get("in"), p["price_in"]), "out": _price(price.get("out"), p["price_out"])}
    return out


def save(values: dict) -> None:
    """Merge `values` into the saved file. A key given as "" keeps the saved key; None removes it."""
    raw = _raw()
    for field in ("provider", "card_prompt"):
        if field in values:
            raw[field] = values[field]
    for group in ("models", "prices"):
        if group in values:
            raw[group] = _section(raw, group) | values[group]
    keys = _section(raw, "keys")
    for name, key in (values.get("keys") or {}).items():
        if key is None:
            keys.pop(name, None)
        elif key:
            keys[name] = key
    raw["keys"] = keys
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")  # write then rename, so the worker never reads half a file
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    for attempt in range(5):  # Windows refuses the rename while another process has the file open
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def active() -> dict:
    """What ai.py needs for the next call."""
    s = load()
    name = s["provider"]
    return {"provider": name, "label": PROVIDERS[name]["label"], "base_url": PROVIDERS[name]["base_url"],
            "env": PROVIDERS[name]["env"], "key": s["keys"][name], "model": s["models"][name],
            "price_in": s["prices"][name]["in"], "price_out": s["prices"][name]["out"]}
