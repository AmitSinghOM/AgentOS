/**
 * H6 (DEBATE-2): an opt-in browser notification when a NEW gate arrives. It carries no authority:
 * the text names the workflow, step and effect class, and clicking it opens the run page, where the
 * decision is made the same way as always. Off unless the operator turned it on (a click, which is
 * also the user gesture the permission prompt needs); the preference lives in localStorage because
 * it is a preference of this browser, not a credential (the token stays in sessionStorage).
 */
import type { Approval } from "./api";
import { approvalKey } from "./ApprovalCard";
import type { Route } from "./router";

export const NOTIFY_PREF_KEY = "agentos.notify";

export function notificationsSupported(): boolean {
  return typeof Notification !== "undefined" && Notification !== null;
}

export function notifyPref(): boolean {
  return window.localStorage.getItem(NOTIFY_PREF_KEY) === "1";
}
export function setNotifyPref(on: boolean): void {
  if (on) window.localStorage.setItem(NOTIFY_PREF_KEY, "1");
  else window.localStorage.removeItem(NOTIFY_PREF_KEY);
}

/** Turn it on: ask the browser if it has not been asked. Returns the resulting preference. */
export async function enableNotifications(): Promise<boolean> {
  if (!notificationsSupported()) return false;
  let permission = Notification.permission;
  if (permission === "default") permission = await Notification.requestPermission();
  const on = permission === "granted";
  setNotifyPref(on);
  return on;
}

/** Which of `next` were not in `prev`. `prev === null` means "first read of this page load", and
 *  nothing is announced: the badge already shows what was pending when the tab opened. */
export function newArrivals(prev: Set<string> | null, next: Approval[]): Approval[] {
  if (prev === null) return [];
  return next.filter((a) => !prev.has(approvalKey(a)));
}

export function announce(a: Approval, navigate: (r: Route) => void): void {
  if (!notificationsSupported() || Notification.permission !== "granted") return;
  const what = a.kind === "cost" ? `cost ceiling (${a.cost_at_request ?? "?"} → ${a.proposed_ceiling ?? "?"})`
    : `step "${a.step_id}" · ${a.effect_classes.join(", ")}`;
  const n = new Notification("AgentOS: approval waiting", {
    body: `${a.workflow} · run ${a.run_id.slice(0, 8)} · ${what}`,
    tag: `agentos:${a.approval_id}`,          // one per gate; a re-poll never re-fires it
  });
  n.onclick = () => { window.focus(); navigate({ page: "run", id: a.run_id }); n.close(); };
}
