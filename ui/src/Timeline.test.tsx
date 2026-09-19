/**
 * Time travel and the cost panel, as an operator sees them:
 *   - the timeline lists every event with its salient field
 *   - seeking asks the SERVER for `?at=k` and the graph shows THAT state; nothing is folded here
 *   - the banner says which seq is shown; Back to live returns to the live fold
 *   - the cost panel shows per-node attempts, duration and cost, and follows the seek
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { RunState, WorkflowDef } from "./graph";

const DEF: WorkflowDef = { name: "w", version: 1, nodes: [
  { id: "a", agent: "calc", depends_on: [] }, { id: "b", agent: "calc", depends_on: ["a"] },
] };
const EVENTS = [
  { seq: 1, event_type: "run.started", occurred_at: "2026-09-20T00:00:00Z" },
  { seq: 2, event_type: "step.started", occurred_at: "2026-09-20T00:00:01Z", step_id: "a", attempt: 1 },
  { seq: 3, event_type: "step.completed", occurred_at: "2026-09-20T00:00:03Z", step_id: "a", cost: { amount: "0.10", currency: "USD" } },
  { seq: 4, event_type: "step.started", occurred_at: "2026-09-20T00:00:04Z", step_id: "b", attempt: 1 },
  { seq: 5, event_type: "step.completed", occurred_at: "2026-09-20T00:00:09Z", step_id: "b", cost: { amount: "0.20", currency: "USD" } },
  { seq: 6, event_type: "run.completed", occurred_at: "2026-09-20T00:00:09Z" },
];
function base(over: Partial<RunState>): RunState {
  return { id: "run1", workflow: "w", workflow_version: 1, status: "completed", steps: [], attempts: {},
    progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {}, cancelled_steps: [], approvals: {},
    total_cost: "0", last_seq: 6, started_at: "2026-09-20T00:00:00Z", ...over };
}
const LIVE = base({ attempts: { a: 1, b: 1 }, total_cost: "0.30",
  steps: [{ node_id: "a", attempt: 1, cost: { amount: "0.10", currency: "USD" } }, { node_id: "b", attempt: 1, cost: { amount: "0.20", currency: "USD" } }] });
// what the SERVER returns for ?at=k (the test is the oracle, so a wrong client fold would show)
const AT: Record<number, RunState> = {
  2: base({ status: "running", attempts: { a: 1 }, last_seq: 2 }),
  3: base({ status: "running", attempts: { a: 1 }, last_seq: 3, total_cost: "0.10", steps: [{ node_id: "a", attempt: 1, cost: { amount: "0.10", currency: "USD" } }] }),
};
function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
let urls: string[];

beforeEach(() => {
  urls = [];
  window.sessionStorage.setItem("agentos.token", "tok");
  window.history.pushState(null, "", "/ui/runs/run1");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    urls.push(url);
    if (url === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit" } });
    if (url === "/runs/run1") return json(200, LIVE);
    if (url.startsWith("/runs/run1?at=")) { const k = Number(url.split("=")[1]); return AT[k] ? json(200, AT[k]) : json(422, { detail: `at must be within 1..6` }); }
    if (url === "/runs/run1/events") return json(200, { data: EVENTS, last_seq: 6 });
    if (url === "/workflows/w") return json(200, DEF);
    if (url === "/runs/run1/stream") return new Response(new ReadableStream({ start(c) { c.close(); } }), { status: 200 });
    return json(404, {});
  });
});
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

const label = (id: string) => screen.getByRole("group", { name: new RegExp(`^${id}:`) }).getAttribute("aria-label");

describe("time travel", () => {
  it("lists events, seeks via the server's ?at= fold, and comes back to live", async () => {
    render(<App />);
    await screen.findByRole("img", { name: /run graph/ });
    const log = await screen.findByRole("table", { name: "event log" });
    expect(within(log).getAllByRole("row")).toHaveLength(EVENTS.length + 1);
    expect(log).toHaveTextContent("cost 0.20");
    expect(label("b")).toBe("b: completed");

    await userEvent.click(within(log).getByRole("button", { name: "view state after seq 2" }));
    await waitFor(() => expect(label("a")).toBe("a: running"));
    expect(label("b")).toBe("b: pending");
    expect(urls).toContain("/runs/run1?at=2");
    const banner = screen.getAllByRole("status").find((el) => el.className.includes("time-travel"))!;
    expect(banner).toHaveTextContent("time travel — the graph shows the run as it was right after seq 2");
    expect(screen.getByRole("slider", { name: "time travel scrubber" })).toHaveValue("2");
    // the cost panel follows the seek: only a is known so far, and it is still running
    const cost = screen.getByRole("table", { name: "cost and latency per node" });
    expect(within(cost).getAllByRole("row")[1]).toHaveTextContent("in flight");
    expect(within(cost).getAllByRole("row")[2]).toHaveTextContent("not started");

    await userEvent.click(screen.getByRole("button", { name: "Back to live" }));
    await waitFor(() => expect(label("b")).toBe("b: completed"));
    expect(screen.queryByText(/time travel —/)).not.toBeInTheDocument();
  });

  it("the scrubber seeks too and never folds in the browser (state comes from ?at=)", async () => {
    render(<App />);
    await screen.findByRole("img", { name: /run graph/ });
    const slider = screen.getByRole("slider", { name: "time travel scrubber" });
    // jsdom has no drag; setting the value and firing change is what a drag ends in
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(slider, { target: { value: "3" } });
    await waitFor(() => expect(urls).toContain("/runs/run1?at=3"));
    await waitFor(() => expect(label("a")).toBe("a: completed"));
    expect(label("b")).toBe("b: pending");                      // ?at=3 says b has not started
    expect(screen.getByRole("group", { name: /^a:/ })).toHaveTextContent("0.10");
  });

  it("shows the API's error for an out-of-range seek and keeps the live graph", async () => {
    render(<App />);
    await screen.findByRole("img", { name: /run graph/ });
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(screen.getByRole("slider", { name: "time travel scrubber" }), { target: { value: "5" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load state at seq 5: 422 at must be within 1..6");
    // no state could be loaded for seq 5, so no graph is drawn — never the live state under a wrong banner
    expect(screen.queryByRole("img", { name: /run graph/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back to live" }));
    await screen.findByRole("img", { name: /run graph/ });
    expect(label("b")).toBe("b: completed");
  });
});

describe("cost panel", () => {
  it("shows per-node attempts, duration and cost from the live fold and event timestamps", async () => {
    render(<App />);
    await screen.findByRole("table", { name: "cost and latency per node" });
    const rows = within(screen.getByRole("table", { name: "cost and latency per node" })).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("a"); expect(rows[1]).toHaveTextContent("completed");
    expect(rows[1]).toHaveTextContent("2.00 s"); expect(rows[1]).toHaveTextContent("0.10");
    expect(rows[2]).toHaveTextContent("5.00 s"); expect(rows[2]).toHaveTextContent("0.20");
    expect(screen.getByText(/run cost/).parentElement).toHaveTextContent("run cost 0.30");
    expect(screen.getByText(/wall/).parentElement).toHaveTextContent("8.00 s");
  });
});
