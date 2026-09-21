/**
 * Comparison pass 2 (reviews/agentos-ui-comparison-2026-09-22/COMPARISON-2.md): the approval
 * surface, tested as an operator uses it.
 *   A1 the run page decides its own pending approvals (Windmill, Argo, GitHub review deployments)
 *      and lists decided ones with who / why / when from the fold
 *   A2 the inbox card links to the run, names the principal the decision is recorded as, and
 *      shows what the approval covers on demand — fetched once, never on the poll
 *   A3 the verbs state their consequence; a non-human principal on a human-only class is warned
 *      before the click (the API stays the authority: warn, don't disable)
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Approval } from "./api";
import type { RunState, WorkflowDef } from "./graph";

const DEF: WorkflowDef = { name: "payments", version: 1, nodes: [
  { id: "a", agent: "calc", depends_on: [] }, { id: "pay", agent: "payer", depends_on: ["a"] },
], budget: { allowed_effect_classes: ["compute"], approval_required_for: ["spend"], allow_agent_approval: false, approval_timeout_seconds: null } };

const PENDING: Approval = {
  run_id: "run1", workflow: "payments", approval_id: "ap1", step_id: "pay",
  kind: "effect", effect_classes: ["spend"], status: "pending",
  requested_at: "2026-09-20T00:00:00Z", expires_at: null, reason: "step 'pay' declares spend",
};
const COST: Approval = {
  ...PENDING, approval_id: "ap2", step_id: null, kind: "cost", effect_classes: [],
  cost_at_request: "5.20", proposed_ceiling: "10.00", reason: "run cost 5.20 exceeds ceiling 5.00",
};

function base(over: Partial<RunState>): RunState {
  return { id: "run1", workflow: "payments", workflow_version: 1, status: "suspended", steps: [], attempts: {},
    progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {}, cancelled_steps: [], approvals: {},
    total_cost: "0.10", last_seq: 4, started_at: "2026-09-20T00:00:00Z", ...over };
}
const SUSPENDED = base({
  attempts: { a: 1 },
  steps: [{ node_id: "a", attempt: 1, cost: { amount: "0.10", currency: "USD" }, output: { amount: 42, currency: "USD" } }],
  inputs: { customer: "c-9" },
  approvals: { ap1: { ...PENDING, decided_by: null, decision_reason: "", decided_at: null } },
});
const EVENTS = [
  { seq: 1, event_type: "run.started", occurred_at: "2026-09-20T00:00:00Z" },
  { seq: 2, event_type: "step.started", occurred_at: "2026-09-20T00:00:01Z", step_id: "a", attempt: 1 },
  { seq: 3, event_type: "step.completed", occurred_at: "2026-09-20T00:00:03Z", step_id: "a", cost: { amount: "0.10", currency: "USD" } },
  { seq: 4, event_type: "approval.requested", occurred_at: "2026-09-20T00:00:04Z", step_id: "pay" },
];

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
type Seen = { url: string; method: string; body: unknown };
let seen: Seen[];
let state: RunState;
let me: unknown;
let pending: Approval[];
let onDecide: (url: string) => Response;

beforeEach(() => {
  seen = [];
  me = { mode: "bearer", principal: { kind: "human", id: "amit" } };
  state = SUSPENDED;
  pending = [PENDING];
  onDecide = () => { pending = []; state = base({ status: "running" }); return json(202, state); };
  window.sessionStorage.setItem("agentos.token", "tok");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url === "/me") return json(200, me);
    if (url === "/approvals") return json(200, { data: pending });
    if (url.startsWith("/runs?")) return json(200, { data: [] });
    if (url === "/runs/run1") return json(200, state);
    if (url.startsWith("/runs/run1/events")) return json(200, { data: EVENTS, last_seq: 4, has_more: false });
    if (url === "/workflows/payments") return json(200, DEF);
    if (url === "/runs/run1/stream") return new Response(new ReadableStream({ start(c) { c.close(); } }), { status: 200 });
    if (method === "POST" && url.includes("/approvals/")) return onDecide(url);
    return json(404, { detail: `no route ${url}` });
  });
});
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

const posts = () => seen.filter((s) => s.method === "POST");
const gets = (u: string) => seen.filter((s) => s.method === "GET" && s.url === u);

describe("A1 approvals on the run page", () => {
  it("decides a pending approval from the run page, with the API's own body, and drops the inbox redirect", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    const panel = await screen.findByRole("region", { name: /approvals/i });
    expect(screen.queryByText(/decide in the inbox/)).not.toBeInTheDocument();
    expect(within(panel).getByText(/step "pay" declares spend/)).toBeInTheDocument();
    await userEvent.type(within(panel).getByRole("textbox", { name: /reason for pay/ }), "looks right");
    await userEvent.click(within(panel).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0]).toEqual({ url: "/runs/run1/approvals/ap1/approve", method: "POST", body: { reason: "looks right" } });
    // the fold is refetched and now says running: the decision card is gone, the status chip follows
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 }).parentElement!).toHaveTextContent("running"));
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("lists a decided approval as a record: status, who, why, when — from the fold", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    state = base({ status: "completed", approvals: { ap1: { ...PENDING, status: "granted",
      decided_by: { kind: "human", id: "amit" }, decision_reason: "looks right", decided_at: "2026-09-20T00:05:00Z" } } });
    render(<App />);
    const panel = await screen.findByRole("region", { name: /approvals/i });
    const row = within(panel).getByRole("listitem");
    expect(row).toHaveTextContent(/granted/);
    expect(row).toHaveTextContent(/human:amit/);
    expect(row).toHaveTextContent(/looks right/);
    expect(row.querySelector("time")).toHaveAttribute("dateTime", "2026-09-20T00:05:00Z");
    expect(within(panel).queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});

describe("A2 inbox card context", () => {
  it("links to the run and names the principal the decision is recorded as, next to the actions", async () => {
    render(<App />);
    const item = await screen.findByRole("listitem");
    const link = within(item).getByRole("link", { name: /run1|payments/ });
    expect(link).toHaveAttribute("href", "/ui/runs/run1");
    const actions = within(item).getByRole("group", { name: /decision/i });
    expect(actions).toHaveTextContent(/recorded as amit/i);
  });

  it("shows what the approval covers on demand — fetched once, not on the poll", async () => {
    render(<App />);
    const item = await screen.findByRole("listitem");
    expect(gets("/runs/run1")).toHaveLength(0);                 // nothing fetched for the card by default
    await userEvent.click(within(item).getByRole("button", { name: /what this approves/i }));
    const ctx = await within(item).findByRole("region", { name: /what this approves/i });
    expect(ctx).toHaveTextContent(/agent.*payer/);              // the step's agent from the definition
    expect(ctx).toHaveTextContent(/customer.*c-9/);             // the run's inputs
    expect(ctx).toHaveTextContent(/from a.*amount.*42/);        // the upstream output the step receives
    expect(ctx).toHaveTextContent(/cost so far.*0\.10/);
    expect(ctx).toHaveTextContent(/approval required for.*spend/);
    expect(gets("/runs/run1")).toHaveLength(1);
    expect(gets("/workflows/payments")).toHaveLength(1);
    // the disclosure stays open across the inbox poll and does not refetch
    await new Promise((r) => setTimeout(r, 3200));
    expect(gets("/approvals").length).toBeGreaterThan(1);       // the poll ran
    expect(gets("/runs/run1")).toHaveLength(1);
    expect(gets("/workflows/payments")).toHaveLength(1);
  });
});

describe("A3 consequences", () => {
  it("states what approve and reject do, on the card and on the buttons", async () => {
    render(<App />);
    const item = await screen.findByRole("listitem");
    expect(item).toHaveTextContent(/Approve lets "pay" run once/);
    expect(item).toHaveTextContent(/Reject dead-letters it and fails the run; retry re-requests approval/);
    expect(within(item).getByRole("button", { name: "Approve" })).toHaveAttribute("title", expect.stringMatching(/run once/));
    expect(within(item).getByRole("button", { name: "Reject" })).toHaveAttribute("title", expect.stringMatching(/fails the run/));
  });

  it("explains a cost approval as a ceiling raise", async () => {
    pending = [COST];
    render(<App />);
    const item = await screen.findByRole("listitem");
    expect(item).toHaveTextContent(/Approve raises the run's cost ceiling to 10\.00 and resumes it/);
  });

  it("warns — without disabling — when a non-human principal faces a human-only class", async () => {
    me = { mode: "bearer", principal: { kind: "agent", id: "bot" } };
    render(<App />);
    const item = await screen.findByRole("listitem");
    expect(within(item).getByRole("note", { name: /human-only/i })).toHaveTextContent(/spend.*requires a human principal.*agent:bot/);
    expect(within(item).getByRole("button", { name: "Approve" })).toBeEnabled();   // the API decides, not the UI
  });

  it("does not warn a human principal", async () => {
    render(<App />);
    const item = await screen.findByRole("listitem");
    expect(within(item).queryByRole("note", { name: /human-only/i })).not.toBeInTheDocument();
  });
});
