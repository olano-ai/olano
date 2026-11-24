import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import type { User } from "../../core/types";
import { slugifyHandle } from "../../ui/HandleField";
import { HandlePreview } from "../agents/HandlePreview";
import { SetupCodeField, useSetupCodeError, useSetupCodeRequired } from "../auth/setupCode";
import { PasswordField, UsernameAnchor } from "../vault/components";
import { StepNav, StepShell } from "./chrome";
import { useCloudDeploymentUrl } from "./cloudDeployment";
import { ArtPeople } from "./illustrations";
import { STR } from "./strings";
import type { StepProps } from "./types";

const MIN_SECRET = 8;

/**
 * "Make this account yours" — one panel for the three things the owner
 * chooses on a fresh box: their name (the handle is minted from it), their
 * own password, and the vault passphrase.
 *
 * Three shapes, decided ONCE at mount from the auth context, because the
 * successful half of a submit refreshes that context and the half must not
 * vanish under the owner while its ✓ line is what they are reading:
 *
 *   claim   — multi-user, temporary password still in play: the box was
 *             provisioned with the owner's own email on the built-in `admin`
 *             row (a managed deployment), so POST /api/auth/claim rewrites
 *             name + handle + password on that row. No second account, no
 *             second email — which is what the old "create your own account"
 *             step demanded, since one email can only own one account.
 *   create  — no accounts yet (a self-hosted box, or a managed one
 *             mid-provisioning): the same fields plus an email, and
 *             POST /api/auth/bootstrap creates the admin sign-in, flipping the
 *             login gate on.
 *   vault   — the password half is already done (the owner bootstrapped their
 *             own account, or claimed it and lost the runtime before the
 *             rekey): only the passphrase fields.
 *
 * The two halves are separate <form>s for the password manager's sake: it
 * pairs a generated password with the nearest username field, and the vault
 * passphrase must never be offered as the sign-in for this origin (that is
 * what the fixed "olano-vault" anchor is for). One visible button drives
 * both in order — account first, then vault — and a half that already
 * succeeded is never re-run.
 */
export function ProfileStep(p: StepProps) {
  const t = useT(STR);
  const { user, multiUser, adminPasswordRotated, vaultPasswordRotated, refresh } = useAuth();
  const cloudUrl = useCloudDeploymentUrl();
  const [creating] = useState(() => !multiUser);
  const [showAccount] = useState(() => !multiUser || !adminPasswordRotated);
  const [showVault] = useState(() => !vaultPasswordRotated);
  // Creating the first admin on a self-hosted box needs the one-time setup
  // code (olano/runtime/setup_code.py); the server says whether it does.
  const codeRequired = useSetupCodeRequired(creating && showAccount);
  const setupCodeError = useSetupCodeError();
  const [setupCode, setSetupCode] = useState("");

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [current, setCurrent] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [passphraseConfirm, setPassphraseConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // Steps remount on revisit; p.done restores "already succeeded" so the
  // owner isn't shown a blank form for work that already happened.
  const [accountOk, setAccountOk] = useState(p.done || !showAccount);
  const [vaultOk, setVaultOk] = useState(p.done || !showVault);
  // What the ✓ line names once the account half lands (only known this mount).
  const [saved, setSaved] = useState<{ name: string; handle: string; email: string } | null>(
    null,
  );
  // An open vault can be rekeyed without its current passphrase (the server
  // rekeys from the live store). That is the managed case: the box auto-unlocks
  // from its boot key and the temporary passphrase lives on another website, so
  // asking for it sends the owner off to copy a secret they never chose.
  // undefined = still asking; a failed probe reads as locked, which only ever
  // asks for more proof, never less.
  const [unlocked, setUnlocked] = useState<boolean | undefined>(undefined);

  useEffect(() => {
    if (!showVault || vaultOk) return;
    api
      .get<{ unlocked?: boolean }>("/api/vault/status")
      .then((s) => setUnlocked(Boolean(s.unlocked)))
      .catch(() => setUnlocked(false));
  }, [showVault, vaultOk]);

  const accountPending = showAccount && !accountOk;
  const vaultPending = showVault && !vaultOk;
  const ok = !accountPending && !vaultPending;

  function validate(): string {
    if (accountPending) {
      if (!name.trim()) return t("ob.profile.err.name");
      if (creating && !email.trim()) return t("ob.profile.err.email");
      if (creating && codeRequired && !setupCode.trim()) {
        return setupCodeError("setup_code_required") ?? "";
      }
      if (password.length < MIN_SECRET) return t("ob.profile.err.short");
      if (password !== confirm) return t("ob.profile.err.mismatch");
    }
    if (vaultPending) {
      if (unlocked === false && !current) return t("ob.vault.err.wrong");
      if (passphrase.length < MIN_SECRET) return t("ob.vault.err.short");
      if (passphrase !== passphraseConfirm) return t("ob.vault.err.mismatch");
    }
    return "";
  }

  async function submitAccount(): Promise<boolean> {
    try {
      const res = creating
        ? await api.post<{ user: User }>("/api/auth/bootstrap", {
            email: email.trim(),
            password,
            name: name.trim(),
            setup_code: setupCode.trim(),
          })
        : await api.post<{ user: User }>("/api/auth/claim", { name: name.trim(), password });
      setSaved({ name: res.user.name, handle: res.user.username, email: res.user.email });
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      const detail = e instanceof ApiError ? e.detail : "";
      if (detail === "already_claimed") {
        // Another tab (or an earlier attempt the runtime lost the answer to)
        // already made this account the owner's — that is success, not a
        // failure to report. The refresh below picks up the new identity.
        setAccountOk(true);
        await refresh();
        return true;
      }
      const codeProblem = setupCodeError(detail);
      if (codeProblem) {
        setErr(codeProblem);
        return false;
      }
      setErr(
        detail === "password_short"
          ? t("ob.profile.err.short")
          : detail === "already_initialized" || detail === "email_exists"
            ? t("ob.profile.err.exists")
            : status === 403 && detail
              ? detail
              : t("ob.profile.err.generic"),
      );
      return false;
    }
    setAccountOk(true);
    // Re-read /api/auth/me: after a bootstrap the auth context flips to the
    // real admin (session cookie, multi-user on); after a claim it carries the
    // new name and handle. onboarding.required stays true, so the wizard keeps
    // its place — this just updates who we are.
    await refresh();
    return true;
  }

  async function submitVault(): Promise<boolean> {
    try {
      await api.post("/api/vault/rekey", {
        ...(unlocked ? {} : { old_passphrase: current }),
        new_passphrase: passphrase,
      });
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setErr(status === 401 ? t("ob.vault.err.wrong") : t("ob.vault.err.generic"));
      return false;
    }
    setVaultOk(true);
    await refresh(); // onboarding.vault_password_rotated is true now
    return true;
  }

  async function submitAll() {
    if (busy || ok) return;
    const problem = validate();
    if (problem) return setErr(problem);
    setBusy(true);
    setErr("");
    try {
      // Account first: on a box with no accounts the rekey is admin-only, and
      // the bootstrap is what makes this browser an admin.
      if (accountPending && !(await submitAccount())) return;
      if (vaultPending && !(await submitVault())) return;
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void submitAll();
  }

  const lead = !showAccount
    ? t("ob.profile.leadVaultOnly")
    : creating
      ? t("ob.profile.leadCreate")
      : t("ob.profile.lead", { email: user?.email ?? "" });

  const canSubmit =
    !busy &&
    !ok &&
    (!accountPending ||
      (!!name.trim() &&
        !!password &&
        !!confirm &&
        (!creating || (!!email.trim() && codeRequired !== undefined)))) &&
    (!vaultPending ||
      (unlocked !== undefined &&
        (unlocked || !!current) &&
        !!passphrase &&
        !!passphraseConfirm));

  return (
    <StepShell art={<ArtPeople />} title={t("ob.profile.title")} lead={lead}>
      {showAccount && (
        <section className="ob-section" aria-label={t("ob.profile.accountHead")}>
          {showVault && <h2 className="ob-section-head">{t("ob.profile.accountHead")}</h2>}
          {accountOk ? (
            <div className="ob-ok">
              ✓{" "}
              {saved
                ? creating
                  ? t("ob.profile.createdDone", { email: saved.email })
                  : t("ob.profile.accountDone", { name: saved.name, handle: saved.handle })
                : t("ob.profile.accountDoneGeneric")}
            </div>
          ) : (
            <form onSubmit={onSubmit} className="ob-form" data-testid="profile-account-form">
              {/* The claim form has no visible identifier — the account is
                  whoever is signed in — so the anchor names it, both to earn
                  the "Suggest password" offer and so the manager updates that
                  entry instead of inventing a second. */}
              {!creating && <UsernameAnchor value={user?.email ?? "admin"} />}
              <div className="field">
                <label>{t("ob.profile.name")}</label>
                <input
                  className="input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t("ob.profile.namePlaceholder")}
                  name="name"
                  autoComplete="name"
                  autoFocus
                />
                <span className="ob-hint">{t("ob.profile.nameHint")}</span>
              </div>
              <HandlePreview
                handle={slugifyHandle(name)}
                label={t("ob.profile.handle")}
                pending={t("ob.profile.handlePending")}
                hint={t("ob.profile.handleHint")}
              />
              {creating && (
                <div className="field">
                  <label>{t("ob.profile.email")}</label>
                  <input
                    className="input"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    // `username`, not `email`: this address IS the sign-in
                    // identifier, and `email` marks a contact field the manager
                    // will not pair a generated password with.
                    name="username"
                    autoComplete="username"
                  />
                  <span className="ob-hint">{t("ob.profile.emailHint")}</span>
                </div>
              )}
              {creating && codeRequired && (
                <SetupCodeField value={setupCode} onChange={setSetupCode} hintClassName="ob-hint" />
              )}
              <div className="field">
                <label>{t("ob.profile.password")}</label>
                <PasswordField
                  value={password}
                  onChange={setPassword}
                  autoComplete="new-password"
                  name="new-password"
                />
                <span className="ob-hint">
                  {creating ? t("ob.profile.passwordCreateHint") : t("ob.profile.passwordHint")}
                </span>
              </div>
              <div className="field">
                <label>{t("ob.profile.confirm")}</label>
                <PasswordField
                  value={confirm}
                  onChange={setConfirm}
                  autoComplete="new-password"
                  name="confirm-password"
                />
              </div>
            </form>
          )}
        </section>
      )}

      {showVault && (
        <section className="ob-section" aria-label={t("ob.profile.vaultHead")}>
          {showAccount && <h2 className="ob-section-head">{t("ob.profile.vaultHead")}</h2>}
          {vaultOk ? (
            <>
              <div className="ob-ok">✓ {t("ob.profile.vaultDone")}</div>
              <p className="ob-hint">{t("ob.vault.envNote")}</p>
            </>
          ) : (
            <form onSubmit={onSubmit} className="ob-form" data-testid="profile-vault-form">
              {/* The passphrase belongs to the box, not to a person, so the
                  anchor is a fixed name rather than the owner's address —
                  pairing it with their email would invite the manager to
                  overwrite the *sign-in* entry for this origin with the vault
                  passphrase. */}
              <UsernameAnchor value="olano-vault" />
              {unlocked === false && (
                <>
                  <div className="field">
                    <label>{t("ob.vault.current")}</label>
                    <PasswordField
                      value={current}
                      onChange={setCurrent}
                      autoComplete="current-password"
                      name="current-passphrase"
                    />
                  </div>
                  {/* On a managed deployment the generic hint's "self-hosted
                      setups default it to the admin password" half is noise —
                      replace it with the console card that holds this box's
                      passphrase. */}
                  {cloudUrl ? (
                    <p className="ob-hint">
                      {t("ob.cloud.vaultHint")}{" "}
                      <a href={cloudUrl} target="_blank" rel="noreferrer noopener">
                        {t("ob.cloud.open")} ↗
                      </a>
                    </p>
                  ) : (
                    <p className="ob-hint">{t("ob.vault.hint")}</p>
                  )}
                </>
              )}
              {unlocked === true && <p className="ob-hint">{t("ob.vault.setHint")}</p>}
              <div className="field">
                <label>{t("ob.profile.passphrase")}</label>
                <PasswordField
                  value={passphrase}
                  onChange={setPassphrase}
                  autoComplete="new-password"
                  name="new-passphrase"
                  autoFocus={!showAccount || accountOk}
                />
                <span className="ob-hint">{t("ob.profile.passphraseHint")}</span>
              </div>
              <div className="field">
                <label>{t("ob.profile.passphraseConfirm")}</label>
                <PasswordField
                  value={passphraseConfirm}
                  onChange={setPassphraseConfirm}
                  autoComplete="new-password"
                  name="confirm-passphrase"
                />
              </div>
            </form>
          )}
        </section>
      )}

      {err && <div className="error-text">{err}</div>}
      {!ok && (
        <button
          type="button"
          className="btn primary ob-profile-submit"
          disabled={!canSubmit}
          onClick={() => void submitAll()}
        >
          {busy ? t("ob.saving") : t("ob.profile.submit")}
        </button>
      )}
      <StepNav
        ok={ok}
        busy={busy}
        required={p.required}
        onBack={p.onBack}
        onSkip={p.onSkip}
        onDone={p.onDone}
      />
    </StepShell>
  );
}
