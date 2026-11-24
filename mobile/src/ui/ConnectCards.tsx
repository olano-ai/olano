/**
 * Magic links in a conversation, as cards.
 *
 * An agent that needs someone to sign in to a service, paste an API key,
 * connect a messenger or edit a file sends a magic link: a secure page on
 * the deployment, opened in a browser, that needs no sign-in of its own (the
 * link and its 6-digit PIN are the key). The agent does not paste the link
 * into its reply (in the app and the dashboard it is told not to): the card
 * comes from the tool's result, read with the dashboard's own rules
 * (views/chat/connectLinks.ts), so it shows live and after a reload alike.
 * Opening it copies the PIN, ready to paste on the page.
 *
 * When the person finishes on that page, the deployment reports how it went
 * into the same conversation: `ConnectResultCard`, with a restart button when
 * the change needs one and the person may restart agents.
 *
 * One kind of link is not opened at all: a request for an API key
 * (`secret-entry` / `provider-key`) on the deployment the app is signed in to
 * is drawn as a form in the conversation (`SecretFormCard`). The person is
 * already signed in here, so a browser page and a PIN would only add steps;
 * the value still goes to the vault through the link's own routes, never as a
 * message. The deployment must be the one the link points at
 * (`inlineFormLink`), and a deployment from before those routes gets the
 * link card as before.
 */

import { IonButton, IonIcon, IonSpinner, useIonToast } from "@ionic/react";
import { alertCircle, checkmarkCircle, copyOutline, keyOutline, openOutline } from "ionicons/icons";
import { useEffect, useState, type FormEvent } from "react";

import type { ConnectResult } from "../chat/model.ts";
import { ApiError } from "../net/api.ts";
import { haptic } from "../native/haptics.ts";
import {
  cardText,
  connectHost,
  inlineFormLink,
  inlineFormSpent,
  resultSucceeded,
  serviceNameFromUrl,
  type ConnectLink,
  type InlineForm,
  type InlineFormField,
} from "../shared/dashboard.ts";
import { currentApi, session } from "../state/session.ts";
import { useStore } from "../state/store.ts";
import { copyText } from "./messageActions.ts";
import { openLink } from "./Markdown.tsx";

/** "123456" → "123 456", easier to read off and type. */
function spacedPin(pin: string): string {
  return /^\d{6}$/.test(pin) ? `${pin.slice(0, 3)} ${pin.slice(3)}` : pin;
}

export function ConnectCard({ link }: { link: ConnectLink }) {
  const { origin } = useStore(session);
  const [inline, setInline] = useState(() => inlineFormLink(link, origin || ""));
  if (inline) return <SecretFormCard link={link} onUnavailable={() => setInline(false)} />;
  return <LinkCard link={link} />;
}

function initialValues(fields: InlineFormField[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of fields) out[f.name] = f.options.length ? (f.options[0]?.value ?? "") : f.secret ? "" : f.value;
  return out;
}

function complete(fields: InlineFormField[], values: Record<string, string>): boolean {
  return fields.every((f) => !f.required || (values[f.name] ?? "").trim() !== "");
}

type FormPhase = "loading" | "ready" | "saving" | "saved" | "spent";

/** A request for an API key, filled in right here: the value goes to the vault, the agent learns the name. */
export function SecretFormCard({ link, onUnavailable }: { link: ConnectLink; onUnavailable: () => void }) {
  const [form, setForm] = useState<InlineForm | null>(null);
  const [phase, setPhase] = useState<FormPhase>("loading");
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const id = link.id || "";

  useEffect(() => {
    let cancelled = false;
    currentApi()
      .magicLinkForm(id)
      .then((f) => {
        if (cancelled) return;
        setForm(f);
        setValues(initialValues(f.fields || []));
        setPhase(inlineFormSpent(f.status) ? "spent" : "ready");
      })
      .catch(() => {
        if (!cancelled) onUnavailable(); // the link card still opens the page
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!form || phase !== "ready" || !complete(form.fields, values)) return;
    setPhase("saving");
    setError("");
    try {
      const next = await currentApi().magicLinkSubmit(id, link.pin || "", values);
      setForm(next);
      setValues(initialValues(next.fields || []));
      setPhase("saved");
      void haptic("success");
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : "";
      if (/no longer active/i.test(detail)) {
        setPhase("spent");
        return;
      }
      setError(detail ? `Couldn't save. ${detail}` : "Couldn't save. Try again.");
      setPhase("ready");
    }
  }

  const head = (title: string, sub: string, done = false) => (
    <div className="olano-connect-head">
      <span className={done ? "olano-connect-icon done" : "olano-connect-icon"} aria-hidden="true">
        <IonIcon icon={done ? checkmarkCircle : keyOutline} />
      </span>
      <div className="olano-connect-text">
        <strong>{title}</strong>
        {sub && <span className="wrap">{sub}</span>}
      </div>
    </div>
  );

  if (phase === "loading" || !form) {
    return (
      <div className="olano-connect olano-connect-form" role="group" aria-label={link.label || "API key"}>
        {head(link.label || "API key", "Preparing the secure form…")}
      </div>
    );
  }
  if (phase === "saved" || phase === "spent") {
    const done = phase === "saved" || form.status === "done";
    return (
      <div className="olano-connect olano-connect-form" role="status">
        {head(
          done ? "Saved to the vault" : form.title,
          done ? form.result || form.title : form.error || "This request is no longer active. Ask the agent again if you still need it.",
          done,
        )}
      </div>
    );
  }
  const canSave = complete(form.fields, values);
  return (
    <form className="olano-connect olano-connect-form" onSubmit={(e) => void submit(e)} aria-label={form.title}>
      {head(form.title, form.instructions)}
      {form.fields.map((f) => {
        const inputId = `secret-${id}-${f.name}`;
        return (
          <label className="olano-connect-field" key={f.name} htmlFor={inputId}>
            <span>{f.label}</span>
            {f.options.length ? (
              <select id={inputId} value={values[f.name] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}>
                {f.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={inputId}
                type={f.secret ? "password" : "text"}
                autoComplete={f.secret ? "new-password" : "off"}
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                placeholder={f.placeholder}
                value={values[f.name] ?? ""}
                disabled={phase === "saving"}
                onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
              />
            )}
            {f.help && <small>{f.help}</small>}
          </label>
        );
      })}
      {error && (
        <p className="olano-connect-error" role="alert">
          {error}
        </p>
      )}
      <IonButton type="submit" expand="block" className="olano-connect-open" disabled={!canSave || phase === "saving"}>
        {phase === "saving" ? <IonSpinner name="crescent" /> : "Save to vault"}
      </IonButton>
      <p className="olano-connect-hint">Goes straight into the encrypted vault, never through this conversation. The agent only learns the name.</p>
    </form>
  );
}

function LinkCard({ link }: { link: ConnectLink }) {
  const [toast] = useIonToast();
  const host = connectHost(link.href);
  const title =
    link.kind === "mcp-oauth"
      ? `Sign in to ${serviceNameFromUrl(link.serverUrl || link.href) || "the service"}`
      : link.label || "Connect your account";

  async function open() {
    void haptic("light");
    if (link.pin) {
      await copyText(link.pin).catch(() => undefined);
      void toast({ message: `PIN ${spacedPin(link.pin)} copied. Paste it on the page.`, duration: 3500, position: "top", color: "dark" });
    }
    await openLink(link.href);
  }

  return (
    <div className="olano-connect" role="group" aria-label={title}>
      <div className="olano-connect-head">
        <span className="olano-connect-icon" aria-hidden="true">
          <IonIcon icon={keyOutline} />
        </span>
        <div className="olano-connect-text">
          <strong>{title}</strong>
          {host && <span>{host}</span>}
        </div>
      </div>
      {link.pin && (
        <button
          type="button"
          className="olano-connect-pin"
          aria-label={`PIN ${link.pin}. Copy it`}
          onClick={() => {
            void copyText(link.pin!).then(() => toast({ message: "PIN copied", duration: 1400, position: "top", color: "dark" }));
          }}
        >
          <span>PIN</span>
          <strong>{spacedPin(link.pin)}</strong>
          <IonIcon icon={copyOutline} aria-hidden="true" />
        </button>
      )}
      <IonButton expand="block" className="olano-connect-open" onClick={() => void open()}>
        <IonIcon slot="start" icon={openOutline} />
        {link.kind === "mcp-oauth" ? "Sign in" : "Open"}
      </IonButton>
      <p className="olano-connect-hint">
        {link.pin
          ? "Opens a secure page in your browser; the PIN is copied for you. When you finish there, this conversation says so."
          : "Opens in your browser. When you finish there, this conversation says so."}
      </p>
    </div>
  );
}

type RestartState = "idle" | "running" | "done" | "failed";

export function ConnectResultCard({ result }: { result: ConnectResult }) {
  const { me } = useStore(session);
  const [state, setState] = useState<RestartState>("idle");
  const ok = resultSucceeded(result.status);
  const agent = result.targetAgent;
  // The runtime lets administrators restart agents (the dashboard's rule too).
  const canRestart = me?.multi_user === false || !!me?.user?.is_admin;
  const needsRestart = ok && result.apply === "restart_needed" && !!agent;

  let note = "";
  if (ok && result.apply === "restarted") note = `Restarted ${agent} so the change is live.`;
  else if (ok && result.apply === "reloaded") note = `Reloaded ${agent}'s tools, so they're usable now.`;
  else if (needsRestart) note = canRestart ? `${agent} needs a restart to pick this up.` : `Ask an admin to restart ${agent} so it picks this up.`;
  if (state === "done") note = `${agent} restarted.`;
  if (state === "failed") note = `Couldn't restart ${agent}. Try again in a moment.`;

  async function restart() {
    setState("running");
    try {
      await currentApi().restartAgent(agent);
      setState("done");
      void haptic("success");
    } catch {
      setState("failed");
    }
  }

  return (
    <div className={ok ? "olano-connect-result" : "olano-connect-result warn"} role="status">
      <IonIcon icon={ok ? checkmarkCircle : alertCircle} aria-hidden="true" />
      <div className="olano-connect-result-text">
        <strong>{ok ? "Connected" : "Didn't complete"}</strong>
        {result.text && <span>{cardText(result.text)}</span>}
        {note && <small>{note}</small>}
        {needsRestart && canRestart && state !== "done" && (
          <IonButton size="small" fill="outline" disabled={state === "running"} onClick={() => void restart()}>
            {state === "running" ? <IonSpinner name="crescent" /> : `Restart ${agent}`}
          </IonButton>
        )}
      </div>
    </div>
  );
}
