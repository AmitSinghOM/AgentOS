import { Inbox } from "./Inbox";
import { RunGraph } from "./RunGraph";
import { RunList } from "./RunList";
import { Link, useRoute } from "./router";
import { ActingAs, TokenForm, Unreachable, useSession } from "./Session";

export function App() {
  const session = useSession();
  const [route, navigate] = useRoute();
  // Only a 401 means "needs a token"; a network error or 5xx is the API not answering.
  const needsToken = !session.loading && session.me === null && !session.unreachable;
  const apiDown = !session.loading && session.me === null && session.unreachable;
  return (
    <main className="app">
      <header className="masthead">
        <div className="brand">
          <h1>AgentOS</h1>
          {session.me && (
            <span className={`chip chip--mode chip--${session.me.mode}`} title="AGENTOS_AUTH mode reported by GET /me">
              {session.me.mode === "bearer" ? "bearer auth" : "asserted auth"}
            </span>
          )}
        </div>
        <p className="tagline">Every decision and every step here is what the run's event log says — nothing more.</p>
        {session.me && (
          <nav aria-label="primary">
            <Link to={{ page: "inbox" }} navigate={navigate} className={route.page === "inbox" ? "active" : ""}>Approvals</Link>
            <Link to={{ page: "runs" }} navigate={navigate} className={route.page !== "inbox" ? "active" : ""}>Runs</Link>
          </nav>
        )}
      </header>
      {session.loading && <p>Connecting…</p>}
      {apiDown && <Unreachable session={session} />}
      {needsToken && <TokenForm session={session} />}
      {session.me && (
        <>
          <ActingAs session={session} />
          {route.page === "inbox" && <Inbox session={session} />}
          {route.page === "runs" && <RunList navigate={navigate} />}
          {route.page === "run" && <RunGraph runId={route.id} navigate={navigate} />}
        </>
      )}
    </main>
  );
}
