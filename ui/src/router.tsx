// Two routes under /ui; the API serves index.html for any /ui/* so real URLs work on reload.
import { useCallback, useEffect, useState } from "react";

export type Route = { page: "inbox" } | { page: "runs" } | { page: "run"; id: string };

const BASE = "/ui";

export function parseRoute(pathname: string): Route {
  const rest = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname;
  const parts = rest.split("/").filter(Boolean);
  if (parts[0] === "runs" && parts[1]) {
    // A malformed id (`%E0%A4%A`) must land on the list, not throw inside a render initializer
    // (there is no error boundary above the router; a throw here is a blank page).
    try {
      return { page: "run", id: decodeURIComponent(parts[1]) };
    } catch {
      return { page: "runs" };
    }
  }
  if (parts[0] === "runs") return { page: "runs" };
  return { page: "inbox" };
}

export function href(route: Route): string {
  if (route.page === "run") return `${BASE}/runs/${encodeURIComponent(route.id)}`;
  if (route.page === "runs") return `${BASE}/runs`;
  return `${BASE}/`;
}

export function useRoute(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));
  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((r: Route) => {
    window.history.pushState(null, "", href(r));
    setRoute(r);
  }, []);
  return [route, navigate];
}

export function Link({ to, navigate, children, className }: {
  to: Route; navigate: (r: Route) => void; children: React.ReactNode; className?: string;
}) {
  return (
    <a href={href(to)} className={className}
       onClick={(e) => { if (!e.metaKey && !e.ctrlKey) { e.preventDefault(); navigate(to); } }}>
      {children}
    </a>
  );
}
