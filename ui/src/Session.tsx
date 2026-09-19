import { useCallback, useEffect, useState } from "react";
import { ApiError, Me, Principal, api, getToken, setToken } from "./api";

export interface Session {
  me: Me | null;
  loading: boolean;
  error: string | null;
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

export function useSession(): Session {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typedId, setTypedId] = useState<string>(
    () => window.sessionStorage.getItem(TYPED_KEY) ?? "");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMe(await api.me());
    } catch (e) {
      setMe(null);
      if (e instanceof ApiError && e.status === 401) {
        setError(getToken() ? `token rejected: ${e.detail}` : null);
      } else {
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

  return { me, loading, error, actingAs, unverified, submitToken, clearToken, setTypedPrincipalId };
}

export function ActingAs({ session }: { session: Session }) {
  const { me, actingAs, clearToken, setTypedPrincipalId } = session;
  if (!me) return null;
  if (me.mode === "bearer" && actingAs) {
    return (
      <p className="acting" role="status">
        Acting as <strong>{actingAs.id}</strong> ({actingAs.kind}
        {actingAs.attestation ? `, ${actingAs.attestation}` : ""}) — verified by the API.
        {" "}<button type="button" onClick={clearToken}>Forget token</button>
      </p>
    );
  }
  return (
    <p className="acting acting--unverified" role="status">
      <strong>Unverified.</strong> This API runs with <code>AGENTOS_AUTH=asserted</code>: the
      principal below is recorded exactly as typed and nothing checks it.
      {" "}
      <label>
        Record decisions as{" "}
        <input
          aria-label="principal id (unverified)"
          value={actingAs?.id ?? ""}
          onChange={(e) => setTypedPrincipalId(e.target.value)}
          placeholder="your id"
        />
      </label>
    </p>
  );
}

export function TokenForm({ session }: { session: Session }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="token-form"
      onSubmit={(e) => { e.preventDefault(); void session.submitToken(value); setValue(""); }}
    >
      <label>
        Bearer token{" "}
        <input
          type="password"
          autoComplete="off"
          aria-label="bearer token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </label>
      <button type="submit">Sign in</button>
      <p className="hint">Kept in this tab only (sessionStorage); never sent anywhere but this origin.</p>
      {session.error && <p role="alert" className="error">{session.error}</p>}
    </form>
  );
}
