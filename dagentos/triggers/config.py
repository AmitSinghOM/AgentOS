"""The triggers file (`AGENTOS_TRIGGERS=<path>`):

    {"triggers": [
      {"name": "nightly", "kind": "cron", "schedule": "0 9 * * 1-5", "tz": "Asia/Kolkata",
       "workflow": "report", "inputs": {"topic": "yesterday"}},
      {"name": "gh", "kind": "webhook", "workflow": "review",
       "secret_env": "GH_WEBHOOK_SECRET", "max_body_bytes": 262144}
    ]}

Read by both processes that act on it: the API mounts one `POST /triggers/webhooks/{name}`
per webhook trigger; `python -m dagentos.triggers` fires the cron triggers. Every error
names the trigger and the field. A webhook whose `secret_env` variable is unset (or shorter
than 16 characters) is a load error, so a misconfigured deployment refuses to START rather
than exposing a route no sender can sign for — the same closed-at-startup shape as
`AGENTOS_AUTH_TOKENS` and `AGENTOS_POLICY`. The secret's VALUE is never accepted in the file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cron import CronSpec
from .errors import TriggerConfigError
from .webhook import MIN_SECRET_LENGTH

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_MAX_BODY_BYTES = 262_144
KNOWN_KEYS = {
    "cron": {"name", "kind", "schedule", "tz", "workflow", "inputs"},
    "webhook": {"name", "kind", "workflow", "inputs", "secret_env", "max_body_bytes"},
}


@dataclass(frozen=True)
class CronTrigger:
    name: str
    schedule: str
    workflow: str
    tz: ZoneInfo
    inputs: dict = field(default_factory=dict)

    @property
    def spec(self) -> CronSpec:
        return CronSpec(self.schedule)


@dataclass(frozen=True)
class WebhookTrigger:
    name: str
    workflow: str
    secret: str                       # resolved from `secret_env`; never logged or echoed
    secret_env: str
    inputs: dict = field(default_factory=dict)
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES


@dataclass(frozen=True)
class TriggersConfig:
    path: Path
    sha256: str
    crons: list[CronTrigger]
    webhooks: dict[str, WebhookTrigger]


def load_triggers(path: Path, *, env: dict[str, str] | None = None) -> TriggersConfig:
    e = os.environ if env is None else env
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise TriggerConfigError(f"AGENTOS_TRIGGERS={str(path)!r}: file not found") from None
    except OSError as exc:
        raise TriggerConfigError(f"AGENTOS_TRIGGERS={str(path)!r}: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise TriggerConfigError(f"AGENTOS_TRIGGERS={str(path)!r}: not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise TriggerConfigError("AGENTOS_TRIGGERS: top level must be an object with 'triggers'")
    if "version" in doc and doc["version"] != 1:
        raise TriggerConfigError(f"AGENTOS_TRIGGERS: unsupported version {doc['version']!r} (this "
                                 f"release reads version 1)")
    items = doc.get("triggers")
    if not isinstance(items, list):
        raise TriggerConfigError("AGENTOS_TRIGGERS: 'triggers' must be a list")

    crons: list[CronTrigger] = []
    webhooks: dict[str, WebhookTrigger] = {}
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise TriggerConfigError(f"AGENTOS_TRIGGERS: triggers[{i}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise TriggerConfigError(f"AGENTOS_TRIGGERS: triggers[{i}] {name!r}: name must match "
                                     f"{NAME_RE.pattern} (letters, digits, . _ -; no spaces)")
        if name in seen:
            raise TriggerConfigError(f"AGENTOS_TRIGGERS: duplicate trigger name {name!r}")
        seen.add(name)
        kind = item.get("kind")
        if kind not in KNOWN_KEYS:
            raise TriggerConfigError(f"trigger {name!r}: kind must be 'cron' or 'webhook', got {kind!r}")
        unknown = set(item) - KNOWN_KEYS[kind]
        if unknown:
            hint = " (the secret goes in the environment variable named by secret_env, never the value in this file)" \
                if kind == "webhook" and "secret" in unknown else ""
            raise TriggerConfigError(f"trigger {name!r}: unknown key(s) {sorted(unknown)} for kind "
                                     f"{kind!r}; use secret_env{hint}" if hint else
                                     f"trigger {name!r}: unknown key(s) {sorted(unknown)} for kind {kind!r}")
        workflow = item.get("workflow")
        if not isinstance(workflow, str) or not workflow:
            raise TriggerConfigError(f"trigger {name!r}: workflow must be a non-empty string")
        inputs = item.get("inputs", {})
        if not isinstance(inputs, dict):
            raise TriggerConfigError(f"trigger {name!r}: inputs must be an object")
        if "trigger" in inputs:
            raise TriggerConfigError(f"trigger {name!r}: inputs may not set the reserved key 'trigger'")

        if kind == "cron":
            schedule = item.get("schedule")
            if not isinstance(schedule, str) or not schedule.strip():
                raise TriggerConfigError(f"trigger {name!r}: schedule is required (5-field cron)")
            try:
                CronSpec(schedule)
            except TriggerConfigError as exc:
                raise TriggerConfigError(f"trigger {name!r}: {exc}") from None
            tz_name = item.get("tz", "UTC")
            try:
                tz = ZoneInfo(str(tz_name))
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                raise TriggerConfigError(f"trigger {name!r}: tz {tz_name!r} is not an IANA zone "
                                         f"(e.g. 'UTC', 'Asia/Kolkata')") from None
            crons.append(CronTrigger(name=name, schedule=schedule, workflow=workflow, tz=tz,
                                     inputs=dict(inputs)))
        else:
            secret_env = item.get("secret_env")
            if not isinstance(secret_env, str) or not secret_env:
                raise TriggerConfigError(f"trigger {name!r}: secret_env is required — the name of the "
                                         f"environment variable holding the shared secret")
            secret = e.get(secret_env)
            if secret is None:
                raise TriggerConfigError(f"trigger {name!r}: environment variable {secret_env} is unset; "
                                         f"set it to the shared webhook secret before starting")
            if len(secret) < MIN_SECRET_LENGTH:
                raise TriggerConfigError(f"trigger {name!r}: {secret_env} must be at least "
                                         f"{MIN_SECRET_LENGTH} characters")
            cap = item.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
                raise TriggerConfigError(f"trigger {name!r}: max_body_bytes must be a positive integer")
            webhooks[name] = WebhookTrigger(name=name, workflow=workflow, secret=secret,
                                            secret_env=secret_env, inputs=dict(inputs),
                                            max_body_bytes=cap)

    return TriggersConfig(path=path, sha256=hashlib.sha256(raw).hexdigest(),
                          crons=crons, webhooks=webhooks)


def triggers_from_env(env: dict[str, str] | None = None) -> TriggersConfig | None:
    """`AGENTOS_TRIGGERS` unset → None (no triggers; nothing mounted). Set → loaded or a
    `TriggerConfigError` that stops the process."""
    e = os.environ if env is None else env
    raw = e.get("AGENTOS_TRIGGERS")
    if not raw:
        return None
    return load_triggers(Path(raw), env=e)
