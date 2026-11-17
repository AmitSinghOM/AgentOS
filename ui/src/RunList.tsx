import { useEffect, useRef, useState } from "react";
import { ApiError, RunSummary, api } from "./api";
import { fmtAgo } from "./fmt";
import { Link, Route } from "./router";
import { TUTORIAL_URL } from "./links";

const PAGE = 100;
/** The API clamps one request to 500 rows; the 5 s refresh re-reads every loaded row in one. */
const MAX_REFRESH = 500;
/** Display order for the status chips: the ones needing attention first, terminal last. */
const STATUS_ORDER = ["running", "pending", "suspended", "paused", "failed", "cancelled", "completed"];

export interface RunFilter { status: string | null; workflow: string | null; q: string }

/** The filter lives in the URL (`?status=&workflow=&q=`) so a narrowed list survives reload and
 *  can be shared — the same choice Inngest and Temporal make. Read once on mount, written on change. */
export function readFilter(search: string): RunFilter {
  const p = new URLSearchParams(search);
  return { status: p.get("status") || null, workflow: p.get("workflow") || null, q: p.get("q") ?? "" };
}
export function writeFilter(f: RunFilter): string {
  const p = new URLSearchParams();
  if (f.status) p.set("status", f.status);
  if (f.workflow) p.set("workflow", f.workflow);
  if (f.q) p.set("q", f.q);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Status and workflow are filtered by the API on its run index (E-07); repeating them here is a
 *  no-op on its answer and keeps rows of the previous filter hidden while the next page loads.
 *  Text is client-side only, over the loaded rows, and the caption says so: run id prefix or
 *  workflow name, case-insensitively. */
export function applyFilter(runs: RunSummary[], f: RunFilter): RunSummary[] {
  const q = f.q.trim().toLowerCase();
  return runs.filter((r) =>
    (!f.status || r.status === f.status) && (!f.workflow || r.workflow === f.workflow) &&
    (!q || r.id.toLowerCase().startsWith(q) || r.workflow.toLowerCase().includes(q)));
}

interface Loaded { key: string; runs: RunSummary[]; next: string | null; counts: Record<string, number> }

export function RunList({ navigate }: { navigate: (r: Route) => void }) {
  const [page, setPage] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const [filter, setFilter] = useState<RunFilter>(() => readFilter(window.location.search));
  const key = `${filter.status ?? ""}|${filter.workflow ?? ""}`;
  const loaded = useRef(0);                 // rows on screen for this key, read by the refresh

  useEffect(() => {
    let alive = true;
    loaded.current = 0;
    const load = async () => {
      const n = Math.max(PAGE, loaded.current);
      if (n > MAX_REFRESH) return;          // shortcut: past 500 loaded rows the list stops refreshing until reload
      try {
        const res = await api.runs({ limit: n, status: filter.status, workflow: filter.workflow });
        if (!alive) return;
        loaded.current = res.data.length;
        setPage({ key, runs: res.data, next: res.next_cursor ?? null, counts: res.counts ?? {} });
        setError(null);
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
      }
    };
    void load();
    const t = window.setInterval(() => { void load(); }, 5000);
    return () => { alive = false; window.clearInterval(t); };
  }, [key, filter.status, filter.workflow]);

  const loadMore = async () => {
    if (!page || page.key !== key || !page.next) return;
    setMore(true);
    try {
      const res = await api.runs({ limit: PAGE, status: filter.status, workflow: filter.workflow, cursor: page.next });
      setPage((p) => {
        if (!p || p.key !== key) return p;
        const have = new Set(p.runs.map((r) => r.id));
        const runs = [...p.runs, ...res.data.filter((r) => !have.has(r.id))];
        loaded.current = runs.length;
        return { ...p, runs, next: res.next_cursor ?? null, counts: res.counts ?? p.counts };
      });
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
    } finally {
      setMore(false);
    }
  };

  // The URL mirrors the filter; replaceState so typing does not build a history trail.
  const update = (patch: Partial<RunFilter>) => {
    const next = { ...filter, ...patch };
    setFilter(next);
    window.history.replaceState(null, "", `${window.location.pathname}${writeFilter(next)}`);
  };

  const runs = page?.runs ?? null;
  const counts = page?.counts ?? {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const statuses = Object.keys(counts).filter((s) => counts[s] > 0);
  if (filter.status && !statuses.includes(filter.status)) statuses.push(filter.status);
  statuses.sort((a, b) => {
    const ia = STATUS_ORDER.indexOf(a), ib = STATUS_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
  const shown = runs ? applyFilter(runs, filter) : [];
  const texting = filter.q.trim() !== "";
  const narrowed = filter.status !== null || filter.workflow !== null;
  const canMore = page !== null && page.key === key && page.next !== null;

  return (
    <section aria-labelledby="runs-heading">
      <div className="page-head">
        <div>
          <h2 id="runs-heading">Runs{total > 0 ? <span className="count">{total}</span> : null}</h2>
          <p className="page-head__sub">Newest first. Open a run for its graph, cost and latency, and the event timeline with time travel.</p>
        </div>
      </div>
      {error && <p role="alert" className="error">Could not load runs: {error}</p>}
      {runs === null && !error && <p className="muted">Loading…</p>}
      {runs !== null && runs.length === 0 && !narrowed && (
        <section className="empty empty--calm" aria-labelledby="runs-empty-heading">
          <p id="runs-empty-heading"><strong>No runs yet.</strong></p>
          <p className="hint">Start one with <code>POST /workflows/{"{name}"}/runs</code> or
            {" "}<code>python scripts/quickstart_llm.py</code>; it appears here as soon as <code>run.started</code> is in the log.</p>
          <p className="hint">First time here? The{" "}
            <a href={TUTORIAL_URL} target="_blank" rel="noopener noreferrer">tutorial</a> starts a run that suspends on an
            approval, so both this list and the inbox have something to show.</p>
        </section>
      )}
      {runs !== null && (runs.length > 0 || narrowed) && (
        <>
          <div className="filters">
            <div className="chips" role="group" aria-label="filter by status">
              <button type="button" className={`chip chip--filter${filter.status === null ? " chip--on" : ""}`}
                      aria-pressed={filter.status === null} onClick={() => update({ status: null })}>
                all <span className="chip__n">{total}</span>
              </button>
              {statuses.map((s) => (
                <button key={s} type="button" className={`chip chip--filter status--${s}${filter.status === s ? " chip--on" : ""}`}
                        aria-pressed={filter.status === s} onClick={() => update({ status: filter.status === s ? null : s })}>
                  {s} <span className="chip__n">{counts[s] ?? 0}</span>
                </button>
              ))}
            </div>
            {filter.workflow && (
              <span className="chip chip--on">workflow {filter.workflow}{" "}
                <button type="button" className="linklike" aria-label="clear workflow filter"
                        onClick={() => update({ workflow: null })}>×</button>
              </span>
            )}
            <input type="search" className="filters__q" aria-label="filter runs" placeholder="run id prefix or workflow name"
                   value={filter.q} onChange={(e) => update({ q: e.target.value })} />
            <span className="muted filters__note">
              {texting ? <>{shown.length} of {runs.length} loaded · text matches loaded rows only</> : <>{runs.length} loaded</>}
            </span>
          </div>
          {shown.length === 0 && (
            <div className="empty empty--calm">
              <p><strong>No runs match.</strong></p>
              <p className="hint">
                {texting ? <>The text filter applies to the {runs.length} loaded run{runs.length === 1 ? "" : "s"}{canMore ? "; load more to search further" : ""}.{" "}</> : null}
                <button type="button" className="linklike" onClick={() => update({ status: null, workflow: null, q: "" })}>Clear filters</button>
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
                    <td>
                      <Link to={{ page: "run", id: r.id }} navigate={navigate} className="runs__wf">{r.workflow}</Link> <span className="chip">v{r.workflow_version}</span>
                      {filter.workflow !== r.workflow && (
                        <> <button type="button" className="linklike" aria-label={`only ${r.workflow} runs`}
                                   onClick={() => update({ workflow: r.workflow })}>only</button></>
                      )}
                    </td>
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
          {canMore && (
            <p><button type="button" className="linklike" disabled={more} onClick={() => { void loadMore(); }}>
              {more ? "Loading…" : "Load more"}</button></p>
          )}
        </>
      )}
    </section>
  );
}
