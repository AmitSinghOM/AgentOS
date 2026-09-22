/**
 * DEBATE-2.md accepted items (reviews/agentos-ui-comparison-2026-09-22/DEBATE-2.md), each UI-only:
 *   U10 the run header carries the integrity VERDICT from GET /runs/{id}/integrity (the UI showed
 *       only `sealed ≤ N`); fetched on load and when the run's status changes, never per frame
 *   H5  when the workflow AND the operator policy are both known, the non-human warning says
 *       plainly whether the API will refuse — the button stays live (the API decides)
 *   H6  an opt-in browser notification when a NEW gate arrives: workflow, step, effect class,
 *       click opens the run; it carries no decision and is off unless the operator turned it on
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Approval } from "./api";
import type { RunState, WorkflowDef } from "./graph";
import { resetPolicyCache } from "./permission";
import MATRIX from "../../tests/fixtures/agent_approval_matrix.json";

const DEF: WorkflowDef = { name: "payments", version: 1, nodes: [
  { id: "a", agent: "calc", depends_on: [] }, { id: "pay", agent: "payer", depends_on: ["a"] },
], budget: { allowed_effect_classes: ["compute"], approval_required_for: ["spend"], allow_agent_approval: false, approval_timeout_seconds: null } };

const PENDING: Approval = {
  run_id: "run1", workflow: "payments", approval_id: "ap1", step_id: "pay",
  kind: "effect", effect_classes: ["spend"], status: "pending",
  requested_at: "2026-09-20T00:00:00Z", expires_at: null, reason: "step 'pay' declares spend",
};

function base(over: Partial<RunState>): RunState {
  return { id: "run1", workflow: "payments", workflow_version: 1, status: "suspended", steps: [], attempts: {},
    progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {}, cancelled_steps: [], approvals: {},
    total_cost: "0.10", last_seq: 4, sealed_through: 4, started_at: "2026-09-20T00:00:00Z", ...over };
}
const SUSPENDED = base({ attempts: { a: 1 },
  steps: [{ node_id: "a", attempt: 1, cost: { amount: "0.10", currency: "USD" }, output: {} }],
  approvals: { ap1: { ...PENDING, decided_by: null, decision_reason: "", decided_at: null } } });
const EVENTS = [
  { seq: 1, event_type: "run.started", occurred_at: "2026-09-20T00:00:00Z" },
  { seq: 2, event_type: "step.started", occurred_at: "2026-09-20T00:00:01Z", step_id: "a", attempt: 1 },
  { seq: 3, event_type: "step.completed", occurred_at: "2026-09-20T00:00:03Z", step_id: "a", cost: { amount: "0.10", currency: "USD" } },
  { seq: 4, event_type: "approval.requested", occurred_at: "2026-09-20T00:00:04Z", step_id: "pay" },
];

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
type Seen = { url: string; method: string };
let seen: Seen[];
let state: RunState;
let me: unknown;
let pending: Approval[];
let integrity: unknown;
let policy: unknown;
let def: WorkflowDef;

beforeEach(() => {
  seen = [];
  resetPolicyCache();
  me = { mode: "bearer", principal: { kind: "human", id: "amit" } };
  state = SUSPENDED;
  pending = [PENDING];
  def = DEF;
  policy = { sha256: null, policy: null, note: "AGENTOS_POLICY unset: no operator ceiling" };
  integrity = { run_id: "run1", ok: true, events: 4, hashed: 4,
    seals: { state: "verified", seals: 1, valid: 1, sealed_through: 4, unsigned_tail: 0, problems: [], unknown_keys: [] } };
  window.sessionStorage.setItem("agentos.token", "tok");
  window.localStorage.clear();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ url, method });
    if (url === "/me") return json(200, me);
    if (url === "/approvals") return json(200, { data: pending });
    if (url === "/policy") return json(200, policy);
    if (url.startsWith("/runs?")) return json(200, { data: [] });
    if (url === "/runs/run1") return json(200, state);
    if (url === "/runs/run1/integrity") return json(200, integrity);
    if (url.startsWith("/runs/run1/events")) return json(200, { data: EVENTS, last_seq: 4, has_more: false });
    if (url === "/workflows/payments") return json(200, def);
    if (url === "/runs/run1/stream") return new Response(new ReadableStream({ start(c) { c.close(); } }), { status: 200 });
    if (method === "POST" && url.includes("/approvals/")) { pending = []; state = base({ status: "running" }); return json(202, state); }
    return json(404, { detail: `no route ${url}` });
  });
});
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

const gets = (u: string) => seen.filter((s) => s.method === "GET" && s.url === u);

describe("U10 integrity verdict on the run header", () => {
  it("shows the verdict from GET /runs/{id}/integrity, fetched once on load and not per refetch", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    const chip = await screen.findByRole("status", { name: /integrity/i });
    expect(chip).toHaveTextContent(/verified/);
    expect(chip).toHaveAttribute("title", expect.stringMatching(/4 of 4 events hashed/));
    expect(chip).toHaveAttribute("title", expect.stringMatching(/sealed through 4/));
    // Four-seat review A4: the verdict subsumes the older `sealed ≤ N` fact chip, so the header
    // states the seal reach once (in this title), not twice.
    expect(screen.queryByText(/sealed ≤/)).toBeNull();
    // the run page refetches the fold on control; the verdict is not refetched with it
    expect(gets("/runs/run1/integrity")).toHaveLength(1);
  });

  it("names a broken chain as such, with the API's error, and marks it bad", async () => {
    integrity = { run_id: "run1", ok: false, events: 4, error: "seq 3 hash mismatch",
      seals: { state: "unsigned", seals: 0, valid: 0, sealed_through: null, unsigned_tail: 4, problems: [], unknown_keys: [] } };
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    const chip = await screen.findByRole("status", { name: /integrity/i });
    expect(chip).toHaveTextContent(/chain broken/i);
    expect(chip).toHaveAttribute("title", expect.stringContaining("seq 3 hash mismatch"));
    expect(chip.className).toMatch(/integrity--bad/);
  });

  it("marks an INVALID seal bad, with the seal problems", async () => {
    integrity = { run_id: "run1", ok: false, events: 4, hashed: 4,
      seals: { state: "INVALID", seals: 1, valid: 0, sealed_through: null, unsigned_tail: 0, problems: ["seal at seq 4: bad mac"], unknown_keys: [] } };
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    const chip = await screen.findByRole("status", { name: /integrity/i });
    expect(chip).toHaveTextContent(/INVALID/);
    expect(chip).toHaveAttribute("title", expect.stringContaining("seal at seq 4: bad mac"));
    expect(chip.className).toMatch(/integrity--bad/);
  });

  it("shows 'unsigned' as a note, not a fault, and says which variable turns seals on", async () => {
    integrity = { run_id: "run1", ok: true, events: 4, hashed: 4,
      seals: { state: "unsigned", seals: 0, valid: 0, sealed_through: null, unsigned_tail: 4, problems: [], unknown_keys: [] } };
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    const chip = await screen.findByRole("status", { name: /integrity/i });
    expect(chip).toHaveTextContent(/unsigned/);
    expect(chip.className).not.toMatch(/integrity--bad/);
    expect(chip).toHaveAttribute("title", expect.stringMatching(/AGENTOS_SIGNING_KEYS/));
  });

  it("re-reads the verdict when the run's status changes (seals appear at idle/terminal), not per frame", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    await screen.findByRole("status", { name: /integrity/i });
    expect(gets("/runs/run1/integrity")).toHaveLength(1);
    // decide from the run page: the fold refetches and says running → the verdict is re-read once
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(gets("/runs/run1/integrity")).toHaveLength(2));
  });
});

describe("H5 permission before the click — wording only", () => {
  beforeEach(() => { me = { mode: "bearer", principal: { kind: "agent", id: "bot" } }; });

  it("says the API will refuse when the workflow denies agent approval (policy unset), button stays live", async () => {
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    const note = await screen.findByRole("note", { name: /human-only approval/ });
    await waitFor(() => expect(note).toHaveTextContent(/The API will refuse this/));
    expect(note).toHaveTextContent(/workflow "payments" does not allow agent approval/);
    expect(note).not.toHaveTextContent(/policy/);
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(gets("/policy")).toHaveLength(1);
    expect(gets("/workflows/payments")).toHaveLength(1);
  });

  it("names both when the workflow allows but the operator policy forbids", async () => {
    def = { ...DEF, budget: { ...DEF.budget, allow_agent_approval: true } };
    policy = { sha256: "abc", policy: { agent_approval_allowed: false } };
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    const note = await screen.findByRole("note", { name: /human-only approval/ });
    await waitFor(() => expect(note).toHaveTextContent(/The API will refuse this/));
    expect(note).toHaveTextContent(/operator policy forbids agent approval/);
    expect(note).toHaveTextContent(/workflow "payments" allows it/);
  });

  it("says the API will accept when both allow, and still warns it is recorded as an agent", async () => {
    def = { ...DEF, budget: { ...DEF.budget, allow_agent_approval: true } };
    policy = { sha256: "abc", policy: { agent_approval_allowed: true } };
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    const note = await screen.findByRole("note", { name: /human-only approval/ });
    await waitFor(() => expect(note).toHaveTextContent(/The API will accept this/));
    expect(note).toHaveTextContent(/agent:bot/);
  });

  // One truth table, two consumers: tests/test_ui_engine_drift.py drives engine.approve through the
  // same rows with a non-human principal. If the engine's rule and this wording ever diverge, one of
  // the two runners goes red -- the UI must not speak for the engine from a copy nobody checks.
  describe("matches tests/fixtures/agent_approval_matrix.json row for row", () => {
    for (const row of MATRIX.rows) {
      const label = `workflow allow=${row.workflow_allow_agent_approval} policy=${String(row.policy_agent_approval_allowed)}`;
      it(`${label} -> ${row.accept ? "accept" : "refuse"}`, async () => {
        def = { ...DEF, budget: { ...DEF.budget, allow_agent_approval: row.workflow_allow_agent_approval } };
        policy = row.policy_agent_approval_allowed === null
          ? { sha256: null, policy: null }
          : { sha256: "abc", policy: { agent_approval_allowed: row.policy_agent_approval_allowed } };
        window.history.pushState(null, "", "/ui/");
        render(<App />);
        const note = await screen.findByRole("note", { name: /human-only approval/ });
        await waitFor(() => expect(note).toHaveTextContent(row.accept ? /The API will accept this/ : /The API will refuse this/));
        expect(note).toHaveTextContent(row.sentence);
        expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();   // wording only; the API decides
      });
    }
  });

  it("does not fetch policy or the definition for a human principal", async () => {
    me = { mode: "bearer", principal: { kind: "human", id: "amit" } };
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    await screen.findByRole("button", { name: "Approve" });
    expect(screen.queryByRole("note", { name: /human-only approval/ })).not.toBeInTheDocument();
    expect(gets("/policy")).toHaveLength(0);
    expect(gets("/workflows/payments")).toHaveLength(0);
  });
});

describe("H6 opt-in browser notification, no authority in it", () => {
  let created: { title: string; opts?: NotificationOptions; onclick: (() => void) | null }[];
  let permission: NotificationPermission;
  beforeEach(() => {
    created = [];
    permission = "default";
    class FakeNotification {
      static get permission() { return permission; }
      static requestPermission = vi.fn(async () => { permission = "granted"; return permission; });
      onclick: (() => void) | null = null;
      constructor(public title: string, public opts?: NotificationOptions) { created.push(this); }
      close() { /* no-op */ }
    }
    vi.stubGlobal("Notification", FakeNotification);
    pending = [];
  });
  afterEach(() => vi.unstubAllGlobals());

  it("is off by default: a new gate arriving fires nothing", async () => {
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    await screen.findByRole("region", { name: /nothing is waiting/i });
    pending = [PENDING];
    await waitFor(() => expect(screen.getByText(/step "pay" declares spend/)).toBeInTheDocument(), { timeout: 4500 });
    expect(created).toHaveLength(0);
  });

  it("once turned on (a click, permission asked then), a NEW gate notifies with workflow/step/class and opens the run", async () => {
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    await screen.findByRole("region", { name: /nothing is waiting/i });
    const toggle = screen.getByRole("switch", { name: /notify/i });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await userEvent.click(toggle);
    expect((Notification as unknown as { requestPermission: () => void }).requestPermission).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    expect(window.localStorage.getItem("agentos.notify")).toBe("1");
    pending = [PENDING];
    await waitFor(() => expect(created).toHaveLength(1), { timeout: 4500 });
    expect(created[0].title).toMatch(/approval waiting/i);
    expect(created[0].opts?.body).toMatch(/payments/);
    expect(created[0].opts?.body).toMatch(/pay/);
    expect(created[0].opts?.body).toMatch(/spend/);
    expect(created[0].opts?.tag).toBe("agentos:ap1");          // one per gate, re-fires never
    // no verb, no decision in it; click just navigates
    expect(JSON.stringify(created[0].opts)).not.toMatch(/approve|reject/i);
    created[0].onclick?.();
    await waitFor(() => expect(window.location.pathname).toBe("/ui/runs/run1"));
  });

  it("does not notify for gates already pending when the page loaded, and stays on across pages", async () => {
    window.localStorage.setItem("agentos.notify", "1");
    permission = "granted";
    pending = [PENDING];
    window.history.pushState(null, "", "/ui/runs");
    render(<App />);
    await screen.findByRole("heading", { name: /runs/i });
    await waitFor(() => expect(gets("/approvals")).toHaveLength(1));   // the shell's badge poll
    expect(created).toHaveLength(0);                                    // already there → no push
    const toggle = screen.getByRole("switch", { name: /notify/i });
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("is absent when the browser has no Notification API, and off when permission was denied", async () => {
    vi.unstubAllGlobals();
    vi.stubGlobal("Notification", undefined);
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    await screen.findByRole("region", { name: /nothing is waiting/i });
    expect(screen.queryByRole("switch", { name: /notify/i })).not.toBeInTheDocument();
  });
});
