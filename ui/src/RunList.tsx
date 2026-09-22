import { useEffect, useMemo, useState } from "react";
import { ApiError, RunSummary, api } from "./api";
import { fmtAgo } from "./fmt";
import { Link, Route } from "./router";
import { TUTORIAL_URL } from "./links";

const PAGE = 100;
/** Display order for the status chips: the ones needing attention first, terminal last. */
const STATUS_ORDER = ["running", "pending", "suspended", "paused", "failed", "cancelled", "completed"];

export interface RunFilter { status: string | null; q: string }

/** The filter lives in the URL (`?status=&q=`) so a narrowed list survives reload and can be
 *  shared — the same choice Inngest and Temporal make. Read once on mount, written on change. */
export function readFilter(search: string): RunFilter {
  const p = new URLSearchParams(search);
  return { status: p.get("status") || null, q: p.get("q") ?? "" };
}
export function writeFilter(f: RunFilter): string {
  const p = new URLSearchParams();
  if (f.status) p.set("status", f.status);
  if (f.q) p.set("q", f.q);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Client-side over the page the API returned: `GET /runs` has no query parameter, and the
 *  caption says so rather than pretending to search the store. Text matches the run id prefix
 *  or the workflow name, case-insensitively. */
export function applyFilter(runs: RunSummary[], f: RunFilter): RunSummary[] {
  const q = f.q.trim().toLowerCase();
  return runs.filter((r) =>
    (!f.status || r.status === f.status) &&
    (!q || r.id.toLowerCase().startsWith(q) || r.workflow.toLowerCase().includes(q)));
}

export function RunList({ navigate }: { navigate: (r: Route) => void }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<RunFilter>(() => readFilter(window.location.search));

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await api.runs(PAGE);
        if (alive) { setRuns(res.data); setError(null); }
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
      }
    };
    void load();
    const t = window.setInterval(() => { void load(); }, 5000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);

  // The URL mirrors the filter; replaceState so typing does not build a history trail.
  const update = (patch: Partial<RunFilter>) => {
    const next = { ...filter, ...patch };
    setFilter(next);
    window.history.replaceState(null, "", `${window.location.pathname}${writeFilter(next)}`);
  };

  const counts = useMemo(() => {
    const c = new Map<string, number>();
    for (const r of runs ?? []) c.set(r.status, (c.get(r.status) ?? 0) + 1);
    return c;
  }, [runs]);
  const statuses = Array.from(counts.keys()).sort((a, b) => {
    const ia = STATUS_ORDER.indexOf(a), ib = STATUS_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
  const shown = runs ? applyFilter(runs, filter) : [];
  const filtering = filter.status !== null || filter.q.trim() !== "";
  const pageFull = runs !== null && runs.length >= PAGE;

  return (
    <section aria-labelledby="runs-heading">
      <div className="page-head">
        <div>
          <h2 id="runs-heading">Runs{runs && runs.length > 0 ? <span className="count">{runs.length}</span> : null}</h2>
          <p className="page-head__sub">Newest first. Open a run for its graph, cost and latency, and the event timeline with time travel.</p>
        </div>
      </div>
      {error && <p role="alert" className="error">Could not load runs: {error}</p>}
      {runs === null && !error && <p className="muted">Loading…</p>}
      {runs !== null && runs.length === 0 && (
        <section className="empty empty--calm" aria-labelledby="runs-empty-heading">
          <p id="runs-empty-heading"><strong>No runs yet.</strong></p>
          <p className="hint">Start one with <code>POST /workflows/{"{name}"}/runs</code> or
            {" "}<code>python scripts/quickstart_llm.py</code>; it appears here as soon as <code>run.started</code> is in the log.</p>
          <p className="hint">First time here? The{" "}
            <a href={TUTORIAL_URL} target="_blank" rel="noopener noreferrer">tutorial</a> starts a run that suspends on an
            approval, so both this list and the inbox have something to show.</p>
        </section>
      )}
      {runs && runs.length > 0 && (
        <>
          <div className="filters">
            <div className="chips" role="group" aria-label="filter by status">
              <button type="button" className={`chip chip--filter${filter.status === null ? " chip--on" : ""}`}
                      aria-pressed={filter.status === null} onClick={() => update({ status: null })}>
                all <span className="chip__n">{runs.length}</span>
              </button>
              {statuses.map((s) => (
                <button key={s} type="button" className={`chip chip--filter status--${s}${filter.status === s ? " chip--on" : ""}`}
                        aria-pressed={filter.status === s} onClick={() => update({ status: filter.status === s ? null : s })}>
                  {s} <span className="chip__n">{counts.get(s)}</span>
                </button>
              ))}
            </div>
            <input type="search" className="filters__q" aria-label="filter runs" placeholder="run id prefix or workflow name"
                   value={filter.q} onChange={(e) => update({ q: e.target.value })} />
            <span className="muted filters__note">
              {filtering ? <>{shown.length} of {runs.length} loaded</> : <>{runs.length} loaded</>}
              {pageFull && <> · the newest {PAGE} only; filters apply to these</>}
            </span>
          </div>
          {shown.length === 0 && (
            <div className="empty empty--calm">
              <p><strong>No runs match.</strong></p>
              <p className="hint">
                Filters apply to the {runs.length} loaded run{runs.length === 1 ? "" : "s"}; older runs are not searched.{" "}
                <button type="button" className="linklike" onClick={() => update({ status: null, q: "" })}>Clear filters</button>
              </p>
            </div>
          )}
          {shown.length > 0 && (
            <div className="card card--table">
            <table className="runs" aria-label="runs">
              <thead>
                <tr><th>Run</th><th>Workflow</th><th>Status</th><th className="num">Cost</th><th className="num">Events</th><th className="num">Started</th></tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.id}>
                    <td><Link to={{ page: "run", id: r.id }} navigate={navigate}><code className="mono" title={r.id}>{r.id.slice(0, 8)}</code></Link></td>
                    <td><Link to={{ page: "run", id: r.id }} navigate={navigate} className="runs__wf">{r.workflow}</Link> <span className="chip">v{r.workflow_version}</span></td>
                    <td>
                      <span className={`status status--${r.status}`}>{r.status}</span>
                      {r.pending_approvals > 0 && (
                        <> <Link to={{ page: "inbox" }} navigate={navigate} className="await">{r.pending_approvals} awaiting approval</Link></>
                      )}
                    </td>
                    <td className="num">{r.total_cost}</td>
                    <td className="num">{r.last_seq}</td>
                    <td className="num muted" title={r.started_at}><time dateTime={r.started_at}>{fmtAgo(r.started_at)}</time></td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
