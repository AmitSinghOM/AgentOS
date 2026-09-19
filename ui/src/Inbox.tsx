import { useCallback, useEffect, useState } from "react";
import { ApiError, Approval, api } from "./api";
import type { Session } from "./Session";

const POLL_MS = 3000;

function describe(a: Approval): string {
  if (a.kind === "cost") {
    return `run cost ${a.cost_at_request ?? "?"} exceeded its ceiling; approving raises it to ${a.proposed_ceiling ?? "?"}`;
  }
  return `step "${a.step_id}" declares ${a.effect_classes.join(", ")}`;
}

function expiry(a: Approval): string {
  if (!a.expires_at) return "no expiry";
  const ms = new Date(a.expires_at).getTime() - Date.now();
  if (Number.isNaN(ms)) return `expires ${a.expires_at}`;
  if (ms <= 0) return "expired — the sweep will reject it";
  const min = Math.round(ms / 60000);
  return min < 1 ? "expires in under a minute" : `expires in ${min} min`;
}

interface Outcome { key: string; text: string; ok: boolean }

export function Inbox({ session }: { session: Session }) {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);

  const refresh = useCallback(async () => {
    try {
      const res = await api.approvals();
      setItems(res.data);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => { void refresh(); }, POLL_MS);
    return () => window.clearInterval(t);
  }, [refresh]);

  const decide = async (a: Approval, verb: "approve" | "reject") => {
    const key = `${a.run_id}:${a.approval_id}`;
    setBusy(key);
    try {
      const principal = session.unverified ? session.actingAs ?? undefined : undefined;
      await api.decide(a, verb, reasons[key] ?? "", principal);
      setOutcomes((o) => [{ key, ok: true,
        text: `${verb === "approve" ? "Approved" : "Rejected"} ${a.step_id ?? "cost ceiling"} on run ${a.run_id.slice(0, 8)} as ${session.actingAs?.id ?? "?"}` }, ...o].slice(0, 8));
      await refresh();
    } catch (e) {
      const text = e instanceof ApiError ? `${e.status}: ${e.detail}` : String(e);
      setOutcomes((o) => [{ key, ok: false, text: `${verb} failed — ${text}` }, ...o].slice(0, 8));
    } finally {
      setBusy(null);
    }
  };

  const canDecide = session.actingAs !== null;

  return (
    <section aria-labelledby="inbox-heading">
      <h2 id="inbox-heading">Pending approvals</h2>
      {error && <p role="alert" className="error">Could not load approvals: {error}</p>}
      {items === null && !error && <p>Loading…</p>}
      {items !== null && items.length === 0 && <p>Nothing is waiting on a decision.</p>}
      {!canDecide && items && items.length > 0 && (
        <p role="note" className="hint">Decisions are disabled until the API knows who is deciding.</p>
      )}
      <ul className="inbox">
        {items?.map((a) => {
          const key = `${a.run_id}:${a.approval_id}`;
          return (
            <li key={key} className="approval">
              <div className="approval__head">
                <strong>{a.workflow}</strong> · run <code>{a.run_id.slice(0, 8)}</code> ·{" "}
                <span className={`kind kind--${a.kind}`}>{a.kind}</span>
              </div>
              <p className="approval__what">{describe(a)}</p>
              <p className="approval__meta">
                requested {a.requested_at} · {expiry(a)}
                {a.reason ? ` · ${a.reason}` : ""}
              </p>
              <label>
                Reason{" "}
                <input
                  aria-label={`reason for ${a.step_id ?? "cost"} on ${a.run_id.slice(0, 8)}`}
                  value={reasons[key] ?? ""}
                  onChange={(e) => setReasons((r) => ({ ...r, [key]: e.target.value }))}
                />
              </label>
              <div className="approval__actions">
                <button type="button" disabled={!canDecide || busy === key}
                        onClick={() => void decide(a, "approve")}>
                  Approve
                </button>
                <button type="button" disabled={!canDecide || busy === key} className="danger"
                        onClick={() => void decide(a, "reject")}>
                  Reject
                </button>
              </div>
            </li>
          );
        })}
      </ul>
      {outcomes.length > 0 && (
        <ul className="outcomes" aria-label="recent decisions">
          {outcomes.map((o, i) => (
            <li key={`${o.key}-${i}`} role={o.ok ? "status" : "alert"} className={o.ok ? "ok" : "error"}>
              {o.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
