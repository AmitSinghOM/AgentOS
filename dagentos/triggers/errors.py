"""Errors raised while loading triggers. Every message names the trigger (when there is
one), the field, and — for environment problems — the variable to fix."""
from __future__ import annotations


class TriggerConfigError(ValueError):
    """A triggers file, cron expression or webhook secret that cannot be used. Raised at
    load time; the API and the runner refuse to start on it (closed at startup)."""
