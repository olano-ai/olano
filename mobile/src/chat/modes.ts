/**
 * How the agent goes about a request (the dashboard's approval modes, on the
 * `mode` of every `POST /api/chat/stream`):
 *
 * - **auto**: it does the work, running tools without asking (actions that
 *   always need a person, like a payment or a send from the browser, still ask);
 * - **plan**: it drafts a plan first and does nothing until "Approve & run";
 * - **manual**: it asks before every step.
 *
 * Approving a plan sends one message in **act** mode (carry it out, every tool
 * allowed), then the conversation goes back to auto. Kept per agent on this
 * phone, like the dashboard keeps it per agent in the browser.
 */

export type ChatMode = "auto" | "plan" | "manual";

export const MODES: { mode: ChatMode; label: string; help: string }[] = [
  { mode: "auto", label: "Auto", help: "Does the work, running tools without asking." },
  { mode: "plan", label: "Plan first", help: "Drafts a plan and waits for your OK before doing anything." },
  { mode: "manual", label: "Ask before each step", help: "Asks you before every tool it runs." },
];

/** What approving a plan says to the agent (the dashboard's words). */
export const PLAN_APPROVED = "Approved. Execute the plan now.";

const KEY = "olano.mode:";

export function parseMode(value: unknown): ChatMode {
  return value === "plan" || value === "manual" ? value : "auto";
}

export function savedMode(agent: string): ChatMode {
  try {
    return parseMode(localStorage.getItem(KEY + agent));
  } catch {
    return "auto";
  }
}

export function saveMode(agent: string, mode: ChatMode): void {
  try {
    if (mode === "auto") localStorage.removeItem(KEY + agent);
    else localStorage.setItem(KEY + agent, mode);
  } catch {
    // Not remembered; it still applies to this conversation.
  }
}

export function modeLabel(mode: ChatMode): string {
  return MODES.find((m) => m.mode === mode)?.label ?? "Auto";
}
