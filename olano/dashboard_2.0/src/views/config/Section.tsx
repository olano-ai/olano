// The card wrapper every block of the Config page uses: a title, a one-line
// hint under it, an optional action slot on the right and the body. Extracted
// from ConfigView.tsx unchanged so cards that live in their own files
// (DiskEncryptionCard) share it without a circular import.

import type { ReactNode } from "react";

import { settingAnchor } from "../../core/settingAnchor";

export function Section({
  title,
  hint,
  right,
  children,
  settingId,
}: {
  title: string;
  hint: string;
  right?: ReactNode;
  children: ReactNode;
  // Anchor id for the command palette: see core/settingAnchor.ts.
  settingId?: string;
}) {
  return (
    <section className="card" style={{ padding: 18, marginTop: 16 }} {...settingAnchor(settingId)}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 15 }}>{title}</h2>
          <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5 }}>
            {hint}
          </p>
        </div>
        {right}
      </div>
      <div style={{ marginTop: 14 }}>{children}</div>
    </section>
  );
}
