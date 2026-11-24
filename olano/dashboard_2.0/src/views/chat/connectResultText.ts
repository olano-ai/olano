// Pure helpers behind ConnectResultCard, kept free of React so node's --test
// can import them.

/**
 * Whether a delivered outcome is a success. The magic-link store says "done";
 * an MCP OAuth sign-in used to deliver its session's own "authorized" instead,
 * and rows persisted that way are still in people's transcripts, so both count.
 */
export function resultSucceeded(status: string): boolean {
  return status === "done" || status === "authorized";
}

/**
 * The backend's sentence, as the card should show it.
 *
 * Drops the leading ✅/⚠️, because the card draws that as an icon, and the
 * `**bold**` markers, because the card renders plain text. The sentence itself
 * keeps both, deliberately: the same string is delivered verbatim to
 * Telegram/WhatsApp, where the emoji is the only status marker available and
 * the markers render as bold.
 */
export function cardText(text: string): string {
  return text.replace(/^\s*(?:✅|⚠️|⚠)\uFE0F?\s*/u, "").replace(/\*\*(.+?)\*\*/g, "$1");
}
