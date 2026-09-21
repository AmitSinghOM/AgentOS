/**
 * Patterns adopted from the popular workflow UIs (see reviews/agentos-ui-comparison-2026-09-22),
 * tested as an operator uses them:
 *   A1 run controls: cancel is a two-step inline confirm with a reason; pause/resume follow the
 *      fold's status; the body is exactly what the API accepts (no principal in bearer mode,
 *      the typed principal in asserted mode); a 409 is shown verbatim
 *   A2 node selection: clicking a graph node shows the step's detail and narrows the timeline to
 *      that step's events plus run-level ones; retry appears only for failed/dead-lettered steps
 *   A3 expandable event rows: the full event record is one click away
 *   A4 runs list: status chips with counts and a text filter, held in the URL
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { RunSummary } from "./api";
import type { RunState, WorkflowDef } from "./graph";

const DEF: WorkflowDef = { name: "w", version: 1, nodes: [
  { id: "a", agent: "calc", depends_on: [] }, { id: "b", agent: "payer", depends_on: ["a"] },
] };
const EVENTS = [
  { seq: 1, event_type: "run.started", occurred_at: "2026-09-20T00:00:00Z" },
  { seq: 2, event_type: "step.started", occurred_at: "2026-09-20T00:00:01Z", step_id: "a", attempt: 1 },
  { seq: 3, event_type: "step.completed", occurred_at: "2026-09-20T00:00:03Z", step_id: "a", cost: { amount: "0.10", currency: "USD" } },
  { seq: 4, event_type: "step.started", occurred_at: "2026-09-20T00:00:04Z", step_id: "b", attempt: 1 },
  { seq: 5, event_type: "step.failed", occurred_at: "2026-09-20T00:00:05Z", step_id: "b", error: "card declined" },
];
function base(over: Partial<RunState>): RunState {
  return { id: "run1", workflow: "w", workflow_version: 1, status: "running", steps: [], attempts: {},
    progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {}, cancelled_steps: [], approvals: {},
    total_cost: "0", last_seq: 5, started_at: "2026-09-20T00:00:00Z", ...over };
}
function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
type Seen = { url: string; method: string; body: unknown };
let seen: Seen[];
let state: RunState;
let me: unknown;
let runs: RunSummary[];
let control: (url: string) => Response | null;

beforeEach(() => {
  seen = [];
  me = { mode: "bearer", principal: { kind: "human", id: "amit" } };
  state = base({ attempts: { a: 1, b: 1 }, steps: [{ node_id: "a", attempt: 1, cost: { amount: "0.10", currency: "USD" } }],
    dead_lettered: { b: "card declined" } });
  runs = [];
  control = () => null;
  window.sessionStorage.setItem("agentos.token", "tok");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    seen.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url === "/me") return json(200, me);
    if (url === "/approvals") return json(200, { data: [] });
    if (url.startsWith("/runs?")) return json(200, { data: runs });
    if (url === "/runs/run1") return json(200, state);
    if (url.startsWith("/runs/run1/events")) return json(200, { data: EVENTS, last_seq: 5, has_more: false });
    if (url === "/workflows/w") return json(200, DEF);
    if (url === "/runs/run1/stream") return new Response(new ReadableStream({ start(c) { c.close(); } }), { status: 200 });
    if (method === "POST") { const r = control(url); if (r) return r; }
    return json(404, { detail: `no route ${url}` });
  });
});
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

const posts = () => seen.filter((s) => s.method === "POST");

describe("A1 run controls", () => {
  it("cancels through an inline confirm with a reason, sending exactly the API's body", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    control = (url) => url === "/runs/run1/cancel" ? (state = base({ status: "cancelled" }), json(202, state)) : null;
    render(<App />);
    const controls = await screen.findByRole("group", { name: "run controls" });
    expect(within(controls).getByRole("button", { name: "Pause" })).toBeEnabled();
    expect(within(controls).getByRole("button", { name: "Resume" })).toBeDisabled();
    const cancel = within(controls).getByRole("button", { name: "Cancel run" });
    expect(cancel).toBeEnabled();
    expect(posts()).toHaveLength(0);

    await userEvent.click(cancel);
    expect(posts()).toHaveLength(0);                      // first click only opens the confirm
    await userEvent.type(screen.getByRole("textbox", { name: "cancel reason" }), "budget cut");
    await userEvent.click(screen.getByRole("button", { name: "Confirm cancel" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0]).toEqual({ url: "/runs/run1/cancel", method: "POST", body: { reason: "budget cut" } });
    // the fold now says cancelled: the chip follows and every control is disabled (terminal)
    await waitFor(() => expect(screen.getByRole("heading", { level: 2 }).parentElement!).toHaveTextContent("cancelled"));
    expect(within(controls).getByRole("button", { name: "Cancel run" })).toBeDisabled();
    expect(within(controls).getByRole("button", { name: "Pause" })).toBeDisabled();
  });

  it("shows the API's 409 verbatim and keeps the run as the fold says", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    control = (url) => url === "/runs/run1/pause" ? json(409, { detail: "run 'run1' is paused already" }) : null;
    render(<App />);
    const controls = await screen.findByRole("group", { name: "run controls" });
    await userEvent.click(within(controls).getByRole("button", { name: "Pause" }));
    await screen.findByText(/run 'run1' is paused already/);
    expect(screen.getByRole("alert")).toHaveTextContent("409");
  });

  it("in asserted mode sends the typed principal, and is disabled until one is typed", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    me = { mode: "asserted", principal: null };
    state = base({ status: "paused" });
    control = (url) => url === "/runs/run1/resume" ? json(202, state) : null;
    render(<App />);
    const controls = await screen.findByRole("group", { name: "run controls" });
    const resume = within(controls).getByRole("button", { name: "Resume" });
    expect(resume).toBeDisabled();                         // paused, but nobody is deciding yet
    await userEvent.type(screen.getByRole("textbox", { name: /principal|your id/i }), "ops-1");
    await waitFor(() => expect(resume).toBeEnabled());
    await userEvent.click(resume);
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body).toEqual({ principal: { kind: "human", id: "ops-1" }, reason: "" });
  });
});

describe("A2 node selection", () => {
  it("shows the step's detail, narrows the timeline to it, and offers retry only when the fold allows", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    control = (url) => url === "/runs/run1/steps/b/retry" ? json(202, state) : null;
    render(<App />);
    await screen.findByRole("group", { name: /run graph/ });
    const log = await screen.findByRole("table", { name: "event log" });
    await waitFor(() => expect(within(log).getAllByRole("row")).toHaveLength(EVENTS.length + 1));
    expect(screen.queryByRole("region", { name: /^step / })).not.toBeInTheDocument();

    // click node a in the graph: detail strip + timeline narrowed to a's events and run-level ones
    await userEvent.click(screen.getByRole("group", { name: /^a:/ }));
    const detail = await screen.findByRole("region", { name: "step a" });
    expect(detail).toHaveTextContent("calc");
    expect(detail).toHaveTextContent("completed");
    expect(detail).toHaveTextContent("2.00 s");            // 00:00:01 → 00:00:03
    expect(detail).toHaveTextContent("0.10");
    expect(within(detail).queryByRole("button", { name: "Retry step" })).not.toBeInTheDocument();
    expect(within(log).getAllByRole("row")).toHaveLength(3 + 1);   // run.started, a started, a completed
    expect(log).not.toHaveTextContent("card declined");

    // node b is dead-lettered: retry is offered and posts to the step's retry route
    await userEvent.click(screen.getByRole("group", { name: /^b:/ }));
    const detailB = await screen.findByRole("region", { name: "step b" });
    expect(detailB).toHaveTextContent("dead-lettered");
    expect(detailB).toHaveTextContent("card declined");
    await userEvent.type(within(detailB).getByRole("textbox", { name: "retry reason" }), "card replaced");
    await userEvent.click(within(detailB).getByRole("button", { name: "Retry step" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0]).toEqual({ url: "/runs/run1/steps/b/retry", method: "POST", body: { reason: "card replaced" } });

    // back to every event
    await userEvent.click(screen.getByRole("button", { name: /show all events/i }));
    expect(screen.queryByRole("region", { name: /^step / })).not.toBeInTheDocument();
    expect(within(log).getAllByRole("row")).toHaveLength(EVENTS.length + 1);
  });
});

describe("A3 expandable event rows", () => {
  it("reveals the full event record under its row", async () => {
    window.history.pushState(null, "", "/ui/runs/run1");
    render(<App />);
    const log = await screen.findByRole("table", { name: "event log" });
    await waitFor(() => expect(within(log).getAllByRole("row")).toHaveLength(EVENTS.length + 1));
    expect(log).not.toHaveTextContent('"currency"');
    const toggle = within(log).getByRole("button", { name: "expand event 3" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const full = within(log).getByRole("region", { name: "event 3 record" });
    expect(full).toHaveTextContent('"currency": "USD"');
    expect(full).toHaveTextContent('"seq": 3');
    await userEvent.click(toggle);
    expect(within(log).queryByRole("region", { name: "event 3 record" })).not.toBeInTheDocument();
  });
});

describe("A4 runs list filters", () => {
  const summary = (over: Partial<RunSummary>): RunSummary => ({ id: "x", workflow: "w", workflow_version: 1, status: "completed",
    total_cost: "0", last_seq: 1, started_at: "2026-09-20T00:00:00Z", pending_approvals: 0, ...over });

  it("filters the loaded page by status chip and text, holding both in the URL", async () => {
    window.history.pushState(null, "", "/ui/runs");
    runs = [
      summary({ id: "00dd284c0000", workflow: "diamond", status: "suspended", pending_approvals: 1 }),
      summary({ id: "656b949b0000", workflow: "haiku", status: "completed" }),
      summary({ id: "77aa000b0000", workflow: "haiku", status: "failed" }),
    ];
    render(<App />);
    const table = await screen.findByRole("table", { name: "runs" });
    expect(within(table).getAllByRole("row")).toHaveLength(3 + 1);
    const chips = screen.getByRole("group", { name: "filter by status" });
    expect(within(chips).getByRole("button", { name: /^all/ })).toHaveTextContent("3");
    expect(within(chips).getByRole("button", { name: /^suspended/ })).toHaveTextContent("1");
    expect(within(chips).getByRole("button", { name: /^completed/ })).toHaveTextContent("1");
    expect(within(chips).getByRole("button", { name: /^failed/ })).toHaveTextContent("1");
    expect(within(chips).queryByRole("button", { name: /^running/ })).not.toBeInTheDocument();   // no zero chips

    await userEvent.click(within(chips).getByRole("button", { name: /^suspended/ }));
    expect(within(chips).getByRole("button", { name: /^suspended/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 1);
    expect(table).toHaveTextContent("diamond");
    expect(window.location.search).toContain("status=suspended");

    await userEvent.click(within(chips).getByRole("button", { name: /^all/ }));
    expect(window.location.search).not.toContain("status=");
    await userEvent.type(screen.getByRole("searchbox", { name: "filter runs" }), "haiku");
    expect(within(table).getAllByRole("row")).toHaveLength(2 + 1);
    expect(window.location.search).toContain("q=haiku");

    // id prefix matches too; nothing matching says so and offers a way back
    await userEvent.clear(screen.getByRole("searchbox", { name: "filter runs" }));
    await userEvent.type(screen.getByRole("searchbox", { name: "filter runs" }), "00dd");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 1);
    await userEvent.type(screen.getByRole("searchbox", { name: "filter runs" }), "zzz");
    expect(screen.getByText(/no runs match/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(within(screen.getByRole("table", { name: "runs" })).getAllByRole("row")).toHaveLength(3 + 1);   // remounted
    expect(window.location.search).toBe("");
  });

  it("restores a filter from the URL on load", async () => {
    window.history.pushState(null, "", "/ui/runs?status=failed");
    runs = [summary({ id: "1", status: "completed" }), summary({ id: "2", status: "failed" })];
    render(<App />);
    const table = await screen.findByRole("table", { name: "runs" });
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 1);
    expect(screen.getByRole("button", { name: /^failed/ })).toHaveAttribute("aria-pressed", "true");
  });
});
