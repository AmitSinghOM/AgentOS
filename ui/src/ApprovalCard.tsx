/**
 * One approval as a decision card — the same component on the inbox and on the run page
 * (comparison pass 2, A1-A3). What it shows is the API's: the approval record, the fold's run,
 * the workflow definition. What it adds is presentation: the consequence of each verb, the
 * principal the log will name, and — on demand — what the gated step will receive.
 *
 * The API stays the authority on whether a decision is allowed: the card warns about the
 * engine's human-only rule but never disables for it, because `allow_agent_approval` can make
 * the API accept what the UI would have refused (the pass-1 pause lesson).
 */
import { useState } from "react";
import { ApiError, Approval, api } from "./api";
import { fmtAgo, fmtDateTime } from "./fmt";
import { focusPrincipalInput } from "./Session";
import type { Session } from "./Session";
import type { RunState, WorkflowDef } from "./graph";
import { Link } from "./router";
import type { Route } from "./router";

/** Mirrors models.HUMAN_ONLY_EFFECTS; a cost ceiling is human-only too (engine.approve). */
export const HUMAN_ONLY_EFFECTS = new Set(["spend", "write_external"]);

export function describeApproval(a: Approval): string {
  if (a.kind === "cost") {
    return `run cost ${a.cost_at_request ?? "?"} exceeded its ceiling; approving raises it to ${a.proposed_ceiling ?? "?"}`;
  }
  return `step "${a.step_id}" declares ${a.effect_classes.join(", ")}`;
}

export function expiryText(a: Approval): string {
  if (!a.expires_at) return "no expiry";
  const ms = new Date(a.expires_at).getTime() - Date.now();
  if (Number.isNaN(ms)) return `expires ${a.expires_at}`;
  if (ms <= 0) return "expired — the sweep will reject it";
  const min = Math.round(ms / 60000);
  return min < 1 ? "expires in under a minute" : `expires in ${min} min`;
}

/** What each verb does, from engine.approve / engine.reject — not a paraphrase of hope. */
export function consequences(a: Approval): { approve: string; reject: string } {
  if (a.kind === "cost") {
    return {
      approve: `Approve raises the run's cost ceiling to ${a.proposed_ceiling ?? "?"} and resumes it.`,
      reject: "Reject fails the run; the cost so far is already spent and recorded.",
    };
  }
  return {
    approve: `Approve lets "${a.step_id}" run once.`,
    reject: "Reject dead-letters it and fails the run; retry re-requests approval.",
  };
}

/** The classes on this approval that the engine grants only to a human principal. */
export function humanOnlyClasses(a: Approval): string[] {
  if (a.kind === "cost") return ["cost ceiling"];
  return a.effect_classes.filter((c) => HUMAN_ONLY_EFFECTS.has(c));
}

export interface ApprovalContext { run: RunState; def: WorkflowDef | null }
export interface Outcome { id: number; key: string; text: string; ok: boolean }
let outcomeSeq = 0;   // stable keys for a prepend-ordered list; index keys re-associate rows
export const approvalKey = (a: Approval) => `${a.run_id}:${a.approval_id}`;

function short(v: unknown): string {
  const s = JSON.stringify(v);
  return s.length > 400 ? `${s.slice(0, 400)}…` : s;
}

/** "What this approves": the gated step's agent, the run's inputs, the upstream outputs it will
 *  receive, cost so far, attempts, and the budget rule that raised the gate. Every value is
 *  the fold's or the definition's; the pairing of upstream outputs to this step is a display
 *  derivation from `depends_on`, labelled so. */
function ContextView({ a, ctx }: { a: Approval; ctx: ApprovalContext }) {
  const { run, def } = ctx;
  const node = a.step_id ? def?.nodes.find((n) => n.id === a.step_id) ?? null : null;
  const upstream = (node?.depends_on ?? []).map((dep) => {
    const rec = [...run.steps].reverse().find((s) => s.node_id === dep);
    return { dep, output: rec?.output };
  });
  const inputs = run.inputs ?? {};
  const budget = def?.budget;
  return (
    <dl className="approval__ctx-list">
      {node && <><dt>agent</dt><dd><code className="mono">{node.agent}</code>{a.step_id && <span className="muted"> runs step <code className="mono">{a.step_id}</code></span>}</dd></>}
      <dt>run inputs</dt>
      <dd>{Object.keys(inputs).length === 0 ? <span className="muted">none</span> : <code className="mono approval__json">{short(inputs)}</code>}</dd>
      {node && (
        <>
          <dt>receives</dt>
          <dd>
            {upstream.length === 0 ? <span className="muted">nothing upstream — the step starts from the run inputs</span> : (
              <ul className="approval__upstream">
                {upstream.map(({ dep, output }) => (
                  <li key={dep}>from <code className="mono">{dep}</code>: {output === undefined
                    ? <span className="muted">no completed output in the fold</span>
                    : <code className="mono approval__json">{short(output)}</code>}</li>
                ))}
              </ul>
            )}
          </dd>
        </>
      )}
      <dt>cost so far</dt><dd className="tnum">{run.total_cost}{run.cost_ceiling ? <span className="muted"> of ceiling {run.cost_ceiling}</span> : null}</dd>
      {a.step_id && <><dt>attempts</dt><dd className="tnum">{run.attempts[a.step_id] ?? 0}</dd></>}
      {budget && (
        <>
          <dt>budget rule</dt>
          <dd>
            approval required for {(budget.approval_required_for ?? []).length ? (budget.approval_required_for ?? []).join(", ") : "nothing"}
            {" · "}agent approval {budget.allow_agent_approval ? "allowed" : "not allowed"}
            {budget.approval_timeout_seconds ? ` · times out after ${budget.approval_timeout_seconds}s` : ""}
          </dd>
        </>
      )}
      <dt className="muted">source</dt><dd className="muted">the fold of run {run.id.slice(0, 8)}{def ? ` and workflow ${def.name} v${def.version}` : ""}; "receives" pairs upstream outputs by depends_on (display derivation).</dd>
    </dl>
  );
}

export function ApprovalCard({ a, session, context, loadContext, linkToRun, navigate, onOutcome, onChanged }: {
  a: Approval;
  session: Session;
  /** Already-loaded context (the run page has the fold and the definition on screen). */
  context?: ApprovalContext;
  /** Fetch-on-open context (the inbox: nothing is fetched until the operator asks). */
  loadContext?: () => Promise<ApprovalContext>;
  linkToRun: boolean;
  navigate: (r: Route) => void;
  onOutcome: (o: Outcome) => void;
  /** The decision landed (or failed): the owner refetches whatever it shows the approval from. */
  onChanged: () => unknown | Promise<unknown>;
}) {
  const [reason, setReason] = useState("");
  const [verbInFlight, setVerbInFlight] = useState<"approve" | "reject" | null>(null);
  const [open, setOpen] = useState(false);
  const [fetched, setFetched] = useState<ApprovalContext | null>(null);
  const [ctxError, setCtxError] = useState<string | null>(null);
  const key = approvalKey(a);
  const canDecide = session.actingAs !== null;
  const busy = verbInFlight !== null;
  const what = consequences(a);
  const humanOnly = humanOnlyClasses(a);
  const actor = session.actingAs;
  const warnNonHuman = actor !== null && actor.kind !== "human" && humanOnly.length > 0;
  const ctx = context ?? fetched;

  const toggleContext = async () => {
    const next = !open;
    setOpen(next);
    if (next && !context && !fetched && loadContext) {
      try { setFetched(await loadContext()); setCtxError(null); }
      catch (e) { setCtxError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)); }
    }
  };

  const decide = async (verb: "approve" | "reject") => {
    setVerbInFlight(verb);
    try {
      const principal = session.unverified ? session.actingAs ?? undefined : undefined;
      await api.decide(a, verb, reason, principal);
      onOutcome({ id: ++outcomeSeq, key, ok: true,
        text: `${verb === "approve" ? "Approved" : "Rejected"} ${a.step_id ?? "cost ceiling"} on run ${a.run_id.slice(0, 8)} as ${session.actingAs?.id ?? "?"}` });
      await onChanged();
    } catch (e) {
      const text = e instanceof ApiError ? `${e.status}: ${e.detail}` : String(e);
      onOutcome({ id: ++outcomeSeq, key, ok: false, text: `${verb} failed — ${text}` });
      await onChanged();      // a 409 means the fold moved under us: show what it says now
    } finally {
      setVerbInFlight(null);
    }
  };

  const ctxId = `ctx-${a.approval_id}`;
  const head = (
    <>
      <strong>{a.workflow}</strong>{" "}
      <span className="muted">run</span>{" "}
      <code className="mono" title={a.run_id}>{a.run_id.slice(0, 8)}</code>
    </>
  );

  return (
    <li className={`approval card approval--${a.kind}`}>
      <div className="approval__head">
        <span className="approval__wf">
          {linkToRun ? <Link to={{ page: "run", id: a.run_id }} navigate={navigate} className="approval__run">{head}</Link> : head}
        </span>
        <span className="approval__badges">
          <span className={`kind kind--${a.kind}`}>{a.kind}</span>
          {a.effect_classes.map((c) => <span key={c} className={`chip chip--effect chip--${c}`}>{c}</span>)}
        </span>
      </div>
      <p className="approval__what">{describeApproval(a)}</p>
      <p className="approval__meta">
        requested <time dateTime={a.requested_at} title={a.requested_at}>{fmtAgo(a.requested_at)}</time>
        <span className="sep">·</span>{expiryText(a)}
        {a.reason && a.reason !== describeApproval(a) && a.reason.replace(/'/g, '"') !== describeApproval(a)
          ? <><span className="sep">·</span>{a.reason}</> : null}
        <span className="sep">·</span>
        <button type="button" className="linklike" aria-expanded={open} aria-controls={ctxId} onClick={() => void toggleContext()}>
          {open ? "hide what this approves" : "what this approves"}
        </button>
      </p>
      {open && (
        <section id={ctxId} className="approval__ctx" role="region" aria-label="what this approves">
          {ctxError && <p role="alert" className="error">Could not load the run: {ctxError}</p>}
          {!ctx && !ctxError && <p className="muted">Loading the run…</p>}
          {ctx && <ContextView a={a} ctx={ctx} />}
        </section>
      )}
      <div className="approval__decide">
        <label className="approval__reason">
          <span>Reason</span>
          <input
            aria-label={`reason for ${a.step_id ?? "cost"} on ${a.run_id.slice(0, 8)}`}
            value={reason}
            placeholder="optional — recorded in the log"
            onChange={(e) => setReason(e.target.value)}
          />
        </label>
        <div className="approval__actions" role="group" aria-label="decision">
          {actor && <span className="approval__as muted">Recorded as <strong>{actor.id}</strong>{session.unverified ? " (unverified)" : ""}</span>}
          <button type="button" className="primary" disabled={!canDecide || busy}
                  title={what.approve}
                  aria-busy={verbInFlight === "approve" ? true : undefined}
                  onClick={() => void decide("approve")}>
            {verbInFlight === "approve" ? "Approving…" : "Approve"}
          </button>
          <button type="button" disabled={!canDecide || busy} className="danger"
                  title={what.reject}
                  aria-busy={verbInFlight === "reject" ? true : undefined}
                  onClick={() => void decide("reject")}>
            {verbInFlight === "reject" ? "Rejecting…" : "Reject"}
          </button>
        </div>
      </div>
      <p className="approval__consequence hint">{what.approve} {what.reject}</p>
      {warnNonHuman && (
        <p role="note" aria-label="human-only approval" className="approval__warn">
          This approval covers {humanOnly.join(", ")} and requires a human principal; you are{" "}
          <code className="mono">{actor.kind}:{actor.id}</code>. The API will refuse unless the workflow's budget
          allows agent approval.
        </p>
      )}
      {!canDecide && (
        <p role="note" className="hint approval__locked">
          Decisions are disabled until the API knows who is deciding —{" "}
          <button type="button" className="linklike" onClick={focusPrincipalInput}>enter your id in the top bar</button>.
        </p>
      )}
    </li>
  );
}

/** A decided approval as the fold records it: status, who, why, when. */
export function ApprovalRecord({ a }: { a: Approval }) {
  const who = a.decided_by ? `${a.decided_by.kind}:${a.decided_by.id}` : "—";
  return (
    <li className={`approval-record approval-record--${a.status}`}>
      <span className={`status status--approval-${a.status}`}>{a.status}</span>
      <span className="approval-record__what">{describeApproval(a)}</span>
      {a.status === "pending" ? <span className="muted">awaiting a decision at this point in the log</span> : (
        <>
          <span className="approval-record__who">by <code className="mono">{who}</code></span>
          {a.decision_reason ? <span className="approval-record__why">“{a.decision_reason}”</span> : <span className="muted">no reason given</span>}
          {a.decided_at && <time className="muted" dateTime={a.decided_at} title={a.decided_at}>{fmtDateTime(a.decided_at)} · {fmtAgo(a.decided_at)}</time>}
        </>
      )}
    </li>
  );
}

export function OutcomesList({ outcomes }: { outcomes: Outcome[] }) {
  if (outcomes.length === 0) return null;
  return (
    <section className="outcomes-wrap" aria-labelledby="outcomes-heading">
      <h3 id="outcomes-heading">Recent decisions</h3>
      <ul className="outcomes" aria-label="recent decisions">
        {outcomes.map((o) => (
          <li key={o.id} role={o.ok ? "status" : "alert"} className={o.ok ? "ok" : "error"}>{o.text}</li>
        ))}
      </ul>
    </section>
  );
}
