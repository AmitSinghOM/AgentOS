import { useCallback, useEffect, useState } from "react";
import { ApiError, Approval, api } from "./api";
import { ApprovalCard, ApprovalContext, Outcome, OutcomesList, approvalKey } from "./ApprovalCard";
import type { Session } from "./Session";
import type { RunState, WorkflowDef } from "./graph";
import type { Route } from "./router";
import { TUTORIAL_URL } from "./links";

const POLL_MS = 3000;

/** `onPending` lets the shell's nav badge follow this list instead of polling on its own while
 *  the inbox is on screen — one reader of /approvals, and the badge drops the moment a decision lands. */
export function Inbox({ session, onPending, navigate }: { session: Session; onPending?: (n: number) => void; navigate: (r: Route) => void }) {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);

  const refresh = useCallback(async () => {
    try {
      const res = await api.approvals();
      setItems(res.data);
      onPending?.(res.data.length);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
    }
  }, [onPending]);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => { void refresh(); }, POLL_MS);
    return () => window.clearInterval(t);
  }, [refresh]);

  /** The inbox lists approvals, not runs; the run and its definition are fetched only when the
   *  operator opens "what this approves" on a card — never on the 3 s poll. */
  const loadContext = (a: Approval) => async (): Promise<ApprovalContext> => {
    const run = await api.run<RunState>(a.run_id);
    let def: WorkflowDef | null = null;
    try { def = await api.workflow<WorkflowDef>(a.workflow); } catch { def = null; }   // the run is the point; the definition is a bonus
    return { run, def };
  };

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
        <section className="empty empty--calm" aria-labelledby="inbox-empty-heading">
          <p id="inbox-empty-heading"><strong>Nothing is waiting on a decision.</strong></p>
          <p className="hint">A run appears here the moment one of its steps declares an effect (spend, external write, code execution) or exceeds its cost ceiling.</p>
          <p className="hint">
            To cause one: register an agent with <code>"declared_effects": ["spend"]</code>, put it in a workflow, start a run.
            The run suspends before that step and its approval lands here. The{" "}
            <a href={TUTORIAL_URL} target="_blank" rel="noopener noreferrer">tutorial</a> walks exactly that run —
            define, start, watch it suspend, decide, verify the log, time-travel — in about ten minutes.
          </p>
        </section>
      )}
      <ul className="inbox">
        {items?.map((a) => (
          <ApprovalCard
            key={approvalKey(a)}
            a={a}
            session={session}
            loadContext={loadContext(a)}
            linkToRun
            navigate={navigate}
            onOutcome={(o) => setOutcomes((prev) => [o, ...prev].slice(0, 8))}
            onChanged={refresh}
          />
        ))}
      </ul>
      <OutcomesList outcomes={outcomes} />
    </section>
  );
}
