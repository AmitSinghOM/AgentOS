"""In-process reference implementation of the Anthropic Messages wire format as an
`httpx.MockTransport` handler, for status codes a live server will not produce on demand
(401, 429, 529). Shapes copy Anthropic's documented envelopes and what Ollama 0.34 returns
from /v1/messages. The committed cassettes were recorded against a real Ollama."""
from __future__ import annotations

import json

import httpx

MODELS = {"qwen2.5:0.5b", "llama3.2:3b", "claude-3-5-haiku-latest"}
API_KEY = "sk-ant-test-reference"


def _error(status: int, etype: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"type": "error",
                                        "error": {"type": etype, "message": message}})


def make_handler(*, require_key: bool = False, fail_with: int | None = None,
                 usage: tuple[int, int] = (14, 11)):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if require_key and request.headers.get("x-api-key") != API_KEY:
            return _error(401, "authentication_error", "invalid x-api-key")
        if path == "/v1/models" and request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"type": "model", "id": m, "display_name": m} for m in sorted(MODELS)],
                "has_more": False})
        if path == "/v1/messages" and request.method == "POST":
            if fail_with == 429:
                return _error(429, "rate_limit_error", "rate limited")
            if fail_with == 529:
                return _error(529, "overloaded_error", "Overloaded")
            if fail_with is not None:
                return _error(fail_with, "api_error", f"configured to fail with {fail_with}")
            body = json.loads(request.content)
            model = body.get("model", "")
            if model not in MODELS:
                return _error(404, "not_found_error", f"model: {model}")
            user = body["messages"][-1]["content"]
            reply = '{"score": 4, "reason": "tight imagery"}' if "JSON" in (body.get("system") or "") \
                else f"[{model}] {user[:60]}"
            pin, pout = usage
            return httpx.Response(200, json={
                "id": "msg_ref_0001", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": pin, "output_tokens": pout},
            })
        return _error(404, "not_found_error", f"no route {path}")

    return handler


def transport(**kw) -> httpx.MockTransport:
    return httpx.MockTransport(make_handler(**kw))
