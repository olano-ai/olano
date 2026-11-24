// Shared rendering helpers for tool-call lines, used by BOTH chat surfaces —
// the dashboard's ChatView and the simple shell's SimpleChatView. They live in
// their own module (not in ChatView.tsx) because the simple shell is its own
// route chunk: importing them from ChatView would drag the whole 2,800-line
// view into the consumer bundle for two ten-line functions.

/** A short peek of a longer string: first `head` words + last `tail` words, so a
 * non-verbose tool line gives a glimpse of what's happening without the wall. */
export function peekText(text: string, head = 20, tail = 10): string {
  const words = text.trim().replace(/\s+/g, " ").split(" ").filter(Boolean);
  if (words.length <= head + tail) return words.join(" ");
  return `${words.slice(0, head).join(" ")} … ${words.slice(-tail).join(" ")}`;
}

/** The `task` tool launches a subagent; its args carry the subagent + brief. */
export function parseTaskArgs(args: string): { subagent: string; description: string } {
  try {
    const obj = JSON.parse(args) as Record<string, unknown>;
    if (obj && typeof obj === "object") {
      return {
        subagent: String(obj.subagent_type || obj.subagentType || ""),
        description: String(obj.description || obj.task || ""),
      };
    }
  } catch {
    // args isn't clean JSON — fall back to the raw string
  }
  return { subagent: "", description: args };
}
