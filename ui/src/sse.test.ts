import { describe, expect, it } from "vitest";
import { createSSEParser, SSEFrame } from "./sse";

function collect(chunks: string[]): SSEFrame[] {
  const out: SSEFrame[] = [];
  const p = createSSEParser((f) => out.push(f));
  for (const c of chunks) p.push(c);
  p.end();
  return out;
}

describe("SSE parser", () => {
  it("parses id/event/data frames exactly as the API formats them", () => {
    const frames = collect(['id: 3\nevent: step.started\ndata: {"seq":3}\n\n']);
    expect(frames).toEqual([{ id: "3", event: "step.started", data: '{"seq":3}' }]);
  });

  it("reassembles a frame split across arbitrary chunk boundaries", () => {
    const whole = 'id: 7\nevent: run.completed\ndata: {"seq":7,"event_type":"run.completed"}\n\n';
    for (let cut = 1; cut < whole.length - 1; cut++) {
      const frames = collect([whole.slice(0, cut), whole.slice(cut)]);
      expect(frames).toEqual([{ id: "7", event: "run.completed", data: '{"seq":7,"event_type":"run.completed"}' }]);
    }
  });

  it("joins multi-line data with newlines and ignores comments and unknown fields", () => {
    const frames = collect([": keep-alive\n\nretry: 5000\ndata: a\ndata: b\n\n: max duration reached\n\n"]);
    expect(frames).toEqual([{ id: null, event: null, data: "a\nb" }]);
  });

  it("carries the last id forward and exposes it for Last-Event-ID", () => {
    const out: SSEFrame[] = [];
    const p = createSSEParser((f) => out.push(f));
    p.push("id: 1\ndata: x\n\ndata: y\n\n");
    expect(out.map((f) => f.id)).toEqual(["1", "1"]);
    expect(p.lastEventId).toBe("1");
  });

  it("treats CRLF like LF and a value without a leading space", () => {
    expect(collect(["id:9\r\ndata:z\r\n\r\n"])).toEqual([{ id: "9", event: null, data: "z" }]);
  });

  it("emits nothing for a frame with no data", () => {
    expect(collect(["event: ping\n\n"])).toEqual([]);
  });
});
