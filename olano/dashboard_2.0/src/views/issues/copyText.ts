// What "Copy issue" puts on the clipboard: the whole thread as Markdown, so it
// pastes into a chat, another tracker or an agent's prompt in one piece. Each
// card contributes the Markdown it was written in, never the rendered text, so
// a list or a code block survives the trip. Assign/label/close events are left
// out: the header line carries where they ended up.
//
// Pure (no React, no store) so node's --test can import it.

export interface CopyIssue {
  id: number;
  title: string;
  body: string;
  author: string;
  assignee: string;
  state: string;
  type: string;
  labels: string[];
  created_at: string;
}

export interface CopyEvent {
  kind: string;
  actor: string;
  ts: string;
  payload: Record<string, unknown>;
}

type T = (k: string, v?: Record<string, string>) => string;

/** `2026-10-16 22:49 UTC`: one zone for everyone the paste reaches. The raw value when it does not parse. */
export function utcStamp(iso: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  return `${new Date(ms).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function issueMarkdown(issue: CopyIssue, events: CopyEvent[], t: T): string {
  const meta = [
    t(`state.${issue.state}`),
    issue.type,
    t("copy.md.opened", { who: issue.author, when: utcStamp(issue.created_at) }),
    issue.assignee ? t("copy.md.assignee", { who: issue.assignee }) : t("detail.unassigned"),
    issue.labels.length ? t("copy.md.labels", { labels: issue.labels.join(", ") }) : "",
  ].filter(Boolean);
  const parts = [
    `# ${issue.title} (#${issue.id})`,
    meta.join(" · "),
    issue.body.trim() || `_${t("detail.noDescription")}_`,
  ];
  for (const event of events) {
    if (event.kind !== "comment") continue;
    parts.push(
      "---",
      `**${event.actor}** ${t("copy.md.commented", { when: utcStamp(event.ts) })}`,
      String(event.payload.body ?? "").trim(),
    );
  }
  return `${parts.join("\n\n")}\n`;
}
