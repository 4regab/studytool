"""AI provider adapter (Gemini). OWNER: agent C.

Only this module imports the provider SDK, so another provider can be added later
behind the same call_json() signature.

Rules:
- PDF text is untrusted data. Wrap it in <source>...</source> and tell the model to
  treat it as data, never instructions. No tools/function calling are enabled.
- Use schema-constrained JSON output (response_schema = the pydantic model).
- Retry transient errors (429, 5xx, timeouts) with bounded exponential backoff
  (config.AI_MAX_RETRIES). Raise AIPermanentError at once for auth (401/403),
  bad request, or a response that still fails validation after retries.
- Never log or store the API key.
"""
import random
import threading
import time
from pathlib import Path
from typing import TypeVar

from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, ValidationError

from studytool import config
from studytool.schemas import ModelUsage

T = TypeVar("T", bound=BaseModel)


class AIError(Exception):
    """Base error."""


class AITransientError(AIError):
    """Worth retrying later (rate limit, network, 5xx)."""


class AIPermanentError(AIError):
    """Do not retry: bad key, permission, malformed input. Message tells the user what to fix."""


# Lazily created, shared client. genai.Client() reads GEMINI_API_KEY itself.
_client: genai.Client | None = None
_client_lock = threading.Lock()

RETRYABLE_CODES = {429, 500, 502, 503, 504}
PERMANENT_CODES = {400, 401, 403, 404}


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = genai.Client()
    return _client


def _estimate_cost(input_tokens: int | None, output_tokens: int | None) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    return (input_tokens / 1_000_000) * config.PRICE_IN_PER_M + (output_tokens / 1_000_000) * config.PRICE_OUT_PER_M


def _usage_from_response(resp, prompt_version: str) -> ModelUsage:
    meta = getattr(resp, "usage_metadata", None)
    input_tokens = getattr(meta, "prompt_token_count", None) if meta else None
    output_tokens = getattr(meta, "candidates_token_count", None) if meta else None
    return ModelUsage(
        provider=config.AI_PROVIDER,
        model=config.GEMINI_MODEL,
        prompt_version=prompt_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=_estimate_cost(input_tokens, output_tokens),
    )


def _sanitized_schema(response_model: type[T]) -> dict:
    """Strip features Gemini's response_schema sometimes rejects (defaults, extra keywords).

    Keeps the shape (types, properties, required, enum) that Gemini documents support.
    """
    schema = response_model.model_json_schema()

    def clean(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            # Gemini doesn't support tuple-style prefixItems; approximate as a plain array.
            if "prefixItems" in node:
                items = node.pop("prefixItems")
                node["type"] = "array"
                node["items"] = clean(items[0]) if items else {"type": "string"}
            for key in ("properties",):
                if key in node and isinstance(node[key], dict):
                    for v in node[key].values():
                        clean(v)
            for key in ("items",):
                if key in node:
                    clean(node[key])
            for key in ("anyOf", "oneOf", "allOf"):
                if key in node and isinstance(node[key], list):
                    for v in node[key]:
                        clean(v)
            if "$defs" in node:
                for v in node["$defs"].values():
                    clean(v)
        return node

    return clean(schema)


def _build_contents(prompt: str, images: list[Path] | None) -> list:
    contents: list = [prompt]
    for img in images or []:
        contents.append(types.Part.from_bytes(data=img.read_bytes(), mime_type="image/png"))
    return contents


def _call_once(system: str, prompt: str, response_model: type[T], images: list[Path] | None, use_json_schema: bool):
    client = _get_client()
    gen_config_kwargs = dict(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=0,
    )
    if use_json_schema:
        gen_config_kwargs["response_json_schema"] = _sanitized_schema(response_model)
    else:
        gen_config_kwargs["response_schema"] = response_model
    return client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=_build_contents(prompt, images),
        config=types.GenerateContentConfig(**gen_config_kwargs),
    )


def call_json(
    system: str,
    prompt: str,
    response_model: type[T],
    prompt_version: str,
    images: list[Path] | None = None,
) -> tuple[T, ModelUsage]:
    """Send one request and return the parsed response plus usage (tokens + estimated cost)."""
    last_error: Exception | None = None
    use_json_schema = False  # start by trying the pydantic model directly
    json_retry_used = False

    for attempt in range(config.AI_MAX_RETRIES + 1):
        try:
            resp = _call_once(system, prompt, response_model, images, use_json_schema)
        except APIError as e:
            code = getattr(e, "code", None)
            if code in PERMANENT_CODES:
                guidance = "check GEMINI_API_KEY" if code in (401, 403) else (
                    f"model not found: set STUDYTOOL_GEMINI_MODEL (current: {config.GEMINI_MODEL})"
                    if code == 404 else "request was rejected; check prompt/schema"
                )
                # response_schema rejecting the pydantic model shows up as a 400; fall back once.
                if code == 400 and not use_json_schema:
                    use_json_schema = True
                    continue
                raise AIPermanentError(f"Gemini API error {code}: {e}. {guidance}") from e
            if code in RETRYABLE_CODES:
                last_error = e
            else:
                raise AIPermanentError(f"Gemini API error {code}: {e}") from e
        except (TimeoutError, ConnectionError, OSError) as e:
            last_error = e
        except ValidationError as e:
            # The SDK builds its own internal schema from response_model before sending the
            # request, and some pydantic features (e.g. tuple/prefixItems) fail that local
            # conversion. Fall back to a sanitized JSON schema sent as response_json_schema.
            if not use_json_schema:
                use_json_schema = True
                continue
            raise AIPermanentError(f"response schema unsupported by Gemini SDK: {e}") from e
        else:
            text = getattr(resp, "text", None)
            try:
                if text is None:
                    raise ValueError("empty response text")
                parsed = response_model.model_validate_json(text)
                return parsed, _usage_from_response(resp, prompt_version)
            except (ValidationError, ValueError) as e:
                if not json_retry_used:
                    # Invalid JSON / schema mismatch: retry once before giving up.
                    json_retry_used = True
                    last_error = e
                    continue
                raise AITransientError(f"Gemini returned invalid JSON after retry: {e}") from e

        if attempt < config.AI_MAX_RETRIES:
            backoff = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(backoff)

    raise AITransientError(f"Gemini call failed after {config.AI_MAX_RETRIES} retries: {last_error}")
