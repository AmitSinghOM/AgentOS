import { NODE_LABEL, RunState, WorkflowDef, nodeState } from "./graph";
import { EventRecord, deriveLatency, fmtDuration } from "./derive";
import { fmtClock } from "./fmt";

/** Per-node cost and latency. Status and cost are the fold's (`nodeState`, the same words and
 *  colours as the graph above); start/finish come from the event timestamps because the fold
 *  does not carry a start time. Display only. */
export function CostPanel({ def, run, events, selected = null, onSelect }: {
  def: WorkflowDef; run: RunState; events: EventRecord[]; selected?: string | null; onSelect?: (id: string) => void;
}) {
  const rows = deriveLatency(events, run, def.nodes.map((n) => n.id));
  const views = new Map(def.nodes.map((n) => [n.id, nodeState(run, n)]));
  const first = events.find((e) => e.event_type === "step.started")?.occurred_at;
  const lastDone = [...events].reverse().find((e) => e.event_type === "step.completed")?.occurred_at;
  const wall = first && lastDone ? new Date(lastDone).getTime() - new Date(first).getTime() : null;
  return (
    <section aria-labelledby="cost-heading" className="cost panel">
      <div className="panel__head">
        <h3 id="cost-heading">Cost and latency</h3>
        <p className="panel__sub">Cost and status are the fold's; start and finish come from the step events (a display derivation, not state).</p>
      </div>
      <p className="muted summary">
        <span className="stat">run cost <strong className="num">{run.total_cost}</strong>
          {run.cost_ceiling ? <span className="muted"> (ceiling raised to {run.cost_ceiling})</span> : null}</span>
        <span className="stat">wall <strong className="num">{fmtDuration(wall)}</strong>
          <span className="muted"> first step start → last completion</span></span>
      </p>
      <table className="cost__table" aria-label="cost and latency per node">
        <thead><tr><th>node</th><th>status</th><th className="num">attempts</th><th className="num">started</th><th className="num">finished</th><th className="num">duration</th><th className="num">cost</th></tr></thead>
        <tbody>
          {rows.map((r) => {
            const v = views.get(r.node_id)!;
            return (
              <tr key={r.node_id} className={selected === r.node_id ? "selected" : undefined} aria-current={selected === r.node_id ? "true" : undefined}>
                <td>
                  {onSelect
                    ? <button type="button" className="linklike" aria-label={`select step ${r.node_id}`} aria-pressed={selected === r.node_id}
                              onClick={() => onSelect(r.node_id)}><strong>{r.node_id}</strong></button>
                    : <strong>{r.node_id}</strong>}
                </td>
                <td><span className={`status status--${v.status}`} title={v.detail ?? undefined}>{NODE_LABEL[v.status]}</span></td>
                <td className="num">{r.attempts}</td>
                <td className="muted num" title={r.started_at ?? undefined}>{fmtClock(r.started_at)}</td>
                <td className="muted num" title={r.finished_at ?? undefined}>{fmtClock(r.finished_at)}</td>
                <td className="num">{fmtDuration(r.duration_ms)}</td>
                <td className="num">{v.cost ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
