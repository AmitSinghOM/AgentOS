"""Live probe behind `TOOL_NOTE` in `executor.py`: how often does a small model copy the
`<input …>` tags into a tool argument (which the server cannot parse as a tool call, so the
call degrades to plain text and the step ends with no tool run), and does the one extra
sentence in the system prompt reduce it?

    python scripts/probe_tool_boundary.py [model]      # default llama3.2:3b, local Ollama

Needs a running server at AGENTOS_OPENAI_BASE_URL (default http://127.0.0.1:11434/v1) with
the model pulled. Measured 2026-11-17 on llama3.2:3b, Ollama 0.40.16, temperature 0, seed 42:
baseline 4/10 clean calls, with TOOL_NOTE 8/10 (the two misses, "the quick brown fox" and
"HTML tags", are unchanged either way).
"""
from __future__ import annotations

import json
import os
import sys

import httpx
from agentos_provider_openai_compat.executor import TOOL_NOTE

from dagentos.providerkit.prompt import DATA_BOUNDARY

BASE_URL = os.environ.get("AGENTOS_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
TOOLS = [{"type": "function", "function": {
    "name": "word_count", "description": "Number of whitespace-separated words in `text`.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"], "additionalProperties": False}}}]
SYSTEM = "Count the words with the word_count tool, then reply with the number."
TOPICS = ["event logs", "the quick brown fox", "hash chained ledgers",
          "durable event logs for agents", "HTML tags", "replay", "a b c d e f g",
          "Ollama on a laptop", "trust boundaries", "one"]


def ask(model: str, system: str, user: str) -> tuple[list[dict], str]:
    body = {"model": model, "temperature": 0, "seed": 42, "tools": TOOLS,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    r = httpx.post(f"{BASE_URL}/chat/completions", json=body, timeout=180)
    r.raise_for_status()
    m = r.json()["choices"][0]["message"]
    calls = [json.loads(c["function"]["arguments"]) for c in (m.get("tool_calls") or [])]
    return calls, (m.get("content") or "")[:60]


def main(argv: list[str]) -> int:
    model = argv[1] if len(argv) > 1 else "llama3.2:3b"
    print(f"model: {model}  server: {BASE_URL}")
    for label, note in (("baseline", ""), ("with TOOL_NOTE", " " + TOOL_NOTE)):
        ok = 0
        for topic in TOPICS:
            calls, content = ask(model, f"{SYSTEM}\n\n{DATA_BOUNDARY}{note}",
                                 f'How many words are in <input name="run.topic">{topic}</input>?')
            good = calls == [{"text": topic}]
            ok += good
            if not good:
                print(f"  {label:15s} {topic:32s} calls={calls} content={content!r}")
        print(f"== {label}: {ok}/{len(TOPICS)} clean tool calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
