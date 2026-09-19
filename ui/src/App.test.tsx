/**
 * The inbox, tested as the operator sees it. Network is a fetch stub (three requests; MSW is a
 * dependency this screen does not need). The rules under test:
 *   - without a token in bearer mode the screen asks for one and sends nothing else
 *   - the token goes to sessionStorage (not localStorage) and out as Authorization: Bearer
 *   - "Acting as" shows exactly what /me returned before any decision is possible
 *   - bearer mode: the decision body carries only `reason` — never a principal
 *   - asserted mode: the screen says UNVERIFIED, requires a typed principal, sends it
 *   - the API's error text (403, 409) is shown verbatim; nothing is decided client-side
 *   - a cost approval shows the proposed ceiling
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Approval } from "./api";

type Call = { method: string; path: string; headers: Record<string, string>; body: unknown };

const PENDING: Approval = {
  run_id: "abcdef1234567890", workflow: "payments", approval_id: "ap1", step_id: "pay",
  kind: "effect", effect_classes: ["spend"], status: "pending",
  requested_at: "2026-09-20T00:00:00Z", expires_at: null, reason: "step 'pay' declares spend",
};
const COST: Approval = {
  ...PENDING, approval_id: "ap2", step_id: null, kind: "cost", effect_classes: [],
  cost_at_request: "5.20", proposed_ceiling: "10.00", reason: "run cost 5.20 exceeds ceiling 5.00",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let calls: Call[];
let route: (c: Call) => Response | Promise<Response>;

beforeEach(() => {
  calls = [];
  window.sessionStorage.clear();
  window.localStorage.clear();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const c: Call = {
      method: init?.method ?? "GET", path: String(input),
      headers: (init?.headers as Record<string, string>) ?? {},
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(c);
    return route(c);
  });
});
afterEach(() => vi.restoreAllMocks());

function bearerServer(opts: { token: string; approvals?: Approval[]; onDecide?: (c: Call) => Response }) {
  const pending = [...(opts.approvals ?? [PENDING])];
  route = (c) => {
    const auth = c.headers.Authorization;
    if (auth !== `Bearer ${opts.token}`) return json(401, { detail: auth ? "unknown bearer token" : "missing bearer token" });
    if (c.path === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit", attestation: "token:sha256:abcdef123456" } });
    if (c.path === "/approvals") return json(200, { data: pending });
    if (c.method === "POST" && c.path.includes("/approvals/")) {
      if (opts.onDecide) return opts.onDecide(c);
      pending.length = 0;
      return json(202, { status: "running" });
    }
    return json(404, { detail: "no route" });
  };
}

describe("bearer mode", () => {
  it("asks for a token, keeps it in sessionStorage only, and sends it as Authorization: Bearer", async () => {
    bearerServer({ token: "s3cret" });
    render(<App />);
    const input = await screen.findByLabelText("bearer token");
    expect(input).toHaveAttribute("type", "password");
    expect(calls.every((c) => c.path === "/me")).toBe(true);        // nothing else was fetched
    await userEvent.type(input, "s3cret");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByText(/acting as/i);
    expect(window.sessionStorage.getItem("agentos.token")).toBe("s3cret");
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    const authed = calls.filter((c) => c.headers.Authorization);
    expect(authed.length).toBeGreaterThan(0);
    expect(authed.every((c) => c.headers.Authorization === "Bearer s3cret")).toBe(true);
  });

  it("shows the API's rejection of a wrong token verbatim and stays signed out", async () => {
    bearerServer({ token: "right" });
    render(<App />);
    await userEvent.type(await screen.findByLabelText("bearer token"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("token rejected: unknown bearer token");
    expect(screen.queryByText(/acting as/i)).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("agentos.token")).toBeNull();   // not re-sent on reload
  });

  it("shows who the log will name, lists the approval, and approves with a reason-only body", async () => {
    window.sessionStorage.setItem("agentos.token", "s3cret");
    bearerServer({ token: "s3cret" });
    render(<App />);
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Acting as amit (human, token:sha256:abcdef123456) — verified by the API");
    expect(screen.queryByText(/unverified/i)).not.toBeInTheDocument();
    const item = await screen.findByRole("listitem");
    expect(item).toHaveTextContent("payments");
    expect(item).toHaveTextContent('step "pay" declares spend');
    await userEvent.type(within(item).getByLabelText(/reason for pay/), "within budget");
    await userEvent.click(within(item).getByRole("button", { name: "Approve" }));
    const post = await waitFor(() => {
      const p = calls.find((c) => c.method === "POST");
      expect(p).toBeDefined();
      return p!;
    });
    expect(post.path).toBe("/runs/abcdef1234567890/approvals/ap1/approve");
    expect(post.body).toEqual({ reason: "within budget" });               // no principal, ever
    expect(await screen.findByText(/Approved pay on run abcdef12 as amit/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/nothing is waiting/i)).toBeInTheDocument());
  });

  it("renders a 403 from the API verbatim and leaves the approval pending", async () => {
    window.sessionStorage.setItem("agentos.token", "s3cret");
    bearerServer({ token: "s3cret", onDecide: () => json(403, { detail: "approval of spend requires a human principal; got agent 'bot'" }) });
    render(<App />);
    const item = await screen.findByRole("listitem");
    await userEvent.click(within(item).getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("approve failed — 403: approval of spend requires a human principal; got agent 'bot'");
    expect(screen.getByRole("listitem")).toHaveTextContent("payments");   // still listed
  });

  it("explains a cost approval as a ceiling raise and rejects with the reason", async () => {
    window.sessionStorage.setItem("agentos.token", "s3cret");
    bearerServer({ token: "s3cret", approvals: [COST] });
    render(<App />);
    const item = await screen.findByRole("listitem");
    expect(item).toHaveTextContent("run cost 5.20 exceeded its ceiling; approving raises it to 10.00");
    await userEvent.type(within(item).getByLabelText(/reason for cost/), "too much");
    await userEvent.click(within(item).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/runs/abcdef1234567890/approvals/ap2/reject");
    expect(post.body).toEqual({ reason: "too much" });
  });
});

describe("asserted mode", () => {
  beforeEach(() => {
    const pending = [PENDING];
    route = (c) => {
      if (c.path === "/me") return json(200, { mode: "asserted", principal: null });
      if (c.path === "/approvals") return json(200, { data: pending });
      if (c.method === "POST") { pending.length = 0; return json(202, { status: "running" }); }
      return json(404, { detail: "no route" });
    };
  });

  it("says UNVERIFIED, disables decisions until a principal is typed, then sends it", async () => {
    render(<App />);
    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Unverified");
    expect(banner).toHaveTextContent("AGENTOS_AUTH=asserted");
    const item = await screen.findByRole("listitem");
    expect(within(item).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("note")).toHaveTextContent(/disabled until the API knows who is deciding/);
    await userEvent.type(screen.getByLabelText("principal id (unverified)"), "amit");
    expect(within(item).getByRole("button", { name: "Approve" })).toBeEnabled();
    await userEvent.click(within(item).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ principal: { kind: "human", id: "amit" }, reason: "" });
    expect(post.headers.Authorization).toBeUndefined();
  });
});
