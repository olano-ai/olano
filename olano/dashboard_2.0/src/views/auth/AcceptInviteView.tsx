import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { UsernameAnchor } from "../vault/components";
import { STR } from "./strings";

type Phase = "loading" | "ready" | "invalid";

export function AcceptInviteView() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const t = useT(STR);
  const [phase, setPhase] = useState<Phase>("loading");
  const [info, setInfo] = useState<{ email: string; name: string; username?: string } | null>(
    null,
  );
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<{ email: string; name: string; username?: string }>(`/api/auth/invite/${token}`)
      .then((i) => {
        setInfo(i);
        setPhase("ready");
      })
      .catch(() => setPhase("invalid"));
  }, [token]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError(t("invite.err.mismatch"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.post(`/api/auth/invite/${token}/accept`, { password });
      await refresh();
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError ? t("invite.err.invalid") : t("invite.err.generic"),
      );
      setBusy(false);
    }
  }

  if (phase === "loading") {
    return (
      <div className="auth-wrap">
        <div className="spinner" />
      </div>
    );
  }

  if (phase === "invalid") {
    return (
      <div className="auth-wrap">
        <div className="card auth-card">
          <h1>{t("invite.invalid.title")}</h1>
          <p className="muted" style={{ margin: 0 }}>
            {t("invite.invalid.body")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={submit}>
        <h1>{t("invite.title")}</h1>
        <p className="muted" style={{ margin: 0 }}>
          {t("invite.welcome", { name: info?.name ? `, ${info.name}` : "" })}
          <strong>{info?.email}</strong>.
        </p>
        {/* The handle was minted with the invite and is permanent — this is
            where its owner first learns it, not an issue assignment later. */}
        {info?.username && (
          <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
            {t("invite.handleLine", { username: info.username })}
          </p>
        )}
        {/* The invite fixes the address, so the account this password belongs
            to is never typed — without carrying it in the form the manager sees
            two anonymous password fields and offers nothing. */}
        <UsernameAnchor value={info?.email ?? ""} />
        <div className="field">
          <label>{t("invite.newPassword")}</label>
          <input
            className="input"
            type="password"
            name="new-password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
        </div>
        <div className="field">
          <label>{t("invite.confirmPassword")}</label>
          <input
            className="input"
            type="password"
            name="confirm-password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </div>
        {error && <div className="error-text">{error}</div>}
        <button className="btn primary" disabled={busy || !password || !confirm}>
          {busy ? t("invite.submitting") : t("invite.submit")}
        </button>
      </form>
    </div>
  );
}
