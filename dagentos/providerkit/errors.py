"""Shared error vocabulary. The engine records any of these as `step.failed` → retries per
the node's policy → `step.dead_lettered` with the message as the cause, so the message is
what an operator reads. Every one says what happened AND what to do."""
from __future__ import annotations

import httpx


class ProviderError(RuntimeError):
    pass


class ProviderUnreachable(ProviderError):
    pass


class AuthenticationFailed(ProviderError):
    pass


class ModelNotFound(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class ProviderServerError(ProviderError):
    pass


class BadResponse(ProviderError):
    pass


class TemplateError(ProviderError):
    pass


def server_message(r: httpx.Response) -> str:
    """The server's own words, appended to ours: `{"error": {"message": …}}` (OpenAI,
    Ollama, Anthropic) or `{"error": "…"}`; falls back to the raw body."""
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        return f"Server said: {msg}" if msg else ""
    except (ValueError, AttributeError):
        return f"Server said: {r.text[:160]!r}" if r.text else ""
