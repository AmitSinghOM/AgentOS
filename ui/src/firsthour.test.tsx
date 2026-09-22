/**
 * First-hour path (DEBATE.md G5, DEBATE-2.md H8): the two empty states teach. An empty inbox
 * says how to CAUSE a gate and points at the tutorial's gated run; an empty runs list points at
 * the same tutorial. Neither state invents anything: the words name the shipped primitives
 * (`declared_effects`, the `spend` class, `POST /workflows/{name}/runs`, docs/tutorial.md).
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  window.sessionStorage.setItem("agentos.token", "tok");
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/me") return json(200, { mode: "bearer", principal: { kind: "human", id: "amit" } });
    if (url === "/approvals") return json(200, { data: [] });
    if (url.startsWith("/runs?")) return json(200, { data: [] });
    return json(404, { detail: `no route ${url}` });
  });
});
afterEach(() => { vi.restoreAllMocks(); window.history.pushState(null, "", "/ui/"); });

describe("G5 empty states teach the first hour", () => {
  it("empty inbox says how to cause a gate and links the tutorial", async () => {
    window.history.pushState(null, "", "/ui/");
    render(<App />);
    const empty = await screen.findByRole("region", { name: /nothing is waiting/i });
    // how to cause one: an agent that DECLARES an effect class the budget gates
    expect(empty).toHaveTextContent(/declared_effects/);
    expect(empty).toHaveTextContent(/spend/);
    // the way there is the tutorial's gated run, not a guess
    const link = screen.getByRole("link", { name: /tutorial/i });
    expect(link).toHaveAttribute("href", expect.stringMatching(/docs\/tutorial\.md$/));
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("empty runs list links the same tutorial and still names the API call", async () => {
    window.history.pushState(null, "", "/ui/runs");
    render(<App />);
    const empty = await screen.findByRole("region", { name: /no runs yet/i });
    expect(empty).toHaveTextContent(/POST \/workflows\/\{name\}\/runs/);
    const link = screen.getByRole("link", { name: /tutorial/i });
    expect(link).toHaveAttribute("href", expect.stringMatching(/docs\/tutorial\.md$/));
  });
});
