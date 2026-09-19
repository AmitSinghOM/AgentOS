"""Authentication at the API boundary: a credential becomes a `Principal`.

Why this exists (ROADMAP → Phase 8 #1). Every governance event records a `Principal`, and
the engine refuses to let a non-human approve `spend` / `write_external`. Until this module
the principal was a field in the request body, so `{"kind": "human"}` was a string any
client could type. Here the *credential* decides who the caller is; the body may not.

Two modes, selected by `AGENTOS_AUTH`:

  asserted   (default) the body's `principal` is recorded as given. Nothing is verified.
             One WARNING at startup says so. For the quickstart and local development.
  bearer     every request except `/health` and `/metrics` must carry
             `Authorization: Bearer <token>`. The token resolves to a `Principal` through
             the operator's token file (`AGENTOS_AUTH_TOKENS`); a body `principal` is
             rejected 422. Unknown or missing token → 401. An `agent`-kind principal may
             not register agents or define workflows → 403.

The token file holds SHA-256 hashes, never tokens:

    {"principals": [{"sha256": "<hex of the token>", "kind": "human", "id": "amit"}]}

`sha256sum <<< "$TOKEN"` is the operator's whole tooling. A file that carries a `token`
key is refused at startup with the fix in the message. The file is read once, at startup;
rotate by restarting. The API has no route that writes it.

The recorded principal carries `attestation = "token:sha256:<12 hex>"` so a reader of the
log can tell *which* credential decided without seeing the file.

Every rejection is logged (method, path, hash prefix if a token was presented — never the
token). Accepted calls are not: the event already records the principal.

`Authenticator` is a protocol so an OIDC/JWKS resolver can replace the static file without
touching the middleware or the handlers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentos.api.ui import is_ui_asset_path
from agentos.core.models import Principal, PrincipalKind

logger = logging.getLogger("agentos.api.auth")

#: Paths that never require a credential: liveness probes and the metrics scraper. Neither
#: returns run data.
OPEN_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})

#: Routes an `agent`-kind principal may not call: definitions are operator-owned
#: (docs/TRUST_BOUNDARY.md — declared effects are what the gate trusts).
DEFINITION_PATHS: frozenset[str] = frozenset({"/agents", "/workflows"})

_ATTESTATION_HEX = 12


class AuthMode(str, Enum):
    asserted = "asserted"
    bearer = "bearer"


class AuthError(RuntimeError):
    """Configuration error. The message names the variable or file entry and the fix."""


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal | None:
        """Resolve a presented bearer token to a Principal, or None if unknown."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _attestation(digest_hex: str) -> str:
    return f"token:sha256:{digest_hex[:_ATTESTATION_HEX]}"


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str
    kind: PrincipalKind
    id: str


class _TokenFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principals: list[_Entry]


def _load_json(p: Path, where: str):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuthError(f"{where}: file not found") from None
    except json.JSONDecodeError as exc:
        raise AuthError(f"{where}: not valid JSON ({exc.msg})") from None


def _refuse_plaintext_tokens(raw, where: str) -> None:
    """The temptation to paste a token into the file must fail loudly, with the fix."""
    if not isinstance(raw, dict):
        return
    for i, entry in enumerate(raw.get("principals") or []):
        if isinstance(entry, dict) and "token" in entry:
            raise AuthError(
                f"{where}: principals[{i}] carries a plaintext 'token'. Store its hash instead: "
                "\"sha256\": \"$(printf %s \"$TOKEN\" | sha256sum | cut -d' ' -f1)\"")


def _parse_token_file(raw, where: str) -> _TokenFile:
    try:
        parsed = _TokenFile.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"]) or "<root>"
        raise AuthError(f"{where}: {loc}: {first['msg']}") from None
    if not parsed.principals:
        raise AuthError(f"{where}: 'principals' is empty; bearer mode would reject every request")
    for i, e in enumerate(parsed.principals):
        if len(e.sha256) != 64 or any(c not in "0123456789abcdef" for c in e.sha256.lower()):
            raise AuthError(f"{where}: principals[{i}].sha256 must be 64 hex characters "
                            "(the SHA-256 of the token)")
    return parsed


class StaticTokenAuthenticator:
    """Bearer tokens resolved through an operator-owned file of SHA-256 hashes."""

    def __init__(self, entries: list[_Entry]) -> None:
        self._entries = entries

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> StaticTokenAuthenticator:
        p = Path(path)
        where = f"AGENTOS_AUTH_TOKENS={str(p)!r}"
        raw = _load_json(p, where)
        _refuse_plaintext_tokens(raw, where)
        parsed = _parse_token_file(raw, where)
        return cls([e.model_copy(update={"sha256": e.sha256.lower()}) for e in parsed.principals])

    def authenticate(self, token: str) -> Principal | None:
        digest = token_digest(token)
        found: _Entry | None = None
        for entry in self._entries:            # constant-time per entry; no early exit
            if hmac.compare_digest(entry.sha256, digest):
                found = entry
        if found is None:
            return None
        return Principal(kind=found.kind, id=found.id, attestation=_attestation(digest))


@dataclass(frozen=True)
class AuthConfig:
    mode: AuthMode
    authenticator: Authenticator | None

    @classmethod
    def from_env(cls) -> AuthConfig:
        raw = os.environ.get("AGENTOS_AUTH", AuthMode.asserted.value).strip().lower()
        try:
            mode = AuthMode(raw)
        except ValueError:
            raise AuthError(f"AGENTOS_AUTH must be one of asserted | bearer, got {raw!r}") from None
        if mode is AuthMode.asserted:
            logger.warning(
                "AGENTOS_AUTH=asserted: principals are taken from request bodies and are NOT "
                "verified. Set AGENTOS_AUTH=bearer and AGENTOS_AUTH_TOKENS=<file> before "
                "exposing this API.")
            return cls(mode, None)
        path = os.environ.get("AGENTOS_AUTH_TOKENS")
        if not path:
            raise AuthError("AGENTOS_AUTH=bearer requires AGENTOS_AUTH_TOKENS=<path to the "
                            "token file> (see agentos/api/auth.py for the format)")
        return cls(mode, StaticTokenAuthenticator.from_file(path))

    @property
    def enforced(self) -> bool:
        return self.mode is AuthMode.bearer


def _reject(request: Request, status: int, detail: str, digest: str | None) -> JSONResponse:
    logger.warning("auth %d on %s %s: %s%s", status, request.method, request.url.path, detail,
                   f" ({_attestation(digest)})" if digest else "")
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(status_code=status, content={"detail": detail}, headers=headers)


async def auth_middleware(request: Request, call_next, *, config: AuthConfig):
    """Resolve the caller. In `asserted` mode nothing is checked and
    `request.state.principal` is None (handlers fall back to the body). In `bearer` mode
    every path outside OPEN_PATHS needs a known token; the resolved Principal is placed on
    `request.state.principal` and definition routes refuse `agent`-kind principals."""
    request.state.principal = None
    if not config.enforced or request.url.path in OPEN_PATHS \
            or is_ui_asset_path(request.method, request.url.path):
        # /ui serves the operator UI's static files only (agentos.api.ui); the app shell must
        # load before the user can present a token. No data lives under that prefix.
        return await call_next(request)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return _reject(request, 401, "missing bearer token", None)
    token = token.strip()
    assert config.authenticator is not None
    principal = config.authenticator.authenticate(token)
    if principal is None:
        return _reject(request, 401, "unknown bearer token", token_digest(token))
    if (request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path in DEFINITION_PATHS
            and principal.kind is PrincipalKind.agent):
        return _reject(request, 403,
                       "an agent principal may not register agents or define workflows",
                       token_digest(token))
    request.state.principal = principal
    return await call_next(request)


def openapi_security(schema: dict, *, config: AuthConfig) -> dict:
    """Declare the bearer requirement in the served OpenAPI document so `/docs` offers
    Authorize and a generated client sends the header. Read-only otherwise: in asserted
    mode the schema is returned unchanged. OPEN_PATHS are marked `security: []`."""
    if not config.enforced:
        return schema
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http", "scheme": "bearer",
        "description": "Token from the operator's AGENTOS_AUTH_TOKENS file (agentos/api/auth.py)."}
    schema["security"] = [{"bearerAuth": []}]
    for path, ops in schema.get("paths", {}).items():
        if path in OPEN_PATHS:
            for op in ops.values():
                if isinstance(op, dict):
                    op["security"] = []
    return schema


def principal_for(request: Request, body_principal: Principal | None, *,
                  config: AuthConfig, required: bool) -> Principal | None:
    """The Principal a handler records. Bearer mode: the token's, and a body principal is
    a 422 (rejected, not ignored — the client must not believe it decided as someone
    else). Asserted mode: the body's; `required` makes its absence a 422 (approve/reject,
    A2: every decision names who made it)."""
    if config.enforced:
        if body_principal is not None:
            raise HTTPException(
                status_code=422,
                detail="principal is derived from the bearer token in AGENTOS_AUTH=bearer "
                       "mode; remove it from the body")
        return request.state.principal
    if required and body_principal is None:
        raise HTTPException(status_code=422, detail="principal is required")
    return body_principal
