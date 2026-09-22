/**
 * H5 (DEBATE-2): word the non-human warning from what the API will actually do, when that is
 * knowable — the workflow's own budget (`GET /workflows/{name}`) and the operator policy
 * (`GET /policy`). engine.approve refuses a non-human on a human-only class unless the EFFECTIVE
 * budget allows agent approval, and the effective budget is the workflow's ∩ the policy's
 * (policy.py `effective_budget`: allow_agent_approval is narrowed to false when the policy says
 * agent_approval_allowed=false). This module computes the sentence; it never disables the button.
 * Policy can change between fetch and click, so the API remains the decider (the pass-1 pause lesson).
 */
import { PolicyDoc, api } from "./api";
import type { WorkflowDef } from "./graph";

let policyOnce: Promise<PolicyDoc> | null = null;
/** One read of /policy per page load: it is one document for the whole deployment. */
export function policy(): Promise<PolicyDoc> {
  policyOnce ??= api.policy().catch((e) => { policyOnce = null; throw e; });
  return policyOnce;
}
/** Test seam. */
export function resetPolicyCache(): void { policyOnce = null; }

export type AgentApprovalVerdict =
  | { known: false }
  | { known: true; accept: true }
  | { known: true; accept: false; workflowDenies: boolean; policyDenies: boolean };

export function agentApprovalVerdict(def: WorkflowDef | null, pol: PolicyDoc | null): AgentApprovalVerdict {
  if (!def || !pol) return { known: false };
  const workflowAllows = def.budget?.allow_agent_approval === true;   // Budget default is false
  const policyAllows = pol.policy === null || pol.policy.agent_approval_allowed !== false;   // OperatorPolicy default is true
  if (workflowAllows && policyAllows) return { known: true, accept: true };
  return { known: true, accept: false, workflowDenies: !workflowAllows, policyDenies: !policyAllows };
}

export function agentApprovalSentence(v: AgentApprovalVerdict, workflow: string): string | null {
  if (!v.known) return null;
  if (v.accept) return `The API will accept this: workflow "${workflow}" allows agent approval and the operator policy permits it.`;
  if (v.workflowDenies && v.policyDenies) {
    return `The API will refuse this: workflow "${workflow}" does not allow agent approval, and the operator policy forbids it too.`;
  }
  if (v.policyDenies) return `The API will refuse this: the operator policy forbids agent approval (workflow "${workflow}" allows it).`;
  return `The API will refuse this: workflow "${workflow}" does not allow agent approval.`;
}
