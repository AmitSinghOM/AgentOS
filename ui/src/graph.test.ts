import { describe, expect, it } from "vitest";
import { NODE_H, NODE_W, RunState, layout, nodeState } from "./graph";

const DIAMOND = { name: "diamond", version: 1, nodes: [
  { id: "a", agent: "writer", depends_on: [] },
  { id: "b", agent: "greeter", depends_on: ["a"] },
  { id: "c", agent: "greeter", depends_on: ["a"] },
  { id: "d", agent: "writer", depends_on: ["b", "c"] },
] };

function run(over: Partial<RunState> = {}): RunState {
  return {
    id: "r1", workflow: "diamond", workflow_version: 1, status: "running", steps: [], attempts: {},
    progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {}, cancelled_steps: [],
    approvals: {}, total_cost: "0", last_seq: 1, started_at: "2026-09-20T00:00:00Z", ...over,
  };
}

describe("layout", () => {
  it("layers by longest path and keeps definition order within a layer", () => {
    const l = layout(DIAMOND);
    const by = Object.fromEntries(l.nodes.map((n) => [n.id, n]));
    expect([by.a.layer, by.b.layer, by.c.layer, by.d.layer]).toEqual([0, 1, 1, 2]);
    expect([by.b.row, by.c.row]).toEqual([0, 1]);
    expect(by.d.x).toBeGreaterThan(by.b.x); expect(by.b.x).toBeGreaterThan(by.a.x);
    expect(l.edges).toEqual([{ from: "a", to: "b" }, { from: "a", to: "c" }, { from: "b", to: "d" }, { from: "c", to: "d" }]);
    expect(l.width).toBe(3 * NODE_W + 2 * 70);
    expect(l.height).toBe(2 * NODE_H + 22);
  });

  it("puts a node with an unknown dependency at layer 0 instead of throwing", () => {
    const l = layout({ name: "w", version: 1, nodes: [{ id: "x", agent: "e", depends_on: ["ghost"] }] });
    expect(l.nodes[0].layer).toBe(0);
    expect(l.edges).toEqual([]);
  });

  it("handles a long chain (deep DAG) linearly", () => {
    const nodes = Array.from({ length: 12 }, (_, i) => ({ id: `n${i}`, agent: "e", depends_on: i ? [`n${i - 1}`] : [] }));
    const l = layout({ name: "chain", version: 1, nodes });
    expect(l.nodes.map((n) => n.layer)).toEqual(nodes.map((_, i) => i));
    expect(l.height).toBe(NODE_H);
  });
});

describe("nodeState precedence — one folded-run field per state", () => {
  const node = DIAMOND.nodes[1];
  it("pending when nothing has happened", () => {
    expect(nodeState(run(), node).status).toBe("pending");
  });
  it("running once an attempt exists and nothing has finished", () => {
    const v = nodeState(run({ attempts: { b: 1 }, progress: { b: 0.4 } }), node);
    expect(v.status).toBe("running"); expect(v.progress).toBe(0.4); expect(v.attempt).toBe(1);
  });
  it("completed wins over everything and carries the cost", () => {
    const v = nodeState(run({ attempts: { b: 2 }, dead_lettered: { b: "old" },
      steps: [{ node_id: "b", attempt: 2, cost: { amount: "0.10", currency: "USD" } }] }), node);
    expect(v.status).toBe("completed"); expect(v.cost).toBe("0.10"); expect(v.attempt).toBe(2);
  });
  it("dead-lettered before failed before cancelled, each with its detail", () => {
    expect(nodeState(run({ dead_lettered: { b: "undeclared spend" }, failed_steps: { b: "x" } }), node))
      .toMatchObject({ status: "dead_lettered", detail: "undeclared spend" });
    expect(nodeState(run({ failed_steps: { b: "boom" }, cancelled_steps: ["b"] }), node))
      .toMatchObject({ status: "failed", detail: "boom" });
    expect(nodeState(run({ cancelled_steps: ["b"] }), node).status).toBe("cancelled");
  });
  it("awaiting approval when a PENDING approval names the step; a granted one does not", () => {
    const pend = run({ approvals: { ap: { approval_id: "ap", step_id: "b", status: "pending", kind: "effect", effect_classes: ["spend"] } } });
    expect(nodeState(pend, node)).toMatchObject({ status: "awaiting_approval", detail: "spend" });
    const granted = run({ approvals: { ap: { approval_id: "ap", step_id: "b", status: "granted", kind: "effect", effect_classes: ["spend"] } } });
    expect(nodeState(granted, node).status).toBe("pending");
  });
  it("retry backoff when a retry is scheduled", () => {
    expect(nodeState(run({ attempts: { b: 1 }, pending_retries: { b: "2026-09-20T00:05:00Z" } }), node))
      .toMatchObject({ status: "retry_backoff", detail: "retry not before 2026-09-20T00:05:00Z" });
  });
});
