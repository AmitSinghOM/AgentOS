import { useEffect, useRef, useState } from "react";
import type { EventRecord } from "./derive";
import { salient } from "./derive";
import { eventFamily, fmtClock, fmtDelta } from "./fmt";

/** A drag across an n-event run must not become n server folds: the thumb's seq is shown
 *  instantly, the seek (`GET /runs/{id}?at=k`, O(k) on the server) fires once the thumb rests. */
export const SEEK_DEBOUNCE_MS = 150;

/** The event log with a scrubber. `at` is the seq whose state the graph shows (null = live).
 *  Seeking asks the SERVER for the fold through that seq (`GET /runs/{id}?at=k`) — the
 *  browser never folds. */
export function Timeline({ events, lastSeq, at, onSeek }: {
  events: EventRecord[]; lastSeq: number; at: number | null; onSeek: (seq: number | null) => void;
}) {
  // `pending` is the thumb position while dragging; committed to onSeek after the debounce.
  const [pending, setPending] = useState<number | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);
  useEffect(() => { setPending(null); }, [at]);   // the parent moved (seek landed / back to live)

  const scrub = (seq: number) => {
    setPending(seq);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { timer.current = null; onSeek(seq); }, SEEK_DEBOUNCE_MS);
  };
  const seekNow = (seq: number | null) => {
    if (timer.current !== null) { window.clearTimeout(timer.current); timer.current = null; }
    setPending(null);
    onSeek(seq);
  };

  const shown = pending ?? at ?? lastSeq;
  return (
    <section aria-labelledby="timeline-heading" className="timeline">
      <h3 id="timeline-heading">Timeline</h3>
      <div className="timeline__controls">
        <label>
          Viewing seq{" "}
          <input type="range" min={1} max={Math.max(1, lastSeq)} value={shown}
                 aria-label="time travel scrubber" aria-valuetext={`seq ${shown} of ${lastSeq}`}
                 onChange={(e) => scrub(Number(e.target.value))} />
          {" "}<strong>{shown}</strong> of {lastSeq}
        </label>
        {at !== null && (
          <>
            {" "}<span role="status" className="time-travel">time travel — the graph shows the run as it was right after seq {at}</span>
            {" "}<button type="button" onClick={() => seekNow(null)}>Back to live</button>
          </>
        )}
      </div>
      <table className="events" aria-label="event log">
        <thead><tr><th>seq</th><th>event</th><th>step</th><th>detail</th><th className="num">at</th><th className="num">gap</th></tr></thead>
        <tbody>
          {events.map((e, i) => {
            const gap = i > 0 ? fmtDelta(events[i - 1].occurred_at, e.occurred_at) : null;
            return (
              <tr key={e.seq} className={e.seq === shown ? "current" : e.seq > shown ? "future" : ""}
                  aria-current={e.seq === shown ? "step" : undefined}>
                <td><button type="button" className="seek" onClick={() => seekNow(e.seq)} aria-label={`view state after seq ${e.seq}`}>{e.seq}</button></td>
                <td><span className={`dot dot--${eventFamily(e.event_type)}`} aria-hidden="true" /><code>{e.event_type}</code></td>
                <td>{e.step_id ? <span className="chip">{e.step_id}</span> : ""}</td>
                <td className="detail">{salient(e)}</td>
                <td className="muted num" title={e.occurred_at}><time dateTime={e.occurred_at}>{fmtClock(e.occurred_at)}</time></td>
                <td className="muted num">{gap ?? ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
