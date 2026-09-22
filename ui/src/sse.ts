// Server-Sent Events over fetch. EventSource cannot send an Authorization header, and the
// token is never allowed in a URL, so the stream is read with fetch + a ReadableStream and
// parsed here. Honours `id:`, `event:`, `data:` (multi-line), comments (`:`), and frames split
// across chunks. Resume is the caller's job via `lastEventId` (→ Last-Event-ID).

import { ApiError, getToken } from "./api";

export interface SSEFrame { id: string | null; event: string | null; data: string }

export function createSSEParser(onFrame: (f: SSEFrame) => void) {
  let buffer = "";
  let id: string | null = null;
  let event: string | null = null;
  let data: string[] = [];

  const dispatch = () => {
    if (data.length === 0 && event === null && id === null) return;
    if (data.length > 0) onFrame({ id, event, data: data.join("\n") });
    event = null;
    data = [];
    // `id` persists across frames per the spec (last event id), so it is NOT reset.
  };

  const line = (l: string) => {
    if (l === "") { dispatch(); return; }
    if (l.startsWith(":")) return;                       // comment / keep-alive
    const colon = l.indexOf(":");
    const field = colon === -1 ? l : l.slice(0, colon);
    let value = colon === -1 ? "" : l.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") data.push(value);
    // `retry` and unknown fields are ignored
  };

  return {
    push(chunk: string) {
      buffer += chunk.replace(/\r\n/g, "\n");
      let nl: number;
      while ((nl = buffer.indexOf("\n")) !== -1) {
        line(buffer.slice(0, nl));
        buffer = buffer.slice(nl + 1);
      }
    },
    end() { if (buffer) { line(buffer); buffer = ""; } dispatch(); },
    get lastEventId() { return id; },
  };
}

export interface ReadSSEOptions {
  lastEventId?: string | null;
  signal?: AbortSignal;
  onFrame: (f: SSEFrame) => void;
}

/** Reads one connection to completion (the server closes on the terminal event or at its
 *  max duration). Returns the last event id seen so the caller can resume. Throws on a
 *  non-2xx response with the API's detail. */
export async function readSSE(url: string, opts: ReadSSEOptions): Promise<string | null> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.lastEventId) headers["Last-Event-ID"] = opts.lastEventId;
  const res = await fetch(url, { headers, signal: opts.signal, credentials: "omit" });
  if (!res.ok || !res.body) {
    // Same error shape as the JSON client, so the page shows "404 unknown run 'x'" whichever of
    // the run fetch or the stream reports first — not the raw JSON body from one of them.
    const text = await res.text().catch(() => "");
    let detail = text || res.statusText;
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed === "object" && parsed !== null && "detail" in parsed) detail = String((parsed as { detail: unknown }).detail);
    } catch { /* not JSON: keep the text */ }
    throw new ApiError(res.status, detail);
  }
  const parser = createSSEParser(opts.onFrame);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    parser.push(decoder.decode(value, { stream: true }));
  }
  parser.push(decoder.decode());
  parser.end();
  return parser.lastEventId ?? opts.lastEventId ?? null;
}
