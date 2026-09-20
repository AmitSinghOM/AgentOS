/**
 * The run page, tested as an operator sees it, over a scripted stream:
 *   - the stream is read with fetch + Authorization (never EventSource, never a token in a URL)
 *   - every frame lands in the ticker and triggers a refetch of the SERVER's folded run; node
 *     states come from that fold, not from the frames
 *   - nodes light up: pending → running → completed as the fold changes
 *   - a pending approval renders as "awaiting approval" and links to the inbox
 *   - a pinned-version mismatch is stated, not hidden
 *   - the stream closing on a terminal run is "closed", not an error
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { RunState, WorkflowDef } from "./graph";

const DEF: WorkflowDef = { name: "diamond", version: 1, nodes: [
  { id: "a", agent: "writer", depends_on: [] },
  { id: "b", agent: "greeter", depends_on: ["a"] },
  { id: "d", agent: "writer", depends_on: ["b"] },
] };

function run(over: Partial<RunState>): RunState {
  return {
    id: "run1", workflow: "diamond", workflow_version: 1, status: "running", steps: [], attempts: {},
    progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {}, cancelled_steps: [],
    approvals: {}, total_cost: "0", last_seq: 1, started_at: "2026-09-20T00:00:00Z", ...over,
  };
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function frame(seq: number, type: string, step?: string) {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ seq, event_type: type, occurred_at: `t${seq}`, step_id: step })}\n\n`;
}
/** A stream body whose frames are released one at a time by the test. */
function scriptedStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  return { body, emit: (s: string) => controller.enqueue(enc.encode(s)), close: () => controller.close() };
}

type Seen = { url: string; headers: Record<string, string> };
let seen: Seen[];
let state: RunState;
let stream: ReturnType<typeof scriptedStream>;

beforeEach(() => {
  seen = [];
  window.sessionStorage.setItem("agentos.token", "tok");
  window.history.pushState(null, "", "/ui/runs/run1");
  stream = scriptedStream();
  state = run({});
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const headers = (init?.headers as Record<string, string>) ?? {};
    seen.push({ url, headers });
    if (headers.Authorization !== "Bearer tok") return json(401, { detail: "missing bearer token" });
    if (url === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit" } });
    if (url === "/runs/run1") return json(200, state);
    if (url === "/workflows/diamond") return json(200, DEF);
    if (url === "/runs/run1/stream") return new Response(stream.body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    return json(404, { detail: `no route ${url}` });
  });
});
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

const nodeLabel = (id: string) => screen.getByRole("group", { name: new RegExp(`^${id}:`) }).getAttribute("aria-label");

describe("run graph", () => {
  it("draws the DAG from the definition and lights nodes up as the folded run changes", async () => {
    render(<App />);
    await screen.findByRole("group", { name: /workflow diamond run graph/ });
    expect(nodeLabel("a")).toBe("a: pending");
    const streamReq = await waitFor(() => { const s = seen.find((x) => x.url === "/runs/run1/stream"); expect(s).toBeDefined(); return s!; });
    expect(streamReq.headers.Authorization).toBe("Bearer tok");
    expect(streamReq.headers.Accept).toBe("text/event-stream");
    expect(seen.every((s) => !s.url.includes("token="))).toBe(true);

    // frame 2: step a started. The fold (server) now says a has an attempt.
    state = run({ attempts: { a: 1 }, last_seq: 2 });
    stream.emit(frame(2, "step.started", "a"));
    await waitFor(() => expect(nodeLabel("a")).toBe("a: running"));
    expect(screen.getByRole("list", { name: "event ticker" })).toHaveTextContent("2 step.started a");

    // frame 3: a completed, b started
    state = run({ attempts: { a: 1, b: 1 }, last_seq: 4,
      steps: [{ node_id: "a", attempt: 1, cost: { amount: "0.10", currency: "USD" } }] });
    stream.emit(frame(3, "step.completed", "a") + frame(4, "step.started", "b"));
    await waitFor(() => expect(nodeLabel("a")).toBe("a: completed"));
    expect(nodeLabel("b")).toBe("b: running");
    expect(nodeLabel("d")).toBe("d: pending");
    expect(screen.getByRole("group", { name: /^a:/ })).toHaveTextContent("0.10");
    expect(screen.getByRole("status", { name: "stream state" })).toHaveTextContent("live");

    // terminal: server closes the stream after run.completed → "closed", no error, no reconnect
    state = run({ status: "completed", last_seq: 7, total_cost: "0.30", attempts: { a: 1, b: 1, d: 1 },
      steps: ["a", "b", "d"].map((n) => ({ node_id: n, attempt: 1, cost: { amount: "0.10", currency: "USD" } })) });
    stream.emit(frame(7, "run.completed"));
    stream.close();
    await waitFor(() => expect(screen.getByRole("status", { name: "stream state" })).toHaveTextContent("closed"));
    expect(nodeLabel("d")).toBe("d: completed");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const streams = seen.filter((s) => s.url === "/runs/run1/stream");
    expect(streams).toHaveLength(1);
  });

  it("reconnects with Last-Event-ID when the server closes a non-terminal stream", async () => {
    const second = scriptedStream();
    let streams = 0;
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      const headers = (init?.headers as Record<string, string>) ?? {};
      seen.push({ url, headers });
      if (url === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit" } });
      if (url === "/runs/run1") return json(200, state);
      if (url === "/workflows/diamond") return json(200, DEF);
      if (url === "/runs/run1/stream") {
        streams += 1;
        const body = streams === 1 ? stream.body : second.body;
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return json(404, {});
    });
    render(<App />);
    await screen.findByRole("group", { name: /run graph/ });
    await waitFor(() => expect(streams).toBe(1));
    state = run({ attempts: { a: 1 }, last_seq: 2 });
    stream.emit(frame(2, "step.started", "a"));
    await waitFor(() => expect(nodeLabel("a")).toBe("a: running"));
    stream.close();                                   // max duration reached; run still running
    await waitFor(() => expect(streams).toBe(2), { timeout: 4000 });
    const resumed = seen.filter((s) => s.url === "/runs/run1/stream")[1];
    expect(resumed.headers["Last-Event-ID"]).toBe("2");
    expect(resumed.headers.Authorization).toBe("Bearer tok");
    second.emit(frame(3, "step.completed", "a"));
    state = run({ status: "completed", attempts: { a: 1 }, last_seq: 3, steps: [{ node_id: "a", attempt: 1, cost: { amount: "0", currency: "USD" } }] });
    second.close();
    await waitFor(() => expect(screen.getByRole("status", { name: "stream state" })).toHaveTextContent("closed"));
    expect(streams).toBe(2);
  });

  it("shows a pending approval on the node and links to the inbox", async () => {
    state = run({ status: "suspended", attempts: { a: 1 }, steps: [{ node_id: "a", attempt: 1, cost: { amount: "0", currency: "USD" } }],
      approvals: { ap1: { approval_id: "ap1", step_id: "b", status: "pending", kind: "effect", effect_classes: ["spend"] } } });
    render(<App />);
    await screen.findByRole("group", { name: /run graph/ });
    expect(nodeLabel("b")).toBe("b: awaiting approval");
    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("1 approval waiting");
    expect(within(note).getByRole("link", { name: /decide in the inbox/ })).toHaveAttribute("href", "/ui/");
  });

  it("states a pinned-version mismatch instead of drawing the wrong graph silently", async () => {
    state = run({ workflow_version: 1 });
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      const headers = (init?.headers as Record<string, string>) ?? {};
      if (url === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit" } });
      if (url === "/runs/run1") return json(200, state);
      if (url === "/workflows/diamond") return json(200, { ...DEF, version: 2 });
      if (url === "/runs/run1/stream") return new Response(stream.body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      void headers;
      return json(404, {});
    });
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("pinned to workflow v1; the definition is now v2");
    expect(alert).toHaveTextContent("cannot advance");
  });

  it("fetches the log incrementally: after the first load, only events past the last seq it holds", async () => {
    // Production review A4: the old page re-downloaded the WHOLE log on every stream frame.
    const log: Record<string, unknown>[] = [
      { seq: 1, event_type: "run.started", occurred_at: "2026-09-20T00:00:01Z" },
    ];
    const eventsCalls: string[] = [];
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.startsWith("/runs/run1/events")) {
        eventsCalls.push(url);
        const after = Number(new URL(url, "http://x").searchParams.get("after") ?? "0");
        const page = log.filter((e) => (e.seq as number) > after);
        return json(200, { data: page, last_seq: page.length ? page[page.length - 1].seq : after, has_more: false });
      }
      return base(input, init);
    });
    render(<App />);
    await screen.findByRole("group", { name: /workflow diamond run graph/ });
    await waitFor(() => expect(eventsCalls.length).toBeGreaterThanOrEqual(1));
    expect(eventsCalls[0]).toMatch(/after=0\b/);

    log.push({ seq: 2, event_type: "step.started", occurred_at: "2026-09-20T00:00:02Z", step_id: "a", attempt: 1 });
    state = run({ attempts: { a: 1 }, last_seq: 2 });
    stream.emit(frame(2, "step.started", "a"));
    await waitFor(() => expect(nodeLabel("a")).toBe("a: running"));
    await waitFor(() => expect(eventsCalls.length).toBeGreaterThanOrEqual(2));
    expect(eventsCalls[eventsCalls.length - 1]).toMatch(/after=1\b/);
    // both events are on the timeline exactly once
    const rows = screen.getAllByRole("button", { name: /view state after seq/ });
    expect(rows.map((r) => r.textContent?.trim())).toEqual(["1", "2"]);
  });

  it("the runs landing page lists runs newest-first and links to each graph", async () => {
    window.history.pushState(null, "", "/ui/runs");
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit" } });
      if (url.startsWith("/runs?")) return json(200, { data: [
        { id: "run2xxxxxxxx", workflow: "w", workflow_version: 1, status: "suspended", total_cost: "0", last_seq: 5, started_at: "t2", pending_approvals: 1 },
        { id: "run1xxxxxxxx", workflow: "w", workflow_version: 1, status: "completed", total_cost: "0.30", last_seq: 9, started_at: "t1", pending_approvals: 0 },
      ] });
      return json(404, {});
    });
    render(<App />);
    const rows = await screen.findAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("run2xxxx");
    expect(within(rows[1]).getByRole("link", { name: "run2xxxx" })).toHaveAttribute("href", "/ui/runs/run2xxxxxxxx");
    expect(within(rows[1]).getByRole("link", { name: /1 awaiting approval/ })).toHaveAttribute("href", "/ui/");
    expect(rows[2]).toHaveTextContent("completed");
  });
});
