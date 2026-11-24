import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Eye, KeyRound, Lock, LockOpen, Pencil, Trash2 } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { StatusDot } from "./components";
import { ENC_LABEL, errMessage, expiresSoon, isExpired, relativeTime } from "./helpers";
import { useVaultMonitor } from "./VaultMonitor";
import { ConfirmDeleteDialog, EmptyState, RekeyDrawer, RevealDialog, SecretDrawer } from "./modals";
import { STR } from "./strings";
import type { SecretMetadata, SecretValue } from "./types";

// A "pending action" is an operation that requires an unlocked vault. If the
// vault is locked when the user clicks Reveal / Edit / Delete / New, we stash
// the intent, pop the unlock modal, and replay it on success — mirroring the
// original dashboard's `openVaultUnlock(() => action())` callback pattern.
type PendingAction =
  | { kind: "new" }
  | { kind: "override"; name: string }
  | { kind: "edit"; name: string; agent?: string | null }
  | { kind: "reveal"; name: string; agent?: string | null }
  | { kind: "delete"; name: string; agent?: string | null };

// What the SecretDrawer is editing/creating.
type EditTarget = SecretMetadata | "new" | { overrideFor: string };

export function VaultView() {
  const t = useT(STR);
  const toast = useToast();
  // Status + the unlock prompt are owned by the app-level VaultMonitorProvider,
  // so this view shares one source of truth (and one modal) with the monitor.
  const { status, refresh, requestUnlock } = useVaultMonitor();
  const [secrets, setSecrets] = useState<SecretMetadata[] | null>(null);
  const secretsRef = useRef(secrets);
  secretsRef.current = secrets;
  const [listError, setListError] = useState<string | null>(null);
  const [listLocked, setListLocked] = useState(false);

  // overlay state
  const [rekeyOpen, setRekeyOpen] = useState(false);
  const [editing, setEditing] = useState<EditTarget | null>(null);
  const [revealing, setRevealing] = useState<SecretValue | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ name: string; agent?: string | null } | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(() => {
    // Keep the current rows on screen while refetching — a manual refresh (or
    // post-mutation reload) shouldn't flash the whole list to a spinner. The
    // initial state is null, so the first load still shows one.
    setListError(null);
    setListLocked(false);
    void refresh();
    api
      .get<SecretMetadata[]>("/api/vault/secrets?include_overrides=true")
      .then(setSecrets)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 503) {
          setSecrets([]);
          setListLocked(true);
        } else if (secretsRef.current === null || secretsRef.current.length === 0) {
          // First load failed → the inline error state (with retry).
          setSecrets([]);
          setListError(errMessage(err, t("toast.loadFailed")));
        } else {
          // Refresh failed → keep the stale rows, surface a toast.
          toast.push(errMessage(err, t("toast.loadFailed")), "error");
        }
      });
  }, [refresh, toast, t]);

  useEffect(load, [load]);

  // Reload the list when the vault transitions to unlocked while we were showing
  // the locked empty-state — e.g. the app-level monitor unlocked it from another
  // view. Without this the locked placeholder would linger until a manual refresh.
  const unlocked = status?.unlocked ?? false;
  useEffect(() => {
    if (unlocked && listLocked) load();
  }, [unlocked, listLocked, load]);

  // ── reveal / delete (need an unlocked vault) ──────────────────────────────
  const scopeQuery = (agent?: string | null) => (agent ? `?agent=${encodeURIComponent(agent)}` : "");

  const doReveal = useCallback(
    async (name: string, agent?: string | null) => {
      try {
        const v = await api.get<SecretValue>(
          `/api/vault/secrets/${encodeURIComponent(name)}/reveal${scopeQuery(agent)}`,
        );
        setRevealing(v);
      } catch (err) {
        toast.push(errMessage(err, t("toast.revealFailed")), "error");
      }
    },
    [toast, t],
  );

  const doDelete = useCallback(
    async (name: string, agent?: string | null) => {
      setDeleting(true);
      try {
        await api.del(`/api/vault/secrets/${encodeURIComponent(name)}${scopeQuery(agent)}`);
        toast.push(t("toast.deleted"));
        setConfirmDelete(null);
        load();
      } catch (err) {
        toast.push(errMessage(err, t("toast.deleteFailed")), "error");
      } finally {
        setDeleting(false);
      }
    },
    [toast, load, t],
  );

  const runAction = useCallback(
    (action: PendingAction) => {
      switch (action.kind) {
        case "new":
          setEditing("new");
          break;
        case "override":
          setEditing({ overrideFor: action.name });
          break;
        case "edit": {
          const cur = (secrets ?? []).find(
            (s) => s.name === action.name && (s.agent ?? null) === (action.agent ?? null),
          );
          if (cur) setEditing(cur);
          break;
        }
        case "reveal":
          void doReveal(action.name, action.agent);
          break;
        case "delete":
          setConfirmDelete({ name: action.name, agent: action.agent });
          break;
      }
    },
    [secrets, doReveal],
  );

  // Open an action, gating behind unlock when the vault is currently locked.
  // The unlock prompt is shared with the app-level monitor; on success we
  // reload and replay the deferred action against the freshly-loaded data.
  const guard = useCallback(
    (action: PendingAction) => {
      if (status && !status.unlocked) {
        requestUnlock(() => {
          load();
          window.setTimeout(() => runAction(action), 0);
        });
        return;
      }
      runAction(action);
    },
    [status, requestUnlock, load, runAction],
  );

  // ── split common vs per-agent overrides; group commons by category ────────
  const allRows = secrets ?? [];
  const commons = allRows.filter((s) => !s.agent);
  const overridesByName = new Map<string, SecretMetadata[]>();
  for (const s of allRows) {
    if (!s.agent) continue;
    const arr = overridesByName.get(s.name) ?? [];
    arr.push(s);
    overridesByName.set(s.name, arr);
  }
  const groups = new Map<string, SecretMetadata[]>();
  for (const s of commons) {
    const cat = s.category || "general";
    const arr = groups.get(cat) ?? [];
    arr.push(s);
    groups.set(cat, arr);
  }
  // Overrides whose common key no longer exists (agent-only secrets) — surface
  // them in a synthetic group so they remain visible/manageable.
  const commonNames = new Set(commons.map((s) => s.name));
  const orphanOverrides = [...overridesByName.entries()]
    .filter(([n]) => !commonNames.has(n))
    .flatMap(([, rows]) => rows);

  // One row (common or per-agent override). `indent` nests an override under
  // its common key; the agent badge marks the scope.
  const renderSecretRow = (s: SecretMetadata, indent = false) => {
    const agent = s.agent ?? null;
    return (
      <div
        key={`${s.name}::${agent ?? ""}`}
        className="row vault-row"
        style={{
          justifyContent: "space-between",
          padding: "12px 16px",
          paddingLeft: indent ? 40 : 16,
          borderTop: "1px solid var(--border)",
          gap: 12,
          // On a phone the actions drop under the name instead of squeezing it
          // down to nothing (the name is the one thing the row is for).
          flexWrap: "wrap",
          transition: "background var(--t-fast) var(--ease)",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "var(--surface-2)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "";
        }}
      >
        <div className="row" style={{ gap: 12, minWidth: 0, flex: "1 1 220px" }}>
          <span
            style={{
              width: 32,
              height: 32,
              borderRadius: "var(--r-sm)",
              background: agent ? "var(--warn-soft)" : "var(--accent-gradient-soft)",
              border: `1px solid ${
                agent
                  ? "color-mix(in srgb, var(--warn) 30%, transparent)"
                  : "color-mix(in srgb, var(--accent) 22%, transparent)"
              }`,
              display: "grid",
              placeItems: "center",
              color: agent ? "var(--warn)" : "var(--accent)",
              flexShrink: 0,
            }}
          >
            <KeyRound size={16} strokeWidth={1.75} />
          </span>
          <div style={{ minWidth: 0 }}>
            <div className="row" style={{ gap: 8, fontFamily: "var(--font-mono)", fontWeight: 600 }}>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{s.name}</span>
              <span
                className="badge"
                style={
                  agent
                    ? {
                        background: "var(--warn-soft)",
                        color: "var(--warn)",
                        borderColor: "color-mix(in srgb, var(--warn) 35%, transparent)",
                        fontFamily: "var(--font-sans)",
                      }
                    : { fontFamily: "var(--font-sans)" }
                }
              >
                {agent ? t("scope.agent", { agent }) : t("scope.common")}
              </span>
            </div>
            <div
              className="muted"
              style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {s.description || t("row.placeholder")} · {t("row.updated", { time: relativeTime(s.updated_at, t) })}
              {s.last_used && t("row.used", { time: relativeTime(s.last_used, t) })}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 6, flexShrink: 0, marginLeft: "auto" }}>
          {s.expires_at &&
            (isExpired(s.expires_at) ? (
              <span
                className="badge"
                style={{
                  background: "var(--danger-soft)",
                  color: "var(--danger)",
                  borderColor: "color-mix(in srgb, var(--danger) 35%, transparent)",
                }}
                title={t("badge.expired.title", { time: relativeTime(s.expires_at, t) })}
              >
                {t("badge.expired")}
              </span>
            ) : (
              <span
                className="badge"
                style={
                  expiresSoon(s.expires_at)
                    ? {
                        background: "var(--warn-soft)",
                        color: "var(--warn)",
                        borderColor: "color-mix(in srgb, var(--warn) 35%, transparent)",
                      }
                    : undefined
                }
              >
                {t("badge.expires", { time: relativeTime(s.expires_at, t) })}
              </span>
            ))}
          {!agent && (
            <button
              className="btn"
              title={t("row.addOverride")}
              onClick={() => guard({ kind: "override", name: s.name })}
            >
              {t("row.addOverride")}
            </button>
          )}
          <button
            className="btn"
            title={t("row.reveal")}
            aria-label={t("row.reveal")}
            onClick={() => guard({ kind: "reveal", name: s.name, agent })}
          >
            <Eye size={15} strokeWidth={1.75} />
          </button>
          <button
            className="btn"
            title={t("row.edit")}
            aria-label={t("row.edit")}
            onClick={() => guard({ kind: "edit", name: s.name, agent })}
          >
            <Pencil size={15} strokeWidth={1.75} />
          </button>
          <button
            className="btn danger"
            title={t("row.delete")}
            aria-label={t("row.delete")}
            onClick={() => guard({ kind: "delete", name: s.name, agent })}
          >
            <Trash2 size={15} strokeWidth={1.75} />
          </button>
        </div>
      </div>
    );
  };

  const keySource = status?.env_key_set
    ? { label: t("status.key.env"), warn: false }
    : status?.keyring_available
      ? { label: t("status.key.keyring"), warn: false }
      : { label: t("status.key.ephemeral"), warn: true };

  const vaultRef = (
    <code style={{ fontFamily: "var(--font-mono)" }}>{"${vault:NAME}"}</code>
  );

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <button className="btn" onClick={load} title={t("btn.refresh.title")}>
            {t("btn.refresh")}
          </button>
          <button className="btn" onClick={() => setRekeyOpen(true)}>
            {t("btn.changePassphrase")}
          </button>
          <button className="btn primary" onClick={() => guard({ kind: "new" })}>
            {t("btn.newSecret")}
          </button>
        </div>
      </div>

      <div className="page">
        <h1>{t("title")}</h1>
        <p className="muted">
          {t("subtitle.prefix")}
          {vaultRef}
          {t("subtitle.suffix")}
        </p>

        {/* ── status card ── */}
        <section className="card" style={{ padding: 18, marginTop: 16 }}>
          {status === null ? (
            <div className="spinner" />
          ) : (
            <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
              <span
                style={{
                  width: 40,
                  height: 40,
                  borderRadius: "var(--r-sm)",
                  background: status.unlocked ? "var(--accent-gradient-soft)" : "var(--danger-soft)",
                  border: `1px solid ${
                    status.unlocked
                      ? "color-mix(in srgb, var(--accent) 30%, transparent)"
                      : "color-mix(in srgb, var(--danger) 40%, transparent)"
                  }`,
                  display: "grid",
                  placeItems: "center",
                  color: status.unlocked ? "var(--accent)" : "var(--danger)",
                  flexShrink: 0,
                }}
              >
                {status.unlocked ? (
                  <LockOpen size={20} strokeWidth={1.75} />
                ) : (
                  <Lock size={20} strokeWidth={1.75} />
                )}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
                  <strong>{t("status.title")}</strong>
                  {secrets !== null && secrets.length > 0 && (
                    <span className="badge">
                      {secrets.length === 1
                        ? t("status.badge.secrets", { count: String(secrets.length) })
                        : t("status.badge.secrets.plural", { count: String(secrets.length) })}
                    </span>
                  )}
                </div>
                <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: "wrap" }}>
                  <span
                    className="badge"
                    style={{
                      gap: 6,
                      background: status.unlocked ? "var(--success-soft)" : "var(--danger-soft)",
                      color: status.unlocked ? "var(--success)" : "var(--danger)",
                      borderColor: status.unlocked
                        ? "color-mix(in srgb, var(--success) 35%, transparent)"
                        : "color-mix(in srgb, var(--danger) 35%, transparent)",
                    }}
                  >
                    <StatusDot tone={status.unlocked ? "success" : "danger"} />
                    {status.unlocked ? t("status.unlocked") : t("status.locked")}
                  </span>
                  <span
                    className="badge"
                    style={keySource.warn ? { background: "var(--warn-soft)", color: "var(--warn)" } : undefined}
                  >
                    {keySource.label}
                  </span>
                  <span className="muted" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>
                    · {ENC_LABEL}
                  </span>
                </div>
                {!status.unlocked && (
                  <>
                    <div style={{ marginTop: 10 }}>
                      <button className="btn primary" onClick={() => requestUnlock(load)}>
                        {t("btn.unlock")}
                      </button>
                    </div>
                    <p className="muted" style={{ marginTop: 6, fontSize: 12 }}>
                      {t("status.unlockHint")}
                    </p>
                  </>
                )}
              </div>
            </div>
          )}
        </section>

        {/* ── secrets list ── */}
        <div style={{ marginTop: 20 }}>
          {secrets === null ? (
            <div className="spinner" style={{ marginTop: 16 }} />
          ) : listLocked ? (
            <EmptyState
              title={t("empty.locked.title")}
              message={t("empty.locked.message")}
              actionLabel={t("btn.unlock")}
              onAction={() => requestUnlock(load)}
            />
          ) : listError ? (
            <EmptyState
              icon={<AlertTriangle size={30} strokeWidth={1.5} style={{ color: "var(--danger)" }} />}
              title={t("empty.error.title")}
              message={listError}
            />
          ) : secrets.length === 0 ? (
            <EmptyState
              title={t("empty.none.title")}
              message={
                <>
                  {t("empty.none.prefix")}
                  {vaultRef}
                  {t("empty.none.suffix")}
                </>
              }
              actionLabel={t("empty.none.action")}
              onAction={() => guard({ kind: "new" })}
            />
          ) : (
            <>
              {[...groups.entries()].map(([cat, rows]) => (
                <section key={cat} style={{ marginBottom: 22 }}>
                  <div className="row" style={{ marginBottom: 8 }}>
                    <h2 style={{ margin: 0, fontSize: 13, textTransform: "uppercase", letterSpacing: 0.4 }}>{cat}</h2>
                    <span className="badge">{rows.length}</span>
                  </div>
                  <div className="card">
                    {rows.map((s) => (
                      <Fragment key={s.name}>
                        {renderSecretRow(s)}
                        {(overridesByName.get(s.name) ?? []).map((o) => renderSecretRow(o, true))}
                      </Fragment>
                    ))}
                  </div>
                </section>
              ))}
              {orphanOverrides.length > 0 && (
                <section style={{ marginBottom: 22 }}>
                  <div className="row" style={{ marginBottom: 8 }}>
                    <h2 style={{ margin: 0, fontSize: 13, textTransform: "uppercase", letterSpacing: 0.4 }}>
                      {t("group.agentOnly")}
                    </h2>
                    <span className="badge">{orphanOverrides.length}</span>
                  </div>
                  <div className="card">{orphanOverrides.map((o) => renderSecretRow(o))}</div>
                </section>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── overlays (the unlock prompt is owned by VaultMonitorProvider) ── */}
      {editing && (
        <SecretDrawer
          existing={
            editing !== "new" && typeof editing === "object" && !("overrideFor" in editing) ? editing : null
          }
          presetName={
            typeof editing === "object" && "overrideFor" in editing ? editing.overrideFor : null
          }
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}

      {rekeyOpen && (
        <RekeyDrawer
          onClose={() => setRekeyOpen(false)}
          onSaved={() => {
            setRekeyOpen(false);
            load();
          }}
        />
      )}

      {revealing && <RevealDialog secret={revealing} onClose={() => setRevealing(null)} />}

      {confirmDelete && (
        <ConfirmDeleteDialog
          name={confirmDelete.agent ? `${confirmDelete.name} (agent: ${confirmDelete.agent})` : confirmDelete.name}
          busy={deleting}
          onCancel={() => setConfirmDelete(null)}
          onConfirm={() => void doDelete(confirmDelete.name, confirmDelete.agent)}
        />
      )}
    </>
  );
}
