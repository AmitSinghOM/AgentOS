import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { CostPanel } from "./CostPanel";
import { NODE_H, NODE_W, NodeView, RunState, WorkflowDef, layout, nodeState } from "./graph";
import { Link, Route } from "./router";
import { SSEFrame, readSSE } from "./sse";
import { Timeline } from "./Timeline";
import type { EventRecord } from "./derive";

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

  const refetch = useCallback(async () => {
    try {
      const r = await api.run<RunState>(runId);
      setRun(r);
      setError(null);
      try {
        const page = await api.events<{ data: EventRecord[] }>(runId);
        setEvents(page.data);
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
    if (at === null) { setAtRun(null); setAtError(null); return; }
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

  const shown = at !== null && atRun ? atRun : run;      // what the graph and panel render

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
          {" · "}<span className={`live live--${live}`} role="status" aria-label="stream state">{live === "live" ? "live" : live}</span>
          {" · "}{run.last_seq} events · cost {run.total_cost}
          {run.sealed_through != null && <> · sealed through {run.sealed_through}</>}
          {run.policy_sha256 && <> · policy {run.policy_sha256.slice(0, 12)}</>}
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
      {def && <Graph def={def} run={shown} />}
      {def && <CostPanel def={def} run={shown} events={at !== null ? events.filter((e) => e.seq <= at) : events} />}
      <Timeline events={events} lastSeq={run.last_seq} at={at} onSeek={setAt} />

      <h3>Live frames</h3>
      <ol className="ticker" aria-label="event ticker" reversed>
        {ticker.map((t) => (
          <li key={t.seq}><code>{t.seq}</code> {t.type}{t.step ? ` (${t.step})` : ""} <span className="muted">{t.at}</span></li>
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

export function Graph({ def, run }: { def: WorkflowDef; run: RunState }) {
  const l = layout(def);
  const pos = new Map(l.nodes.map((n) => [n.id, n]));
  const pad = 8;
  return (
    <svg className="graph" role="img" aria-label={`workflow ${def.name} run graph`}
         viewBox={`${-pad} ${-pad} ${l.width + 2 * pad} ${l.height + 2 * pad}`}
         width={Math.min(l.width + 2 * pad, 960)} style={{ maxWidth: "100%" }}>
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
        </marker>
      </defs>
      {l.edges.map((e) => {
        const a = pos.get(e.from)!; const b = pos.get(e.to)!;
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
        const mx = (x1 + x2) / 2;
        return <path key={`${e.from}->${e.to}`} className="edge" d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`} markerEnd="url(#arrow)" />;
      })}
      {def.nodes.map((n) => {
        const p = pos.get(n.id)!;
        const v = nodeState(run, n);
        return (
          <g key={n.id} className={`node node--${v.status}`} transform={`translate(${p.x} ${p.y})`}
             role="group" aria-label={`${n.id}: ${LABEL[v.status]}`}>
            <rect width={NODE_W} height={NODE_H} rx={8} />
            <text x={10} y={20} className="node__id">{n.id}</text>
            <text x={10} y={36} className="node__agent">{n.agent}{v.attempt > 1 ? ` · attempt ${v.attempt}` : ""}</text>
            <text x={10} y={49} className="node__state">
              {LABEL[v.status]}{v.cost && v.cost !== "0" ? ` · ${v.cost}` : ""}
              {v.status === "running" && v.progress !== null ? ` · ${Math.round(v.progress * 100)}%` : ""}
            </text>
            {v.detail && <title>{v.detail}</title>}
          </g>
        );
      })}
    </svg>
  );
}
