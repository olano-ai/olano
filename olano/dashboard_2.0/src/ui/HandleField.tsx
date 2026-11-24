// The one handle input, shared by every account-creation surface (first-run
// bootstrap, the onboarding account step, the admin invite form). A handle is
// permanent, so this field does what a plain <input> cannot: it asks the
// server live whether the typed handle is free (GET /api/auth/username-available,
// debounced), shows the normalized form the server would actually store, and
// offers the next free variant on a clash — BEFORE submit, because the server
// now refuses a taken explicit handle rather than silently suffixing it.
// Four hand-rolled copies of that behaviour is how the surfaces would drift.
//
// i18n stays per-view: callers pass `messages` from their own strings file
// (each with {suggestion}/{normalized} already substituted via t(key, vars)
// or as templates — see `fmt` below).

import { useEffect, useRef, useState } from "react";
import { api } from "../core/api";

export interface HandleAvailability {
  available: boolean;
  normalized: string;
  suggestion: string;
}

export interface HandleFieldMessages {
  /** Status when the normalized handle is free, e.g. "Available". */
  available: string;
  /** Status when taken; `{suggestion}` is replaced, e.g. "Taken — try {suggestion}". */
  taken: string;
  /** Shown when the typed text will be normalized; `{normalized}` is replaced. */
  savedAs: string;
  /** While the probe is in flight. */
  checking: string;
}

// Client-side mirror of `olano.issues.principals.slugify` — the HUMAN handle
// rule ([^a-z0-9]+ → "-"), which is stricter than the agent one (underscores
// are legal in an agent name, never in a handle). Placeholder derivation only:
// the server's `normalized` is always the authority.
export function slugifyHandle(input: string): string {
  return input
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function fmt(template: string, vars: Record<string, string>): string {
  let s = template;
  for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, v);
  return s;
}

export function HandleField({
  value,
  onChange,
  placeholder,
  messages,
  disabled,
  className,
}: {
  value: string;
  onChange: (next: string) => void;
  /** Usually the handle derived from the name field, so an empty input reads
   *  as "you'll get roughly this". */
  placeholder?: string;
  messages: HandleFieldMessages;
  disabled?: boolean;
  className?: string;
}) {
  const [probe, setProbe] = useState<HandleAvailability | null>(null);
  const [checking, setChecking] = useState(false);
  const timer = useRef<number | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    const typed = value.trim();
    if (!typed) {
      // Empty means auto-mint server-side; there is nothing to verify and a
      // stale "taken" line under an empty field reads as a blocker.
      setProbe(null);
      setChecking(false);
      return;
    }
    const mySeq = ++seq.current;
    // Drop the previous verdict immediately: it was about different text, and
    // showing "Already taken" under a freshly typed free handle for the whole
    // debounce+round-trip is actively wrong feedback.
    setProbe(null);
    setChecking(true);
    timer.current = window.setTimeout(() => {
      api
        .get<HandleAvailability>(
          `/api/auth/username-available?username=${encodeURIComponent(typed)}`,
        )
        .then((res) => {
          if (seq.current === mySeq) setProbe(res);
        })
        .catch(() => {
          // The probe is a convenience; creation still fails loudly with a
          // 409 if it was wrong. Never block or alarm on a failed check.
          if (seq.current === mySeq) setProbe(null);
        })
        .finally(() => {
          if (seq.current === mySeq) setChecking(false);
        });
    }, 350);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [value]);

  const typed = value.trim();
  let status: { text: string; tone: "ok" | "bad" | "muted" } | null = null;
  if (typed && checking && !probe) {
    status = { text: messages.checking, tone: "muted" };
  } else if (typed && probe) {
    if (!probe.available) {
      status = { text: fmt(messages.taken, { suggestion: probe.suggestion }), tone: "bad" };
    } else if (probe.normalized !== typed) {
      status = { text: fmt(messages.savedAs, { normalized: probe.normalized }), tone: "ok" };
    } else {
      status = { text: messages.available, tone: "ok" };
    }
  }

  return (
    <>
      <input
        className={className || "input"}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
      />
      {status && (
        <div
          className={status.tone === "bad" ? "error-text" : "muted"}
          style={{
            fontSize: 12,
            marginTop: 4,
            ...(status.tone === "ok" ? { color: "var(--success)" } : {}),
          }}
        >
          {status.text}
        </div>
      )}
    </>
  );
}
