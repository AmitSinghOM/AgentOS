import { useEffect, useState } from "react";
import { ApiError, RunSummary, api } from "./api";
import { fmtAgo } from "./fmt";
import { Link, Route } from "./router";

export function RunList({ navigate }: { navigate: (r: Route) => void }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await api.runs(100);
        if (alive) { setRuns(res.data); setError(null); }
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? `${e.status} ${e.detail}` : String(e));
      }
    };
    void load();
    const t = window.setInterval(() => { void load(); }, 5000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);

  return (
    <section aria-labelledby="runs-heading">
      <h2 id="runs-heading">Runs</h2>
      {error && <p role="alert" className="error">Could not load runs: {error}</p>}
      {runs === null && !error && <p>Loading…</p>}
      {runs !== null && runs.length === 0 && (
        <p className="empty">No runs yet. Start one with <code>POST /workflows/{"{name}"}/runs</code> or
          {" "}<code>python scripts/quickstart_llm.py</code>; it will appear here as soon as <code>run.started</code> is in the log.</p>
      )}
      {runs && runs.length > 0 && (
        <table className="runs">
          <thead>
            <tr><th>Run</th><th>Workflow</th><th>Status</th><th className="num">Cost</th><th className="num">Events</th><th className="num">Started</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td><Link to={{ page: "run", id: r.id }} navigate={navigate}><code className="mono" title={r.id}>{r.id.slice(0, 8)}</code></Link></td>
                <td>{r.workflow} <span className="chip">v{r.workflow_version}</span></td>
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
      )}
    </section>
  );
}
