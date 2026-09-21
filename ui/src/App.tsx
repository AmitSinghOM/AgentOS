import { useEffect, useState } from "react";
import { Inbox } from "./Inbox";
import { RunGraph } from "./RunGraph";
import { RunList } from "./RunList";
import { api } from "./api";
import { Link, useRoute } from "./router";
import { ActingAs, TokenForm, Unreachable, useSession } from "./Session";

const PENDING_POLL_MS = 15_000;   // the badge is a hint, not the inbox; /approvals folds every non-terminal run

/** The pending-approval count for the nav badge and the tab title. The inbox has its own
 *  poll for its list; this one is the shell's, so the badge is right on every page. */
function usePendingCount(enabled: boolean, paused: boolean): [number | null, (n: number) => void] {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    if (!enabled) { setCount(null); return; }
    if (paused) return;                 // the inbox is mounted and reports the count itself
    let alive = true;
    const load = async () => {
      try {
        const res = await api.approvals();
        if (alive) setCount(res.data.length);
      } catch {
        if (alive) setCount(null);   // the inbox itself reports the error; the badge just goes quiet
      }
    };
    void load();
    const t = window.setInterval(() => { void load(); }, PENDING_POLL_MS);
    return () => { alive = false; window.clearInterval(t); };
  }, [enabled, paused]);
  return [count, setCount];
}

export function App() {
  const session = useSession();
  const [route, navigate] = useRoute();
  // Only a 401 means "needs a token"; a network error or 5xx is the API not answering.
  const needsToken = !session.loading && session.me === null && !session.unreachable;
  const apiDown = !session.loading && session.me === null && session.unreachable;
  const [pending, setPending] = usePendingCount(session.me !== null, route.page === "inbox");

  useEffect(() => {
    const base = route.page === "inbox" ? "Approvals" : route.page === "runs" ? "Runs" : `Run ${route.id.slice(0, 8)}`;
    document.title = pending ? `(${pending}) ${base} — AgentOS` : `${base} — AgentOS`;
  }, [route, pending]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar__inner">
          <div className="brand">
            <span className="brand__mark" aria-hidden="true" />
            <h1>AgentOS</h1>
            {session.me && (
              <span className={`chip chip--mode chip--${session.me.mode}`} title="AGENTOS_AUTH mode reported by GET /me">
                {session.me.mode === "bearer" ? "bearer auth" : "asserted auth"}
              </span>
            )}
          </div>
          {session.me && (
            <nav aria-label="primary">
              <Link to={{ page: "inbox" }} navigate={navigate} className={route.page === "inbox" ? "active" : ""}>
                Approvals
                {pending ? <span className="badge" aria-label={`${pending} pending`}>{pending}</span> : null}
              </Link>
              <Link to={{ page: "runs" }} navigate={navigate} className={route.page !== "inbox" ? "active" : ""}>Runs</Link>
            </nav>
          )}
          {session.me && <ActingAs session={session} />}
        </div>
      </header>
      <main className="app">
        {session.loading && <p className="muted state state--inline">Connecting to the API…</p>}
        {apiDown && <Unreachable session={session} />}
        {needsToken && <TokenForm session={session} />}
        {session.me && (
          <>
            {route.page === "inbox" && <Inbox session={session} onPending={setPending} />}
            {route.page === "runs" && <RunList navigate={navigate} />}
            {route.page === "run" && <RunGraph runId={route.id} navigate={navigate} />}
          </>
        )}
      </main>
      <footer className="foot">
        Every decision and every step shown here is what the run's event log says — nothing more.
        The browser never folds events or authorizes anything; it renders the API's fold.
      </footer>
    </div>
  );
}
