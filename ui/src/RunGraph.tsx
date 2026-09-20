import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { CostPanel } from "./CostPanel";
import { NODE_H, NODE_W, NodeView, Point, RunState, WorkflowDef, layout, nodeState } from "./graph";
import { Link, Route } from "./router";
import { SSEFrame, readSSE } from "./sse";
import { Timeline } from "./Timeline";
import type { EventRecord } from "./derive";
import { eventFamily, fmtClock, fmtDateTime, shortHash } from "./fmt";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const REFETCH_DEBOUNCE_MS = 150;
const RECONNECT_MS = 1000;
const MAX_CONSECUTIVE_ERRORS = 5;
const TICKER_MAX = 40;

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

export function RunGraph({ runId, navigate }: { runId: string; navigate: (r: Route) => void }) {
  const [run, setRun] = useState<RunState | null>(null);
  const [def, setDef] = useState<WorkflowDef | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticker, setTicker] = useState<TickerItem[]>([]);
  const [live, setLive] = useState<"connecting" | "live" | "closed" | "error">("connecting");
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [at, setAt] = useState<number | null>(null);            // time travel: null = live
  const [atRun, setAtRun] = useState<RunState | null>(null);    // the SERVER's fold through `at`
  const [atError, setAtError] = useState<string | null>(null);
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
        // per frame instead of the whole log.
        let held = eventsRef.current;
        let after = held.length ? held[held.length - 1].seq : 0;
        for (;;) {
          const page = await api.events<{ data: EventRecord[]; last_seq: number; has_more: boolean }>(runId, after);
          if (page.data.length) held = held.concat(page.data.filter((e) => e.seq > after));
          after = page.last_seq;
          if (!page.has_more) break;
        }
        eventsRef.current = held;
        setEvents(held);
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
          setError(e instanceof Error ? e.message : String(e));
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

  if (error && !run) return <p role="alert" className="error">Could not load run: {error}</p>;
  if (!run) return <p>Loading…</p>;

  const shown = at === null ? run : atRun;      // live fold, or the SERVER's fold through `at`

  const pending = Object.values(run.approvals).filter((a) => a.status === "pending").length;
  const versionMismatch = def !== null && def.version !== run.workflow_version;

  return (
    <section aria-labelledby="run-heading" className="run">
      <header className="run__head">
        <h2 id="run-heading">
          <Link to={{ page: "runs" }} navigate={navigate}>Runs</Link> / {run.workflow}{" "}
          <code>{run.id.slice(0, 8)}</code>
        </h2>
        <p className="run__meta">
          <span className={`status status--${run.status}`}>{run.status}</span>
          <span className={`live live--${live}`} role="status" aria-label="stream state">{live === "live" ? "live" : live}</span>
          <span className="chip" title="workflow version this run is pinned to">v{run.workflow_version}</span>
          <span className="chip num" title="events in the log">{run.last_seq} events</span>
          <span className="chip num" title="run cost from the fold">cost {run.total_cost}</span>
          {run.sealed_through != null && <span className="chip num" title="last seq covered by an integrity seal">sealed ≤ {run.sealed_through}</span>}
          {run.policy_sha256 && <span className="chip mono" title={`operator policy sha256 ${run.policy_sha256}`}>policy {shortHash(run.policy_sha256)}</span>}
          <span className="muted" title={run.started_at}>started {fmtDateTime(run.started_at)}</span>
        </p>
        {pending > 0 && (
          <p role="note" className="hint">
            {pending} approval{pending > 1 ? "s" : ""} waiting —{" "}
            <Link to={{ page: "inbox" }} navigate={navigate}>decide in the inbox</Link>.
          </p>
        )}
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
      {def && shown && <Graph def={def} run={shown} />}
      {def && shown && <CostPanel def={def} run={shown} events={at !== null ? events.filter((e) => e.seq <= at) : events} />}
      {def && !shown && !atError && <p role="status" aria-label="seek state">Loading the state at seq {at}…</p>}
      <Timeline events={events} lastSeq={run.last_seq} at={at} onSeek={setAt} />

      <h3>Live frames</h3>
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
  );
}

const LABEL: Record<NodeView["status"], string> = {
  pending: "pending", running: "running", completed: "completed", failed: "failed",
  dead_lettered: "dead-lettered", cancelled: "cancelled", awaiting_approval: "awaiting approval",
  retry_backoff: "retry backoff",
};

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

export function Graph({ def, run }: { def: WorkflowDef; run: RunState }) {
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
        return (
          <g key={n.id} className={`node node--${v.status}`} transform={`translate(${p.x} ${p.y})`}
             role="group" aria-label={`${n.id}: ${LABEL[v.status]}`}>
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
