/**
 * UI polish on v0.15.0 — the small interactions an operator reaches for:
 *   - the run page's id is copyable in full (the visible id is the 8-char prefix)
 *   - the inbox's "decisions disabled" note is an action: it puts focus in the principal input
 *   - a run that cannot be loaded says so in a card with a way back to the list, not a bare line
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { RunState, WorkflowDef } from "./graph";

const DEF: WorkflowDef = { name: "w", version: 1, nodes: [{ id: "a", agent: "calc", depends_on: [] }] };
const RUN: RunState = {
  id: "0123456789abcdef0123456789abcdef", workflow: "w", workflow_version: 1, status: "completed",
  steps: [], attempts: {}, progress: {}, dead_lettered: {}, failed_steps: {}, pending_retries: {},
  cancelled_steps: [], approvals: {}, total_cost: "0", last_seq: 1, started_at: "2026-09-20T00:00:00Z",
};
const PENDING = {
  run_id: "abcdef1234567890", workflow: "payments", approval_id: "ap1", step_id: "pay",
  kind: "effect", effect_classes: ["spend"], status: "pending",
  requested_at: "2026-09-20T00:00:00Z", expires_at: null, reason: "step 'pay' declares spend",
};

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => { window.sessionStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

function server(runStatus = 200) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/me") return json(200, { mode: "asserted", principal: null });
    if (url === "/approvals") return json(200, { data: [PENDING] });
    if (url === `/runs/${RUN.id}`) return runStatus === 200 ? json(200, RUN) : json(runStatus, { detail: "run not found" });
    if (url.startsWith(`/runs/${RUN.id}/events`)) return json(200, { data: [], last_seq: 1, has_more: false });
    if (url === "/workflows/w") return json(200, DEF);
    if (url === `/runs/${RUN.id}/stream`) return new Response(new ReadableStream({ start(c) { c.close(); } }), { status: 200 });
    return json(404, { detail: "no route" });
  });
}

describe("run id copy", () => {
  it("copies the FULL run id to the clipboard and says so", async () => {
    server();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    window.history.pushState(null, "", `/ui/runs/${RUN.id}`);
    render(<App />);
    const btn = await screen.findByRole("button", { name: "copy run id" });
    expect(btn).toHaveTextContent("01234567");                 // the prefix stays the visible label
    await userEvent.click(btn);
    expect(writeText).toHaveBeenCalledWith(RUN.id);
    expect(await screen.findByText("copied")).toBeInTheDocument();
  });
});

describe("inbox lock note", () => {
  it("is an action: clicking it focuses the principal input in the top bar", async () => {
    server();
    render(<App />);
    await screen.findByRole("listitem");
    const note = screen.getByRole("note");
    const go = within(note).getByRole("button", { name: /enter your id/i });
    await userEvent.click(go);
    expect(screen.getByLabelText("principal id (unverified)")).toHaveFocus();
  });
});

describe("run load failure", () => {
  it("shows the API's error in a card with a link back to the runs list", async () => {
    server(404);
    window.history.pushState(null, "", `/ui/runs/${RUN.id}`);
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load run: 404 run not found");
    const back = screen.getByRole("link", { name: /back to runs/i });
    await userEvent.click(back);
    await waitFor(() => expect(screen.getByRole("heading", { name: /^Runs/ })).toBeInTheDocument());
  });
});
