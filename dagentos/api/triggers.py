"""`POST /triggers/webhooks/{name}` — one route per webhook trigger in `AGENTOS_TRIGGERS`.

The route authenticates the DELIVERY, not a person: `X-AgentOS-Timestamp` and
`X-AgentOS-Signature: v1=<hmac>` over `{timestamp}.{delivery}.{body}` with the trigger's
shared secret (dagentos.triggers.webhook). It is listed in `auth.SELF_AUTHENTICATED_PREFIXES`,
so the bearer middleware lets it through to this check; there is no path to the engine
without a valid signature. Order, all before the engine is touched: 404 unknown trigger → 413
over the trigger's body cap → 415 not JSON → 401 missing/stale/invalid signature.

The run it starts records a `system` principal `webhook:{name}` with attestation
`hmac-sha256:v1` on `run.started`, and its inputs carry the delivery under `trigger`
(`{"kind": "webhook", "name", "delivery", "body"}`) — the body is data to the workflow's
prompts (C12), never instructions. `Idempotency-Key` is the sender's `X-AgentOS-Delivery`
id, else the body hash, so a redelivery maps to the same run.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from dagentos.core.models import Principal, PrincipalKind
from dagentos.triggers.config import TriggersConfig
from dagentos.triggers.webhook import SCHEME, delivery_key, verify

log = logging.getLogger("agentos.api.triggers")
CHALLENGE = f'AgentOS-Webhook realm="triggers", algorithm="hmac-sha256", scheme="{SCHEME}"'


def build_router(cfg: TriggersConfig, *, engine, store) -> APIRouter:
    router = APIRouter()

    @router.post("/triggers/webhooks/{name}", status_code=202,
                 summary="Start a run from a signed webhook delivery",
                 description=("Authenticated by `X-AgentOS-Timestamp` + `X-AgentOS-Signature` "
                              "(HMAC-SHA256 over `{timestamp}.{delivery}.{body}` with the trigger's "
                              "shared secret), not by a bearer token. Optional `X-AgentOS-Delivery` is "
                              "the idempotency key and is inside the signed string; otherwise the body "
                              "hash is the key. Responds 202 with the run."),
                 responses={401: {"description": "missing, stale or invalid signature"},
                            404: {"description": "no such trigger or workflow"},
                            413: {"description": "body over the trigger's max_body_bytes"},
                            415: {"description": "body is not application/json"}})
    async def webhook(name: str, request: Request, response: Response) -> dict:
        trigger = cfg.webhooks.get(name)
        if trigger is None:
            raise HTTPException(status_code=404, detail=f"unknown webhook trigger {name!r}")
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > trigger.max_body_bytes:
            raise HTTPException(status_code=413, detail=f"body exceeds max_body_bytes "
                                                        f"({trigger.max_body_bytes}) for {name!r}")
        body = await request.body()
        if len(body) > trigger.max_body_bytes:
            raise HTTPException(status_code=413, detail=f"body exceeds max_body_bytes "
                                                        f"({trigger.max_body_bytes}) for {name!r}")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPException(status_code=415, detail="webhook body must be application/json")

        ts = request.headers.get("x-agentos-timestamp", "")
        sig = request.headers.get("x-agentos-signature", "")
        delivery = request.headers.get("x-agentos-delivery") or None
        if not ts or not sig:
            return _unauthorized(name, "missing webhook signature (X-AgentOS-Timestamp and "
                                       "X-AgentOS-Signature are required)")
        if not verify(trigger.secret, ts, body, sig, delivery=delivery, now=time.time()):
            reason = "stale timestamp" if _stale(ts) else "invalid signature"
            return _unauthorized(name, f"{reason} for webhook {name!r}")

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=f"webhook body is not valid JSON: {exc}") from exc
        key = delivery_key(name, delivery, body)
        inputs = {**trigger.inputs, "trigger": {"kind": "webhook", "name": name,
                                                "delivery": delivery, "body": payload}}
        principal = Principal(kind=PrincipalKind.system, id=f"webhook:{name}",
                              attestation=f"hmac-sha256:{SCHEME}")
        try:
            run_id = engine.create_run(trigger.workflow, request_id=key, principal=principal,
                                       inputs=inputs)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.push(run_id)
        run = engine.get_run(run_id)
        assert run is not None
        log.info("webhook %r delivery %s -> run %s", name, delivery or key, run_id)
        return run.model_dump()

    return router


def _stale(ts: str) -> bool:
    try:
        return abs(time.time() - int(ts)) > 300
    except ValueError:
        return False


def _unauthorized(name: str, detail: str) -> JSONResponse:
    log.warning("webhook %r refused: %s", name, detail)
    return JSONResponse(status_code=401, content={"detail": detail},
                        headers={"WWW-Authenticate": CHALLENGE})
