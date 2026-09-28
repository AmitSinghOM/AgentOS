"""The cron runner: a client of the API, nothing more.

For each cron trigger it holds the next slot. On every `tick()` a slot at or before `now`
is fired as `POST /workflows/{workflow}/runs` with `Idempotency-Key: cron:{name}:{slot}`;
the key is what makes a restart, a duplicate runner or a retried post harmless — the API
answers with the same run. A failed post is retried with bounded exponential backoff
(1, 2, 4 … 60 s) for at most `RETRY_HORIZON_SECONDS` and never past the trigger's next slot,
then the slot is given up with one ERROR line naming it — a slot is never fired late into the
following one, and one trigger's outage cannot hold the runner for the others.

Missed slots are not backfilled: a runner started at 11:00 does not fire 09:00, and a slot
whose whole window passed while the runner was stalled is logged as missed, not fired. That
is a deliberate choice for a scheduler whose runs can spend money; an operator who wants the
missed run starts it by hand with the same key.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from .config import CronTrigger
from .cron import next_fire, slot_key

log = logging.getLogger("agentos.triggers")

# (workflow, *, idempotency_key, inputs, principal) -> the run as the API returned it
PostFn = Callable[..., dict]
BACKOFF_START, BACKOFF_CAP = 1.0, 60.0
# How long one slot may keep retrying a failed post. Bounded so a daily trigger whose API is
# down cannot hold the single-threaded runner for a day and starve every other trigger
# (self-review R2); also never past the trigger's next slot.
RETRY_HORIZON_SECONDS = 10 * 60


class Runner:
    def __init__(self, crons: list[CronTrigger], *, post: PostFn,
                 now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 principal: str | None = "asserted") -> None:
        """`principal="asserted"` sends a `system` principal in the body (asserted-mode API);
        `None` sends none — in bearer mode the token names the caller and a body principal
        is a 422."""
        self._crons = list(crons)
        self._post = post
        self._now = now or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._send_principal = principal == "asserted"
        # First slot AT OR AFTER the current minute: a runner started at 09:00:40 fires the
        # 09:00 slot (a cron daemon would), one started at 09:01:00 does not. Nothing earlier
        # is ever backfilled.
        start = self._now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        self._next: dict[str, datetime] = {c.name: next_fire(c.spec, start, tz=c.tz)
                                           for c in self._crons}

    def next_fire_times(self) -> dict[str, datetime]:
        return dict(self._next)

    def tick(self) -> int:
        """Fire every slot that is due AND still current. A slot whose successor is also in
        the past (the runner stalled or was paused across it) is reported as missed and
        skipped — firing it late would be backfill by another name. Returns how many runs
        were started."""
        started = 0
        for c in self._crons:
            slot = self._next[c.name]
            now = self._now().astimezone(c.tz)
            if now < slot:
                continue
            following = next_fire(c.spec, slot, tz=c.tz)
            if now >= following:
                # Advance to the first slot still in the future, counting what was skipped.
                missed = [slot]
                while now >= following:
                    missed.append(following)
                    following = next_fire(c.spec, following, tz=c.tz)
                current = missed.pop()                      # the slot whose window we are in
                log.warning("trigger %r: missed %d slot(s) while stalled (%s .. %s); not backfilled",
                            c.name, len(missed), slot_key(c.name, missed[0]),
                            slot_key(c.name, missed[-1]))
                slot = current
            self._next[c.name] = following
            if self._fire(c, slot, give_up_at=min(following, now + timedelta(seconds=RETRY_HORIZON_SECONDS))):
                started += 1
        return started

    def seconds_until_next(self) -> float:
        if not self._next:
            return 60.0
        now = self._now()
        return max(0.0, min((t - now.astimezone(t.tzinfo)).total_seconds() for t in self._next.values()))

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        log.info("triggers runner: %d cron trigger(s); next: %s", len(self._crons),
                 {k: v.isoformat(timespec="seconds") for k, v in self._next.items()})
        while not stop():
            self.tick()
            self._sleep(min(30.0, max(0.5, self.seconds_until_next())))

    def _fire(self, c: CronTrigger, slot: datetime, *, give_up_at: datetime) -> bool:
        key = slot_key(c.name, slot)
        inputs = {**c.inputs, "trigger": {"kind": "cron", "name": c.name,
                                          "slot": slot.isoformat(timespec="seconds"),
                                          "schedule": c.schedule, "tz": str(c.tz)}}
        principal = {"kind": "system", "id": f"cron:{c.name}",
                     "attestation": "dagentos.triggers"} if self._send_principal else None
        delay = BACKOFF_START
        attempt = 0
        while True:
            attempt += 1
            try:
                run = self._post(c.workflow, idempotency_key=key, inputs=inputs, principal=principal)
            except Exception as exc:  # noqa: BLE001 — any client failure is retried, bounded
                if self._now().astimezone(c.tz) + timedelta(seconds=delay) >= give_up_at:
                    log.error("trigger %r: gave up slot %s after %d attempt(s): %s",
                              c.name, key, attempt, exc)
                    return False
                level = logging.WARNING if attempt <= 3 or attempt % 10 == 0 else logging.DEBUG
                log.log(level, "trigger %r: attempt %d for %s failed (%s); retrying in %.0fs",
                        c.name, attempt, key, exc, delay)
                self._sleep(delay)
                delay = min(delay * 2, BACKOFF_CAP)
                continue
            log.info("trigger %r: slot %s -> run %s (%s)", c.name, key, run.get("id"), run.get("status"))
            return True


def http_poster(api_url: str, token: str | None, timeout: float = 30.0) -> PostFn:
    """The real PostFn: one `POST /workflows/{name}/runs` per call via httpx."""
    import httpx

    def post(workflow: str, *, idempotency_key: str, inputs: dict, principal: dict | None) -> dict:
        headers = {"Idempotency-Key": idempotency_key, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body: dict = {"inputs": inputs}
        if principal is not None:
            body["principal"] = principal
        r = httpx.post(f"{api_url.rstrip('/')}/workflows/{workflow}/runs", json=body,
                       headers=headers, timeout=timeout)
        if r.status_code == 404:
            raise RuntimeError(f"workflow {workflow!r} is not defined on the API ({r.text[:200]})")
        r.raise_for_status()
        return r.json()

    return post
