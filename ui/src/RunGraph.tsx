import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, Approval, api } from "./api";
import { ApprovalCard, ApprovalRecord, Outcome, OutcomesList } from "./ApprovalCard";
import { CostPanel } from "./CostPanel";
import { NODE_H, NODE_LABEL, NODE_W, NodeView, Point, RunState, WorkflowDef, WorkflowNodeDef, layout, nodeState } from "./graph";
import { Link, Route } from "./router";
import type { Session } from "./Session";
import { focusPrincipalInput } from "./Session";
import { SSEFrame, readSSE } from "./sse";
import { Timeline } from "./Timeline";
import type { EventRecord } from "./derive";
import { deriveLatency, fmtDuration, mergeEvents } from "./derive";
import { eventFamily, fmtAgo, fmtClock, fmtDateTime, shortHash } from "./fmt";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const REFETCH_DEBOUNCE_MS = 150;
const RECONNECT_MS = 1000;
const MAX_CONSECUTIVE_ERRORS = 5;
const TICKER_MAX = 40;
const COPIED_MS = 1500;

/** Which verbs the fold permits, mirroring the engine's 409 rules (C5) so a disabled button and a
 *  409 never disagree: cancel unless terminal (request_cancel); pause unless terminal or already
 *  paused (request_pause — a suspended run CAN be paused); resume only paused (resume). */
export function allowedControls(status: string): { cancel: boolean; pause: boolean; resume: boolean } {
  return {
    cancel: !TERMINAL.has(status),
    pause: !TERMINAL.has(status) && status !== "paused",
    resume: status === "paused",
  };
}

const errText = (e: unknown) => e instanceof ApiError ? `${e.status} ${e.detail}` : String(e);

/** Cancel / Pause / Resume on the run page (Temporal's workflow actions, Hatchet's header). Cancel
 *  is irreversible, so it is a two-step inline confirm with a reason — the house pattern is the
 *  inbox's decision footer, not a modal. The principal rule is the inbox's: asserted mode sends
 *  the id the operator typed and nothing is enabled until one exists. */
export function RunControls({ run, session, onDone }: { run: RunState; session: Session; onDone: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"cancel" | "pause" | "resume" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const allowed = allowedControls(run.status);
  const canAct = session.actingAs !== null;
  const principal = session.unverified ? session.actingAs ?? undefined : undefined;

  const act = async (verb: "cancel" | "pause" | "resume") => {
    setBusy(verb);
    setError(null);
    try {
      await api.control(run.id, verb, verb === "cancel" ? reason : "", principal);
      setConfirming(false);
      setReason("");
      onDone();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="controls" role="group" aria-label="run controls">
      <div className="controls__row">
        <button type="button" className="ghost" disabled={!canAct || !allowed.pause || busy !== null}
                title={allowed.pause ? "finish the current wave, then hold the run" : "a terminal or already-paused run cannot be paused"}
                onClick={() => void act("pause")}>{busy === "pause" ? "Pausing…" : "Pause"}</button>
        <button type="button" className={allowed.resume ? "primary" : undefined} disabled={!canAct || !allowed.resume || busy !== null}
                title={allowed.resume ? "re-enqueue the paused run" : "only a paused run can be resumed"}
                onClick={() => void act("resume")}>{busy === "resume" ? "Resuming…" : "Resume"}</button>
        <button type="button" className="danger" disabled={!canAct || !allowed.cancel || busy !== null || confirming}
                title={allowed.cancel ? "stop the run at the worker's next boundary; cannot be undone" : "the run is already terminal"}
                onClick={() => setConfirming(true)}>Cancel run</button>
        {!canAct && (
          <span className="hint controls__hint">
            Controls are disabled until the API knows who is acting —{" "}
            <button type="button" className="linklike" onClick={focusPrincipalInput}>enter your id in the top bar</button>.
          </span>
        )}
      </div>
      {confirming && (
        <form className="confirm" onSubmit={(e) => { e.preventDefault(); void act("cancel"); }}>
          <p className="confirm__text">
            <strong>Cancel this run?</strong> The in-flight step stops at its next progress call; recorded steps stay in the log. This cannot be undone.
          </p>
          <label className="confirm__reason">Reason
            <input type="text" aria-label="cancel reason" value={reason} placeholder="optional — recorded in the log"
                   onChange={(e) => setReason(e.target.value)} autoFocus />
          </label>
          <div className="confirm__actions">
            <button type="submit" className="danger" disabled={busy !== null}>{busy === "cancel" ? "Cancelling…" : "Confirm cancel"}</button>
            <button type="button" className="ghost" disabled={busy !== null} onClick={() => { setConfirming(false); setReason(""); setError(null); }}>Keep running</button>
          </div>
        </form>
      )}
      {error && <p role="alert" className="error">{error}</p>}
    </div>
  );
}

const RETRYABLE = new Set(["failed", "dead_lettered"]);

/** The selected node's facts (Argo's node panel, Hatchet's step detail), all from the fold plus
 *  the step's own events; retry only when the fold says the API will accept it (C11). */
export function StepDetail({ node, view, run, events, session, live, onClose, onDone }: {
  node: WorkflowNodeDef; view: NodeView; run: RunState; events: EventRecord[]; session: Session;
  /** False while time-travelling: the facts are the historical fold, and retry acts on the LIVE run,
   *  so it is not offered against a state that may no longer hold. */
  live: boolean;
  onClose: () => void; onDone: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lat = deriveLatency(events, run, [node.id])[0];
  const canAct = session.actingAs !== null;
  const principal = session.unverified ? session.actingAs ?? undefined : undefined;
  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.retry(run.id, node.id, reason, principal);
      setReason("");
      onDone();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const headingId = `step-${node.id}-heading`;
  return (
    <section className="panel step" role="region" aria-labelledby={headingId}>
      <div className="panel__head">
        <h3 id={headingId}>step {node.id}</h3>
        <p className="panel__sub">From the fold and this step's events. The timeline below shows only this step's events and run-level ones.</p>
        <button type="button" className="ghost" onClick={onClose}>Show all events</button>
      </div>
      <dl className="step__facts">
        <div><dt>agent</dt><dd>{node.agent}</dd></div>
        <div><dt>state</dt><dd><span className={`status status--${view.status}`}>{NODE_LABEL[view.status]}</span></dd></div>
        <div><dt>attempts</dt><dd className="num">{lat.attempts}</dd></div>
        <div><dt>cost</dt><dd className="num">{view.cost ?? "—"}</dd></div>
        <div><dt>started</dt><dd className="num" title={lat.started_at ?? undefined}>{fmtClock(lat.started_at)}</dd></div>
        <div><dt>finished</dt><dd className="num" title={lat.finished_at ?? undefined}>{fmtClock(lat.finished_at)}</dd></div>
        <div><dt>duration</dt><dd className="num">{fmtDuration(lat.duration_ms)}</dd></div>
        {node.depends_on.length > 0 && <div><dt>after</dt><dd>{node.depends_on.join(", ")}</dd></div>}
        {view.detail && (
          <div className="step__wide">
            <dt>{view.status === "dead_lettered" ? "cause" : view.status === "failed" ? "error" : view.status === "awaiting_approval" ? "effect" : "detail"}</dt>
            <dd className={RETRYABLE.has(view.status) ? "error" : "note"}>{view.detail}</dd>
          </div>
        )}
      </dl>
      {live && RETRYABLE.has(view.status) && (
        <form className="confirm" onSubmit={(e) => { e.preventDefault(); void retry(); }}>
          <label className="confirm__reason">Reason
            <input type="text" aria-label="retry reason" value={reason} placeholder="optional — recorded as step.retry_requested"
                   onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="confirm__actions">
            <button type="submit" className="primary" disabled={!canAct || busy}>{busy ? "Requesting…" : "Retry step"}</button>
            {!canAct && <span className="hint">Disabled until the API knows who is acting.</span>}
          </div>
        </form>
      )}
      {error && <p role="alert" className="error">{error}</p>}
    </section>
  );
}

/** The run id, shown as its 8-char prefix (as everywhere else) and copied in full on click. The
 *  prefix is what an operator reads; the full id is what they paste into a curl or a ticket. */
export function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), COPIED_MS);
    } catch { /* clipboard denied (insecure context, permission): the title still carries the full id */ }
  };
  return (
    <button type="button" className={`copy-id${copied ? " copy-id--copied" : ""}`} onClick={() => void copy()} aria-label="copy run id" title={`${id} — click to copy`}>
      <code className="mono run__id">{id.slice(0, 8)}</code>
      <span className="copy-id__hint" aria-live="polite">{copied ? "copied" : "copy"}</span>
    </button>
  );
}

export interface TickerItem { seq: number; type: string; at: string; step?: string }

function tickerItem(f: SSEFrame): TickerItem | null {
  if (!f.data) return null;
  try {
    const d = JSON.parse(f.data) as { seq: number; event_type: string; occurred_at: string; step_id?: string };
    return { seq: d.seq, type: d.event_type, at: d.occurred_at, step: d.step_id };
  } catch {
    return null;
  }
}

export function RunGraph({ runId, navigate, session }: { runId: string; navigate: (r: Route) => void; session: Session }) {
  const [run, setRun] = useState<RunState | null>(null);
  const [def, setDef] = useState<WorkflowDef | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticker, setTicker] = useState<TickerItem[]>([]);
  const [live, setLive] = useState<"connecting" | "live" | "closed" | "error">("connecting");
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [at, setAt] = useState<number | null>(null);            // time travel: null = live
  const [atRun, setAtRun] = useState<RunState | null>(null);    // the SERVER's fold through `at`
  const [atError, setAtError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null); // node whose detail + events are shown
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);      // decisions made from this page
  const debounce = useRef<number | null>(null);
  const lastId = useRef<string | null>(null);
  const eventsRef = useRef<EventRecord[]>([]);     // what we hold, so refetch asks only for what follows

  const refetch = useCallback(async () => {
    try {
      const r = await api.run<RunState>(runId);
      setRun(r);
      setError(null);
      try {
        // Only the tail: the log is append-only, so events we hold never change. Walk the
        // pages until has_more is false; a run with thousands of events costs one small page
        // per frame instead of the whole log. Re-read the ref each page: another refetch may
        // have landed meanwhile, and mergeEvents keeps each seq once.
        let after = eventsRef.current.length ? eventsRef.current[eventsRef.current.length - 1].seq : 0;
        for (let guard = 0; guard < 100; guard++) {           // never trust has_more to terminate
          const page = await api.events<{ data: EventRecord[]; last_seq: number; has_more: boolean }>(runId, after);
          eventsRef.current = mergeEvents(eventsRef.current, page.data);
          if (!page.has_more || page.last_seq <= after) break;
          after = page.last_seq;
        }
        setEvents(eventsRef.current);
      } catch { /* the ticker still shows frames; the timeline catches up on the next refetch */ }
      return r;
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
      return null;
    }
  }, [runId]);

  // initial load: the run, then its (current) definition
  useEffect(() => {
    let alive = true;
    eventsRef.current = [];         // a different run starts from an empty log
    setEvents([]);
    setSelected(null);              // a node id from another run means nothing here
    (async () => {
      const r = await refetch();
      if (!r || !alive) return;
      try {
        setDef(await api.workflow<WorkflowDef>(r.workflow));
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
      }
    })();
    return () => { alive = false; };
  }, [refetch]);

  // time travel: the state at `at` is the server's fold of the log prefix (?at=k); we never fold here
  useEffect(() => {
    setAtRun(null);                 // never show another seq's state under this seq's banner
    setAtError(null);
    if (at === null) return;
    let alive = true;
    api.runAt<RunState>(runId, at)
      .then((r) => { if (alive) { setAtRun(r); setAtError(null); } })
      .catch((e) => { if (alive) setAtError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)); });
    return () => { alive = false; };
  }, [runId, at]);

  // the stream: every frame lands in the ticker and schedules ONE refetch of the folded run
  useEffect(() => {
    const ctrl = new AbortController();
    let stopped = false;
    const scheduleRefetch = () => {
      if (debounce.current !== null) window.clearTimeout(debounce.current);
      debounce.current = window.setTimeout(() => { void refetch(); }, REFETCH_DEBOUNCE_MS);
    };
    let errors = 0;
    const loop = async () => {
      while (!stopped) {
        setLive("connecting");
        try {
          const last = await readSSE(`/runs/${encodeURIComponent(runId)}/stream`, {
            lastEventId: lastId.current,
            signal: ctrl.signal,
            onFrame: (f) => {
              if (f.id) lastId.current = f.id;
              const item = tickerItem(f);
              if (item) setTicker((t) => [item, ...t].slice(0, TICKER_MAX));
              scheduleRefetch();
              setLive("live");
            },
          });
          if (last) lastId.current = last;
          errors = 0;
        } catch (e) {
          if (ctrl.signal.aborted) return;
          setLive("error");
          setError(e instanceof ApiError ? `${e.status} ${e.detail}` : e instanceof Error ? e.message : String(e));
          if (++errors >= MAX_CONSECUTIVE_ERRORS) {
            setError((prev) => `${prev ?? "stream failed"} — gave up after ${errors} attempts; reload to retry`);
            return;
          }
          await new Promise((r) => setTimeout(r, RECONNECT_MS * 5));
          continue;
        }
        // server closed: terminal event or max duration. Refetch once; reconnect unless terminal.
        const r = await refetch();
        if (!r || TERMINAL.has(r.status)) { setLive("closed"); return; }
        await new Promise((res) => setTimeout(res, RECONNECT_MS));
      }
    };
    void loop();
    return () => {
      stopped = true;
      ctrl.abort();
      if (debounce.current !== null) window.clearTimeout(debounce.current);
    };
  }, [runId, refetch]);

  if (error && !run) {
    return (
      <div className="card card--center state">
        <h2>Could not load run</h2>
        <p role="alert" className="error">Could not load run: {error}</p>
        <p className="hint">The id in the address bar may be wrong, or the run may live in another store.</p>
        <Link to={{ page: "runs" }} navigate={navigate} className="button-link">← Back to runs</Link>
      </div>
    );
  }
  if (!run) return <p className="muted state state--inline">Loading run…</p>;

  const shown = at === null ? run : atRun;      // live fold, or the SERVER's fold through `at`

  // Approvals as the SHOWN fold carries them (live, or the server's fold through `at`), with
  // run_id/workflow re-attached for the shared card. Decisions are offered only on the live fold:
  // a historical fold's "pending" may be decided already, and the card would disagree with the API.
  const approvals: Approval[] = Object.values((shown ?? run).approvals).map((a) => ({ ...a, run_id: run.id, workflow: run.workflow }));
  const pendingApprovals = approvals.filter((a) => a.status === "pending");
  const decidable = at === null;
  const decidedApprovals = approvals.filter((a) => a.status !== "pending")
    .sort((x, y) => (x.decided_at ?? "").localeCompare(y.decided_at ?? ""));
  const versionMismatch = def !== null && def.version !== run.workflow_version;
  const selectedNode = def?.nodes.find((n) => n.id === selected) ?? null;
  // The step's own events plus run-level ones (no step_id): the run's frame around the step.
  const timelineEvents = selectedNode ? events.filter((e) => !e.step_id || e.step_id === selectedNode.id) : events;
  const toggleSelect = (id: string) => setSelected((cur) => (cur === id ? null : id));

  return (
    <section aria-labelledby="run-heading" className="run">
      <header className="run__head page-head page-head--stack">
        <nav aria-label="breadcrumb" className="crumbs">
          <Link to={{ page: "runs" }} navigate={navigate}>Runs</Link>
          <span className="crumbs__sep" aria-hidden="true">/</span>
          <span>{run.workflow}</span>
        </nav>
        <h2 id="run-heading">
          {run.workflow} <CopyId id={run.id} />
        </h2>
        <p className="run__meta">
          <span className={`status status--${run.status}`}>{run.status}</span>
          <span className={`live live--${live}`} role="status" aria-label="stream state">{live === "live" ? "live" : live}</span>
          <span className="chip" title="workflow version this run is pinned to">v{run.workflow_version}</span>
          <span className="chip num" title="events in the log">{run.last_seq} events</span>
          <span className="chip num" title="run cost from the fold">cost {run.total_cost}</span>
          {run.sealed_through != null && <span className="chip num" title="last seq covered by an integrity seal">sealed ≤ {run.sealed_through}</span>}
          {run.policy_sha256 && <span className="chip mono" title={`operator policy sha256 ${run.policy_sha256}`}>policy {shortHash(run.policy_sha256)}</span>}
          <span className="muted" title={run.started_at}>started {fmtDateTime(run.started_at)} · {fmtAgo(run.started_at)}</span>
        </p>
        <RunControls run={run} session={session} onDone={() => { void refetch(); }} />
        {run.error && <p role="alert" className="error">{run.error}</p>}
        {versionMismatch && (
          <p role="alert" className="error">
            This run is pinned to workflow v{run.workflow_version}; the definition is now v{def!.version}.
            The graph below is the current definition, and the run cannot advance against it (C3).
          </p>
        )}
        {error && <p role="alert" className="error">{error}</p>}
      </header>

      {atError && <p role="alert" className="error">Could not load state at seq {at}: {atError}</p>}
      {approvals.length > 0 && (
        <section className="panel approvals" role="region" aria-labelledby="approvals-heading">
          <div className="panel__head">
            <h3 id="approvals-heading">Approvals{pendingApprovals.length > 0 ? <span className="count">{pendingApprovals.length}</span> : null}</h3>
            <p className="panel__sub">
              {!decidable
                ? <>Gates as the server's fold through seq <strong>{at}</strong> records them; decide from the live view. </>
                : pendingApprovals.length > 0
                ? "The run is suspended until each pending gate is decided; decide here or in the inbox. "
                : "Every gate this run raised, as the log records it. "}
              Decisions are recorded with who decided and why.
            </p>
          </div>
          {!decidable && pendingApprovals.length > 0 && (
            <ul className="approval-records" aria-label="pending approvals at this seq">
              {pendingApprovals.map((a) => <ApprovalRecord key={a.approval_id} a={a} />)}
            </ul>
          )}
          {decidable && pendingApprovals.length > 0 && (
            <ul className="inbox inbox--inline">
              {pendingApprovals.map((a) => (
                <ApprovalCard
                  key={a.approval_id}
                  a={a}
                  session={session}
                  context={{ run, def }}
                  linkToRun={false}
                  navigate={navigate}
                  onOutcome={(o) => setOutcomes((prev) => [o, ...prev].slice(0, 8))}
                  onChanged={refetch}
                />
              ))}
            </ul>
          )}
          {decidedApprovals.length > 0 && (
            <ul className="approval-records" aria-label="decided approvals">
              {decidedApprovals.map((a) => <ApprovalRecord key={a.approval_id} a={a} />)}
            </ul>
          )}
          <OutcomesList outcomes={outcomes} />
        </section>
      )}
      {def && shown && (
        <section className="panel" aria-labelledby="graph-heading">
          <div className="panel__head">
            <h3 id="graph-heading">Graph</h3>
            <p className="panel__sub">
              {at !== null ? <>State right after seq <strong>{at}</strong> — the server's fold of that prefix.</> : "Node states from the live fold; edges follow the workflow definition."}
              {" "}Click a node for its detail.
            </p>
          </div>
          <Graph def={def} run={shown} selected={selected} onSelect={toggleSelect} />
        </section>
      )}
      {def && shown && <CostPanel def={def} run={shown} events={at !== null ? events.filter((e) => e.seq <= at) : events} selected={selected} onSelect={toggleSelect} />}
      {def && !shown && !atError && <p role="status" aria-label="seek state">Loading the state at seq {at}…</p>}
      {selectedNode && shown && (
        <StepDetail node={selectedNode} view={nodeState(shown, selectedNode)} run={shown} events={at !== null ? events.filter((e) => e.seq <= at) : events} session={session} live={at === null}
                    onClose={() => setSelected(null)} onDone={() => { void refetch(); }} />
      )}
      <Timeline key={runId} events={timelineEvents} lastSeq={run.last_seq} at={at} onSeek={setAt}
                filterNote={selectedNode ? `step ${selectedNode.id} and run-level events only` : null} />

      <section className="panel" aria-labelledby="ticker-heading">
        <div className="panel__head">
          <h3 id="ticker-heading">Live frames</h3>
          <p className="panel__sub">Newest first, straight from the event stream; the timeline above is the durable log.</p>
        </div>
        <ol className="ticker" aria-label="event ticker" reversed>
          {ticker.map((t) => (
            <li key={t.seq}>
              <code className="seq">{t.seq}</code>{" "}
              <span className={`dot dot--${eventFamily(t.type)}`} aria-hidden="true" />
              <code>{t.type}</code>{t.step ? <> <span className="chip">{t.step}</span></> : null}{" "}
              <span className="muted num" title={t.at}>{fmtClock(t.at)}</span>
            </li>
          ))}
          {ticker.length === 0 && <li className="muted">waiting for the stream…</li>}
        </ol>
      </section>
    </section>
  );
}

const LABEL = NODE_LABEL;

/** SVG path through dagre's waypoints: a cubic between each consecutive pair with horizontal
 *  tangents, so a two-point edge is the same S-curve as before and a routed long edge bends
 *  smoothly around the layer it skips. */
export function edgePath(points: Point[]): string {
  if (points.length === 0) return "";
  const [first, ...rest] = points;
  let d = `M ${first.x} ${first.y}`;
  let prev = first;
  for (const p of rest) {
    const mx = (prev.x + p.x) / 2;
    d += ` C ${mx} ${prev.y}, ${mx} ${p.y}, ${p.x} ${p.y}`;
    prev = p;
  }
  return d;
}

export function Graph({ def, run, selected = null, onSelect }: {
  def: WorkflowDef; run: RunState; selected?: string | null; onSelect?: (id: string) => void;
}) {
  const l = layout(def);
  const pos = new Map(l.nodes.map((n) => [n.id, n]));
  const pad = 8;
  const views = def.nodes.map((n) => [n, nodeState(run, n)] as const);
  const present = Array.from(new Set(views.map(([, v]) => v.status)));
  return (
    <div className="graph__wrap">
    {/* role="group", not "img": `img` makes children presentational, which would hide every
        node's "<id>: <state>" label from assistive tech — the one thing an operator asks of it. */}
    <svg className="graph" role="group" aria-label={`workflow ${def.name} run graph`}
         viewBox={`${-pad} ${-pad} ${l.width + 2 * pad} ${l.height + 2 * pad}`}
         width={Math.min(l.width + 2 * pad, 960)} style={{ maxWidth: "100%" }}>
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
        </marker>
      </defs>
      {l.edges.map((e) => (
        <path key={`${e.from}->${e.to}`} className="edge" d={edgePath(e.points)} markerEnd="url(#arrow)" />
      ))}
      {views.map(([n, v]) => {
        const p = pos.get(n.id)!;
        const isSel = selected === n.id;
        // The node stays a labelled group (its "<id>: <state>" is what a reader hears); selection
        // is a click or Enter/Space on it, announced through aria-current.
        return (
          <g key={n.id} className={`node node--${v.status}${isSel ? " node--selected" : ""}${onSelect ? " node--clickable" : ""}`}
             transform={`translate(${p.x} ${p.y})`}
             role="group" aria-label={`${n.id}: ${LABEL[v.status]}`} aria-current={isSel ? "true" : undefined}
             tabIndex={onSelect ? 0 : undefined}
             onClick={onSelect ? () => onSelect(n.id) : undefined}
             onKeyDown={onSelect ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(n.id); } } : undefined}>
            <rect width={NODE_W} height={NODE_H} rx={8} />
            <rect className="node__stripe" width={4} height={NODE_H - 12} x={0} y={6} rx={2} />
            <text x={12} y={20} className="node__id">{n.id}</text>
            <text x={12} y={36} className="node__agent">{n.agent}{v.attempt > 1 ? ` · attempt ${v.attempt}` : ""}</text>
            <text x={12} y={49} className="node__state">
              {LABEL[v.status]}{v.cost && v.cost !== "0" ? ` · ${v.cost}` : ""}
              {v.status === "running" && v.progress !== null ? ` · ${Math.round(v.progress * 100)}%` : ""}
            </text>
            {v.detail && <title>{v.detail}</title>}
          </g>
        );
      })}
    </svg>
    <ul className="legend" aria-label="node states in this run">
      {present.map((s) => <li key={s}><span className={`swatch swatch--${s}`} aria-hidden="true" />{LABEL[s]}</li>)}
    </ul>
    </div>
  );
}
