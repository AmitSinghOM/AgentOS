"""In-process reference implementation of the OpenAI chat-completions wire format, as an
`httpx.MockTransport` handler. Used to unit-test specific status codes (401, 429, 5xx)
without a server, and as the `--no-live` fallback of `scripts/record_cassettes.py`. The
committed cassettes were recorded against a real Ollama (see each file's `source`).

Response and error shapes copy what Ollama 0.34 and the OpenAI API actually return, so a
test that passes here exercises the same parsing paths as production.
"""
from __future__ import annotations

import json

import httpx

MODELS = {"qwen2.5:0.5b", "llama3.2:3b", "gpt-4o-mini"}
API_KEY = "sk-test-reference"          # only enforced when `require_key` is on


def _reply(prompt: str, model: str) -> str:
    if "haiku" in prompt.lower():
        return "Event log flows on—\neach step a stone in the stream;\nreplay finds the path."
    if "json" in prompt.lower():
        return json.dumps({"summary": "AgentOS keeps state in an append-only log.",
                           "confidence": 0.9})
    return f"[{model}] You said: {prompt[:80]}"


def make_handler(*, require_key: bool = False, fail_with: int | None = None,
                 usage: tuple[int, int] = (12, 9)):
    """Build a handler. `fail_with` makes /chat/completions answer that status."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if require_key and request.headers.get("authorization") != f"Bearer {API_KEY}":
            return httpx.Response(401, json={"error": {
                "message": "Incorrect API key provided.", "type": "invalid_request_error",
                "code": "invalid_api_key"}})
        if path.endswith("/models") and request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "library"} for m in sorted(MODELS)]})
        if path.endswith("/chat/completions") and request.method == "POST":
            if fail_with is not None:
                return httpx.Response(fail_with, json={"error": {
                    "message": f"reference server configured to fail with {fail_with}",
                    "type": "server_error"}})
            body = json.loads(request.content)
            model = body.get("model", "")
            if model not in MODELS:            # Ollama 0.34's exact 404 body
                return httpx.Response(404, json={"error": {
                    "message": f"model '{model}' not found",
                    "type": "not_found_error", "param": None, "code": None}})
            user = next((m["content"] for m in reversed(body["messages"])
                         if m["role"] == "user"), "")
            content = _reply(user, model)
            pin, pout = usage
            return httpx.Response(200, json={
                "id": "chatcmpl-ref-0001", "object": "chat.completion", "created": 1757800000,
                "model": model, "system_fingerprint": "fp_ollama",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": pin, "completion_tokens": pout,
                          "total_tokens": pin + pout},
            })
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})

    return handler


def transport(**kw) -> httpx.MockTransport:
    return httpx.MockTransport(make_handler(**kw))
