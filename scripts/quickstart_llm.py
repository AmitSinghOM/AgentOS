"""Run docs/quickstart-llm.md steps 3–5 against a running API — one command instead of
five curls. Prints what the doc says you should see and exits non-zero if it does not.

    python scripts/quickstart_llm.py [--api http://localhost:8000] [--topic "event logs"] [--sync]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def call(api: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(api + path, data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
    try:
        with request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except error.URLError as e:
        sys.exit(f"cannot reach {api}: {e.reason}. Start it with "
                 f"`uvicorn agentos.api.main:app --port 8000`.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--topic", default="event logs")
    p.add_argument("--sync", action="store_true", help="run in the API process (no worker)")
    p.add_argument("--executor", default="openai-compat",
                   help="which registered executor the example agents use "
                        "(openai-compat | anthropic | openai-agents | pydantic-ai)")
    a = p.parse_args(argv)

    # 3. check what can run
    _, executors = call(a.api, "GET", "/executors")
    names = {e["name"]: e for e in executors}
    if a.executor not in names:
        sys.exit(f"{a.executor} is not registered: `pip install -e providers/<name>` "
                 "and restart the API and worker. Registered: " + ", ".join(sorted(names)))
    health = names[a.executor].get("health", {})
    print(f"executors: {', '.join(sorted(names))}")
    print(f"{a.executor}: reachable={health.get('reachable')} "
          f"aliases_available={health.get('aliases_available')}")
    if not health.get("reachable"):
        sys.exit("model server unreachable: " + health.get("hint", health.get("error", "")))

    # 4. define agents and workflow (idempotent: re-posting the same body is a no-op)
    for f, path in (("poet_agent.json", "/agents"), ("critic_agent.json", "/agents"),
                    ("haiku_workflow.json", "/workflows")):
        definition = json.loads((EXAMPLES / f).read_text())
        if path == "/agents" and a.executor != "openai-compat":
            # Same agent, different wire format: the one field that changes. Versioned so
            # it never overwrites the openai-compat definition (agent versions are immutable).
            definition["executor"] = a.executor
            definition["version"] = 2
        code, body = call(a.api, "POST", path, definition)
        if code not in (200, 201):
            sys.exit(f"POST {path} {f}: HTTP {code} {body}")
        print(f"registered {f} → {code}")

    # 5. run it
    q = "?sync=true" if a.sync else ""
    code, run = call(a.api, "POST", f"/workflows/haiku/runs{q}", {"inputs": {"topic": a.topic}})
    if code not in (201, 202):
        sys.exit(f"start run: HTTP {code} {run}")
    print(f"run {run['id']} → {code} ({run['status']})")
    deadline = time.monotonic() + 180
    while run["status"] not in ("completed", "failed", "cancelled") and time.monotonic() < deadline:
        time.sleep(1)
        _, run = call(a.api, "GET", f"/runs/{run['id']}")
    print(f"status: {run['status']}  total_cost: {run['total_cost']}  inputs: {run['inputs']}")
    if run["status"] != "completed":
        print(f"error: {run['error']}")
        return 1
    for s in run["steps"]:
        o, pr, c = s["output"], s["provenance"], s["cost"]
        meters = {m["name"]: int(m["quantity"]) for m in c["units"]}
        print(f"\n[{s['node_id']}] model={pr['model_id']} alias={pr['model_alias']} "
              f"cost={c['amount']} {c['currency']} tokens={meters.get('input_tokens')}+"
              f"{meters.get('output_tokens')} pricing={c['pricing_snapshot_hash'][:12]}…")
        print("  " + o["text"].strip().replace("\n", "\n  "))
        if "json" in o:
            print(f"  json: {o['json']}")
    _, ev = call(a.api, "GET", f"/runs/{run['id']}/events?after=0")
    print("\nevents:", " → ".join(e["event_type"] for e in ev["data"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
