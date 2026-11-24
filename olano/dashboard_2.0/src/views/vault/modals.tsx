import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Eye, Lock, Trash2 } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Overlay, PasswordField, UsernameAnchor } from "./components";
import { errMessage } from "./helpers";
import { STR } from "./strings";
import type { RekeyResponse, SecretInput, SecretMetadata, SecretValue, UnlockResponse } from "./types";

// ── empty / locked / error state ────────────────────────────────────────────
export function EmptyState({
  icon,
  title,
  message,
  actionLabel,
  onAction,
}: {
  icon?: ReactNode;
  title: string;
  message: ReactNode;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="card" style={{ padding: 40, textAlign: "center" }}>
      <div style={{ display: "flex", justifyContent: "center", color: "var(--text-3)" }}>
        {icon ?? <Lock size={30} strokeWidth={1.5} />}
      </div>
      <h2 style={{ fontSize: 16, margin: "10px 0 4px" }}>{title}</h2>
      <p className="muted" style={{ maxWidth: 460, margin: "0 auto 14px" }}>
        {message}
      </p>
      {actionLabel && onAction && (
        <button className="btn primary" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}

// ── unlock modal ────────────────────────────────────────────────────────────
export function UnlockModal({
  onClose,
  onSuccess,
}: {
  onClose: () => void;
  onSuccess: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [passphrase, setPassphrase] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit() {
    if (loading) return;
    if (!passphrase) {
      toast.push(t("unlock.passphraseRequired"), "error");
      return;
    }
    setLoading(true);
    try {
      await api.post<UnlockResponse>("/api/vault/unlock", { passphrase });
      toast.push(t("unlock.success"));
      onSuccess();
    } catch (err) {
      toast.push(
        err instanceof ApiError && err.status === 401
          ? t("unlock.wrong")
          : errMessage(err, t("unlock.failed")),
        "error",
      );
      setLoading(false);
    }
  }

  return (
    // While the unlock POST is in flight the dialog must not be dismissible —
    // Escape/backdrop would bypass the disabled Cancel and drop the pending
    // requestUnlock success callback (the gated action would silently vanish).
    <Overlay onClose={loading ? () => {} : onClose} kind="vault-unlock">
      <h2 style={{ marginTop: 0, fontSize: 16 }}>{t("unlock.title")}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {t("unlock.subtitle")}
      </p>
      <div className="field">
        <label>{t("unlock.label")}</label>
        <PasswordField
          value={passphrase}
          onChange={setPassphrase}
          placeholder={t("unlock.placeholder")}
          autoComplete="current-password"
          autoFocus
          onEnter={() => void submit()}
        />
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
        {t("unlock.note.prefix")}
        <strong>{t("unlock.note.bold")}</strong>
        {t("unlock.note.suffix")}
        <code>OLANO_VAULT_KEY</code>
        {t("unlock.note.tail")}
      </p>
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn" onClick={onClose} disabled={loading}>
          {t("btn.cancel")}
        </button>
        <button className="btn primary" onClick={() => void submit()} disabled={loading || !passphrase}>
          {loading ? t("unlock.submitting") : t("unlock.title")}
        </button>
      </div>
    </Overlay>
  );
}

// ── add / edit secret drawer ────────────────────────────────────────────────
export function SecretDrawer({
  existing,
  presetName,
  onClose,
  onSaved,
}: {
  existing: SecretMetadata | null;
  // When set (and existing is null), opens a "new per-agent override" for this
  // common key: the name is locked and the scope defaults to picking an agent.
  presetName?: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const isEdit = existing !== null;
  const nameLocked = isEdit || !!presetName;
  // "" = common value; a non-empty string = per-agent override for that agent.
  const lockedScope = isEdit; // editing keeps the row's existing scope
  const [name, setName] = useState(existing?.name ?? presetName ?? "");
  const [agent, setAgent] = useState<string>(existing?.agent ?? "");
  const [agents, setAgents] = useState<string[]>([]);
  const [category, setCategory] = useState(existing?.category ?? "api-keys");
  const [value, setValue] = useState("");
  const [showValue, setShowValue] = useState(false);
  const [description, setDescription] = useState(existing?.description ?? "");
  const [expiresIn, setExpiresIn] = useState("");
  const [rotation, setRotation] = useState("90");
  const [saving, setSaving] = useState(false);

  // Agent roster for the scope picker (only when the scope is selectable).
  useEffect(() => {
    if (lockedScope) return;
    api
      .cachedGet<{ name: string }[]>("/api/agents")
      .then((rows) => {
        const names = rows.map((r) => r.name);
        setAgents(names);
        // New override for a common key → default to the first agent.
        if (presetName && names.length) setAgent((cur) => cur || names[0]);
      })
      .catch(() => {});
  }, [lockedScope, presetName]);

  const isOverride = agent.trim() !== "";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    const trimmed = name.trim();
    if (!trimmed) {
      toast.push(t("secret.nameRequired"), "error");
      return;
    }
    if (!isEdit && !value) {
      toast.push(t("secret.valueRequired"), "error");
      return;
    }
    // Edit-without-value: skip the API call, no-op (mirrors original).
    if (isEdit && !value) {
      toast.push(t("secret.noChange"));
      onClose();
      return;
    }
    const body: SecretInput = {
      name: trimmed,
      value,
      description: description.trim(),
      category: category.trim() || "general",
      expires_in_days: expiresIn ? Number.parseInt(expiresIn, 10) : null,
      rotation_reminder_days: rotation ? Number.parseInt(rotation, 10) : 90,
      agent: agent.trim() || null,
    };
    setSaving(true);
    try {
      await api.post<{ ok: true; name: string }>("/api/vault/secrets", body);
      toast.push(isEdit ? t("secret.updated") : t("secret.stored"));
      onSaved();
    } catch (err) {
      toast.push(errMessage(err, t("secret.saveFailed")), "error");
      setSaving(false);
    }
  }

  return (
    <Overlay onClose={saving ? () => {} : onClose} side>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14, height: "100%" }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 16 }}>
            {existing ? t("secret.edit.title", { name: existing.name }) : t("secret.new.title")}
          </h2>
          {!isEdit && (
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
              {t("secret.new.subtitle")}
            </p>
          )}
        </div>

        {/* ── scope: common vs per-agent override ── */}
        {lockedScope ? (
          agent ? (
            <div className="badge" style={{ alignSelf: "flex-start" }}>
              {t("secret.scope.editing", { agent })}
            </div>
          ) : null
        ) : (
          <div className="field">
            <label>{t("secret.scope")}</label>
            <select className="input" value={agent} onChange={(e) => setAgent(e.target.value)}>
              <option value="">{t("secret.scope.common")}</option>
              {agents.map((a) => (
                <option key={a} value={a}>
                  {t("secret.scope.override", { agent: a })}
                </option>
              ))}
            </select>
            <span className="muted" style={{ fontSize: 11 }}>
              {isOverride ? t("secret.scope.hint.override") : t("secret.scope.hint.common")}
            </span>
          </div>
        )}

        <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
          <div className="field" style={{ flex: 1 }}>
            <label>{t("secret.name")}</label>
            <input
              className="input"
              value={name}
              readOnly={nameLocked}
              placeholder="OPENAI_API_KEY"
              onChange={(e) => setName(e.target.value)}
              style={{ fontFamily: "var(--font-mono)" }}
            />
            <span className="muted" style={{ fontSize: 11 }}>
              {t("secret.name.hint.prefix")}
              <code>{"${vault:NAME}"}</code>
              {t("secret.name.hint.suffix")}
            </span>
          </div>
          {!isOverride && (
            <div className="field" style={{ flex: 1 }}>
              <label>{t("secret.category")}</label>
              <input
                className="input"
                value={category}
                placeholder="api-keys"
                onChange={(e) => setCategory(e.target.value)}
              />
            </div>
          )}
        </div>

        <div className="field">
          <label>
            {t("secret.value")}{" "}
            {isEdit && (
              <span className="muted" style={{ fontSize: 11, fontWeight: 400 }}>
                {t("secret.value.keepHint")}
              </span>
            )}
          </label>
          <div className="row" style={{ gap: 6 }}>
            <input
              className="input"
              type={showValue ? "text" : "password"}
              value={value}
              autoComplete="new-password"
              placeholder={isEdit ? "••••••••" : t("secret.value.placeholder")}
              onChange={(e) => setValue(e.target.value)}
            />
            <button
              type="button"
              className="btn"
              style={{ flexShrink: 0 }}
              onClick={() => setShowValue((s) => !s)}
            >
              {showValue ? t("field.hide") : t("field.show")}
            </button>
          </div>
        </div>

        <div className="field">
          <label>{t("secret.description")}</label>
          <input
            className="input"
            value={description}
            placeholder={t("secret.description.placeholder")}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>

        <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
          <div className="field" style={{ flex: 1 }}>
            <label>
              {t("secret.expiresIn")}{" "}
              <span className="muted" style={{ fontSize: 11, fontWeight: 400 }}>
                {t("secret.optional")}
              </span>
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={3650}
              value={expiresIn}
              placeholder={t("secret.expiresIn.placeholder")}
              onChange={(e) => setExpiresIn(e.target.value)}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>{t("secret.rotation")}</label>
            <input
              className="input"
              type="number"
              min={1}
              value={rotation}
              onChange={(e) => setRotation(e.target.value)}
            />
          </div>
        </div>

        <div className="row" style={{ justifyContent: "flex-end", marginTop: "auto", paddingTop: 8 }}>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            {t("btn.cancel")}
          </button>
          <button type="submit" className="btn primary" disabled={saving}>
            {isEdit ? t("secret.save") : t("secret.add")}
          </button>
        </div>
      </form>
    </Overlay>
  );
}

// ── change passphrase (rekey) drawer ────────────────────────────────────────
export function RekeyDrawer({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const t = useT(STR);
  const toast = useToast();
  const [oldPass, setOldPass] = useState("");
  const [newPass, setNewPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    if (!oldPass || !newPass) {
      toast.push(t("rekey.bothRequired"), "error");
      return;
    }
    if (newPass !== confirm) {
      toast.push(t("rekey.mismatch"), "error");
      return;
    }
    if (newPass.length < 8) {
      toast.push(t("rekey.tooShort"), "error");
      return;
    }
    setSaving(true);
    try {
      const res = await api.post<RekeyResponse>("/api/vault/rekey", {
        old_passphrase: oldPass,
        new_passphrase: newPass,
      });
      toast.push(
        res.rekeyed === 1
          ? t("rekey.done", { count: String(res.rekeyed) })
          : t("rekey.done.plural", { count: String(res.rekeyed) }),
      );
      onSaved();
    } catch (err) {
      toast.push(errMessage(err, t("rekey.failed")), "error");
      setSaving(false);
    }
  }

  return (
    <Overlay onClose={saving ? () => {} : onClose} side>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14, height: "100%" }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>{t("rekey.title")}</h2>

        {/* Same anchor value as the onboarding vault step: one passphrase, so
            one entry in the password manager, whichever surface rotates it. */}
        <UsernameAnchor value="olano-vault" />
        <div className="field">
          <label>{t("rekey.current")}</label>
          <PasswordField
            value={oldPass}
            onChange={setOldPass}
            autoComplete="current-password"
            name="current-passphrase"
          />
        </div>
        <div className="field">
          <label>{t("rekey.new")}</label>
          <PasswordField
            value={newPass}
            onChange={setNewPass}
            autoComplete="new-password"
            name="new-passphrase"
            placeholder={t("rekey.new.placeholder")}
          />
        </div>
        <div className="field">
          <label>{t("rekey.confirm")}</label>
          <PasswordField
            value={confirm}
            onChange={setConfirm}
            autoComplete="new-password"
            name="confirm-passphrase"
          />
        </div>

        <div
          style={{
            background: "var(--warn-soft)",
            color: "var(--warn)",
            borderRadius: "var(--r-sm)",
            padding: 12,
            fontSize: 12,
          }}
        >
          <strong>{t("rekey.warning.bold")}</strong>
          {t("rekey.warning.prefix")}
          <code>OLANO_VAULT_KEY=&lt;new&gt;</code>
          {t("rekey.warning.mid")}
          <code>olano vault</code>
          {t("rekey.warning.suffix")}
        </div>

        <div className="row" style={{ justifyContent: "flex-end", marginTop: "auto", paddingTop: 8 }}>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            {t("btn.cancel")}
          </button>
          <button type="submit" className="btn danger" disabled={saving}>
            {t("rekey.submit")}
          </button>
        </div>
      </form>
    </Overlay>
  );
}

// ── reveal value dialog ─────────────────────────────────────────────────────
export function RevealDialog({ secret, onClose }: { secret: SecretValue; onClose: () => void }) {
  const t = useT(STR);
  const toast = useToast();

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret.value);
      toast.push(t("reveal.copied"));
    } catch {
      toast.push(t("reveal.copyFailed"), "error");
    }
  }

  return (
    <Overlay onClose={onClose} width={440}>
      <div className="row" style={{ gap: 10, alignItems: "center", minWidth: 0 }}>
        <span
          aria-hidden
          style={{
            width: 30,
            height: 30,
            borderRadius: "var(--r-sm)",
            background: "var(--accent-gradient-soft)",
            border: "1px solid color-mix(in srgb, var(--accent) 22%, transparent)",
            display: "grid",
            placeItems: "center",
            color: "var(--accent)",
            flexShrink: 0,
          }}
        >
          <Eye size={15} strokeWidth={1.75} />
        </span>
        <h2
          style={{
            margin: 0,
            fontSize: 16,
            fontFamily: "var(--font-mono)",
            wordBreak: "break-all",
            minWidth: 0,
          }}
        >
          {secret.name}
        </h2>
      </div>
      <p className="muted" style={{ margin: "10px 0 0" }}>
        {t("reveal.subtitle")}
      </p>
      <pre
        style={{
          userSelect: "all",
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          background: "var(--code-bg)",
          border: "1px solid var(--code-border)",
          borderRadius: "var(--r-sm)",
          padding: 12,
          margin: "10px 0 0",
          maxHeight: 160,
          overflow: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
        }}
      >
        {secret.value}
      </pre>
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn" onClick={onClose}>
          {t("reveal.close")}
        </button>
        <button className="btn primary" onClick={() => void copy()}>
          {t("reveal.copy")}
        </button>
      </div>
    </Overlay>
  );
}

// ── delete confirmation ─────────────────────────────────────────────────────
export function ConfirmDeleteDialog({
  name,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useT(STR);
  return (
    <Overlay onClose={busy ? () => {} : onCancel} width={400}>
      <div className="row" style={{ gap: 10, alignItems: "center" }}>
        <span
          aria-hidden
          style={{
            width: 30,
            height: 30,
            borderRadius: "var(--r-sm)",
            background: "var(--danger-soft)",
            border: "1px solid color-mix(in srgb, var(--danger) 40%, transparent)",
            display: "grid",
            placeItems: "center",
            color: "var(--danger)",
            flexShrink: 0,
          }}
        >
          <Trash2 size={15} strokeWidth={1.75} />
        </span>
        <h2 style={{ margin: 0, fontSize: 16 }}>{t("delete.title", { name })}</h2>
      </div>
      <p className="muted" style={{ margin: "10px 0 0" }}>
        {t("delete.message", { name })}
      </p>
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn" onClick={onCancel} disabled={busy}>
          {t("btn.cancel")}
        </button>
        <button className="btn danger" onClick={onConfirm} disabled={busy}>
          {busy ? t("delete.deleting") : t("delete.confirm")}
        </button>
      </div>
    </Overlay>
  );
}
