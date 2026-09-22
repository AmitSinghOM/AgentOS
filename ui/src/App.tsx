import { useCallback, useEffect, useRef, useState } from "react";
import { Inbox } from "./Inbox";
import { RunGraph } from "./RunGraph";
import { RunList } from "./RunList";
import { Approval, api } from "./api";
import { approvalKey } from "./ApprovalCard";
import { announce, enableNotifications, newArrivals, notificationsSupported, notifyPref, setNotifyPref } from "./notify";
import { Link, useRoute } from "./router";
import type { Route } from "./router";
import { ActingAs, TokenForm, Unreachable, useSession } from "./Session";

const PENDING_POLL_MS = 15_000;   // the badge is a hint, not the inbox; /approvals folds every non-terminal run

/** The pending approvals for the nav badge and the tab title. The inbox has its own poll for its
 *  list and reports it upward while mounted; this one is the shell's, so the badge is right on
 *  every page. New arrivals since the page loaded are announced when the operator opted in (H6). */
function usePending(enabled: boolean, paused: boolean, navigate: (r: Route) => void): [number | null, (items: Approval[]) => void] {
  const [count, setCount] = useState<number | null>(null);
  const seenKeys = useRef<Set<string> | null>(null);       // null until the first read of this page load
  const report = useCallback((items: Approval[]) => {
    setCount(items.length);
    if (notifyPref()) newArrivals(seenKeys.current, items).forEach((a) => announce(a, navigate));
    seenKeys.current = new Set(items.map(approvalKey));
  }, [navigate]);
  useEffect(() => {
    if (!enabled) { setCount(null); seenKeys.current = null; return; }
    if (paused) return;                 // the inbox is mounted and reports the list itself
    let alive = true;
    const load = async () => {
      try {
        const res = await api.approvals();
        if (alive) report(res.data);
      } catch {
        if (alive) setCount(null);   // the inbox itself reports the error; the badge just goes quiet
      }
    };
    void load();
    const t = window.setInterval(() => { void load(); }, PENDING_POLL_MS);
    return () => { alive = false; window.clearInterval(t); };
  }, [enabled, paused, report]);
  return [count, report];
}

/** The opt-in switch (H6). Absent when the browser has no Notification API. Turning it on is the
 *  user gesture the permission prompt needs; a denied permission leaves it off and says so. */
function NotifySwitch() {
  const [on, setOn] = useState(() => notifyPref() && Notification.permission === "granted");
  const denied = Notification.permission === "denied";
  const toggle = async () => {
    if (on) { setNotifyPref(false); setOn(false); return; }
    setOn(await enableNotifications());
  };
  return (
    <button type="button" role="switch" aria-checked={on} className={`notify${on ? " notify--on" : ""}`}
            onClick={() => void toggle()} disabled={denied}
            title={denied ? "Notifications are blocked for this site in the browser; allow them there to use this."
              : on ? "A browser notification when a new approval arrives — it names the gate and opens the run; the decision is still made here."
                : "Notify me when a new approval arrives (browser notification, opt-in; carries no decision)."}>
      <span aria-hidden="true">{on ? "🔔" : "🔕"}</span> notify
    </button>
  );
}

export function App() {
  const session = useSession();
  const [route, navigate] = useRoute();
  // Only a 401 means "needs a token"; a network error or 5xx is the API not answering.
  const needsToken = !session.loading && session.me === null && !session.unreachable;
  const apiDown = !session.loading && session.me === null && session.unreachable;
  const [pending, reportPending] = usePending(session.me !== null, route.page === "inbox", navigate);

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
          {session.me && (
            <div className="topbar__right">
              {notificationsSupported() && <NotifySwitch />}
              <ActingAs session={session} />
            </div>
          )}
        </div>
      </header>
      <main className="app">
        {session.loading && <p className="muted state state--inline">Connecting to the API…</p>}
        {apiDown && <Unreachable session={session} />}
        {needsToken && <TokenForm session={session} />}
        {session.me && (
          <>
            {route.page === "inbox" && <Inbox session={session} onPending={reportPending} navigate={navigate} />}
            {route.page === "runs" && <RunList navigate={navigate} />}
            {route.page === "run" && <RunGraph runId={route.id} navigate={navigate} session={session} />}
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
