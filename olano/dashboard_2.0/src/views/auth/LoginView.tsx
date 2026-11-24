import { useEffect, useState, type FormEvent } from "react";
import { ExternalLink, KeyRound } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { uiModeHint, useAuth } from "../../core/auth";
import { OlanoMark } from "../../ui/OlanoMark";
import { useT } from "../../core/i18n";
import { consoleSecurityLink } from "../../core/serverMonitor/deployment";
import { useServerHealth, type Health } from "../../core/serverMonitor/useServerHealth";
import { STR } from "./strings";

interface SignInHintResponse {
  temporary_password: { deployment_id: string } | null;
}

/**
 * The console link to a just-deployed instance's temporary password, or null.
 *
 * Non-null only while the runtime says the provisioned admin still signs in
 * with the temporary password the cloud generated (GET /api/auth/sign-in-hint:
 * a managed-cloud box, provisioned from that password, not replaced since).
 * That is the one moment an email + password form asks for a password its
 * owner never chose; everywhere else the screen stays the ordinary one.
 *
 * Asked once the server is online, not on mount: the first visit to a fresh
 * box often lands while the runtime is still starting, and a request that
 * cannot connect is retried on the next offline-to-online edge. A server that
 * answered (an older runtime's 404 included) is final for this mount.
 */
function useTemporaryPasswordLink(online: boolean): string | null {
  const [link, setLink] = useState<string | null>(null);
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    if (!online || settled) return;
    let live = true;
    api.get<SignInHintResponse>("/api/auth/sign-in-hint").then(
      (res) => {
        if (!live) return;
        const id = res?.temporary_password?.deployment_id;
        setLink(typeof id === "string" && id ? consoleSecurityLink(id) : null);
        setSettled(true);
      },
      (err) => {
        if (live && err instanceof ApiError) setSettled(true);
      },
    );
    return () => {
      live = false;
    };
  }, [online, settled]);
  return link;
}

/** Where the temporary password is: the welcome email, or the console link. */
function TemporaryPasswordNote({
  link,
  title,
  body,
}: {
  link: string;
  title?: string;
  body: string;
}) {
  const t = useT(STR);
  return (
    <div className="auth-first-signin" role="note" data-testid="first-signin-hint">
      <KeyRound size={16} aria-hidden />
      <div>
        {title && <strong>{title}</strong>}
        <p>{body}</p>
        <a
          href={link}
          target="_blank"
          rel="noreferrer noopener"
          title={t("signin.first.linkTitle")}
        >
          {link}
          <ExternalLink size={12} aria-hidden />
        </a>
      </div>
    </div>
  );
}

function ServerStatus({ health }: { health: Health }) {
  const t = useT(STR);
  if (health === "online") {
    return (
      <div className="row" style={{ gap: 6, fontSize: 12.5 }}>
        <span
          aria-hidden
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: "var(--success)",
            display: "inline-block",
          }}
        />
        <span style={{ color: "var(--success)" }}>{t("status.online")}</span>
      </div>
    );
  }
  return (
    <div className="row" style={{ gap: 8, fontSize: 12.5 }}>
      <span className="spinner" style={{ width: 13, height: 13 }} />
      <span className="muted">{health === "checking" ? t("status.checking") : t("status.starting")}</span>
    </div>
  );
}

export function LoginView() {
  const [mode, setMode] = useState<"signin" | "forgot">("signin");
  // Dress the screen as the shell this DEVICE last used. This is the one
  // moment the server cannot answer — your mode is known only after you are —
  // so the screen guesses from the last sign-in here and stays neutral on a
  // fresh browser. A wrong guess costs nothing: the resolved mode rides the
  // same /api/auth/me response that ends the gate, so the right shell mounts
  // immediately either way. Read once per mount, deliberately not reactive —
  // the hint changing mid-login is not a state worth animating.
  const simple = uiModeHint() === "simple";
  // One probe for the whole login screen, shared by both panels. Only `health`
  // matters here: signing in works the moment the API answers, so an agent-boot
  // loop still running (`booting`) must not gate the form.
  const { health } = useServerHealth();
  // Shared by both panels: "Forgot password?" on a box whose only password is
  // the temporary one must point at it, not at an admin who does not exist yet.
  const temporaryPasswordLink = useTemporaryPasswordLink(health === "online");
  return mode === "forgot" ? (
    <ForgotPanel
      health={health}
      simple={simple}
      temporaryPasswordLink={temporaryPasswordLink}
      onBack={() => setMode("signin")}
    />
  ) : (
    <SignInPanel
      health={health}
      simple={simple}
      temporaryPasswordLink={temporaryPasswordLink}
      onForgot={() => setMode("forgot")}
    />
  );
}

const linkButton: React.CSSProperties = {
  background: "none",
  border: "none",
  color: "var(--accent)",
  cursor: "pointer",
  padding: 0,
  font: "inherit",
  alignSelf: "center",
};

function SignInPanel({
  health,
  simple,
  temporaryPasswordLink,
  onForgot,
}: {
  health: Health;
  simple: boolean;
  temporaryPasswordLink: string | null;
  onForgot: () => void;
}) {
  const { login } = useAuth();
  const t = useT(STR);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const online = health === "online";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!online) return;
    setBusy(true);
    setError("");
    try {
      await login(email, password);
    } catch (err) {
      if (err instanceof ApiError) {
        // Server responded — credentials wrong (or some other handled error).
        setError(
          err.detail === "invalid_credentials"
            ? t("signin.err.credentials")
            : t("signin.err.generic"),
        );
      } else {
        // Network/connection failure — the server is down or restarting.
        setError(t("signin.err.offline"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={simple ? "auth-wrap is-simple" : "auth-wrap"}>
      <form className="card auth-card" onSubmit={submit}>
        {/* The product's own mark. It used to be three generated bot beads —
            which is the face of a GROUP CHAT everywhere else in this app, and
            on the one screen where nobody is signed in there are no bots for
            it to be a group of. So it read as decoration at best and as
            somebody else's three bots at worst. The logo says whose box this
            is, which is the one thing a sign-in screen has to answer, and it
            says it in both shells rather than only the consumer one. */}
        <div className="auth-mark">
          <OlanoMark size={56} title={t("signin.markAlt")} />
        </div>
        {/* A first sign-in is nobody's "welcome back": on a just-deployed box
            the heading and the line under it greet a new owner instead, in
            both shells, and the note says where the password is. */}
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <h1 style={{ margin: 0 }}>
            {simple && !temporaryPasswordLink ? t("signin.titleSimple") : t("signin.title")}
          </h1>
          <ServerStatus health={health} />
        </div>
        <p className="muted" style={{ margin: 0 }}>
          {temporaryPasswordLink
            ? t("signin.first.subtitle")
            : simple
              ? t("signin.subtitleSimple")
              : t("signin.subtitle")}
        </p>
        {temporaryPasswordLink && (
          <TemporaryPasswordNote
            link={temporaryPasswordLink}
            title={t("signin.first.title")}
            body={t("signin.first.body")}
          />
        )}
        {/* Sign-in is the other half of the contract: the manager needs to know
            this pair is the saved credential for the origin, or the sign-up and
            change-password forms elsewhere have nothing to match against. */}
        <div className="field">
          <label>{t("field.email")}</label>
          <input
            className="input"
            type="email"
            name="username"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            disabled={health === "offline" || busy}
            autoFocus
          />
        </div>
        <div className="field">
          <label>{t("field.password")}</label>
          <input
            className="input"
            type="password"
            name="current-password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={health === "offline" || busy}
          />
        </div>
        {health === "offline" && (
          <div className="muted" style={{ fontSize: 12.5 }}>{t("signin.waiting")}</div>
        )}
        {error && <div className="error-text">{error}</div>}
        <button className="btn primary" disabled={busy || !email || !password || !online}>
          {busy ? t("signin.submitting") : health === "offline" ? t("signin.starting") : t("signin.submit")}
        </button>
        <button type="button" style={linkButton} onClick={onForgot}>
          {t("signin.forgot")}
        </button>
      </form>
    </div>
  );
}

function ForgotPanel({
  health,
  simple,
  temporaryPasswordLink,
  onBack,
}: {
  health: Health;
  simple: boolean;
  temporaryPasswordLink: string | null;
  onBack: () => void;
}) {
  const t = useT(STR);
  return (
    <div className={simple ? "auth-wrap is-simple" : "auth-wrap"}>
      <div className="card auth-card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <h1 style={{ margin: 0 }}>{t("forgot.title")}</h1>
          <ServerStatus health={health} />
        </div>
        {/* On a just-deployed box the only password is the temporary one and
            the person asking is its owner: "ask your admin" would send them
            looking for someone who does not exist yet. */}
        {temporaryPasswordLink ? (
          <TemporaryPasswordNote link={temporaryPasswordLink} body={t("forgot.first.body")} />
        ) : (
          <>
            <p className="muted" style={{ margin: 0 }}>
              {t("forgot.body")}
            </p>
            <p className="muted" style={{ margin: 0, fontSize: 12 }}>
              {t("forgot.cliHint")}{" "}
              <code>olano users reset-password --email you@example.com</code>.
            </p>
          </>
        )}
        <button className="btn" onClick={onBack}>
          {t("forgot.back")}
        </button>
      </div>
    </div>
  );
}
