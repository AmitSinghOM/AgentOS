// The one place the UI talks to the API. The bearer token lives in sessionStorage — gone with
// the tab, never localStorage, never a cookie (no CSRF surface), never a URL — and is sent as
// `Authorization: Bearer` on every request. The UI holds no authorization logic of its own:
// it POSTs the two bodies the API accepts and shows the API's 401/403/409/422 text verbatim.

const TOKEN_KEY = "agentos.token";

export type PrincipalKind = "human" | "agent" | "system";
export interface Principal { kind: PrincipalKind; id: string; attestation?: string | null }
export interface Me { mode: "asserted" | "bearer"; principal: Principal | null }

export interface Approval {
  run_id: string;
  workflow: string;
  approval_id: string;
  step_id: string | null;
  kind: "effect" | "cost";
  effect_classes: string[];
  status: string;
  requested_at: string;
  expires_at: string | null;
  reason?: string;
  cost_at_request?: string | null;
  proposed_ceiling?: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`${status}: ${detail}`);
  }
}

export function getToken(): string | null {
  return window.sessionStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string | null): void {
  if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
  else window.sessionStorage.removeItem(TOKEN_KEY);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit",
  });
  const text = await res.text();
  let parsed: unknown = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
  if (!res.ok) {
    const detail = typeof parsed === "object" && parsed !== null && "detail" in parsed
      ? String((parsed as { detail: unknown }).detail)
      : text || res.statusText;
    throw new ApiError(res.status, detail);
  }
  return parsed as T;
}

export interface RunSummary {
  id: string; workflow: string; workflow_version: number; status: string; total_cost: string;
  last_seq: number; started_at: string; pending_approvals: number;
}

export const api = {
  me: () => request<Me>("GET", "/me"),
  approvals: () => request<{ data: Approval[] }>("GET", "/approvals"),
  runs: (limit = 50) => request<{ data: RunSummary[] }>("GET", `/runs?limit=${limit}`),
  run: <T>(id: string) => request<T>("GET", `/runs/${encodeURIComponent(id)}`),
  /** Time travel: the SERVER folds the log prefix through `at` (same fold, fewer events). */
  runAt: <T>(id: string, at: number) => request<T>("GET", `/runs/${encodeURIComponent(id)}?at=${at}`),
  /** The log is paged (`limit`, `has_more`); pass the last seq you hold to fetch only what follows. */
  events: <T>(id: string, after = 0, limit = 1000) =>
    request<T>("GET", `/runs/${encodeURIComponent(id)}/events?after=${after}&limit=${limit}`),
  workflow: <T>(name: string) => request<T>("GET", `/workflows/${encodeURIComponent(name)}`),
  /** In bearer mode the body carries only `reason`; the API derives the principal from the
   *  token and REJECTS a body principal (422). In asserted mode the API requires one — the
   *  caller passes the principal the operator typed, and the UI labels it unverified. */
  decide: (a: Approval, verb: "approve" | "reject", reason: string, principal?: Principal) =>
    request<unknown>("POST", `/runs/${a.run_id}/approvals/${a.approval_id}/${verb}`,
      principal ? { principal, reason } : { reason }),
};
