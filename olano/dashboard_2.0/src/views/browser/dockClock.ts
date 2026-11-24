// Pure helpers for the chat's browser window (no React), so node --test covers them.

/** "4:05" from seconds: how long the agent still waits for a person to pass a check. */
export function clockText(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
