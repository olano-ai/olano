// The inline secret card: a `secret-entry` / `provider-key` magic link drawn
// as a form under the agent's reply, instead of a link to open in a new tab.
//
// The link and its PIN exist for a messenger, where the reader is
// unauthenticated and has no UI. Here the person is already signed in on the
// deployment's own origin, so the detour bought nothing; the property that
// matters survives: the value is posted to the admin routes behind this card
// (`/api/magic-links/links/{id}/form` + `/submit`), never as a chat message,
// never into the transcript or the model's context. The backend drives the
// SAME link record and flow `submit` as the page, so single use, the audit
// trail and the completion notice (`ConnectResultCard`) are unchanged.
//
// `onUnavailable` is the fallback to the plain link card: a runtime that
// predates these routes (404/405), a link past its 24 h retention, or a
// network failure while loading the form. The decision to render this card
// at all (`inlineFormLink`) is made by `ConnectCard`, including the
// same-origin rule that keeps a spoofed tool result from putting a credential
// field in the chat.

import { Check, KeyRound } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { inlineFormSpent, type ConnectLink, type InlineForm, type InlineFormField } from "./connectLinks";

type Phase = "loading" | "ready" | "saving" | "saved" | "spent";

function initialValues(fields: InlineFormField[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of fields) {
    // A select starts on its first option, as the page does; a secret never
    // carries a prefill (the backend blanks it), a text field may.
    out[f.name] = f.options.length ? f.options[0]?.value ?? "" : f.secret ? "" : f.value;
  }
  return out;
}

function complete(fields: InlineFormField[], values: Record<string, string>): boolean {
  return fields.every((f) => !f.required || (values[f.name] ?? "").trim() !== "");
}

export function SecretFormCard({ link, onUnavailable }: { link: ConnectLink; onUnavailable: () => void }) {
  const t = useT(STR);
  const [form, setForm] = useState<InlineForm | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const id = link.id || "";

  useEffect(() => {
    let cancelled = false;
    api
      .get<InlineForm>(`/api/magic-links/links/${encodeURIComponent(id)}/form`)
      .then((f) => {
        if (cancelled) return;
        setForm(f);
        setValues(initialValues(f.fields || []));
        setPhase(inlineFormSpent(f.status) ? "spent" : "ready");
      })
      .catch(() => {
        // Whatever the reason, the link card still works: it opens the page.
        if (!cancelled) onUnavailable();
      });
    return () => {
      cancelled = true;
    };
    // `onUnavailable` is a parent setter; the form is keyed on the link id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!form || phase !== "ready" || !complete(form.fields, values)) return;
    setPhase("saving");
    setError("");
    try {
      const next = await api.post<InlineForm>(`/api/magic-links/links/${encodeURIComponent(id)}/submit`, {
        pin: link.pin || "",
        fields: values,
      });
      setForm(next);
      setValues(initialValues(next.fields || [])); // never keep the value in memory
      setPhase("saved");
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "";
      if (detail && /no longer active/i.test(detail)) {
        setPhase("spent");
        return;
      }
      setError(t("secret.failed", { detail: detail || "" }).trim());
      setPhase("ready");
    }
  }

  if (phase === "loading" || !form) {
    return (
      <div className="connect-card connect-card--form connect-form" data-testid="secret-form-card">
        <div className="connect-form__head">
          <span className="connect-card__logo connect-card__logo--mono" aria-hidden>
            <KeyRound size={16} strokeWidth={1.75} />
          </span>
          <div className="connect-card__text">
            <strong>{link.label || t("connect.magicTitle")}</strong>
            <span className="connect-card__sub">{t("secret.loading")}</span>
          </div>
        </div>
      </div>
    );
  }

  const done = phase === "saved" || (phase === "spent" && form.status === "done");
  if (phase === "saved" || phase === "spent") {
    return (
      <div className="connect-card connect-card--form connect-form" data-testid="secret-form-card">
        <div className="connect-form__head">
          <span className={`connect-card__logo connect-card__logo--mono${done ? " connect-form__logo--done" : ""}`} aria-hidden>
            {done ? <Check size={16} strokeWidth={2} /> : <KeyRound size={16} strokeWidth={1.75} />}
          </span>
          <div className="connect-card__text">
            <strong>{done ? t("secret.savedTitle") : form.title}</strong>
            <span className="connect-form__status">{done ? form.result || form.title : form.error || t("secret.spent")}</span>
          </div>
        </div>
      </div>
    );
  }

  const canSave = complete(form.fields, values);
  return (
    <form className="connect-card connect-card--form connect-form" onSubmit={(e) => void submit(e)} data-testid="secret-form-card">
      <div className="connect-form__head">
        <span className="connect-card__logo connect-card__logo--mono" aria-hidden>
          <KeyRound size={16} strokeWidth={1.75} />
        </span>
        <div className="connect-card__text">
          <strong>{form.title}</strong>
          {form.instructions && <span className="connect-form__instructions">{form.instructions}</span>}
        </div>
      </div>
      <div className="connect-form__fields">
        {form.fields.map((f) => {
          const inputId = `secret-${id}-${f.name}`;
          return (
            <div className="field" key={f.name}>
              <label htmlFor={inputId}>{f.label}</label>
              {f.options.length ? (
                <select
                  id={inputId}
                  className="input"
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                >
                  {f.options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={inputId}
                  className="input"
                  type={f.secret ? "password" : "text"}
                  autoComplete={f.secret ? "new-password" : "off"}
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  placeholder={f.placeholder}
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                  disabled={phase === "saving"}
                />
              )}
              {f.help && <span className="connect-form__help">{f.help}</span>}
            </div>
          );
        })}
      </div>
      {error && (
        <div className="error-text connect-form__error" role="alert">
          {error}
        </div>
      )}
      <div className="connect-form__actions">
        <span className="connect-card__hint">{t("secret.hint")}</span>
        <button type="submit" className="btn primary connect-card__btn" disabled={!canSave || phase === "saving"}>
          {phase === "saving" ? t("secret.saving") : t("secret.save")}
        </button>
      </div>
    </form>
  );
}
