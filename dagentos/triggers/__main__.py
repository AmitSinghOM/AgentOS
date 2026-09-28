"""`python -m dagentos.triggers [--dry-run]` — composition root for the cron runner.

    AGENTOS_TRIGGERS   the triggers file (required)
    AGENTOS_API_URL    where the API listens (default http://127.0.0.1:8000)
    AGENTOS_API_TOKEN  bearer token when the API runs AGENTOS_AUTH=bearer; the token's
                       principal is what `run.started` records. Unset → asserted mode, and
                       the runner sends `system` principal `cron:{name}` in the body.

`--dry-run` prints the next fire times and the webhook routes the API will mount, and exits
without contacting anything. SIGTERM/SIGINT stop after the current tick.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from .config import load_triggers
from .cron import next_fire
from .errors import TriggerConfigError
from .runner import Runner, http_poster

log = logging.getLogger("agentos.triggers")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    e = os.environ if env is None else env
    p = argparse.ArgumentParser(prog="dagentos.triggers")
    p.add_argument("--triggers", default=e.get("AGENTOS_TRIGGERS"),
                   help="triggers file (default: $AGENTOS_TRIGGERS)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the next fire times and webhook routes; contact nothing")
    p.add_argument("--count", type=int, default=3, help="fire times per trigger in --dry-run")
    p.add_argument("--now", default=None, help="ISO datetime to compute --dry-run from")
    p.add_argument("--once", action="store_true", help="one tick, then exit (tests)")
    a = p.parse_args(argv)
    logging.basicConfig(level=e.get("AGENTOS_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not a.triggers:
        print("AGENTOS_TRIGGERS (or --triggers) is required", file=sys.stderr)
        return 2
    try:
        cfg = load_triggers(Path(a.triggers), env=e)
    except TriggerConfigError as exc:
        print(f"triggers: {exc}", file=sys.stderr)
        return 2

    if a.dry_run:
        now = datetime.fromisoformat(a.now) if a.now else datetime.now().astimezone()
        print(f"triggers file {cfg.path} sha256 {cfg.sha256[:12]}")
        for c in cfg.crons:
            t = now
            times = []
            for _ in range(max(1, a.count)):
                t = next_fire(c.spec, t, tz=c.tz)
                times.append(t.isoformat(timespec="seconds"))
            print(f"cron     {c.name:<20} {c.schedule:<16} {c.tz!s:<18} -> {c.workflow}: {', '.join(times)}")
        for w in cfg.webhooks.values():
            print(f"webhook  {w.name:<20} POST /triggers/webhooks/{w.name}  -> {w.workflow} "
                  f"(secret from ${w.secret_env}, body <= {w.max_body_bytes} bytes)")
        return 0

    api = e.get("AGENTOS_API_URL", "http://127.0.0.1:8000")
    token = e.get("AGENTOS_API_TOKEN") or None
    runner = Runner(cfg.crons, post=http_poster(api, token),
                    principal=None if token else "asserted")
    if a.once:
        runner.tick()
        return 0
    state = {"stop": False}

    def handler(signum, frame):
        state["stop"] = True
        log.info("%s received: stopping after the current tick", signal.Signals(signum).name)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)
    runner.run_forever(stop=lambda: state["stop"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
