// Browser actions a person must approve every time: a form submit, purchase,
// payment, send or delete the agent attempted in its browser. Mirrors
// runner._always_asks_human on the server (olano/browser/policy.py
// ALWAYS_ASK_TOOL_NAMES; tests/test_browser_api_and_gating.py pins the two): those approvals are never auto-approved there, and the
// chat's own "Accept all" shortcut must not approve them here either, or a web
// page's injected instruction could ride a blanket approval.
export const BROWSER_APPROVAL_TOOLS: ReadonlySet<string> = new Set([
  "browser_navigate",
  "browser_snapshot",
  "browser_click",
  "browser_type",
  "browser_fill",
  "browser_press_key",
  "browser_extract",
  "browser_screenshot",
  "browser_record",
  "browser_request_user",
  "browser_close",
  "browser_get_auto_approvals",
  "browser_set_auto_approvals",
  "browser_navigate_and_screenshot",
  "page_screenshot",
  "olano_set_browser_auto_approvals",
]);

export function needsPersonEveryTime(actions: { name?: string }[]): boolean {
  return actions.some((a) => BROWSER_APPROVAL_TOOLS.has(String(a.name || "")));
}
