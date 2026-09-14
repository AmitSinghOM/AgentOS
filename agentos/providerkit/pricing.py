"""Metered cost from token usage and a content-addressed pricing table (§11 A6)."""
from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from agentos.core.models import Cost, Meter

_MILLION = Decimal(1_000_000)
_CENTS_PRECISION = Decimal("0.000001")   # micro-dollars: fine enough for a 0.5b model


class PricingTable:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.bytes = self.path.read_bytes()
        self.data = json.loads(self.bytes)
        if self.data.get("schema") != "agentos.pricing/1":
            raise ValueError(f"{path}: unknown pricing schema {self.data.get('schema')!r}")
        self.sha256 = hashlib.sha256(self.bytes).hexdigest()
        self.currency = self.data.get("currency", "USD")

    def snapshot(self) -> bytes:
        """The exact bytes whose sha256 every step records; stored in the BlobStore."""
        return self.bytes

    def price_of(self, model_id: str) -> tuple[Decimal, Decimal] | None:
        """(input per 1M, output per 1M) or None when the model is unpriced."""
        row = self.data["models"].get(model_id)
        if row is None:  # exact match first, then family prefix e.g. "gpt-4o-mini-2024-07-18"
            for name, r in self.data["models"].items():
                if model_id.startswith(name + "-"):
                    row = r
                    break
        if row is not None:
            return Decimal(row["input"]), Decimal(row["output"])
        low = model_id.lower()
        if any(low.startswith(p) for p in self.data.get("free_prefixes", [])):
            return Decimal(0), Decimal(0)
        return None

    def cost(self, model_id: str, input_tokens: int, output_tokens: int,
             extra_meters: list[Meter] | None = None) -> tuple[Cost, bool]:
        """Returns (Cost, priced). `priced=False` means the model is not in the table: the
        meters are still recorded, the amount is 0, and the step output says so.
        `extra_meters` are recorded but not priced (e.g. cached input tokens, which the
        caller has already counted inside `input_tokens` at the full rate — conservative)."""
        units = [Meter(name="input_tokens", quantity=input_tokens),
                 Meter(name="output_tokens", quantity=output_tokens),
                 Meter(name="requests", quantity=1), *(extra_meters or [])]
        prices = self.price_of(model_id)
        if prices is None:
            return Cost(units=units, amount="0", currency=self.currency,
                        pricing_snapshot_hash=self.sha256), False
        pin, pout = prices
        amount = (Decimal(input_tokens) * pin + Decimal(output_tokens) * pout) / _MILLION
        amount = amount.quantize(_CENTS_PRECISION, rounding=ROUND_HALF_UP).normalize()
        text = format(amount, "f") if amount else "0"
        return Cost(units=units, amount=text, currency=self.currency,
                    pricing_snapshot_hash=self.sha256), True
