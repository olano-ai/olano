// The read-only handle line under a "name" field on every create form.
//
// Nobody chooses an agent's handle any more: a person names the agent
// ("Hillary") and the server mints the permanent key from that name (slug,
// then `-2`, `-3` if taken). This shows what that will be as they type, so the
// permanence of the key is visible before it exists — and says plainly when
// nothing can be previewed (an all-CJK name slugifies to nothing and the
// server falls back to the archetype's slug).
//
// Strings come from the caller: each shell has its own dictionary, and the
// three create surfaces (dashboard New agent, simple New bot, onboarding)
// word the same fact for different readers.

export function HandlePreview({
  handle,
  label,
  pending,
  hint,
}: {
  /** The slug preview; empty when nothing survives of the name yet. */
  handle: string;
  /** Row label, e.g. "Handle". */
  label: string;
  /** Shown in place of the handle while it is empty. */
  pending: string;
  /** The explanation under the row — what a handle is and that it is permanent. */
  hint: string;
}) {
  return (
    <div className="field" style={{ marginBottom: 14 }} data-testid="handle-preview">
      <label>{label}</label>
      <div
        className="input"
        aria-readonly="true"
        style={{
          fontFamily: "var(--font-mono, monospace)",
          fontSize: 13,
          color: handle ? "var(--text-2, var(--text))" : "var(--text-4, var(--text-3))",
          background: "var(--surface-2, var(--surface))",
          cursor: "default",
          userSelect: "text",
        }}
      >
        {handle || pending}
      </div>
      <div className="muted" style={{ fontSize: 11, lineHeight: 1.45 }}>
        {hint}
      </div>
    </div>
  );
}
