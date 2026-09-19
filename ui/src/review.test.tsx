/**
 * Package review v0.11.0 — two failure paths an operator actually hits:
 *   - A4: a malformed run id in the URL lands on the runs list; it must not throw in render
 *   - A5: an API that does not answer (network error / 5xx) says so with Retry — it must not
 *         show the sign-in form, which is the answer to a 401 only
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { parseRoute } from "./router";

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("router (A4)", () => {
  it("parses well-formed routes", () => {
    expect(parseRoute("/ui/")).toEqual({ page: "inbox" });
    expect(parseRoute("/ui/runs")).toEqual({ page: "runs" });
    expect(parseRoute("/ui/runs/abc%20d")).toEqual({ page: "run", id: "abc d" });
  });
  it("does not throw on malformed percent-encoding; falls back to the runs list", () => {
    expect(() => parseRoute("/ui/runs/%E0%A4%A")).not.toThrow();
    expect(parseRoute("/ui/runs/%E0%A4%A")).toEqual({ page: "runs" });
  });
});

describe("API not answering (A5)", () => {
  beforeEach(() => { window.sessionStorage.clear(); window.history.pushState(null, "", "/ui/"); });
  afterEach(() => { vi.restoreAllMocks(); });

  it("a network error shows 'not answering' + Retry, never the token form", async () => {
    let down = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (down) throw new TypeError("Failed to fetch");
      if (String(input) === "/me") return json(200, { mode: "asserted", principal: null });
      if (String(input) === "/approvals") return json(200, { data: [] });
      return json(404, { detail: "no route" });
    });
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The API is not answering: Failed to fetch");
    expect(screen.queryByLabelText("bearer token")).not.toBeInTheDocument();
    down = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Unverified"));
  });

  it("a 500 from /me is also 'not answering', while a 401 is the token form", async () => {
    let status = 500;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      json(status, { detail: status === 500 ? "store unavailable" : "missing bearer token" }));
    const { unmount } = render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("500: store unavailable");
    expect(screen.queryByLabelText("bearer token")).not.toBeInTheDocument();
    unmount();
    status = 401;
    render(<App />);
    await screen.findByLabelText("bearer token");
    expect(screen.queryByText(/not answering/)).not.toBeInTheDocument();
  });
});
