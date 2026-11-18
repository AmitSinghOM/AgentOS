"""A minimal MCP server for the tests: real newline-delimited JSON-RPC 2.0 over stdio, the
framing every stdio MCP server uses (spec 2025-11-25, basic/transports), driven by a
scenario named in `FAKE_MCP_SCENARIO`.

It implements what the executor needs of a server — `initialize`, the `notifications/
initialized` notification, `tools/list` (with pagination) and `tools/call` — and, per
scenario, misbehaves the way a real server might. Everything it receives is appended to a
JSONL file named in `FAKE_MCP_LOG` so a test can assert what the executor SENT (protocol
version, client capabilities, the rendered arguments) and what it never sent.

Scenarios:
  ok            two tools (read_file: read, write_file: write); tools/call returns one text item
  paged         same tools, served one per tools/list page via nextCursor
  structured    tools/call also returns structuredContent and a second text item
  image         tools/call returns a text item and an image item (base64 must not be inlined)
  is_error      tools/call answers isError: true with the server's SECRET env value in the text
  rpc_error     tools/call answers a JSON-RPC error (-32602 unknown tool)
  flood         tools/call answers one text item of FAKE_MCP_FLOOD_BYTES bytes (default 4 MiB)
  flood_nonl    writes FAKE_MCP_FLOOD_BYTES bytes of a frame with NO newline, then hangs
  hang          never answers tools/call (the executor must time out and kill us)
  bad_version   answers initialize with protocolVersion "1999-01-01"
  crash         exits 3 right after answering initialize
  env           tools/call returns the server's own environment as JSON text
  changed       write_file's inputSchema differs from `ok` (for the schema pin test)
"""
from __future__ import annotations

import json
import os
import sys
import time

SCENARIO = os.environ.get("FAKE_MCP_SCENARIO", "ok")
LOG = os.environ.get("FAKE_MCP_LOG")
FLOOD_BYTES = int(os.environ.get("FAKE_MCP_FLOOD_BYTES", str(4 << 20)))

READ_FILE = {
    "name": "read_file",
    "description": "Read a file",
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
}
WRITE_FILE = {
    "name": "write_file",
    "description": "Write a file",
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"},
                                                     "content": {"type": "string"}},
                    "required": ["path", "content"]},
    # Untrusted by the spec; the executor must never derive an effect class from it.
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}
TOOLS = [READ_FILE, WRITE_FILE]
if SCENARIO == "changed":
    TOOLS = [READ_FILE, dict(WRITE_FILE, inputSchema={"type": "object",
                                                       "properties": {"path": {"type": "string"},
                                                                      "content": {"type": "string"},
                                                                      "mode": {"type": "string"}},
                                                       "required": ["path", "content", "mode"]})]


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


def text(s: str) -> dict:
    return {"type": "text", "text": s}


def handle_call(rid, params: dict) -> None:
    name, args = params.get("name"), params.get("arguments") or {}
    if SCENARIO == "hang":
        while True:
            time.sleep(1)
    if SCENARIO == "rpc_error":
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
        return
    if SCENARIO == "is_error":
        secret = os.environ.get("SECRET", "<unset>")
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [text(f"permission denied for {args.get('path')} (token {secret})")],
            "isError": True}})
        return
    if SCENARIO == "flood":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [text("x" * FLOOD_BYTES)], "isError": False}})
        return
    if SCENARIO == "flood_nonl":
        # A frame that never ends: bytes without a newline, then silence. Only a cap enforced
        # WHILE reading (not after the line completes) can stop this one.
        head = '{"jsonrpc":"2.0","id":' + json.dumps(rid) + ',"result":{"content":[{"type":"text","text":"'
        sys.stdout.write(head + "y" * FLOOD_BYTES)
        sys.stdout.flush()
        while True:
            time.sleep(1)
    if SCENARIO == "env":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [text(json.dumps(dict(os.environ), sort_keys=True))], "isError": False}})
        return
    if SCENARIO == "structured":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [text("line one"), text("line two")],
            "structuredContent": {"lines": 2, "path": args.get("path")},
            "isError": False}})
        return
    if SCENARIO == "image":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [text("a picture"), {"type": "image", "data": "aW1hZ2ViYXNlNjQ=",
                                             "mimeType": "image/png"}],
            "isError": False}})
        return
    # ok / paged / changed: echo what was asked
    send({"jsonrpc": "2.0", "id": rid, "result": {
        "content": [text(f"{name} of {args.get('path')}: hello from the fake server")],
        "isError": False}})


def handle_list(rid, params: dict) -> None:
    if SCENARIO != "paged":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        return
    cursor = params.get("cursor")
    if cursor is None:
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOLS[0]], "nextCursor": "p2"}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOLS[1]]}})


def main() -> None:
    while True:
        msg = recv()
        if msg is None:
            return
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            version = "1999-01-01" if SCENARIO == "bad_version" else msg["params"]["protocolVersion"]
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-mcp", "version": "0.0.1"}}})
            if SCENARIO == "crash":
                sys.exit(3)
        elif method == "notifications/initialized":
            pass                                        # notification; nothing to answer
        elif method == "notifications/cancelled":
            pass
        elif method == "tools/list":
            handle_list(rid, msg.get("params") or {})
        elif method == "tools/call":
            handle_call(rid, msg.get("params") or {})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
