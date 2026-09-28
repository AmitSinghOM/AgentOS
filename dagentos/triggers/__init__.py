"""Triggers: cron slots and authenticated webhooks that start runs through the public API.

An adapter, not core: the runner (`python -m dagentos.triggers`) is an HTTP client of
`POST /workflows/{name}/runs`; the webhook route (`dagentos.api.triggers`) is mounted by the
API's composition root when `AGENTOS_TRIGGERS` is set. Idempotency keys derived from the cron
slot or the delivery id make every fire replay-safe.
"""
from .config import (
    CronTrigger,
    TriggersConfig,
    WebhookTrigger,
    load_triggers,
    triggers_from_env,
)
from .cron import CronSpec, next_fire, slot_key
from .errors import TriggerConfigError
from .runner import Runner, http_poster
from .webhook import delivery_key, sign, verify

__all__ = [
    "CronSpec", "CronTrigger", "Runner", "TriggerConfigError", "TriggersConfig",
    "WebhookTrigger", "delivery_key", "http_poster", "load_triggers", "next_fire", "sign",
    "slot_key", "triggers_from_env", "verify",
]
