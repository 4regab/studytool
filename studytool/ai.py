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
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from studytool.schemas import ModelUsage

T = TypeVar("T", bound=BaseModel)


class AIError(Exception):
    """Base error."""


class AITransientError(AIError):
    """Worth retrying later (rate limit, network, 5xx)."""


class AIPermanentError(AIError):
    """Do not retry: bad key, permission, malformed input. Message tells the user what to fix."""


def call_json(
    system: str,
    prompt: str,
    response_model: type[T],
    prompt_version: str,
    images: list[Path] | None = None,
) -> tuple[T, ModelUsage]:
    """Send one request and return the parsed response plus usage (tokens + estimated cost)."""
    raise NotImplementedError
