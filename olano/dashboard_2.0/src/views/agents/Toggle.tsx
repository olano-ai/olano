// Labelled config rows for the agent Overview tab. The pill switch itself is
// the shared app-wide one (ui/Toggle) — re-exported so this view's existing
// `import { Toggle } from "./Toggle"` call sites keep working.

import type { ReactNode } from "react";

import { settingAnchor } from "../../core/settingAnchor";

export { Toggle } from "../../ui/Toggle";

// A labelled key/value row: label (+ optional hint) on the left, the control on
// the right, and an optional full-width `note` underneath. `note` is where the
// per-option explanations for a dropdown go — they need the row's full width,
// which the narrow left column can't give them.
export function KvRow({
  label,
  hint,
  note,
  children,
  settingId,
}: {
  label: ReactNode;
  hint?: ReactNode;
  note?: ReactNode;
  children: ReactNode;
  // Anchor id for the command palette — see core/settingAnchor.ts.
  settingId?: string;
}) {
  return (
    <div
      style={{ padding: "9px 0", borderBottom: "1px solid var(--border)" }}
      {...settingAnchor(settingId)}
    >
      <div className="row" style={{ justifyContent: "space-between", gap: 14 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13 }}>{label}</div>
          {hint ? (
            <div className="muted" style={{ fontSize: 11, marginTop: 1, lineHeight: 1.4 }}>
              {hint}
            </div>
          ) : null}
        </div>
        <div style={{ flexShrink: 0 }}>{children}</div>
      </div>
      {note}
    </div>
  );
}

// (labelKey, textKey) pair for one option's explanation. Both halves live in
// strings.ts so both get translated; `noteItems` resolves them for OptionNotes.
export type NoteKeys = [labelKey: string, textKey: string];

export function noteItems(t: (key: string) => string, pairs: NoteKeys[]) {
  return pairs.map(([labelKey, textKey]) => ({ label: t(labelKey), text: t(textKey) }));
}

// Per-option explanations, rendered under the control they belong to. An option
// label is far too short to say what the choice actually does ("strictest"
// tells nobody which tools start asking for approval), so every select in the
// agent config pairs its label with one of these lists.
export function OptionNotes({ items }: { items: { label: string; text: string }[] }) {
  return (
    <ul
      className="muted"
      style={{
        listStyle: "none",
        margin: "8px 0 0",
        padding: 0,
        display: "flex",
        flexDirection: "column",
        gap: 5,
        fontSize: 11,
        lineHeight: 1.45,
      }}
    >
      {items.map((it) => (
        <li key={it.label}>
          <strong style={{ color: "var(--text-2)", fontWeight: 600 }}>{it.label}</strong> — {it.text}
        </li>
      ))}
    </ul>
  );
}
