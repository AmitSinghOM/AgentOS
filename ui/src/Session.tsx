import { useCallback, useEffect, useState } from "react";
import { ApiError, Me, Principal, api, getToken, setToken } from "./api";

export interface Session {
  me: Me | null;
  loading: boolean;
  error: string | null;
  /** True when the last `/me` failed for a reason other than 401 (network, 5xx): the API is not
   *  answering, so a token form would be the wrong call to action. */
  unreachable: boolean;
  /** Reload `/me` (Retry after an unreachable API). */
  reload: () => Promise<void>;
  /** The principal a decision will be recorded under, or null when none is known yet. In
   *  bearer mode it is the token's; in asserted mode it is whatever the operator typed, and
   *  `unverified` is true. */
  actingAs: Principal | null;
  unverified: boolean;
  submitToken: (token: string) => Promise<void>;
  clearToken: () => void;
  setTypedPrincipalId: (id: string) => void;
}

const TYPED_KEY = "agentos.typed_principal";

/** The unverified-mode principal input lives in the top bar; the inbox's "enter your id" note
 *  sends focus here so the operator does not have to find it. */
export const PRINCIPAL_INPUT_ID = "principal-id";
export function focusPrincipalInput(): void {
  const el = document.getElementById(PRINCIPAL_INPUT_ID);
  if (el instanceof HTMLInputElement) { el.focus(); el.select(); }
}

export function useSession(): Session {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [typedId, setTypedId] = useState<string>(
    () => window.sessionStorage.getItem(TYPED_KEY) ?? "");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMe(await api.me());
      setUnreachable(false);
    } catch (e) {
      setMe(null);
      if (e instanceof ApiError && e.status === 401) {
        setUnreachable(false);
        // A token the API rejected must not be re-sent on every reload: forget it and say why.
        const had = getToken();
        if (had) setToken(null);
        setError(had ? `token rejected: ${e.detail}` : null);
      } else {
        // Not an auth answer: the API is down, unreachable, or broken. Say that, not "sign in".
        setUnreachable(true);
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const submitToken = useCallback(async (token: string) => {
    setToken(token.trim());
    await load();
  }, [load]);

  const clearToken = useCallback(() => {
    setToken(null);
    setMe(null);
    setError(null);
  }, []);

  const setTypedPrincipalId = useCallback((id: string) => {
    setTypedId(id);
    window.sessionStorage.setItem(TYPED_KEY, id);
  }, []);

  const unverified = me?.mode === "asserted";
  const actingAs: Principal | null = me === null ? null
    : me.mode === "bearer" ? me.principal
    : typedId.trim() ? { kind: "human", id: typedId.trim() } : null;

  return { me, loading, error, unreachable, reload: load, actingAs, unverified,
           submitToken, clearToken, setTypedPrincipalId };
}

export function Unreachable({ session }: { session: Session }) {
  return (
    <div className="card card--center unreachable">
      <h2>Cannot reach the API</h2>
      <p role="alert" className="error">
        The API is not answering: {session.error ?? "unknown error"}
      </p>
      <p className="hint">
        Nothing here is a sign-in problem — check that the API process is running and reachable at
        this origin.
      </p>
      <button type="button" className="primary" onClick={() => void session.reload()}>Retry</button>
    </div>
  );
}

export function ActingAs({ session }: { session: Session }) {
  const { me, actingAs, clearToken, setTypedPrincipalId } = session;
  if (!me) return null;
  if (me.mode === "bearer" && actingAs) {
    return (
      <div className="identity" role="status">
        <span className="identity__label">Acting as</span>
        <strong className="identity__who">{actingAs.id}</strong>
        <span className="identity__kind muted">{actingAs.kind}{actingAs.attestation ? ` · ${actingAs.attestation}` : ""} · verified by the API</span>
        <button type="button" className="ghost" onClick={clearToken}>Forget token</button>
      </div>
    );
  }
  return (
    <div className="identity identity--unverified" role="status">
      <span className="identity__label"><strong>Unverified.</strong> Record decisions as</span>
      <input
        id={PRINCIPAL_INPUT_ID}
        aria-label="principal id (unverified)"
        value={actingAs?.id ?? ""}
        onChange={(e) => setTypedPrincipalId(e.target.value)}
        placeholder="your id"
        autoComplete="off"
      />
      <details className="identity__why">
        <summary aria-label="why unverified">why?</summary>
        <p>
          This API runs with <code>AGENTOS_AUTH=asserted</code>: the principal is recorded in the
          event log exactly as typed here and nothing checks it. Run the API with bearer tokens
          (<code>AGENTOS_AUTH=bearer</code>) to have the API verify who decides.
        </p>
      </details>
    </div>
  );
}

export function TokenForm({ session }: { session: Session }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="card card--center token-form"
      onSubmit={(e) => { e.preventDefault(); void session.submitToken(value); setValue(""); }}
    >
      <h2>Sign in</h2>
      <p className="hint">This API verifies operators by bearer token (<code>AGENTOS_AUTH=bearer</code>). Decisions are recorded under the token's principal.</p>
      <label>
        Bearer token
        <input
          type="password"
          autoComplete="off"
          aria-label="bearer token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </label>
      <button type="submit" className="primary">Sign in</button>
      <p className="hint">Kept in this tab only (sessionStorage); never sent anywhere but this origin.</p>
      {session.error && <p role="alert" className="error">{session.error}</p>}
    </form>
  );
}
