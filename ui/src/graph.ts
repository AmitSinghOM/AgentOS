// Pure functions behind the run graph: a layered DAG layout and the per-node state derived
// from the SERVER's folded run. The browser never folds events itself (docs/REPLAY.md: one
// derivation of state, with a golden corpus); the stream only tells the page when to refetch.

export interface WorkflowNodeDef { id: string; agent: string; depends_on: string[] }
export interface WorkflowDef { name: string; version: number; nodes: WorkflowNodeDef[]; max_parallelism?: number }

export interface StepRecord { node_id: string; attempt: number; cost: { amount: string; currency: string }; finished_at?: string }
export interface RunApproval { approval_id: string; step_id: string | null; status: string; kind: string; effect_classes: string[] }
export interface RunState {
  id: string;
  workflow: string;
  workflow_version: number;
  status: string;
  steps: StepRecord[];
  attempts: Record<string, number>;
  progress: Record<string, number>;
  dead_lettered: Record<string, string>;
  failed_steps: Record<string, string>;
  pending_retries: Record<string, string>;
  cancelled_steps: string[];
  approvals: Record<string, RunApproval>;
  total_cost: string;
  cost_ceiling?: string | null;
  last_seq: number;
  started_at: string;
  policy_sha256?: string | null;
  sealed_through?: number | null;
  error?: string | null;
}

export type NodeStatus =
  | "pending" | "running" | "completed" | "failed" | "dead_lettered"
  | "cancelled" | "awaiting_approval" | "retry_backoff";

export interface NodeView {
  id: string;
  agent: string;
  status: NodeStatus;
  attempt: number;
  progress: number | null;
  cost: string | null;
  detail: string | null;
}

/** One field of the folded run decides each state; the order below is the precedence. */
export function nodeState(run: RunState, node: WorkflowNodeDef): NodeView {
  const completed = run.steps.find((s) => s.node_id === node.id);
  const attempt = run.attempts[node.id] ?? completed?.attempt ?? 0;
  const base = { id: node.id, agent: node.agent, attempt, progress: run.progress[node.id] ?? null };
  if (completed) {
    return { ...base, status: "completed", cost: completed.cost.amount, detail: null };
  }
  if (node.id in run.dead_lettered) {
    return { ...base, status: "dead_lettered", cost: null, detail: run.dead_lettered[node.id] };
  }
  if (node.id in run.failed_steps) {
    return { ...base, status: "failed", cost: null, detail: run.failed_steps[node.id] };
  }
  if (run.cancelled_steps.includes(node.id)) {
    return { ...base, status: "cancelled", cost: null, detail: null };
  }
  const pendingApproval = Object.values(run.approvals).find(
    (a) => a.step_id === node.id && a.status === "pending");
  if (pendingApproval) {
    return { ...base, status: "awaiting_approval", cost: null,
      detail: pendingApproval.kind === "cost" ? "cost ceiling" : pendingApproval.effect_classes.join(", ") };
  }
  if (node.id in run.pending_retries) {
    return { ...base, status: "retry_backoff", cost: null, detail: `retry not before ${run.pending_retries[node.id]}` };
  }
  if (attempt > 0) {
    return { ...base, status: "running", cost: null, detail: null };
  }
  return { ...base, status: "pending", cost: null, detail: null };
}

export interface Placed { id: string; layer: number; row: number; x: number; y: number }
export interface Layout { nodes: Placed[]; edges: { from: string; to: string }[]; width: number; height: number }

export const NODE_W = 150;
export const NODE_H = 54;
const GAP_X = 70;
const GAP_Y = 22;

/** Longest-path layering: a node's layer is 1 + the max layer of its dependencies; sources
 *  are layer 0. Rows within a layer keep definition order. Cycles cannot occur (the API
 *  validates the DAG) but an unknown dependency is placed at layer 0 rather than thrown. */
export function layout(def: WorkflowDef): Layout {
  const byId = new Map(def.nodes.map((n) => [n.id, n]));
  const memo = new Map<string, number>();
  const layerOf = (id: string, seen: Set<string>): number => {
    const hit = memo.get(id);
    if (hit !== undefined) return hit;
    const node = byId.get(id);
    if (!node || seen.has(id)) return 0;
    seen.add(id);
    const deps = node.depends_on.filter((d) => byId.has(d));
    const l = deps.length ? 1 + Math.max(...deps.map((d) => layerOf(d, seen))) : 0;
    memo.set(id, l);
    return l;
  };
  const rows = new Map<number, number>();
  const nodes: Placed[] = def.nodes.map((n) => {
    const layer = layerOf(n.id, new Set());
    const row = rows.get(layer) ?? 0;
    rows.set(layer, row + 1);
    return { id: n.id, layer, row, x: layer * (NODE_W + GAP_X), y: row * (NODE_H + GAP_Y) };
  });
  const edges = def.nodes.flatMap((n) => n.depends_on.filter((d) => byId.has(d)).map((d) => ({ from: d, to: n.id })));
  const layers = Math.max(0, ...nodes.map((n) => n.layer)) + 1;
  const tallest = Math.max(1, ...rows.values());
  return { nodes, edges, width: layers * NODE_W + (layers - 1) * GAP_X, height: tallest * NODE_H + (tallest - 1) * GAP_Y };
}
