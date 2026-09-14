"""Generate varied demo traffic against a running API so the Grafana dashboard and Jaeger
have something to show: successful runs on both providers, a run suspended on a spend
approval (then approved), and a run that dead-letters on a missing model.

    python scripts/demo_traffic.py [--api http://localhost:8000] [--rounds 3]
"""
from __future__ import annotations

import argparse
import json
import time
from urllib import error, request

TOPICS = ["event logs", "durable execution", "a fenced lease", "human approval",
          "idempotency keys", "replaying the log", "a pricing hash", "two wire formats"]


def call(api, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(api + path, data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
    try:
        with request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(api, run_id, until=("completed", "failed", "cancelled", "suspended"), timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, run = call(api, "GET", f"/runs/{run_id}")
        if run["status"] in until:
            return run
        time.sleep(1)
    return run


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--rounds", type=int, default=3)
    a = p.parse_args(argv)

    poet = {"name": "poet", "type": "llm", "executor": "openai-compat", "config": {
        "model": "chat.fast", "system": "You are a terse poet. Answer with the poem only.",
        "prompt": "Write a haiku about {run.topic}."}}
    critic = {"name": "critic", "type": "llm", "executor": "openai-compat", "config": {
        "model": "chat.fast", "json_output": True,
        "prompt": "Rate this haiku 1-5 as JSON with keys score and reason:\n\n{write.text}"}}
    for agent in (poet, critic):
        call(a.api, "POST", "/agents", agent)
        call(a.api, "POST", "/agents", {**agent, "version": 2, "executor": "anthropic"})
    call(a.api, "POST", "/agents", {"name": "payer", "type": "echo",
                                     "declared_effects": ["compute", "spend"]})
    call(a.api, "POST", "/agents", {"name": "ghost", "type": "llm", "executor": "openai-compat",
                                     "config": {"model": "no-such-model:1b", "prompt": "hi"}})
    call(a.api, "POST", "/workflows", {"name": "haiku", "budget": {"max_run_cost": "0.01"},
                                        "nodes": [{"id": "write", "agent": "poet"},
                                                  {"id": "review", "agent": "critic",
                                                   "depends_on": ["write"],
                                                   "retry": {"max_attempts": 3}}]})
    call(a.api, "POST", "/workflows", {"name": "payment", "nodes": [
        {"id": "write", "agent": "poet"}, {"id": "pay", "agent": "payer", "depends_on": ["write"]}]})
    call(a.api, "POST", "/workflows", {"name": "haunted", "nodes": [
        {"id": "summon", "agent": "ghost", "retry": {"max_attempts": 2, "backoff_seconds": 1}}]})

    for r in range(a.rounds):
        for topic in TOPICS:
            _, run = call(a.api, "POST", "/workflows/haiku/runs", {"inputs": {"topic": topic}})
            run = wait(a.api, run["id"])
            print(f"haiku    {topic:20s} {run['status']}")
        _, run = call(a.api, "POST", "/workflows/payment/runs", {"inputs": {"topic": "money"}})
        run = wait(a.api, run["id"])
        print(f"payment  suspended={run['status'] == 'suspended'}")
        if run["status"] == "suspended":
            time.sleep(3)                                             # visible approval wait
            (aid,) = run["approvals"]
            call(a.api, "POST", f"/runs/{run['id']}/approvals/{aid}/approve",
                 {"principal": {"kind": "human", "id": "amit"}, "reason": "demo"})
            run = wait(a.api, run["id"], until=("completed", "failed"))
            print(f"payment  after approval: {run['status']}")
        _, run = call(a.api, "POST", "/workflows/haunted/runs")
        run = wait(a.api, run["id"], until=("completed", "failed"))
        print(f"haunted  {run['status']}: {(run['error'] or '')[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
