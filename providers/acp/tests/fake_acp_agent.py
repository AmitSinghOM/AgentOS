"""A minimal ACP agent for the tests: real newline-delimited JSON-RPC 2.0 over stdio, the
same framing `kiro-cli acp` uses, driven by a scenario named in `FAKE_ACP_SCENARIO`.

It implements the baseline the protocol requires of every agent (`initialize`,
`session/new`, `session/prompt`, `session/cancel`) and, per scenario, emits the client-bound
traffic the executor must handle: `session/update` notifications (message chunks, tool
calls, usage) and `session/request_permission` requests. Everything it receives is echoed
to a JSONL file named in `FAKE_ACP_LOG` so a test can assert what the executor SENT
(capabilities, prompt text, the permission option it selected).

Scenarios:
  text          two agent_message_chunks, end_turn
  tool_read     a `read`-kind tool_call (no permission asked) then end_turn
  permission    asks permission for an `execute`-kind tool; runs it only if allowed;
                then end_turn. The selected option id is logged.
  undeclared    performs an `edit`-kind tool_call WITHOUT asking, end_turn
  usage         emits a usage_update with a cost, end_turn
  refusal       stopReason=refusal
  hang          never answers session/prompt (the executor must time out and kill us)
  bad_version   answers initialize with protocolVersion 99
  crash         exits 3 after initialize
"""
from __future__ import annotations

import json
import os
import sys
import time

SCENARIO = os.environ.get("FAKE_ACP_SCENARIO", "text")
LOG = os.environ.get("FAKE_ACP_LOG")
_next_id = 1000


def _log(direction: str, msg: dict) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"dir": direction, "msg": msg}) + "\n")


def send(msg: dict) -> None:
    _log("out", msg)
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def recv() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    msg = json.loads(line)
    _log("in", msg)
    return msg


def notify(session_id: str, update: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session_id, "update": update}})


def request(method: str, params: dict) -> dict:
    """Agent -> client request; block until the matching response arrives."""
    global _next_id
    _next_id += 1
    rid = _next_id
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        msg = recv()
        if msg is None:
            sys.exit(0)
        if msg.get("id") == rid and ("result" in msg or "error" in msg):
            return msg
        # Anything else while we wait (e.g. session/cancel) is ignored by this fake.


def chunk(session_id: str, text: str) -> None:
    notify(session_id, {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text}})


def prompt_turn(session_id: str, req_id, params: dict) -> None:
    stop = "end_turn"
    if SCENARIO == "text":
        chunk(session_id, "Hello from ")
        chunk(session_id, "the fake agent.")
    elif SCENARIO == "tool_read":
        notify(session_id, {"sessionUpdate": "tool_call", "toolCallId": "call_1",
                            "title": "Read README.md", "kind": "read", "status": "in_progress",
                            "rawInput": {"path": "/tmp/README.md"}})
        notify(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_1",
                            "status": "completed", "rawOutput": {"text": "# hi"}})
        chunk(session_id, "Read it.")
    elif SCENARIO == "permission":
        notify(session_id, {"sessionUpdate": "tool_call", "toolCallId": "call_x",
                            "title": "Run pytest", "kind": "execute", "status": "pending",
                            "rawInput": {"command": "pytest -q"}})
        resp = request("session/request_permission", {
            "sessionId": session_id,
            "toolCall": {"toolCallId": "call_x", "title": "Run pytest", "kind": "execute",
                         "rawInput": {"command": "pytest -q"}},
            "options": [
                {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ]})
        outcome = resp.get("result", {}).get("outcome", {})
        if outcome.get("outcome") == "selected" and outcome.get("optionId", "").startswith("allow"):
            notify(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_x",
                                "status": "completed", "rawOutput": {"exit": 0}})
            chunk(session_id, "Tests pass.")
        else:
            notify(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_x",
                                "status": "failed"})
            chunk(session_id, "I was not allowed to run the tests.")
    elif SCENARIO == "undeclared":
        notify(session_id, {"sessionUpdate": "tool_call", "toolCallId": "call_e",
                            "title": "Edit main.py", "kind": "edit", "status": "completed",
                            "rawInput": {"path": "/tmp/main.py"}, "rawOutput": {"ok": True}})
        chunk(session_id, "Edited.")
    elif SCENARIO == "tool_failed":
        # The agent RAN an edit (no permission asked) and the tool reported failure — a
        # half-applied change is still a change. The executor must report it as an effect.
        notify(session_id, {"sessionUpdate": "tool_call", "toolCallId": "call_f",
                            "title": "Edit main.py", "kind": "edit", "status": "in_progress",
                            "rawInput": {"path": "/tmp/main.py"}})
        notify(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "call_f",
                            "status": "failed", "rawOutput": {"error": "disk full after 3 of 5 hunks"}})
        chunk(session_id, "The edit failed partway.")
    elif SCENARIO == "usage":
        chunk(session_id, "Done.")
        notify(session_id, {"sessionUpdate": "usage_update", "used": 1234, "size": 200000,
                            "cost": {"amount": 0.0042, "currency": "USD"}})
    elif SCENARIO == "refusal":
        stop = "refusal"
    elif SCENARIO == "hang":
        while True:
            time.sleep(1)
    elif SCENARIO == "answer_then_exit":
        # Final response written and the process gone in the same instant: the client
        # must deliver the response, not report a crash.
        chunk(session_id, "Bye.")
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        os._exit(0)
    send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": stop}})


def main() -> None:
    session_id = "sess-fake-1"
    while True:
        msg = recv()
        if msg is None:
            return
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            version = 99 if SCENARIO == "bad_version" else msg["params"]["protocolVersion"]
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version,
                "agentCapabilities": {"loadSession": False},
                "agentInfo": {"name": "fake-acp", "version": "0.0.1"}}})
            if SCENARIO == "crash":
                sys.exit(3)
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": session_id}})
        elif method == "session/prompt":
            prompt_turn(session_id, rid, msg["params"])
        elif method == "session/cancel":
            pass                                     # notification; nothing to answer
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
