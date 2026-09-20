// Display-only derivations over the raw event list. STATE still comes from the server's fold;
// these compute what the fold does not carry: when each step started (step.started.occurred_at)
// and therefore how long it took, plus one salient field per event type for the timeline.

import type { RunState } from "./graph";

export interface EventRecord {
  seq: number;
  event_type: string;
  occurred_at: string;
  step_id?: string | null;
  attempt?: number;
  principal?: { kind: string; id: string } | null;
  reason?: string;
  cause?: string;
  error?: string;
  effect_classes?: string[];
  cost?: { amount: string; currency: string };
  narrowed?: string[];
  policy_sha256?: string;
  sealed_seq?: number;
  key_id?: string;
  from_model?: string;
  to_model?: string;
  fraction?: number;
  [k: string]: unknown;
}

/** One line of detail per event type — the field an operator would ask about first. */
export function salient(e: EventRecord): string {
  switch (e.event_type) {
    case "step.started": return `attempt ${e.attempt ?? 1}`;
    case "step.completed": return e.cost && e.cost.amount !== "0" ? `cost ${e.cost.amount}` : "";
    case "step.progress": return e.fraction != null ? `${Math.round(e.fraction * 100)}%` : "";
    case "step.failed": return e.error ?? "";
    case "step.dead_lettered": return e.cause ?? "";
    case "run.failed": return e.error ?? "";
    case "approval.requested": return (e.effect_classes ?? []).join(", ") || (e.reason ?? "");
    case "approval.granted":
    case "approval.rejected":
    case "run.cancel_requested":
    case "run.pause_requested":
    case "run.resumed":
    case "step.retry_requested":
      return e.principal ? `by ${e.principal.id} (${e.principal.kind})${e.reason ? ` — ${e.reason}` : ""}` : (e.reason ?? "");
    case "governance.policy_applied": return `policy ${String(e.policy_sha256 ?? "").slice(0, 12)}${e.narrowed?.length ? `, ${e.narrowed.length} narrowing(s)` : ""}`;
    case "integrity.sealed": return `seals seq ${e.sealed_seq} (key ${e.key_id})`;
    case "executor.substituted": return `${e.from_model} → ${e.to_model}`;
    default: return "";
  }
}

export interface NodeLatency {
  node_id: string;
  attempts: number;
  started_at: string | null;   // first step.started
  finished_at: string | null;  // step.completed
  duration_ms: number | null;
  cost: string | null;         // from the fold (completed steps only)
  status: "completed" | "in flight" | "not started" | "ended without completion";
}

export function deriveLatency(events: EventRecord[], run: RunState, nodeIds: string[]): NodeLatency[] {
  return nodeIds.map((id) => {
    const starts = events.filter((e) => e.event_type === "step.started" && e.step_id === id);
    const done = events.find((e) => e.event_type === "step.completed" && e.step_id === id);
    const started_at = starts[0]?.occurred_at ?? null;
    const finished_at = done?.occurred_at ?? null;
    const completed = run.steps.find((s) => s.node_id === id);
    let duration_ms: number | null = null;
    if (started_at && finished_at) {
      const ms = new Date(finished_at).getTime() - new Date(started_at).getTime();
      duration_ms = Number.isNaN(ms) ? null : ms;
    }
    const ended = id in run.dead_lettered || id in run.failed_steps || run.cancelled_steps.includes(id);
    const status: NodeLatency["status"] = completed ? "completed" : ended ? "ended without completion"
      : starts.length ? "in flight" : "not started";
    return { node_id: id, attempts: starts.length, started_at, finished_at, duration_ms,
      cost: completed?.cost.amount ?? null, status };
  });
}

export function fmtDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

/** Merge a fetched page into the events already held. The log is append-only and seq is
 *  dense, so the only thing that can go wrong is a page we already have (two refetches in
 *  flight both read the same tail): keep every seq once, in seq order. Pure, so it is tested
 *  without the page. */
export function mergeEvents(held: EventRecord[], page: EventRecord[]): EventRecord[] {
  if (page.length === 0) return held;
  const last = held.length ? held[held.length - 1].seq : 0;
  const fresh = page.filter((e) => e.seq > last);
  if (fresh.length === page.length) return held.concat(fresh);
  // overlap: rebuild by seq, later fetch wins for an identical seq (same event either way)
  const bySeq = new Map<number, EventRecord>();
  for (const e of held) bySeq.set(e.seq, e);
  for (const e of page) bySeq.set(e.seq, e);
  return Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
}
