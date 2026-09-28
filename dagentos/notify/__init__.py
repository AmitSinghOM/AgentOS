"""Notification adapters: Observers that tell a human where they already are (chat) that a
run is waiting on them. They carry information, never authority — decisions stay in the UI
and API where the principal is verified. Composed by `dagentos.observability.build_observers`
from the environment; the core never imports this package.
"""
from .slack import ConfigError, SlackNotifier, notifier_from_env

__all__ = ["ConfigError", "SlackNotifier", "notifier_from_env"]
