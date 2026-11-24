import type { ReactNode } from "react";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// Shared step chrome. Lives in its own module so OnboardingView (shell) and
// the step panels don't import each other — that ES-module cycle is benign
// today but breaks the moment either module touches the other at eval time.

export function StepShell({
  art,
  title,
  lead,
  optional,
  children,
}: {
  art: ReactNode;
  title: string;
  lead: string;
  optional?: boolean;
  children: ReactNode;
}) {
  const t = useT(STR);
  return (
    <div className="card ob-card">
      {art}
      <h1>
        {title}
        {optional && <span className="badge ob-optional">{t("ob.optional")}</span>}
      </h1>
      <p className="ob-lead">{lead}</p>
      {children}
    </div>
  );
}

/** Pill-button link that opens the credential's source (provider console,
 * BotFather, tavily.com) in a new tab, so admins never hunt for where a key
 * comes from mid-setup. */
export function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a className="ob-extlink" href={href} target="_blank" rel="noreferrer noopener">
      {children}
      <span aria-hidden>↗</span>
    </a>
  );
}

/** Back / Skip-or-Continue footer. When `ok` the step succeeded: the only
 * forward action is Continue (primary). Otherwise Skip advances — unless the
 * step is `required` (rotating a managed deployment's temporary credentials),
 * in which case there is no forward action until the work is done. */
export function StepNav({
  ok,
  busy,
  required,
  onBack,
  onSkip,
  onDone,
}: {
  ok: boolean;
  busy?: boolean;
  required?: boolean;
  onBack(): void;
  onSkip(): void;
  onDone(): void;
}) {
  const t = useT(STR);
  return (
    <div className="ob-actions">
      <button type="button" className="btn ghost" onClick={onBack} disabled={busy}>
        {t("ob.back")}
      </button>
      {ok ? (
        <button type="button" className="btn primary" onClick={onDone} disabled={busy}>
          {t("ob.continue")}
        </button>
      ) : required ? (
        <span className="ob-required-note">{t("ob.requiredStep")}</span>
      ) : (
        <button type="button" className="btn ghost" onClick={onSkip} disabled={busy}>
          {t("ob.skipStep")}
        </button>
      )}
    </div>
  );
}
