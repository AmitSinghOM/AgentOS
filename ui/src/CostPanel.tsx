import type { RunState, WorkflowDef } from "./graph";
import { EventRecord, deriveLatency, fmtDuration } from "./derive";

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
      <p className="muted">
        run cost <strong>{run.total_cost}</strong>
        {run.cost_ceiling ? ` (ceiling raised to ${run.cost_ceiling})` : ""} · wall{" "}
        <strong>{fmtDuration(wall)}</strong> from first step start to last completion
      </p>
      <table className="cost__table" aria-label="cost and latency per node">
        <thead><tr><th>node</th><th>status</th><th>attempts</th><th>started</th><th>finished</th><th>duration</th><th>cost</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.node_id}>
              <td><strong>{r.node_id}</strong></td>
              <td>{r.status}</td>
              <td>{r.attempts}</td>
              <td className="muted">{r.started_at ?? "—"}</td>
              <td className="muted">{r.finished_at ?? "—"}</td>
              <td>{fmtDuration(r.duration_ms)}</td>
              <td>{r.cost ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
