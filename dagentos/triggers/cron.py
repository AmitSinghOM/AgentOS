"""Five-field cron (`minute hour day-of-month month day-of-week`), standard library only.

Supported per field: `*`, a number, `a-b`, `a,b,c`, `*/n`, `a-b/n`. Day-of-week accepts 0-7
with both 0 and 7 meaning Sunday. When BOTH day fields are restricted the classic Vixie rule
applies: a day matches if EITHER field matches. No names, no `@daily` aliases, no seconds —
an operator who needs more writes two triggers.

`next_fire(expr, after, tz)` returns the first slot strictly after `after`, as an aware
datetime in `tz`. Slots are whole minutes; the search is bounded (a schedule that can never
fire, such as `* * 31 2 *`, is a definition error, not an infinite loop).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from .errors import TriggerConfigError

FIELDS = ("minute", "hour", "day-of-month", "month", "day-of-week")
RANGES = {"minute": (0, 59), "hour": (0, 23), "day-of-month": (1, 31),
          "month": (1, 12), "day-of-week": (0, 7)}
MAX_SEARCH_DAYS = 366 * 8 + 2          # covers a Feb-29 schedule from any start


class CronSpec:
    __slots__ = ("dom_star", "doms", "dow_star", "dows", "expr", "hours", "minutes", "months")

    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise TriggerConfigError(f"cron {expr!r}: expected 5 fields "
                                     f"(minute hour day-of-month month day-of-week), got {len(parts)}")
        self.expr = expr
        vals: list[set[int]] = []
        for name, part in zip(FIELDS, parts, strict=True):
            vals.append(_parse_field(name, part, expr))
        self.minutes, self.hours, self.doms, self.months, dows = vals
        self.dows = {0 if d == 7 else d for d in dows}
        self.dom_star = parts[2] == "*"
        self.dow_star = parts[4] == "*"
        # Reject schedules that can never fire: a day-of-month no listed month has.
        if not self.dom_star and self.dow_star:
            max_day = max(_DAYS_IN_MONTH[m] for m in self.months)
            if min(self.doms) > max_day:
                raise TriggerConfigError(f"cron {expr!r}: day-of-month {min(self.doms)} never "
                                         f"occurs in month(s) {sorted(self.months)}; it can never fire")

    def matches_day(self, d: datetime) -> bool:
        dom_ok = d.day in self.doms
        dow_ok = ((d.weekday() + 1) % 7) in self.dows       # Monday=0 → cron Monday=1; Sunday=0
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok                             # Vixie: both restricted → OR


_DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31,
                  11: 30, 12: 31}


def _parse_field(name: str, part: str, expr: str) -> set[int]:
    lo, hi = RANGES[name]
    out: set[int] = set()
    for item in part.split(","):
        step = 1
        rng = item
        if "/" in item:
            rng, _, step_s = item.partition("/")
            step = _int(name, step_s, expr)
            if step < 1:
                raise TriggerConfigError(f"cron {expr!r}: {name} step must be >= 1, got {step_s!r}")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            a, b = _int(name, a_s, expr), _int(name, b_s, expr)
            if a > b:
                raise TriggerConfigError(f"cron {expr!r}: {name} range {rng!r} runs backwards")
        else:
            a = b = _int(name, rng, expr)
            if "/" in item:
                b = hi                                      # "5/15" means from 5 to the end
        for v in (a, b):
            if not (lo <= v <= hi):
                raise TriggerConfigError(f"cron {expr!r}: {name} value {v} outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


def _int(name: str, s: str, expr: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise TriggerConfigError(f"cron {expr!r}: {name} has a non-numeric part {s!r}") from None


def next_fire(expr: str, after: datetime, tz: tzinfo | None = None) -> datetime:
    """First slot strictly after `after`, as an aware datetime in `tz` (default: `after`'s
    zone, or UTC when it is naive)."""
    spec = expr if isinstance(expr, CronSpec) else CronSpec(expr)
    zone = tz or after.tzinfo or UTC
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    t = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = t + timedelta(days=MAX_SEARCH_DAYS)
    while t <= deadline:
        if t.month not in spec.months:
            t = _bump_month(t, zone)
            continue
        if not spec.matches_day(t):
            t = _bump_day(t, zone)
            continue
        if t.hour not in spec.hours:
            t = (t + timedelta(hours=1)).replace(minute=0)
            t = _relocalise(t, zone)
            continue
        if t.minute not in spec.minutes:
            t = _relocalise(t + timedelta(minutes=1), zone)
            continue
        return t
    raise TriggerConfigError(f"cron {expr!r}: no slot within {MAX_SEARCH_DAYS} days; it can never fire")


def _bump_day(t: datetime, zone: tzinfo) -> datetime:
    return _relocalise((t + timedelta(days=1)).replace(hour=0, minute=0), zone)


def _bump_month(t: datetime, zone: tzinfo) -> datetime:
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return _relocalise(t.replace(year=y, month=m, day=1, hour=0, minute=0), zone)


def _relocalise(t: datetime, zone: tzinfo) -> datetime:
    """Arithmetic on aware datetimes is wall-clock in Python; re-attach the zone so the
    offset is right after a DST edge."""
    if isinstance(zone, ZoneInfo):
        return t.replace(tzinfo=None).replace(tzinfo=zone)
    return t


def slot_key(name: str, slot: datetime) -> str:
    """The `Idempotency-Key` for one cron slot: the schedule's wall-clock time with its
    offset, so a restarted runner cannot start the slot twice."""
    return f"cron:{name}:{slot.isoformat(timespec='seconds')}"
