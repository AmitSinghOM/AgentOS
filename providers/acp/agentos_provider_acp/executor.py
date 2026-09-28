"""An ACP agent as an AgentOS inner harness.

One AgentOS step = one ACP prompt turn in a fresh session of a fresh agent process
(`kiro-cli acp` by default): the agent may plan, call its own tools and loop as it likes; the
whole turn is recorded as ONE `step.completed` — the agent's text, its stop reason, every
tool call's title/kind/status with argument and output hashes, context-window usage metered
as cost, and an `Effect` per class of tool the agent actually ran. The governor gated the
step before this executor was spawned; the governor's `_settle` checks the reported effects
after. The ACP agent owns its loop and its tools; AgentOS owns the ledger and the authority.

The governance seam that makes this more than a subprocess wrapper:

- **Permission requests are answered from the step's declaration, not by a human at a
  terminal.** When the agent asks `session/request_permission` for a tool, the tool's ACP
  `kind` is mapped to an `EffectClass`; if that class is in `req.declared_effects` the client
  selects the agent's `allow_once` option, otherwise `reject_once`. `allow_always` is never
  selected — a decision belongs to one governed step, never to the agent's memory. The human
  who approved the step (or the operator who wrote the declaration) already made this call.
- **Tool calls the agent runs without asking are reported honestly.** They land in the
  trajectory by mapped class, so an agent that edits a file under a step declared
  `[compute, read]` produces a step the core dead-letters (`undeclared effect
  'execute_code'`) rather than a quiet success. A rejected request never ran, so it is
  recorded as `permission: rejected` but is NOT an effect.
- **The client lends the agent nothing of AgentOS's.** No `fs` or `terminal` capability is
  advertised; the agent uses its own tools in `cwd`, under its own login. The worker's
  environment is inherited (that is how kiro-cli finds its credentials) and nothing from it
  is recorded.

Boundaries, each tested with a real ACP subprocess (`tests/fake_acp_agent.py`):
- Inputs are delimited data (`render_prompt` / `wrap_input`) under `DATA_BOUNDARY` (C12).
- One process per step, torn down in `finally`; a turn past `timeout_seconds` gets
  `session/cancel` then SIGTERM/SIGKILL on the whole process group.
- `progress()` is called on every inbound frame: it renews the lease and is the engine's
  cooperative cancel token; when it raises, the agent is cancelled and killed.
- Stop reasons other than `end_turn` are `BadResponse` (retry policy applies);
  `max_turn_requests`/`max_tokens` name the agent-side limit that ended the turn.
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

from dagentos.core.models import (
    Cost,
    Effect,
    EffectClass,
    Meter,
    Provenance,
    StepRequest,
    StepResult,
)
from dagentos.core.ports import ProgressFn
from dagentos.providerkit.errors import BadResponse, ProviderError, ProviderUnreachable
from dagentos.providerkit.prompt import DATA_BOUNDARY, render_prompt, wrap_input

from .config import AcpConfig, from_env
from .protocol import (
    CLIENT_CAPABILITIES,
    PROTOCOL_VERSION,
    AcpClient,
    AcpRpcError,
    AcpTimeout,
    AcpTransportError,
)

NAME = "acp"
try:
    VERSION = _dist_version("agentos-provider-acp")
except PackageNotFoundError:  # running from a checkout without install
    VERSION = "0.0.0+dev"

# ACP ToolKind -> the AgentOS effect class it is reported as. Conservative: anything that
# mutates the host or runs a command is `execute_code`; anything unknown (including a kind
# this provider has never seen) is treated as the most dangerous local class.
KIND_TO_EFFECT: dict[str, EffectClass] = {
    "read": EffectClass.read,
    "search": EffectClass.read,
    "fetch": EffectClass.read,
    "think": EffectClass.compute,
    "switch_mode": EffectClass.compute,
    "edit": EffectClass.execute_code,
    "delete": EffectClass.execute_code,
    "move": EffectClass.execute_code,
    "execute": EffectClass.execute_code,
    "other": EffectClass.execute_code,
}
UNKNOWN_KIND_EFFECT = EffectClass.execute_code
HEALTH_TIMEOUT_SECONDS = 20.0
HEALTH_CACHE_SECONDS = 60.0


def effect_class_for_kind(kind: str | None) -> EffectClass:
    return KIND_TO_EFFECT.get(kind or "", UNKNOWN_KIND_EFFECT)


def permission_decision(options: list[dict], *, allowed: bool) -> tuple[str | None, str]:
    """Pick the option id to select for a `session/request_permission`, and the label to
    record. Never `allow_always`: a grant is scoped to this step. If the agent offers no
    acceptable option for the decision, cancel the request rather than widen it."""
    by_kind: dict[str, str] = {}
    for opt in options:
        kind, oid = opt.get("kind"), opt.get("optionId")
        if isinstance(kind, str) and isinstance(oid, str) and kind not in by_kind:
            by_kind[kind] = oid
    if allowed and "allow_once" in by_kind:
        return by_kind["allow_once"], "allowed"
    if "reject_once" in by_kind:
        return by_kind["reject_once"], "rejected"
    if "reject_always" in by_kind:
        return by_kind["reject_always"], "rejected"
    return None, "cancelled"


class AcpExecutor:
    name = NAME
    version = VERSION

    def __init__(self, config: AcpConfig | None = None) -> None:
        self.config = config or from_env()
        self._health_cache: tuple[float, dict] | None = None

    # -- optional hooks ------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "wire_format": "acp", "protocol_version": PROTOCOL_VERSION,
            "command": list(self.config.command), "cwd": str(self.config.cwd),
            "timeout_seconds": self.config.timeout_seconds,
            "client_capabilities": CLIENT_CAPABILITIES,
            "kind_to_effect": {k: v.value for k, v in KIND_TO_EFFECT.items()},
        }

    def health(self) -> dict:
        """Start the agent, `initialize`, report what it says about itself, stop it. Cached
        for `HEALTH_CACHE_SECONDS`: `GET /executors` calls this on every request, and
        spawning kiro-cli costs seconds, not milliseconds."""
        now = time.monotonic()
        if self._health_cache and now - self._health_cache[0] < HEALTH_CACHE_SECONDS:
            return dict(self._health_cache[1])
        client = AcpClient(self.config.command, self.config.cwd, env=self.config.env)
        try:
            client.start()
            client.initialize(now + HEALTH_TIMEOUT_SECONDS)
        except AcpTransportError as exc:
            report = {"reachable": False, "error": str(exc), "hint": self._unreachable_hint()}
        except (AcpTimeout, AcpRpcError) as exc:
            report = {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            report = {"reachable": True, "agent": client.agent_info,
                      "agent_capabilities": client.agent_capabilities}
        finally:
            client.close()
        self._health_cache = (now, report)
        return dict(report)

    # -- the port -----------------------------------------------------------------------
    def execute(self, req: StepRequest, progress: ProgressFn) -> StepResult:
        cfg = req.agent.config
        text_in = self._prompt(cfg, req.inputs)
        prompt_hash = _sha(json.dumps({"prompt": text_in, "command": self.config.command},
                                      sort_keys=True, separators=(",", ":")))
        turn = _Turn(req.declared_effects, progress)
        client = AcpClient(self.config.command, self.config.cwd, env=self.config.env,
                           on_notification=turn.on_notification, on_request=turn.on_request)
        deadline = time.monotonic() + self.config.timeout_seconds
        session_id: str | None = None
        progress(0.0, f"acp spawn {self.config.command[0]} cwd={self.config.cwd}")
        try:
            try:
                client.start()
                client.initialize(deadline)
                session_id = client.new_session(deadline)
                progress(0.05, f"acp session {session_id} on {client.agent_info.get('name')}")
                stop = client.prompt(session_id, text_in, deadline)
            except AcpTimeout as exc:
                if session_id:
                    client.cancel(session_id)
                raise ProviderError(
                    f"acp turn timed out after {self.config.timeout_seconds:g}s during {exc}; "
                    f"the agent was cancelled and killed. Raise AGENTOS_ACP_TIMEOUT or give "
                    f"the step a smaller task") from exc
            except AcpTransportError as exc:
                raise ProviderUnreachable(f"{exc}. {self._unreachable_hint()}") from exc
            except AcpRpcError as exc:
                if exc.code == -32000:
                    raise ProviderUnreachable(
                        f"agent requires authentication: {exc}. Log the agent in on this host "
                        f"(kiro-cli: `kiro-cli login`) — the worker inherits that session") from exc
                raise BadResponse(f"acp {exc}") from exc
            except BaseException:
                # Engine cancel / lease loss raised from progress(), or anything else:
                # tell the agent, then the finally below kills it.
                if session_id:
                    client.cancel(session_id)
                raise
        finally:
            client.close()

        if stop != "end_turn":
            raise BadResponse(f"acp turn ended with stopReason {stop!r} (not end_turn): "
                              f"{_STOP_HINTS.get(stop, 'the agent did not finish the task')}; "
                              f"partial text: {turn.text[:120]!r}")

        calls = turn.calls()
        ran_classes = sorted({c["effect_class"] for c in calls
                              if c.get("permission") != "rejected"
                              and c.get("status") != "failed"})
        cost = turn.cost(len(calls))
        progress(1.0, f"acp end_turn: {len(calls)} tool call(s), "
                      f"{turn.context_used or 0} context tokens, {cost.amount} {cost.currency}")

        output: dict[str, Any] = {
            "text": turn.text, "stop_reason": stop, "tool_calls": calls,
            "agent": client.agent_info,
            "usage": {"context_tokens_used": turn.context_used, "context_window": turn.context_size},
        }
        if turn.plan is not None:
            output["plan"] = turn.plan
        effects = [Effect(effect_class=EffectClass.compute,
                          description=f"acp turn on {client.agent_info.get('name') or self.config.command[0]}")]
        effects += [Effect(effect_class=EffectClass(ec), description=f"acp tool kind(s) mapped to {ec}")
                    for ec in ran_classes if ec != EffectClass.compute.value]
        return StepResult(
            output=output, effects=effects, cost=cost,
            provenance=Provenance(executor=self.name, executor_version=self.version,
                                  model_id=client.agent_info.get("name"),
                                  prompt_hash=prompt_hash),
        )

    # -- internals ----------------------------------------------------------------------
    @staticmethod
    def _prompt(cfg: dict, inputs: dict) -> str:
        # C12: the agent definition is the only source of instructions; everything from
        # inputs is delimited data and the text says so. ACP has no separate system slot on
        # the wire (that lives in the agent's own config), so instructions lead the prompt.
        parts: list[str] = []
        text = cfg.get("instructions") or cfg.get("system")
        if text:
            parts.append(str(text))
        parts.append(DATA_BOUNDARY)
        if cfg.get("prompt"):
            parts.append(render_prompt(str(cfg["prompt"]), inputs))
        elif inputs:
            parts.append(wrap_input("inputs", json.dumps(inputs, sort_keys=True, ensure_ascii=False)))
        else:
            parts.append("Hello.")
        return "\n\n".join(parts)

    def _unreachable_hint(self) -> str:
        prog = self.config.command[0]
        if prog.startswith("kiro-cli"):
            return ("Is kiro-cli installed and logged in on this host? `kiro-cli --version`, "
                    "`kiro-cli login`. Set AGENTOS_ACP_COMMAND to point at another ACP agent")
        return f"Check AGENTOS_ACP_COMMAND ({self.config.command!r}) runs and speaks ACP on stdio"


_STOP_HINTS = {
    "refusal": "the agent refused to continue",
    "max_tokens": "the agent hit its token limit for the turn",
    "max_turn_requests": "the agent hit its per-turn request limit",
    "cancelled": "the turn was cancelled",
}


class _Turn:
    """Everything one prompt turn produced, folded from the inbound frames."""

    def __init__(self, declared: frozenset[EffectClass], progress: ProgressFn) -> None:
        self._declared = declared
        self._progress = progress
        self._chunks: list[str] = []
        self._calls: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self.context_used: int | None = None
        self.context_size: int | None = None
        self._cost_amount: str | None = None
        self._cost_currency = "USD"
        self.plan: list[dict] | None = None
        self._frames = 0

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def on_notification(self, method: str, params: dict) -> None:
        self._frames += 1
        if method != "session/update":
            return
        upd = params.get("update") or {}
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = upd.get("content") or {}
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                self._chunks.append(content["text"])
        elif kind == "tool_call":
            self._upsert_call(upd, new=True)
        elif kind == "tool_call_update":
            self._upsert_call(upd, new=False)
        elif kind == "usage_update":
            self.context_used = _int_or_none(upd.get("used"))
            self.context_size = _int_or_none(upd.get("size"))
            cost = upd.get("cost") or {}
            if cost.get("amount") is not None:
                self._cost_amount = _decimal_str(cost["amount"])
                self._cost_currency = str(cost.get("currency") or "USD")
        elif kind == "plan":
            entries = upd.get("entries")
            if isinstance(entries, list):
                self.plan = [{"content": str(e.get("content", "")), "status": e.get("status"),
                              "priority": e.get("priority")} for e in entries if isinstance(e, dict)]
        # Renew the lease / poll the cancel token on every frame (the engine rate-limits
        # what actually reaches the log).
        self._progress(0.5, f"acp {kind or method}")

    def on_request(self, method: str, params: dict) -> dict:
        self._frames += 1
        if method != "session/request_permission":
            # fs/*, terminal/*: we advertised none of them; the base class rejects with -32601.
            from .protocol import _RejectRequest
            raise _RejectRequest(f"client does not implement {method} (capability not advertised)")
        tool = params.get("toolCall") or {}
        call_id = str(tool.get("toolCallId") or f"perm-{self._frames}")
        ec = effect_class_for_kind(tool.get("kind") or self._calls.get(call_id, {}).get("kind"))
        allowed = ec in self._declared
        option_id, label = permission_decision(params.get("options") or [], allowed=allowed)
        rec = self._calls.setdefault(call_id, self._new_call(call_id))
        if call_id not in self._order:
            self._order.append(call_id)
        rec.update({"title": tool.get("title", rec.get("title")), "kind": tool.get("kind", rec.get("kind")),
                    "effect_class": ec.value, "permission": label})
        if tool.get("rawInput") is not None:
            rec["raw_input_sha256"] = _sha(_canonical(tool["rawInput"]))
        self._progress(0.5, f"acp permission {label} for {tool.get('title') or call_id} ({ec.value})")
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    def calls(self) -> list[dict[str, Any]]:
        return [self._calls[cid] for cid in self._order]

    def cost(self, n_calls: int) -> Cost:
        units = [Meter(name="tool_calls", quantity=float(n_calls))]
        if self.context_used is not None:
            units.append(Meter(name="context_tokens_used", quantity=float(self.context_used)))
        return Cost(units=units, amount=self._cost_amount or "0", currency=self._cost_currency)

    def _upsert_call(self, upd: dict, *, new: bool) -> None:
        call_id = str(upd.get("toolCallId") or f"call-{self._frames}")
        rec = self._calls.setdefault(call_id, self._new_call(call_id))
        if call_id not in self._order:
            self._order.append(call_id)
        for src, dst in (("title", "title"), ("kind", "kind"), ("status", "status")):
            if upd.get(src) is not None:
                rec[dst] = upd[src]
        if upd.get("rawInput") is not None:
            rec["raw_input_sha256"] = _sha(_canonical(upd["rawInput"]))
        if upd.get("rawOutput") is not None:
            rec["raw_output_sha256"] = _sha(_canonical(upd["rawOutput"]))
        rec["effect_class"] = effect_class_for_kind(rec.get("kind")).value

    @staticmethod
    def _new_call(call_id: str) -> dict[str, Any]:
        return {"call_id": call_id, "title": None, "kind": None, "status": None,
                "effect_class": UNKNOWN_KIND_EFFECT.value,
                "raw_input_sha256": None, "raw_output_sha256": None}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _decimal_str(v: Any) -> str:
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return "0"
    return format(d.normalize(), "f") if d != 0 else "0"
