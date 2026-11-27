/** Rows of the Multi-user tab's allowed-actions editor, kept pure so node's
 * test runner can import it (no React, no store). */

export type ActionRow = { name: string; script: string; draft?: boolean };

export function rowsFromActions(actions: Record<string, string>): ActionRow[] {
  return Object.entries(actions).map(([name, script]) => ({ name, script }));
}

/** The rows to show after the saved config reloads. A reload (any save on the
 * tab, a refresh) used to replace every row, wiping one the person had just
 * added and not saved. Local drafts survive unless the saved list now holds
 * their name (their own save landed). */
export function mergeActionRows(prev: ActionRow[], actions: Record<string, string>): ActionRow[] {
  const drafts = prev.filter((r) => r.draft && !Object.hasOwn(actions, r.name.trim()));
  return [...rowsFromActions(actions), ...drafts];
}

/** The rows as they would be saved: an untouched empty row is not a change
 * (the save skips it too), and the draft flag is display state only. */
export function savedShape(rows: ActionRow[]): { name: string; script: string }[] {
  return rows
    .filter((r) => r.name.trim() || r.script.trim())
    .map(({ name, script }) => ({ name, script }));
}
