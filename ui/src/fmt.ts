// Display formatting only. Every function degrades to the raw input when it cannot parse, so a
// test fixture like `occurred_at: "t3"` or an unexpected server value renders as itself rather
// than as "Invalid Date". Nothing here derives state — that is the server's fold.

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

/** ISO instant → local wall-clock `HH:MM:SS.mmm`; the full ISO string belongs in a title. */
export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** ISO instant → local `YYYY-MM-DD HH:MM:SS` for lists where the day matters. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** "12 s ago", "3 min ago", "2 h ago", "4 d ago" relative to `now`; raw input when unparsable. */
export function fmtAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

/** Milliseconds between two ISO instants as "+12 ms" / "+2.30 s" / "+1 min 5 s"; null if either
 *  side is unparsable. Used for the per-row gap in the timeline. */
export function fmtDelta(prevIso: string | null | undefined, iso: string | null | undefined): string | null {
  if (!prevIso || !iso) return null;
  const a = new Date(prevIso).getTime(), b = new Date(iso).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  const ms = b - a;
  const sign = ms < 0 ? "−" : "+";
  const abs = Math.abs(ms);
  if (abs < 1000) return `${sign}${abs} ms`;
  if (abs < 60_000) return `${sign}${(abs / 1000).toFixed(2)} s`;
  return `${sign}${Math.floor(abs / 60_000)} min ${Math.round((abs % 60_000) / 1000)} s`;
}

/** First 12 hex of a digest for display; the full value goes in a title. */
export function shortHash(h: string | null | undefined, n = 12): string {
  return h ? h.slice(0, n) : "—";
}

/** `run.started` → "run", `step.completed` → "step": the family colours the timeline dot. */
export function eventFamily(type: string): string {
  const i = type.indexOf(".");
  return i === -1 ? type : type.slice(0, i);
}
