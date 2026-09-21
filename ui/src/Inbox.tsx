import { useCallback, useEffect, useState } from "react";
import { ApiError, Approval, api } from "./api";
import { fmtAgo } from "./fmt";
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

interface Outcome { id: number; key: string; text: string; ok: boolean }
let outcomeSeq = 0;   // stable keys for a prepend-ordered list; index keys re-associate rows

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
      setOutcomes((o) => [{ id: ++outcomeSeq, key, ok: true,
        text: `${verb === "approve" ? "Approved" : "Rejected"} ${a.step_id ?? "cost ceiling"} on run ${a.run_id.slice(0, 8)} as ${session.actingAs?.id ?? "?"}` }, ...o].slice(0, 8));
      await refresh();
    } catch (e) {
      const text = e instanceof ApiError ? `${e.status}: ${e.detail}` : String(e);
      setOutcomes((o) => [{ id: ++outcomeSeq, key, ok: false, text: `${verb} failed — ${text}` }, ...o].slice(0, 8));
    } finally {
      setBusy(null);
    }
  };

  const canDecide = session.actingAs !== null;

  return (
    <section aria-labelledby="inbox-heading">
      <div className="page-head">
        <div>
          <h2 id="inbox-heading">Pending approvals{items && items.length > 0 ? <span className="count">{items.length}</span> : null}</h2>
          <p className="page-head__sub">Runs suspended before an effect. Approving lets the step run; rejecting fails it. Both are recorded in the run's log with who decided and why.</p>
        </div>
      </div>
      {error && <p role="alert" className="error">Could not load approvals: {error}</p>}
      {items === null && !error && <p className="muted">Loading…</p>}
      {items !== null && items.length === 0 && (
        <div className="empty empty--calm">
          <p><strong>Nothing is waiting on a decision.</strong></p>
          <p className="hint">A run appears here the moment one of its steps declares an effect (spend, external write, code execution) or exceeds its cost ceiling.</p>
        </div>
      )}
      <ul className="inbox">
        {items?.map((a) => {
          const key = `${a.run_id}:${a.approval_id}`;
          return (
            <li key={key} className={`approval card approval--${a.kind}`}>
              <div className="approval__head">
                <span className="approval__wf"><strong>{a.workflow}</strong></span>
                <span className="muted">run</span>
                <code className="mono" title={a.run_id}>{a.run_id.slice(0, 8)}</code>
                <span className="approval__badges">
                  <span className={`kind kind--${a.kind}`}>{a.kind}</span>
                  {a.effect_classes.map((c) => <span key={c} className={`chip chip--effect chip--${c}`}>{c}</span>)}
                </span>
              </div>
              <p className="approval__what">{describe(a)}</p>
              <p className="approval__meta">
                requested <time dateTime={a.requested_at} title={a.requested_at}>{fmtAgo(a.requested_at)}</time>
                <span className="sep">·</span>{expiry(a)}
                {a.reason && a.reason !== describe(a) && a.reason.replace(/'/g, '"') !== describe(a)
                  ? <><span className="sep">·</span>{a.reason}</> : null}
              </p>
              <div className="approval__decide">
                <label className="approval__reason">
                  <span>Reason</span>
                  <input
                    aria-label={`reason for ${a.step_id ?? "cost"} on ${a.run_id.slice(0, 8)}`}
                    value={reasons[key] ?? ""}
                    placeholder="optional — recorded in the log"
                    onChange={(e) => setReasons((r) => ({ ...r, [key]: e.target.value }))}
                  />
                </label>
                <div className="approval__actions">
                  <button type="button" className="primary" disabled={!canDecide || busy === key}
                          onClick={() => void decide(a, "approve")}>
                    Approve
                  </button>
                  <button type="button" disabled={!canDecide || busy === key} className="danger"
                          onClick={() => void decide(a, "reject")}>
                    Reject
                  </button>
                </div>
              </div>
              {!canDecide && (
                <p role="note" className="hint approval__locked">
                  Decisions are disabled until the API knows who is deciding — enter your id in the top bar.
                </p>
              )}
            </li>
          );
        })}
      </ul>
      {outcomes.length > 0 && (
        <section className="outcomes-wrap" aria-labelledby="outcomes-heading">
          <h3 id="outcomes-heading">Recent decisions</h3>
          <ul className="outcomes" aria-label="recent decisions">
            {outcomes.map((o) => (
              <li key={o.id} role={o.ok ? "status" : "alert"} className={o.ok ? "ok" : "error"}>
                {o.text}
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}
