import { Inbox } from "./Inbox";
import { ActingAs, TokenForm, useSession } from "./Session";

export function App() {
  const session = useSession();
  const needsToken = !session.loading && session.me === null;
  return (
    <main className="app">
      <header>
        <h1>AgentOS</h1>
        <p className="tagline">Approvals inbox — every decision here is recorded in the run's event log with who made it.</p>
      </header>
      {session.loading && <p>Connecting…</p>}
      {needsToken && <TokenForm session={session} />}
      {session.me && (
        <>
          <ActingAs session={session} />
          <Inbox session={session} />
        </>
      )}
    </main>
  );
}
