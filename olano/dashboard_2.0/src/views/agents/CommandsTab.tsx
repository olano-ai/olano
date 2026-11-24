// Per-agent Commands tab — control which built-in chat slash commands are
// exposed to users, and define custom commands (a name → prompt sent to the
// agent). Applies to every chat surface (messaging platforms/channels + the
// dashboard chat).
//
// Built-in exposure is stored as *deviations* from the catalog defaults
// (sensitive commands default off), so a clean agent keeps an empty map.

import { useCallback, useEffect, useState } from "react";
import { Pencil, Plus, Trash2, Terminal } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useToast } from "../../ui/toast";
import { Modal, Field, ConfirmModal } from "./Modal";
import { Toggle } from "./Toggle";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import type { AgentConfig, CommandCatalogResponse, CommandCatalogEntry, CustomCommand } from "./types";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

export function CommandsTab({
  cfg,
  setCfg,
  savePatch,
}: {
  cfg: AgentConfig;
  setCfg: (c: AgentConfig) => void;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [catalog, setCatalog] = useState<CommandCatalogResponse | null>(null);
  const [editing, setEditing] = useState<{ index: number; cmd: CustomCommand } | null>(null);
  const [confirmDel, setConfirmDel] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<CommandCatalogResponse>("/api/catalog/commands")
      .then((c) => alive && setCatalog(c))
      .catch((e) => alive && toast.push(errText(e, t("cmd.toast.loadFailed")), "error"));
    return () => {
      alive = false;
    };
  }, [toast, t]);

  const enabledMap = cfg.commands?.enabled || {};
  const custom = cfg.commands?.custom || [];

  // Effective exposure = saved override, else the catalog default.
  const isExposed = (c: CommandCatalogEntry): boolean =>
    c.name in enabledMap ? !!enabledMap[c.name] : c.default_exposed;

  // Persist the whole `commands` block (the PUT replaces it wholesale), keeping
  // only deviations from the catalog default in `enabled` so configs stay tidy.
  const persist = useCallback(
    (nextEnabled: Record<string, boolean>, nextCustom: CustomCommand[]) => {
      const commands = { enabled: nextEnabled, custom: nextCustom };
      setCfg({ ...cfg, commands });
      void savePatch({ commands });
    },
    [cfg, setCfg, savePatch],
  );

  const toggle = (c: CommandCatalogEntry, next: boolean) => {
    const map = { ...enabledMap };
    if (next === c.default_exposed) delete map[c.name];
    else map[c.name] = next;
    persist(map, custom);
  };

  const saveCustom = (cmd: CustomCommand, index: number) => {
    const name = cmd.name.trim().replace(/^\//, "").toLowerCase();
    if (!name || !/^[a-z0-9][a-z0-9_-]*$/.test(name)) {
      toast.push(t("cmd.toast.nameInvalid"), "error");
      return;
    }
    if (!cmd.prompt.trim()) {
      toast.push(t("cmd.toast.promptRequired"), "error");
      return;
    }
    if (catalog?.commands.some((c) => c.name === name || c.aliases.includes(name))) {
      toast.push(t("cmd.toast.collides", { name }), "error");
      return;
    }
    if (custom.some((c, i) => i !== index && c.name === name)) {
      toast.push(t("cmd.toast.alreadyDefined", { name }), "error");
      return;
    }
    const next = [...custom];
    const entry = { ...cmd, name };
    if (index < 0) next.push(entry);
    else next[index] = entry;
    persist(enabledMap, next);
    setEditing(null);
  };

  const removeCustom = (index: number) => {
    persist(
      enabledMap,
      custom.filter((_, i) => i !== index),
    );
    setConfirmDel(null);
  };

  if (!catalog) return <p className="muted">{t("cmd.loading")}</p>;

  const groupsWith = catalog.groups
    .map((g) => ({ ...g, items: catalog.commands.filter((c) => c.group === g.id) }))
    .filter((g) => g.items.length > 0);

  return (
    <>
      <div style={{ marginBottom: 16 }}>
        <strong style={{ fontSize: 15 }}>{t("cmd.title")}</strong>
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
          {t("cmd.introA")} <strong>{t("cmd.messagingPlatforms")}</strong>{" "}
          {t("cmd.introB")}{" "}
          <strong>{t("cmd.dashboardChat")}</strong> {t("cmd.introC")}
        </p>
        <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
          {t("cmd.applyNote")}
        </p>
      </div>

      {groupsWith.map((g) => (
        <div key={g.id} style={{ marginBottom: 18 }}>
          <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
            {g.label}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {g.items.map((c) => {
              const on = isExposed(c);
              return (
                <div
                  key={c.name}
                  data-command={c.name}
                  className="row"
                  style={{
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: 12,
                    padding: "8px 10px",
                    borderRadius: "var(--r-sm)",
                    background: "var(--surface-2)",
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div className="row" style={{ gap: 8, alignItems: "center" }}>
                      <code style={{ fontSize: 13, opacity: on ? 1 : 0.55 }}>/{c.name}</code>
                      {c.sensitive && (
                        <span
                          style={{
                            fontSize: 10,
                            fontWeight: 700,
                            textTransform: "uppercase",
                            letterSpacing: 0.4,
                            color: "var(--warn, #d4860b)",
                            border: "1px solid var(--warn, #d4860b)",
                            borderRadius: "var(--r-pill)",
                            padding: "0 7px",
                          }}
                        >
                          {t("cmd.sensitiveBadge")}
                        </span>
                      )}
                    </div>
                    <div className="muted" style={{ fontSize: 11.5, marginTop: 1 }}>
                      {c.description}
                      {c.aliases.length > 0 && (
                        <span style={{ opacity: 0.7 }}> {t("cmd.akaLabel")} {c.aliases.map((a) => `/${a}`).join(", ")}</span>
                      )}
                    </div>
                  </div>
                  <Toggle checked={on} onChange={(v) => toggle(c, v)} />
                </div>
              );
            })}
          </div>
        </div>
      ))}

      {/* ── Custom commands ──────────────────────────────────────────────── */}
      <div style={{ marginTop: 24, borderTop: "1px solid var(--border)", paddingTop: 16 }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <strong style={{ fontSize: 15 }}>{t("cmd.customTitle")}</strong>
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
              {t("cmd.customIntroA")} <code>/your-command</code> {t("cmd.customIntroB")}{" "}
              <code>{"{args}"}</code> {t("cmd.customIntroC")}
            </p>
          </div>
          <button
            className="btn primary row"
            style={{ gap: 6, flexShrink: 0 }}
            onClick={() => setEditing({ index: -1, cmd: { name: "", prompt: "", description: "" } })}
          >
            <Plus size={15} strokeWidth={1.75} /> {t("cmd.add")}
          </button>
        </div>

        {custom.length === 0 ? (
          <div
            className="muted"
            style={{ fontSize: 12.5, padding: "18px 0", display: "flex", alignItems: "center", gap: 8 }}
          >
            <Terminal size={15} strokeWidth={1.75} /> {t("cmd.emptyCustom")}
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 12 }}>
            {custom.map((c, i) => (
              <div
                key={`${c.name}-${i}`}
                data-custom={c.name}
                className="row"
                style={{
                  justifyContent: "space-between",
                  alignItems: "flex-start",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: "var(--r-sm)",
                  background: "var(--surface-2)",
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <code style={{ fontSize: 13 }}>/{c.name}</code>
                  {c.description && (
                    <span className="muted" style={{ fontSize: 11.5, marginLeft: 8 }}>
                      {c.description}
                    </span>
                  )}
                  <div
                    className="muted"
                    style={{ fontSize: 11.5, marginTop: 3, whiteSpace: "pre-wrap", opacity: 0.85 }}
                  >
                    {c.prompt}
                  </div>
                </div>
                <div className="row" style={{ gap: 4, flexShrink: 0 }}>
                  <button className="btn" style={{ padding: "4px 8px" }} onClick={() => setEditing({ index: i, cmd: { ...c } })} aria-label={t("wh.edit")}>
                    <Pencil size={14} strokeWidth={1.75} />
                  </button>
                  <button className="btn danger" style={{ padding: "4px 8px" }} onClick={() => setConfirmDel(i)} aria-label={t("cmd.delete")}>
                    <Trash2 size={14} strokeWidth={1.75} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {editing && (
        <Modal
          title={editing.index < 0 ? t("cmd.modal.titleAdd") : t("cmd.modal.titleEdit", { name: editing.cmd.name })}
          subtitle={t("cmd.modal.subtitle")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="btn" onClick={() => setEditing(null)}>
                {t("common.cancel")}
              </button>
              <button className="btn primary" onClick={() => saveCustom(editing.cmd, editing.index)}>
                {t("vars.editor.save")}
              </button>
            </>
          }
        >
          <Field label={t("cmd.field.name")} hint={t("cmd.field.nameHint")}>
            <input
              value={editing.cmd.name}
              onChange={(e) => setEditing({ ...editing, cmd: { ...editing.cmd, name: e.target.value } })}
              placeholder={t("cmd.field.namePlaceholder")}
            />
          </Field>
          <Field label={t("cmd.field.description")} hint={t("cmd.field.descriptionHint")}>
            <input
              value={editing.cmd.description || ""}
              onChange={(e) => setEditing({ ...editing, cmd: { ...editing.cmd, description: e.target.value } })}
              placeholder={t("cmd.field.descriptionPlaceholder")}
            />
          </Field>
          <Field label={t("cmd.field.prompt")} hint={t("cmd.field.promptHint")}>
            <textarea
              value={editing.cmd.prompt}
              onChange={(e) => setEditing({ ...editing, cmd: { ...editing.cmd, prompt: e.target.value } })}
              rows={5}
              placeholder={t("cmd.field.promptPlaceholder")}
            />
          </Field>
        </Modal>
      )}

      {confirmDel !== null && (
        <ConfirmModal
          title={t("cmd.confirmDelete.title", { name: custom[confirmDel]?.name })}
          message={t("cmd.confirmDelete.msg")}
          confirmLabel={t("cmd.delete")}
          danger
          onConfirm={() => removeCustom(confirmDel)}
          onClose={() => setConfirmDel(null)}
        />
      )}
    </>
  );
}
