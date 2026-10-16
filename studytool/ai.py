"""AI provider adapter: DeepSeek, Gemini or OpenRouter, all through the OpenAI-compatible chat API,
called with the standard library. The provider, key and model come from settings.py (chosen on the
Settings page) and are read before every call.

Rules:
- PDF text is untrusted data. Callers wrap it in <source>...</source> and tell the model to
  treat it as data, never instructions. No tools/function calling are enabled.
- JSON mode guarantees valid JSON but not a schema, so the pydantic model's JSON
  schema goes in the system prompt and every reply is validated with pydantic. An invalid
  reply is sent back once with the validation error so the model can fix it.
- Retry transient errors (429, 5xx, timeouts, empty replies) with bounded exponential backoff
  (config.AI_MAX_RETRIES). Raise AIPermanentError at once for a bad key, no balance, or a
  rejected request.
- Never log or store the API key.
"""
import base64
import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from studytool import config, settings
from studytool.schemas import ModelUsage

T = TypeVar("T", bound=BaseModel)


class AIError(Exception):
    """Base error."""


class AITransientError(AIError):
    """Worth retrying later (rate limit, network, 5xx)."""


class AIPermanentError(AIError):
    """Do not retry: bad key, no balance, malformed input. Message tells the user what to fix."""


RETRYABLE_CODES = {429, 500, 502, 503, 504}
PERMANENT_HINTS = {
    400: "the request was rejected as malformed",
    401: "the API key was refused; check it on the Settings page",
    402: "the account has no balance left; top it up with the provider",
    403: "the API key may not use this model",
    404: "model not found; check the model name on the Settings page",
    422: "the request had invalid parameters",
}


def _api_key(s: dict) -> str:
    if not s["key"]:
        raise AIPermanentError(f"No {s['label']} API key. Add one on the Settings page.")
    if not s["key"].isascii():
        raise AIPermanentError(f"The {s['label']} API key has unusual characters. Paste it again on the Settings page.")
    return s["key"]


def _usage(body: dict, prompt_version: str) -> ModelUsage:
    s = settings.active()
    u = body.get("usage") or {}
    tokens_in, tokens_out = u.get("prompt_tokens"), u.get("completion_tokens")
    cost = None
    if tokens_in is not None and tokens_out is not None:
        hit = u.get("prompt_cache_hit_tokens") or 0  # DeepSeek only
        cached_price = config.PRICE_IN_CACHED_PER_M if s["provider"] == "deepseek" else s["price_in"]
        cost = ((tokens_in - hit) * s["price_in"] + hit * cached_price + tokens_out * s["price_out"]) / 1_000_000
    return ModelUsage(provider=s["provider"], model=body.get("model") or s["model"],
                      prompt_version=prompt_version, input_tokens=tokens_in, output_tokens=tokens_out,
                      estimated_cost=cost)


def _post(messages: list[dict]) -> dict:
    """One HTTP request. Raises AITransientError or AIPermanentError."""
    s = settings.active()
    name = s["label"]
    payload = {
        "model": s["model"],
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": config.AI_MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    thinking = s["provider"] == "deepseek" and config.DEEPSEEK_THINKING
    if s["provider"] == "deepseek":  # DeepSeek-only switch; other providers reject unknown fields
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if not thinking:
        payload["temperature"] = 0  # ignored in thinking mode
    base = config.DEEPSEEK_BASE_URL if s["provider"] == "deepseek" else s["base_url"]
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_api_key(s)}"}
    if s["provider"] == "openrouter":
        headers["X-Title"] = "Flashcards (studytool)"
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=config.AI_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        try:  # most providers send {"error": {"message": ...}}; show just the message
            body = json.loads(detail)
            body = body[0] if isinstance(body, list) and body else body
            detail = str((body.get("error") or {}).get("message") or detail)[:300]
        except (ValueError, AttributeError):
            pass
        if e.code in RETRYABLE_CODES:
            raise AITransientError(f"{name} API error {e.code}: {detail}") from e
        hint = PERMANENT_HINTS.get(e.code, "request rejected")
        if "api key" in detail.lower():  # Gemini reports a bad key as 400
            hint = PERMANENT_HINTS[401]
        raise AIPermanentError(f"{name} API error {e.code}: {hint}. {detail}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as e:
        raise AITransientError(f"{name} network error: {e}") from e


def _system_with_schema(system: str, response_model: type[BaseModel]) -> str:
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
    return (f"{system}\n\nReply with one JSON object only (no prose, no code fences) that "
            f"matches this JSON schema:\n{schema}")


def _user_content(prompt: str, images: list[Path] | None):
    if not images:
        return prompt
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return parts


def call_json(
    system: str,
    prompt: str,
    response_model: type[T],
    prompt_version: str,
    images: list[Path] | None = None,
    retries: int | None = None,
) -> tuple[T, ModelUsage]:
    """Send one request and return the parsed response plus usage (tokens + estimated cost).

    Usage is from the final successful request; a repair round-trip's tokens are added to it.
    """
    messages = [
        {"role": "system", "content": _system_with_schema(system, response_model)},
        {"role": "user", "content": _user_content(prompt, images)},
    ]
    last_error: Exception | None = None
    repaired = False
    tokens_in = tokens_out = 0
    cost = 0.0

    retries = config.AI_MAX_RETRIES if retries is None else retries
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
        try:
            body = _post(messages)
        except AITransientError as e:
            last_error = e
            continue
        usage = _usage(body, prompt_version)
        tokens_in += usage.input_tokens or 0
        tokens_out += usage.output_tokens or 0
        cost += usage.estimated_cost or 0.0
        text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not text.strip():
            # JSON mode can occasionally return empty content; just retry.
            last_error = AITransientError("The model returned an empty reply")
            continue
        try:
            parsed = response_model.model_validate_json(text)
        except ValidationError as e:
            if repaired:
                raise AITransientError(f"The model reply did not match the schema after a repair try: {e}") from e
            # Show the model its own reply and the error once, then try again.
            repaired = True
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That JSON does not match the schema:\n{e}\n"
                                                     "Reply again with the corrected JSON object only."}]
            last_error = e
            continue
        usage.input_tokens, usage.output_tokens, usage.estimated_cost = tokens_in, tokens_out, cost
        return parsed, usage

    raise AITransientError(f"AI call failed after {retries} retries: {last_error}")
