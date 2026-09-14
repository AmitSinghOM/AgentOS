"""Environment → ProviderConfig, with a per-provider prefix so two providers can be
configured side by side (AGENTOS_OPENAI_*, AGENTOS_ANTHROPIC_*). Every variable has a
documented default; a bad value raises ConfigError naming the variable.

    {P}_BASE_URL       provider default          the server; include any path prefix the
                                                 provider documents
    {P}_API_KEY        unset (→ fallback var)    Ollama needs none
    {P}_ALIASES        provider default          capability alias → model id (JSON, merged)
    {P}_PRICING        bundled pricing.json      path to a pricing table
    {P}_TIMEOUT        60                        seconds per request
    {P}_CASSETTES      off | replay | record     HTTP record/replay (§11 A10)
    {P}_CASSETTE_DIR   ./cassettes
    {P}_CASSETTE       default                   which cassette file (name.json)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """Bad provider configuration. Raised at load time, naming the variable to fix."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    aliases: dict[str, str] = field(default_factory=dict)
    pricing_path: Path = Path("pricing.json")
    api_key: str | None = None
    timeout_seconds: float = 60.0
    cassettes: str = "off"                       # off | replay | record
    cassette_dir: Path = Path("cassettes")
    cassette_name: str = "default"

    def resolve(self, model: str) -> tuple[str, str | None]:
        """(concrete model id, alias used or None). Anything not in the alias table is a
        concrete id, so `"gpt-4o-mini"` and `"chat.fast"` both work."""
        if model in self.aliases:
            return self.aliases[model], model
        return model, None


def config_from_env(prefix: str, *, default_base_url: str, default_aliases: dict[str, str],
                    bundled_pricing: Path, key_fallbacks: tuple[str, ...] = (),
                    env: dict[str, str] | None = None) -> ProviderConfig:
    e = os.environ if env is None else env
    p = prefix.rstrip("_")

    aliases = dict(default_aliases)
    raw = e.get(f"{p}_ALIASES")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{p}_ALIASES is not valid JSON ({exc.msg} at column {exc.colno}); expected "
                f'e.g. {{"chat.fast": "{next(iter(default_aliases.values()), "model-id")}"}}'
            ) from exc
        if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise ConfigError(f"{p}_ALIASES must be a JSON object of string alias → string model id")
        aliases.update(parsed)

    mode = e.get(f"{p}_CASSETTES", "off").lower()
    if mode not in ("off", "replay", "record"):
        raise ConfigError(f"{p}_CASSETTES={mode!r}; expected off | replay | record")

    pricing = Path(e[f"{p}_PRICING"]) if e.get(f"{p}_PRICING") else bundled_pricing
    if not pricing.exists():
        raise ConfigError(f"{p}_PRICING points at {pricing}, which does not exist")

    try:
        timeout = float(e.get(f"{p}_TIMEOUT", "60"))
    except ValueError as exc:
        raise ConfigError(f"{p}_TIMEOUT must be a number of seconds") from exc

    api_key = e.get(f"{p}_API_KEY") or next((e[k] for k in key_fallbacks if e.get(k)), None)
    return ProviderConfig(
        base_url=e.get(f"{p}_BASE_URL", default_base_url).rstrip("/"),
        aliases=aliases, pricing_path=pricing, api_key=api_key, timeout_seconds=timeout,
        cassettes=mode, cassette_dir=Path(e.get(f"{p}_CASSETTE_DIR", "cassettes")),
        cassette_name=e.get(f"{p}_CASSETTE", "default"),
    )
