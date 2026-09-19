import { describe, expect, it } from "vitest";
import { EventRecord, deriveLatency, fmtDuration, salient } from "./derive";
import type { RunState } from "./graph";

const EVENTS: EventRecord[] = [
  { seq: 1, event_type: "run.started", occurred_at: "2026-09-20T00:00:00Z" },
  { seq: 2, event_type: "step.started", occurred_at: "2026-09-20T00:00:01Z", step_id: "a", attempt: 1 },
  { seq: 3, event_type: "step.failed", occurred_at: "2026-09-20T00:00:02Z", step_id: "a", error: "boom" },
  { seq: 4, event_type: "step.started", occurred_at: "2026-09-20T00:00:05Z", step_id: "a", attempt: 2 },
  { seq: 5, event_type: "step.completed", occurred_at: "2026-09-20T00:00:07.500Z", step_id: "a", cost: { amount: "0.10", currency: "USD" } },
  { seq: 6, event_type: "approval.requested", occurred_at: "2026-09-20T00:00:08Z", step_id: "pay", effect_classes: ["spend"] },
  { seq: 7, event_type: "approval.granted", occurred_at: "2026-09-20T00:01:00Z", step_id: "pay", principal: { kind: "human", id: "amit" }, reason: "ok" },
  { seq: 8, event_type: "step.started", occurred_at: "2026-09-20T00:01:01Z", step_id: "pay", attempt: 1 },
];
const RUN: RunState = {
  id: "r", workflow: "w", workflow_version: 1, status: "running",
  steps: [{ node_id: "a", attempt: 2, cost: { amount: "0.10", currency: "USD" } }],
  attempts: { a: 2, pay: 1 }, progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {},
  cancelled_steps: [], approvals: {}, total_cost: "0.10", last_seq: 8, started_at: "2026-09-20T00:00:00Z",
};

describe("deriveLatency", () => {
  it("measures from the FIRST start to completion and counts attempts; cost comes from the fold", () => {
    const [a, pay, d] = deriveLatency(EVENTS, RUN, ["a", "pay", "d"]);
    expect(a).toMatchObject({ node_id: "a", attempts: 2, started_at: "2026-09-20T00:00:01Z",
      finished_at: "2026-09-20T00:00:07.500Z", duration_ms: 6500, cost: "0.10", status: "completed" });
    expect(pay).toMatchObject({ attempts: 1, finished_at: null, duration_ms: null, cost: null, status: "in flight" });
    expect(d).toMatchObject({ attempts: 0, started_at: null, status: "not started" });
  });
  it("names a step that ended without completing", () => {
    const run = { ...RUN, dead_lettered: { pay: "undeclared" } };
    expect(deriveLatency(EVENTS, run, ["pay"])[0].status).toBe("ended without completion");
  });
});

describe("salient", () => {
  it("shows the one field an operator asks about first, per type", () => {
    expect(salient(EVENTS[2])).toBe("boom");
    expect(salient(EVENTS[4])).toBe("cost 0.10");
    expect(salient(EVENTS[5])).toBe("spend");
    expect(salient(EVENTS[6])).toBe("by amit (human) — ok");
    expect(salient({ seq: 9, event_type: "integrity.sealed", occurred_at: "t", sealed_seq: 8, key_id: "k1" })).toBe("seals seq 8 (key k1)");
    expect(salient({ seq: 9, event_type: "governance.policy_applied", occurred_at: "t", policy_sha256: "abcdef1234567890", narrowed: ["x"] })).toBe("policy abcdef123456, 1 narrowing(s)");
    expect(salient({ seq: 9, event_type: "run.completed", occurred_at: "t" })).toBe("");
  });
});

describe("fmtDuration", () => {
  it("picks a unit", () => {
    expect(fmtDuration(null)).toBe("—");
    expect(fmtDuration(250)).toBe("250 ms");
    expect(fmtDuration(6500)).toBe("6.50 s");
    expect(fmtDuration(125_000)).toBe("2 min 5 s");
  });
});
