// The browser auto-approval rules as the Browser tab edits them. Pure (no
// React, no DOM) so node's --test can import it; AutoApprovals.tsx renders it.
// The server validates again (olano/config/agent_config.py BrowserAutoApproveRule)
// and is the authority: this only catches what a person can fix before saving.

export type ActionKind = "send" | "submit" | "signup";
export const ACTION_KINDS: readonly ActionKind[] = ["send", "submit", "signup"];

export interface AutoApproveRule {
  site: string;
  actions: ActionKind[];
  in_background: boolean;
}

/** "https://www.LinkedIn.com/feed/" → "linkedin.com": what a person pastes
 *  becomes the bare host name a rule takes. A leading "www." is dropped, since
 *  "the LinkedIn site" is what a person means and the bare name covers www.
 *  too. Leaves "*.example.com" alone. Mirrors auto_approve.normalize_site. */
export function normalizeSite(raw: string): string {
  let s = (raw || "").trim().toLowerCase();
  s = s.replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
  s = s.split(/[/?#]/, 1)[0];
  s = s.replace(/^[^@]*@/, "");
  s = s.replace(/:\d+$/, "").replace(/\.$/, "");
  s = s.replace(/^www\.(?=[^.]+\.)/, "");
  return s;
}

// The server's rule (agent_config._SITE_RE): a host name with a dot and a
// letters-only ending, optionally "*." in front.
const SITE_RE =
  /^(?:\*\.)?(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

export function siteIsValid(site: string): boolean {
  return SITE_RE.test(normalizeSite(site));
}

export type DraftProblem =
  | { key: "br.aa.needSite" }
  | { key: "br.aa.badSite"; site: string }
  | { key: "br.aa.dupSite"; site: string }
  | { key: "br.aa.needAction"; site: string };

/** What is wrong with one rule (given the rules before it), or null. */
export function ruleProblem(
  rule: AutoApproveRule,
  earlier: AutoApproveRule[] = [],
): DraftProblem | null {
  const site = normalizeSite(rule.site);
  if (!site) return { key: "br.aa.needSite" };
  if (!SITE_RE.test(site)) return { key: "br.aa.badSite", site: rule.site.trim() };
  if (earlier.some((r) => normalizeSite(r.site) === site)) return { key: "br.aa.dupSite", site };
  if (rule.actions.length === 0) return { key: "br.aa.needAction", site };
  return null;
}

/** The first thing stopping a save, or null. */
export function draftProblem(rules: AutoApproveRule[]): DraftProblem | null {
  for (let i = 0; i < rules.length; i++) {
    const problem = ruleProblem(rules[i], rules.slice(0, i));
    if (problem) return problem;
  }
  return null;
}

/** One-click starting points for a person who has never written a rule. */
export const EXAMPLES: readonly { id: string; rule: AutoApproveRule }[] = [
  { id: "linkedin", rule: { site: "linkedin.com", actions: ["send"], in_background: false } },
  { id: "x", rule: { site: "x.com", actions: ["send"], in_background: false } },
  { id: "calendly", rule: { site: "calendly.com", actions: ["signup"], in_background: false } },
];

/** What is sent to the server: normalized sites, actions in a stable order. */
export function cleanRules(rules: AutoApproveRule[]): AutoApproveRule[] {
  return rules.map((r) => ({
    site: normalizeSite(r.site),
    actions: ACTION_KINDS.filter((k) => r.actions.includes(k)),
    in_background: !!r.in_background,
  }));
}

export function sameRules(a: AutoApproveRule[], b: AutoApproveRule[]): boolean {
  return JSON.stringify(cleanRules(a)) === JSON.stringify(cleanRules(b));
}
