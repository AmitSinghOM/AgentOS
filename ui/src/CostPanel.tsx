import type { RunState, WorkflowDef } from "./graph";
import { EventRecord, deriveLatency, fmtDuration } from "./derive";
import { fmtClock } from "./fmt";

const STATUS_CLASS: Record<string, string> = {
  completed: "completed", "in flight": "running", "not started": "pending", "ended without completion": "failed",
};

/** Per-node cost and latency. Cost is the fold's (`steps[].cost`); start/finish come from the
 *  event timestamps because the fold does not carry a start time. Display only. */
export function CostPanel({ def, run, events }: { def: WorkflowDef; run: RunState; events: EventRecord[] }) {
  const rows = deriveLatency(events, run, def.nodes.map((n) => n.id));
  const first = events.find((e) => e.event_type === "step.started")?.occurred_at;
  const lastDone = [...events].reverse().find((e) => e.event_type === "step.completed")?.occurred_at;
  const wall = first && lastDone ? new Date(lastDone).getTime() - new Date(first).getTime() : null;
  return (
    <section aria-labelledby="cost-heading" className="cost">
      <h3 id="cost-heading">Cost and latency</h3>
      <p className="muted summary">
        <span className="stat">run cost <strong className="num">{run.total_cost}</strong>
          {run.cost_ceiling ? <span className="muted"> (ceiling raised to {run.cost_ceiling})</span> : null}</span>
        <span className="stat">wall <strong className="num">{fmtDuration(wall)}</strong>
          <span className="muted"> first step start → last completion</span></span>
      </p>
      <table className="cost__table" aria-label="cost and latency per node">
        <thead><tr><th>node</th><th>status</th><th className="num">attempts</th><th className="num">started</th><th className="num">finished</th><th className="num">duration</th><th className="num">cost</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.node_id}>
              <td><strong>{r.node_id}</strong></td>
              <td><span className={`status status--${STATUS_CLASS[r.status] ?? "pending"}`}>{r.status}</span></td>
              <td className="num">{r.attempts}</td>
              <td className="muted num" title={r.started_at ?? undefined}>{fmtClock(r.started_at)}</td>
              <td className="muted num" title={r.finished_at ?? undefined}>{fmtClock(r.finished_at)}</td>
              <td className="num">{fmtDuration(r.duration_ms)}</td>
              <td className="num">{r.cost ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
