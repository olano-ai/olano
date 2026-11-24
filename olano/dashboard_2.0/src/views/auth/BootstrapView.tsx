import { useState, type FormEvent } from "react";
import { ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { HandleField, slugifyHandle } from "../../ui/HandleField";
import { SetupCodeField, useSetupCodeError, useSetupCodeRequired } from "./setupCode";
import { STR } from "./strings";

export function BootstrapView() {
  const { bootstrap } = useAuth();
  const t = useT(STR);
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const codeRequired = useSetupCodeRequired();
  const setupCodeError = useSetupCodeError();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await bootstrap(email, password, name, username.trim(), code);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "";
      setError(
        setupCodeError(detail) ??
          (detail === "username_exists" ? t("bootstrap.err.usernameTaken") : t("bootstrap.err")),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={submit}>
        <h1>{t("bootstrap.title")}</h1>
        <p className="muted" style={{ margin: 0 }}>
          {t("bootstrap.subtitle")}
        </p>
        <div className="field">
          <label>{t("bootstrap.name")}</label>
          <input
            className="input"
            name="name"
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </div>
        <div className="field">
          <label>
            {t("field.handle")}
            <span className="muted" style={{ fontWeight: 400, marginLeft: 6 }}>
              {t("field.handleOptional")}
            </span>
          </label>
          <HandleField
            value={username}
            onChange={setUsername}
            placeholder={slugifyHandle(name) || t("field.handlePlaceholder")}
            messages={{
              available: t("handle.available"),
              taken: t("handle.taken"),
              savedAs: t("handle.savedAs"),
              checking: t("handle.checking"),
            }}
          />
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
            {t("field.handleHelp")}
          </p>
        </div>
        <div className="field">
          <label>{t("field.email")}</label>
          <input
            className="input"
            type="email"
            name="username"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label>{t("field.password")}</label>
          {/* Creating the first admin: `new-password` is what makes a manager
              offer to suggest one here rather than trying to fill an old one. */}
          <input
            className="input"
            type="password"
            name="new-password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {codeRequired && <SetupCodeField value={code} onChange={setCode} />}
        {error && <div className="error-text">{error}</div>}
        <button
          className="btn primary"
          disabled={
            busy ||
            !email ||
            !password ||
            codeRequired === undefined ||
            (codeRequired && !code.trim())
          }
        >
          {busy ? t("bootstrap.submitting") : t("bootstrap.submit")}
        </button>
      </form>
    </div>
  );
}
