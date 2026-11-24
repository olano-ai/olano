// Agent-scoped vault variables — the "Variables" tab in the agent edit view.
// Manages ONE agent's per-agent vault overrides via /api/agents/{name}/vault/*,
// which the backend gates with assert_agent_admin (global admin or that agent's
// owner; plain members 403). The tab button is hidden from members in
// AgentDetail (useAuth().canAdminAgent), and a direct deep-link renders the
// forbidden notice instead of data. Values are encrypted in the vault's
// credential_overrides table and win over the same-named common secret for
// this agent only — the org-wide vault view stays admin-only.

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Eye, KeyRound, Lock, Pencil, Plus, ShieldAlert, Trash2 } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { expiresSoon, isExpired, relativeTime } from "../vault/helpers";
import { STR as VAULT_STR } from "../vault/strings";
import { useVaultMonitor } from "../vault/VaultMonitor";
import { ConfirmModal, Field, Modal } from "./Modal";
import { STR } from "./strings";
import type { AgentVaultListResponse, AgentVaultSecret } from "./types";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

// What the editor modal is doing: creating (optionally with a preset name from
// a common key's "Override" button) or editing an existing override row.
type EditorTarget = { existing: AgentVaultSecret | null; presetName: string | null };

export function VariablesTab({ agentName }: { agentName: string }) {
  const t = useT(STR);
  // The vault view's relative-time phrases ("time.*") live in its own dict;
  // a second translator reuses them instead of duplicating the keys here.
  const tv = useT(VAULT_STR);
  const toast = useToast();
  const { user } = useAuth();
  const { requestUnlock } = useVaultMonitor();

  const [data, setData] = useState<AgentVaultListResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const [revealing, setRevealing] = useState<{ name: string; value: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const base = `/api/agents/${encodeURIComponent(agentName)}/vault/secrets`;

  const load = useCallback(() => {
    setLoadError(null);
    setForbidden(false);
    api
      .get<AgentVaultListResponse>(base)
      .then(setData)
      .catch((e) => {
        setData(null);
        if (e instanceof ApiError && e.status === 403) setForbidden(true);
        else setLoadError(errText(e, t("vars.loadFailed")));
      });
  }, [base, t]);

  useEffect(load, [load]);

  const doReveal = useCallback(
    async (name: string) => {
      try {
        const v = await api.get<{ name: string; value: string }>(
          `${base}/${encodeURIComponent(name)}/reveal`,
        );
        setRevealing({ name: v.name, value: v.value });
      } catch (e) {
        toast.push(errText(e, t("vars.revealFailed")), "error");
      }
    },
    [base, toast, t],
  );

  const doDelete = useCallback(
    async (name: string) => {
      setDeleting(true);
      try {
        await api.del(`${base}/${encodeURIComponent(name)}`);
        toast.push(t("vars.deleted"));
        setConfirmDelete(null);
        load();
      } catch (e) {
        toast.push(errText(e, t("vars.deleteFailed")), "error");
      } finally {
        setDeleting(false);
      }
    },
    [base, toast, t, load],
  );

  // ── error / gate states ──────────────────────────────────────────────────
  if (forbidden) {
    return (
      <Notice icon={<ShieldAlert size={26} strokeWidth={1.5} />} title={t("vars.forbidden.title")}>
        {t("vars.forbidden.msg")}
      </Notice>
    );
  }
  if (loadError) {
    return (
      <Notice icon={<KeyRound size={26} strokeWidth={1.5} />} title={t("vars.loadFailed")}>
        <span>{loadError}</span>
        <div style={{ marginTop: 10 }}>
          <button className="btn" onClick={load}>
            {t("btn.refresh")}
          </button>
        </div>
      </Notice>
    );
  }
  if (!data) {
    return (
      <div className="card" style={{ padding: 16 }}>
        <div className="spinner" />
      </div>
    );
  }

  if (!data.unlocked) {
    // A non-admin owner can't unlock the vault (that's an admin surface) —
    // show the ask-your-admin note instead of a dead-end unlock button.
    const isAdmin = !!user?.is_admin;
    return (
      <Notice icon={<Lock size={26} strokeWidth={1.5} />} title={t("vars.locked.title")}>
        <span>{isAdmin ? t("vars.locked.admin") : t("vars.locked.member")}</span>
        {isAdmin && (
          <div style={{ marginTop: 12 }}>
            <button className="btn primary" onClick={() => requestUnlock(load)}>
              {t("vars.unlock")}
            </button>
          </div>
        )}
      </Notice>
    );
  }

  const overridden = new Set(data.secrets.map((s) => s.name));
  const suggestions = data.common.map((c) => c.name).filter((n) => !overridden.has(n));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 860 }}>
      {/* ── this agent's overrides ── */}
      <div className="card" style={{ padding: 16 }}>
        <div className="row" style={{ justifyContent: "space-between", gap: 8, marginBottom: 4 }}>
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: 13 }}>{t("vars.title")}</strong>
            <div className="muted" style={{ fontSize: 12, marginTop: 2, lineHeight: 1.5 }}>
              {t("vars.subtitle", { name: agentName })}
            </div>
          </div>
          <button
            className="btn primary row"
            style={{ gap: 6, flexShrink: 0 }}
            onClick={() => setEditor({ existing: null, presetName: null })}
          >
            <Plus size={15} strokeWidth={1.75} /> {t("vars.add")}
          </button>
        </div>

        {data.secrets.length === 0 ? (
          <p className="muted" style={{ fontSize: 13, margin: "10px 0 2px" }}>
            {t("vars.empty", { name: agentName })}
          </p>
        ) : (
          <div style={{ marginTop: 10 }}>
            {data.secrets.map((s) => (
              <div
                key={s.name}
                className="row"
                style={{
                  justifyContent: "space-between",
                  gap: 10,
                  padding: "10px 0",
                  borderTop: "1px solid var(--border)",
                  flexWrap: "wrap",
                }}
              >
                <div className="row" style={{ gap: 10, minWidth: 0, flex: 1 }}>
                  <span
                    aria-hidden
                    style={{
                      width: 30,
                      height: 30,
                      borderRadius: "var(--r-sm)",
                      background: "var(--warn-soft)",
                      border: "1px solid color-mix(in srgb, var(--warn) 30%, transparent)",
                      display: "grid",
                      placeItems: "center",
                      color: "var(--warn)",
                      flexShrink: 0,
                    }}
                  >
                    <KeyRound size={14} strokeWidth={1.75} />
                  </span>
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        fontFamily: "var(--font-mono)",
                        fontWeight: 600,
                        fontSize: 13,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {s.name}
                    </div>
                    <div
                      className="muted"
                      style={{
                        fontSize: 11,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {s.description ? `${s.description} · ` : ""}
                      {t("vars.updated", { time: relativeTime(s.updated_at, tv) })}
                    </div>
                  </div>
                </div>
                <div className="row" style={{ gap: 6, flexShrink: 0 }}>
                  {s.expires_at && (
                    <span
                      className="badge"
                      style={
                        isExpired(s.expires_at)
                          ? {
                              background: "var(--danger-soft)",
                              color: "var(--danger)",
                              borderColor: "color-mix(in srgb, var(--danger) 35%, transparent)",
                            }
                          : expiresSoon(s.expires_at)
                            ? {
                                background: "var(--warn-soft)",
                                color: "var(--warn)",
                                borderColor: "color-mix(in srgb, var(--warn) 35%, transparent)",
                              }
                            : undefined
                      }
                    >
                      {isExpired(s.expires_at)
                        ? t("vars.expired")
                        : t("vars.expires", { time: relativeTime(s.expires_at, tv) })}
                    </span>
                  )}
                  <button
                    className="btn"
                    title={t("vars.reveal")}
                    aria-label={t("vars.reveal")}
                    onClick={() => void doReveal(s.name)}
                  >
                    <Eye size={14} strokeWidth={1.75} />
                  </button>
                  <button
                    className="btn"
                    title={t("vars.edit")}
                    aria-label={t("vars.edit")}
                    onClick={() => setEditor({ existing: s, presetName: null })}
                  >
                    <Pencil size={14} strokeWidth={1.75} />
                  </button>
                  <button
                    className="btn danger"
                    title={t("vars.delete")}
                    aria-label={t("vars.delete")}
                    onClick={() => setConfirmDelete(s.name)}
                  >
                    <Trash2 size={14} strokeWidth={1.75} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── shared (common) keys this agent can override ── */}
      <div className="card" style={{ padding: 16 }}>
        <strong style={{ fontSize: 13 }}>{t("vars.common.title")}</strong>
        <div className="muted" style={{ fontSize: 12, marginTop: 2, lineHeight: 1.5 }}>
          {t("vars.common.hint", { name: agentName })}
        </div>
        {data.common.length === 0 ? (
          <p className="muted" style={{ fontSize: 13, margin: "10px 0 2px" }}>
            {t("vars.common.empty")}
          </p>
        ) : (
          <div style={{ marginTop: 10 }}>
            {data.common.map((c) => (
              <div
                key={c.name}
                className="row"
                style={{
                  justifyContent: "space-between",
                  gap: 10,
                  padding: "8px 0",
                  borderTop: "1px solid var(--border)",
                }}
              >
                <div className="row" style={{ gap: 8, minWidth: 0 }}>
                  <span
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: 13,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {c.name}
                  </span>
                  <span className="badge">{c.category}</span>
                </div>
                {overridden.has(c.name) ? (
                  <span
                    className="badge"
                    style={{
                      background: "var(--warn-soft)",
                      color: "var(--warn)",
                      borderColor: "color-mix(in srgb, var(--warn) 35%, transparent)",
                      flexShrink: 0,
                    }}
                  >
                    {t("vars.common.overridden")}
                  </span>
                ) : (
                  <button
                    className="btn"
                    style={{ flexShrink: 0 }}
                    onClick={() => setEditor({ existing: null, presetName: c.name })}
                  >
                    {t("vars.common.override")}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── overlays ── */}
      {editor && (
        <VariableEditorModal
          agentName={agentName}
          existing={editor.existing}
          presetName={editor.presetName}
          suggestions={suggestions}
          onClose={() => setEditor(null)}
          onSaved={() => {
            setEditor(null);
            load();
          }}
        />
      )}

      {revealing && (
        <RevealModal secret={revealing} onClose={() => setRevealing(null)} />
      )}

      {confirmDelete && (
        <ConfirmModal
          title={t("vars.confirmDelete.title", { key: confirmDelete })}
          message={t("vars.confirmDelete.msg", { key: confirmDelete, name: agentName })}
          confirmLabel={t("vars.delete")}
          danger
          busy={deleting}
          onConfirm={() => void doDelete(confirmDelete)}
          onClose={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}

// ── notice card (locked / forbidden / error states) ──────────────────────────
function Notice({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="card" style={{ padding: 32, textAlign: "center" }}>
      <div style={{ display: "flex", justifyContent: "center", color: "var(--text-3)" }}>{icon}</div>
      <h2 style={{ fontSize: 15, margin: "10px 0 4px" }}>{title}</h2>
      <div className="muted" style={{ maxWidth: 460, margin: "0 auto", fontSize: 13, lineHeight: 1.55 }}>
        {children}
      </div>
    </div>
  );
}

// ── add / edit modal ─────────────────────────────────────────────────────────
function VariableEditorModal({
  agentName,
  existing,
  presetName,
  suggestions,
  onClose,
  onSaved,
}: {
  agentName: string;
  existing: AgentVaultSecret | null;
  presetName: string | null;
  suggestions: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const isEdit = existing !== null;
  const nameLocked = isEdit || !!presetName;
  const [name, setName] = useState(existing?.name ?? presetName ?? "");
  const [value, setValue] = useState("");
  const [showValue, setShowValue] = useState(false);
  const [description, setDescription] = useState(existing?.description ?? "");
  const [expiresIn, setExpiresIn] = useState("");
  const [rotation, setRotation] = useState("90");
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    const trimmed = name.trim();
    if (!trimmed) {
      toast.push(t("vars.nameRequired"), "error");
      return;
    }
    if (!isEdit && !value) {
      toast.push(t("vars.valueRequired"), "error");
      return;
    }
    // Edit with the value left blank → nothing to change (values are
    // write-only here; mirrors the Vault drawer's keep-current semantics).
    if (isEdit && !value) {
      toast.push(t("vars.noChange"));
      onClose();
      return;
    }
    setSaving(true);
    try {
      await api.post(`/api/agents/${encodeURIComponent(agentName)}/vault/secrets`, {
        name: trimmed,
        value,
        description: description.trim(),
        expires_in_days: expiresIn ? Number.parseInt(expiresIn, 10) : null,
        rotation_reminder_days: rotation ? Number.parseInt(rotation, 10) : 90,
      });
      toast.push(isEdit ? t("vars.updatedToast") : t("vars.stored"));
      onSaved();
    } catch (err) {
      toast.push(errText(err, t("vars.saveFailed")), "error");
      setSaving(false);
    }
  }

  return (
    <Modal
      title={
        isEdit
          ? t("vars.editor.editTitle", { key: existing.name })
          : t("vars.editor.newTitle", { name: agentName })
      }
      subtitle={t("vars.editor.subtitle", { name: agentName })}
      onClose={saving ? () => {} : onClose}
      width={520}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            {t("btn.cancel")}
          </button>
          <button type="submit" form="agent-var-editor" className="btn primary" disabled={saving}>
            {saving ? t("modal.working") : isEdit ? t("vars.editor.save") : t("vars.editor.add")}
          </button>
        </>
      }
    >
      <form id="agent-var-editor" onSubmit={submit}>
        <Field label={t("vars.editor.name")} hint={t("vars.editor.nameHint")}>
          <input
            className="input"
            value={name}
            readOnly={nameLocked}
            placeholder="OPENAI_API_KEY"
            list={nameLocked ? undefined : "agent-var-suggestions"}
            onChange={(e) => setName(e.target.value)}
            style={{ fontFamily: "var(--font-mono)" }}
            autoFocus={!nameLocked}
          />
          {!nameLocked && suggestions.length > 0 && (
            <datalist id="agent-var-suggestions">
              {suggestions.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          )}
        </Field>

        <Field
          label={t("vars.editor.value")}
          hint={isEdit ? t("vars.editor.valueKeepHint") : undefined}
        >
          <div className="row" style={{ gap: 6 }}>
            <input
              className="input"
              type={showValue ? "text" : "password"}
              value={value}
              autoComplete="new-password"
              placeholder={isEdit ? "••••••••" : t("vars.editor.valuePlaceholder")}
              onChange={(e) => setValue(e.target.value)}
              autoFocus={nameLocked}
            />
            <button
              type="button"
              className="btn"
              style={{ flexShrink: 0 }}
              onClick={() => setShowValue((s) => !s)}
            >
              {showValue ? t("vars.editor.hide") : t("vars.editor.show")}
            </button>
          </div>
        </Field>

        <Field label={t("vars.editor.description")}>
          <input
            className="input"
            value={description}
            placeholder={t("vars.editor.descriptionPlaceholder")}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Field label={t("vars.editor.expiresIn")} hint={t("vars.editor.optional")}>
            <input
              className="input"
              type="number"
              min={1}
              max={3650}
              value={expiresIn}
              placeholder={t("vars.editor.expiresPlaceholder")}
              onChange={(e) => setExpiresIn(e.target.value)}
            />
          </Field>
          <Field label={t("vars.editor.rotation")}>
            <input
              className="input"
              type="number"
              min={1}
              value={rotation}
              onChange={(e) => setRotation(e.target.value)}
            />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

// ── reveal modal ─────────────────────────────────────────────────────────────
function RevealModal({
  secret,
  onClose,
}: {
  secret: { name: string; value: string };
  onClose: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret.value);
      toast.push(t("vars.reveal.copied"));
    } catch {
      toast.push(t("vars.reveal.copyFailed"), "error");
    }
  }

  return (
    <Modal
      title={secret.name}
      subtitle={t("vars.reveal.subtitle")}
      onClose={onClose}
      width={440}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            {t("card.close")}
          </button>
          <button className="btn primary" onClick={() => void copy()}>
            {t("vars.reveal.copy")}
          </button>
        </>
      }
    >
      <pre
        style={{
          userSelect: "all",
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          background: "var(--code-bg)",
          border: "1px solid var(--code-border)",
          borderRadius: "var(--r-sm)",
          padding: 12,
          margin: 0,
          maxHeight: 160,
          overflow: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
        }}
      >
        {secret.value}
      </pre>
    </Modal>
  );
}
