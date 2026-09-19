"""Measure whether a model echoes the C12 `DATA_BOUNDARY` paragraph into its answer.

    python scripts/probe_boundary_echo.py [--base-url http://localhost:11434/v1] [--model qwen2.5:0.5b]

Sends the quickstart poet task (system prompt + `DATA_BOUNDARY`, exactly as the providers
assemble it) for twenty topics at temperature 0 against any OpenAI-compatible server and
counts answers that contain the boundary paragraph's own text or the `<input` tag. The
control column is the same task with no boundary paragraph at all. No AgentOS process is
needed — this measures the model's response to the prompt shape, which is why the
`DATA_BOUNDARY` wording carries a note about it (`dagentos/providerkit/prompt.py`).

Result on 2026-09-17, qwen2.5:0.5b on Ollama: unframed sentence 7/20 echoed, framed
("Note on the input format: …") 1/20, control 0/20.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dagentos.providerkit.prompt import DATA_BOUNDARY, render_prompt

SYSTEM = "You are a terse poet. Answer with the poem only."
TOPICS = ("event logs", "audit trails", "input validation", "untrusted data", "log files",
          "database records", "user input", "tool results", "the sea", "autumn",
          "a payment ledger", "a hash chain", "step output", "a JSON object", "a webhook",
          "a queue", "HTML tags", "a system prompt", "trust", "instructions")
LEAK_MARKERS = ("<input", "</input", "supplied to this step", "instructions found inside",
                "input format", "tags is untrusted", "tags are untrusted")


def ask(base_url: str, model: str, system: str, topic: str) -> str:
    user = render_prompt("Write a haiku about {run.topic}.", {"run": {"topic": topic}})
    body = {"model": model, "temperature": 0, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = request.Request(f"{base_url.rstrip('/')}/chat/completions",
                          data=json.dumps(body).encode(),
                          headers={"content-type": "application/json"})
    with request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def leaked(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in LEAK_MARKERS)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default="http://localhost:11434/v1")
    p.add_argument("--model", default="qwen2.5:0.5b")
    a = p.parse_args()

    shapes = {
        "control (no boundary)": SYSTEM,
        "current DATA_BOUNDARY (appended, as providers do)": f"{SYSTEM}\n\n{DATA_BOUNDARY}",
    }
    worst = 0
    for label, system in shapes.items():
        bad = []
        for topic in TOPICS:
            answer = ask(a.base_url, a.model, system, topic)
            if leaked(answer):
                bad.append((topic, answer.splitlines()[0][:100] if answer else "<empty>"))
        print(f"{label}: boundary leaked in {len(bad)}/{len(TOPICS)}")
        for topic, first in bad:
            print(f"   [{topic}] {first}")
        worst = max(worst, len(bad))
    return 0 if worst <= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
