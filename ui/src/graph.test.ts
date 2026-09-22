import { describe, expect, it } from "vitest";
import { Layout, NODE_H, NODE_W, Placed, RunState, layout, nodeState } from "./graph";

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

/** Number of pairs of edges between two consecutive layers whose row order flips. */
function crossings(l: Layout): number {
  const by = new Map(l.nodes.map((n) => [n.id, n]));
  const spans = l.edges.map((e) => ({ a: by.get(e.from)!, b: by.get(e.to)! })).filter(({ a, b }) => b.layer === a.layer + 1);
  let n = 0;
  for (let i = 0; i < spans.length; i++) for (let j = i + 1; j < spans.length; j++) {
    const p = spans[i], q = spans[j];
    if (p.a.layer !== q.a.layer) continue;
    if ((p.a.row - q.a.row) * (p.b.row - q.b.row) < 0) n++;
  }
  return n;
}

function overlaps(a: Placed, b: Placed): boolean {
  return a.x < b.x + NODE_W && b.x < a.x + NODE_W && a.y < b.y + NODE_H && b.y < a.y + NODE_H;
}

describe("layout", () => {
  it("layers by longest path, dependencies strictly left of dependants, siblings in one column", () => {
    const l = layout(DIAMOND);
    const by = Object.fromEntries(l.nodes.map((n) => [n.id, n]));
    expect([by.a.layer, by.b.layer, by.c.layer, by.d.layer]).toEqual([0, 1, 1, 2]);
    expect(new Set([by.b.row, by.c.row])).toEqual(new Set([0, 1]));
    expect(by.b.x).toBe(by.c.x);
    expect(by.d.x).toBeGreaterThan(by.b.x); expect(by.b.x).toBeGreaterThan(by.a.x);
    expect(l.edges.map(({ from, to }) => ({ from, to })))
      .toEqual([{ from: "a", to: "b" }, { from: "a", to: "c" }, { from: "b", to: "d" }, { from: "c", to: "d" }]);
    expect(l.width).toBeGreaterThanOrEqual(3 * NODE_W);
    expect(l.height).toBeGreaterThanOrEqual(2 * NODE_H);
    for (const n of l.nodes) {
      expect(n.x).toBeGreaterThanOrEqual(0); expect(n.y).toBeGreaterThanOrEqual(0);
      expect(n.x + NODE_W).toBeLessThanOrEqual(l.width); expect(n.y + NODE_H).toBeLessThanOrEqual(l.height);
    }
  });

  it("every edge starts at its source's right edge and ends at its target's left edge", () => {
    const l = layout(DIAMOND);
    const by = new Map(l.nodes.map((n) => [n.id, n]));
    for (const e of l.edges) {
      const a = by.get(e.from)!, b = by.get(e.to)!;
      expect(e.points.length).toBeGreaterThanOrEqual(2);
      const first = e.points[0], last = e.points[e.points.length - 1];
      expect(first.x).toBeCloseTo(a.x + NODE_W, 5);
      expect(first.y).toBeGreaterThanOrEqual(a.y); expect(first.y).toBeLessThanOrEqual(a.y + NODE_H);
      expect(last.x).toBeCloseTo(b.x, 5);
      expect(last.y).toBeGreaterThanOrEqual(b.y); expect(last.y).toBeLessThanOrEqual(b.y + NODE_H);
    }
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

  // The two cases the hand-rolled longest-path layout got wrong, and the reason to adopt dagre.
  it("orders rows to avoid edge crossings on a wide fan (hand layout crossed by definition order)", () => {
    // sources a,b,c defined in that order; x needs c, y needs a. Definition order put x above y,
    // so a->y and c->x crossed.
    const l = layout({ name: "fan", version: 1, nodes: [
      { id: "a", agent: "e", depends_on: [] }, { id: "b", agent: "e", depends_on: [] }, { id: "c", agent: "e", depends_on: [] },
      { id: "x", agent: "e", depends_on: ["c"] }, { id: "y", agent: "e", depends_on: ["a"] }, { id: "z", agent: "e", depends_on: ["b"] },
    ] });
    expect(crossings(l)).toBe(0);
    const ns = l.nodes;
    for (let i = 0; i < ns.length; i++) for (let j = i + 1; j < ns.length; j++) expect(overlaps(ns[i], ns[j])).toBe(false);
  });

  it("routes an edge that spans a layer around the nodes in between instead of through them", () => {
    // a -> d spans layer 1 where b and c sit; a straight cubic from a to d cut through that column.
    const l = layout({ name: "span", version: 1, nodes: [
      ...DIAMOND.nodes.slice(0, 3), { id: "d", agent: "writer", depends_on: ["b", "c", "a"] },
    ] });
    const by = new Map(l.nodes.map((n) => [n.id, n]));
    const long = l.edges.find((e) => e.from === "a" && e.to === "d")!;
    expect(long.points.length).toBeGreaterThan(2);
    const midX = by.get("b")!.x + NODE_W / 2;
    // every waypoint inside layer 1's x-band must lie outside the b and c node boxes
    for (const p of long.points) {
      if (p.x < by.get("b")!.x || p.x > by.get("b")!.x + NODE_W) continue;
      for (const id of ["b", "c"]) {
        const n = by.get(id)!;
        expect(p.y < n.y || p.y > n.y + NODE_H).toBe(true);
      }
    }
    expect(midX).toBeGreaterThan(by.get("a")!.x);
  });

  it("is deterministic: the same definition lays out identically twice", () => {
    const def = { name: "fan", version: 1, nodes: [
      { id: "a", agent: "e", depends_on: [] }, { id: "b", agent: "e", depends_on: [] }, { id: "c", agent: "e", depends_on: [] },
      { id: "x", agent: "e", depends_on: ["c", "a"] }, { id: "y", agent: "e", depends_on: ["a", "b"] },
    ] };
    expect(layout(def)).toEqual(layout(def));
  });

  it("puts every node in the column of the wave the scheduler runs it in (a source stays in column 0)", () => {
    // dagre's default ranking shortens edges, which would park source `a` beside `c` (rank 2) just
    // because its only edge is long — reading as "a starts after b". The worker runs `a` in wave 0.
    const l = layout({ name: "waves", version: 1, nodes: [
      { id: "b", agent: "e", depends_on: [] }, { id: "c", agent: "e", depends_on: ["b"] },
      { id: "a", agent: "e", depends_on: [] }, { id: "d", agent: "e", depends_on: ["c", "a"] },
      { id: "e", agent: "e", depends_on: ["a"] },
    ] });
    const by = Object.fromEntries(l.nodes.map((n) => [n.id, n]));
    expect({ a: by.a.layer, b: by.b.layer, c: by.c.layer, d: by.d.layer, e: by.e.layer }).toEqual({ a: 0, b: 0, c: 1, d: 2, e: 1 });
    expect(by.a.x).toBe(by.b.x);
    expect(by.e.x).toBe(by.c.x);
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
    const pend = run({ approvals: { ap: { approval_id: "ap", step_id: "b", status: "pending", kind: "effect", effect_classes: ["spend"], requested_at: "2026-09-20T00:00:04Z", expires_at: null } } });
    expect(nodeState(pend, node)).toMatchObject({ status: "awaiting_approval", detail: "spend" });
    const granted = run({ approvals: { ap: { approval_id: "ap", step_id: "b", status: "granted", kind: "effect", effect_classes: ["spend"], requested_at: "2026-09-20T00:00:04Z", expires_at: null } } });
    expect(nodeState(granted, node).status).toBe("pending");
  });
  it("retry backoff when a retry is scheduled", () => {
    expect(nodeState(run({ attempts: { b: 1 }, pending_retries: { b: "2026-09-20T00:05:00Z" } }), node))
      .toMatchObject({ status: "retry_backoff", detail: "retry not before 2026-09-20T00:05:00Z" });
  });
});
