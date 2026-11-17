"""`agentos init` + `agentos run` (audit E-04): the first five minutes without a server.

  * `init <dir>` writes a minimal project (.env, agent.json, workflow.json, README.md) copied
    from the README quick start's example files, and refuses a non-empty directory (exit 2).
  * `run workflow.json --agents agent.json` registers both against an in-process engine on the
    given store and advances the run synchronously (the API's `?sync=true` path, no server):
    exit 0 completed, 3 suspended for approval, 1 failed / dead-lettered.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dagentos.cli import main as cli_main
from dagentos.store.sqlite import SqliteStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def no_env(monkeypatch):
    """`run` must not pick up the host's store/policy/keyring unless asked."""
    for var in ("AGENTOS_STORE", "AGENTOS_SQLITE_PATH", "AGENTOS_POLICY", "AGENTOS_SIGNING_KEYS"):
        monkeypatch.delenv(var, raising=False)


# ------------------------------------------------------------------ init

def test_init_writes_the_quick_start_files(tmp_path):
    target = tmp_path / "demo"
    assert cli_main(["init", str(target)]) == 0
    assert sorted(p.name for p in target.iterdir()) == [".env", "README.md", "agent.json",
                                                        "workflow.json"]
    # The agent and workflow ARE the README quick start's files (tests/test_quickstart.py).
    assert json.loads((target / "agent.json").read_text()) == json.loads(
        (EXAMPLES / "echo_agent.json").read_text())
    assert json.loads((target / "workflow.json").read_text()) == json.loads(
        (EXAMPLES / "hello_workflow.json").read_text())
    env = (target / ".env").read_text()
    assert "AGENTOS_STORE=sqlite" in env and "AGENTOS_AUTH=asserted" in env
    assert "# AGENTOS_AUTH=bearer" in env                   # the comment pointing at bearer
    readme = (target / "README.md").read_text()
    for cmd in ("agentos run workflow.json --agents agent.json",
                "uvicorn dagentos.api.main:app", "python -m dagentos.worker"):
        assert cmd in readme, cmd


def test_init_into_an_existing_empty_dir_is_fine(tmp_path):
    target = tmp_path / "demo"
    target.mkdir()
    assert cli_main(["init", str(target)]) == 0
    assert (target / "workflow.json").exists()


def test_init_refuses_a_non_empty_dir_and_writes_nothing(tmp_path, capsys):
    target = tmp_path / "taken"
    target.mkdir()
    (target / "notes.txt").write_text("mine")
    assert cli_main(["init", str(target)]) == 2
    err = capsys.readouterr().err
    assert str(target) in err and "not empty" in err
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]


# ------------------------------------------------------------------ run

def _store_arg(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'run.db'}"


def test_init_then_run_completes_with_the_echo_executor(tmp_path, no_env, capsys):
    target = tmp_path / "demo"
    assert cli_main(["init", str(target)]) == 0
    capsys.readouterr()
    assert cli_main(["run", str(target / "workflow.json"), "--agents", str(target / "agent.json"),
                     "--store", _store_arg(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "completed" in out
    # the printed run id names a run the store holds, in that state, with that many events
    store = SqliteStore(str(tmp_path / "run.db"))
    [run_id] = store.list_run_ids()
    assert run_id in out
    events = store.read_events(run_id)
    assert f"{len(events)} events" in out
    assert any(e.event_type == "run.completed" for e in events)


def test_run_json_output_and_inputs(tmp_path, no_env, capsys):
    target = tmp_path / "demo"
    assert cli_main(["init", str(target)]) == 0
    capsys.readouterr()
    assert cli_main(["--json", "run", str(target / "workflow.json"),
                     "--agents", str(target / "agent.json"), "--input", "topic=rain",
                     "--store", _store_arg(tmp_path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "completed" and doc["events"] > 0 and doc["run_id"]
    store = SqliteStore(str(tmp_path / "run.db"))
    run = store.read_events(doc["run_id"])[0]
    assert run.event_type == "run.started"


def test_run_suspends_for_approval_and_says_so_with_exit_3(tmp_path, no_env, capsys):
    """The diamond example: `payer` declares `spend`, so the run suspends before that step
    and the CLI must say so — nothing silently 'completed'."""
    agents = [str(EXAMPLES / "calc_agent.json"), str(EXAMPLES / "payer_agent.json")]
    assert cli_main(["run", str(EXAMPLES / "diamond_workflow.json"), "--agents", *agents,
                     "--store", _store_arg(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "suspended" in out and "approval" in out


def test_run_exits_1_when_a_step_is_dead_lettered(tmp_path, no_env, capsys):
    """A budget that allows nothing refuses the echo agent's `compute` before dispatch; the
    step is dead-lettered and the run fails — the exit code must say so."""
    wf = {"name": "refused", "budget": {"allowed_effect_classes": [],
                                        "approval_required_for": []},
          "nodes": [{"id": "a", "agent": "greeter", "depends_on": []}]}
    (tmp_path / "wf.json").write_text(json.dumps(wf))
    assert cli_main(["run", str(tmp_path / "wf.json"), "--agents", str(EXAMPLES / "echo_agent.json"),
                     "--store", _store_arg(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "failed" in out and "dead-lettered" in out


def test_run_rejects_a_bad_input_and_a_missing_agent_file(tmp_path, no_env, capsys):
    target = tmp_path / "demo"
    assert cli_main(["init", str(target)]) == 0
    assert cli_main(["run", str(target / "workflow.json"), "--input", "no-equals-sign",
                     "--store", _store_arg(tmp_path)]) == 2
    assert "--input" in capsys.readouterr().err
    assert cli_main(["run", str(target / "workflow.json"), "--agents", str(tmp_path / "nope.json"),
                     "--store", _store_arg(tmp_path)]) == 2
    assert "nope.json" in capsys.readouterr().err


def test_console_help_lists_init_and_run():
    from dagentos.cli import build_parser
    text = build_parser().format_help()
    assert "init" in text and "run" in text
