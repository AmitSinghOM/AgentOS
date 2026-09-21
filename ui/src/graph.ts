// Pure functions behind the run graph: a layered DAG layout and the per-node state derived
// from the SERVER's folded run. The browser never folds events itself (docs/REPLAY.md: one
// derivation of state, with a golden corpus); the stream only tells the page when to refetch.

import * as dagre from "@dagrejs/dagre";
import type { Approval } from "./api";

export interface WorkflowNodeDef { id: string; agent: string; depends_on: string[] }
/** The budget the core enforces (models.Budget); the UI only reads it to explain a gate. */
export interface BudgetDef {
  allowed_effect_classes?: string[]; approval_required_for?: string[];
  allow_agent_approval?: boolean; approval_timeout_seconds?: number | null;
}
export interface WorkflowDef { name: string; version: number; nodes: WorkflowNodeDef[]; max_parallelism?: number; budget?: BudgetDef }

export interface StepRecord { node_id: string; attempt: number; cost: { amount: string; currency: string }; finished_at?: string;
  /** Hydrated output for callers (GET /runs/{id}); what a downstream step receives as its input. */
  output?: Record<string, unknown> }
/** One approval as the fold carries it — same shape as the inbox's `Approval` minus run_id/workflow. */
export interface RunApproval extends Omit<Approval, "run_id" | "workflow"> {}
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
  /** Run inputs, hydrated for callers. */
  inputs?: Record<string, unknown>;
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

/** The one vocabulary for a node's state, used by the graph, its legend and the cost table, so a
 *  node is never "awaiting approval" in one place and "not started" in another. */
export const NODE_LABEL: Record<NodeStatus, string> = {
  pending: "pending", running: "running", completed: "completed", failed: "failed",
  dead_lettered: "dead-lettered", cancelled: "cancelled", awaiting_approval: "awaiting approval",
  retry_backoff: "retry backoff",
};

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
export interface Point { x: number; y: number }
export interface LaidEdge { from: string; to: string; points: Point[] }
export interface Layout { nodes: Placed[]; edges: LaidEdge[]; width: number; height: number }

export const NODE_W = 150;
export const NODE_H = 54;
const GAP_X = 70;   // between layers (dagre ranksep)
const GAP_Y = 22;   // between rows in a layer (dagre nodesep)

/** Layered left-to-right layout via dagre (Sugiyama: crossing minimisation, Brandes-Köpf
 *  coordinates, long edges routed around intermediate layers). Ranking is pinned to the
 *  scheduler's waves — a node's column is the longest path from a source, exactly the wave the
 *  worker dispatches it in — by giving each edge `minlen` = wave gap; dagre's default ranker
 *  would otherwise shorten edges and park a source with one long edge beside later nodes, which
 *  reads as "starts later". Node insertion follows definition order, so the result is
 *  deterministic for a definition. `x`/`y` are the node's top-left corner; `layer` is the wave
 *  (0 = sources) and `row` the position within it. Cycles cannot occur (the API validates the
 *  DAG); an unknown dependency is dropped rather than thrown, as before. */
export function layout(def: WorkflowDef): Layout {
  const byId = new Map(def.nodes.map((n) => [n.id, n]));
  const edgeList = def.nodes.flatMap((n) => n.depends_on.filter((d) => byId.has(d) && d !== n.id).map((d) => ({ from: d, to: n.id })));
  const wave = waves(def.nodes, byId);
  const g = new dagre.graphlib.Graph({ directed: true, multigraph: false, compound: false })
    .setGraph({ rankdir: "LR", ranksep: GAP_X, nodesep: GAP_Y, edgesep: GAP_Y / 2, marginx: 0, marginy: 0 })
    .setDefaultEdgeLabel(() => ({}));
  for (const n of def.nodes) g.setNode(n.id, { width: NODE_W, height: NODE_H });
  for (const e of edgeList) g.setEdge(e.from, e.to, { minlen: Math.max(1, wave.get(e.to)! - wave.get(e.from)!) });
  dagre.layout(g);

  // dagre leaves gaps in `rank` for the dummy ranks it inserts on long edges; compress to 0..n.
  const ranks = Array.from(new Set(def.nodes.map((n) => g.node(n.id).rank as number))).sort((a, b) => a - b);
  const layerOf = new Map(ranks.map((r, i) => [r, i]));
  const nodes: Placed[] = def.nodes.map((n) => {
    const p = g.node(n.id);
    return { id: n.id, layer: layerOf.get(p.rank as number)!, row: p.order as number,
      x: round(p.x - NODE_W / 2), y: round(p.y - NODE_H / 2) };
  });
  const edges: LaidEdge[] = edgeList.map((e) => ({
    ...e, points: g.edge(e.from, e.to).points.map((p: Point) => ({ x: round(p.x), y: round(p.y) })),
  }));
  const gr = g.graph();
  return { nodes, edges, width: round(gr.width ?? NODE_W), height: round(gr.height ?? NODE_H) };
}

/** Longest path from a source, per node: the wave the scheduler runs it in. Unknown or
 *  self dependencies are ignored; a cycle (impossible past the API) resolves to 0. */
function waves(nodes: WorkflowNodeDef[], byId: Map<string, WorkflowNodeDef>): Map<string, number> {
  const memo = new Map<string, number>();
  const visit = (id: string, seen: Set<string>): number => {
    const hit = memo.get(id);
    if (hit !== undefined) return hit;
    const node = byId.get(id);
    if (!node || seen.has(id)) return 0;
    seen.add(id);
    const deps = node.depends_on.filter((d) => byId.has(d) && d !== id);
    const w = deps.length ? 1 + Math.max(...deps.map((d) => visit(d, seen))) : 0;
    memo.set(id, w);
    return w;
  };
  for (const n of nodes) visit(n.id, new Set());
  return memo;
}

const round = (v: number) => Math.round(v * 100) / 100;
