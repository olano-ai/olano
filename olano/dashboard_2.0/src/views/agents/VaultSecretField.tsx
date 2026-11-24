// A connector credential, edited where it actually lives: the vault.
//
// This field used to be an ordinary text input bound to the channel config, so
// after the first save it displayed the literal string
// "${vault:TELEGRAM_BOT_TOKEN}" and the eye toggle revealed *the reference*.
// There was no way to see or confirm the real token from the connector panel.
//
// Now the credential never round-trips through agent.json5 at all. Every
// connector resolves one canonical key (telegram → TELEGRAM_BOT_TOKEN) scoped
// to the agent, so this reads that key's per-agent override through
// /api/vault/secrets — masked by default, revealed on demand, and every
// operation gated behind an unlocked vault exactly like the Vault view.
//
// What the user types is the form's state (`draft`), not this field's: the
// connector's own Save stores it in the vault before it saves the connector
// (SubModals.tsx, ChannelModal). The field used to hold the draft itself
// behind a separate "Save to vault" button, so a token typed here and followed
// by the form's Save was dropped without a word, and the connector then would
// not start for want of the credential the user had just entered.

import { useCallback, useEffect, useState } from "react";
import { Check, Eye, EyeOff, LockKeyhole, ShieldAlert, Sparkles, Trash2 } from "lucide-react";
import { api } from "../../core/api";
import { useT, type LocalDict } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useVaultMonitor } from "../vault/VaultMonitor";
import { CopyButton } from "./CopyButton";

const STR: LocalDict = {
  en: {
    "scope": "Per-agent override",
    "set": "Set for this agent",
    "shared": "Using the shared value",
    "sharedHelp": "Other agents read the same value — set an override so this agent is its own bot.",
    "missing": "Not set — this connector will not start",
    "locked": "Vault is locked — unlock to view or change this credential.",
    "unlock": "Unlock the vault",
    "reveal": "Show value",
    "hide": "Hide value",
    "clear": "Clear override",
    "placeholder": "Paste the credential",
    "pending": "Stored in the vault for this agent when you save the connector.",
    "updated": "updated {date}",
    "cleared": "Override removed.",
    "err.load": "Could not read the vault.",
    "err.save": "Could not save to the vault.",
    "err.reveal": "Could not reveal the value.",
    "generate": "Generate a key",
    "generated": "Key generated. Review it, then save the connector.",
    "copy": "Copy value",
    "err.generate": "This browser cannot generate a key securely. Paste one instead.",
  },
  zh: {
    "scope": "按智能体覆盖",
    "set": "已为该智能体设置",
    "shared": "正在使用共享值",
    "sharedHelp": "其他智能体读取同一个值——设置覆盖值可让该智能体成为独立机器人。",
    "missing": "未设置——该连接器将无法启动",
    "locked": "保险库已锁定——解锁后才能查看或修改。",
    "unlock": "解锁保险库",
    "reveal": "显示值",
    "hide": "隐藏值",
    "clear": "清除覆盖值",
    "placeholder": "粘贴凭据",
    "pending": "保存连接器时存入该智能体的保险库。",
    "updated": "更新于 {date}",
    "cleared": "覆盖值已删除。",
    "err.load": "无法读取保险库。",
    "err.save": "无法保存到保险库。",
    "err.reveal": "无法显示该值。",
    "generate": "生成密钥",
    "generated": "已生成密钥,确认后保存连接器。",
    "copy": "复制值",
    "err.generate": "此浏览器无法安全生成密钥,请手动粘贴。",
  },
  es: {
    "scope": "Anulación por agente",
    "set": "Configurado para este agente",
    "shared": "Usando el valor compartido",
    "sharedHelp": "Otros agentes leen el mismo valor — define una anulación para que este agente sea su propio bot.",
    "missing": "Sin configurar — este conector no arrancará",
    "locked": "La bóveda está bloqueada — desbloquéala para ver o cambiar la credencial.",
    "unlock": "Desbloquear la bóveda",
    "reveal": "Mostrar valor",
    "hide": "Ocultar valor",
    "clear": "Quitar anulación",
    "placeholder": "Pega la credencial",
    "pending": "Se guarda en la bóveda para este agente al guardar el conector.",
    "updated": "actualizado {date}",
    "cleared": "Anulación eliminada.",
    "err.load": "No se pudo leer la bóveda.",
    "err.save": "No se pudo guardar en la bóveda.",
    "err.reveal": "No se pudo mostrar el valor.",
    "generate": "Generar una clave",
    "generated": "Clave generada. Revísala y guarda el conector.",
    "copy": "Copiar valor",
    "err.generate": "Este navegador no puede generar una clave de forma segura. Pega una en su lugar.",
  },
};

interface OverrideRow {
  name: string;
  updated_at: string | null;
}

type State = "override" | "shared" | "missing" | "unknown";

export function VaultSecretField({
  vaultKey,
  agentName,
  label,
  help,
  draft,
  onDraftChange,
  reloadSignal = 0,
  generate,
  missingHint,
}: {
  vaultKey: string;
  agentName: string;
  label: string;
  help?: string;
  /** What the user typed, not stored yet: the form's Save stores it. */
  draft: string;
  onDraftChange: (value: string) => void;
  /** Bumped by the form after it stores a value, so the state line re-reads. */
  reloadSignal?: number;
  /**
   * Offer to mint the value. Only for credentials WE issue (the agent's own
   * API key) — never for a token a remote service hands out, where a random
   * string would just be wrong.
   */
  generate?: "hex32";
  /**
   * Overrides the empty state's "Not set — this connector will not start"
   * for connectors that authenticate another way when the vault entry is
   * absent (weixin uses the QR-linked login persisted on the server). Also
   * softens the tone: absent-by-design is not a defect.
   */
  missingHint?: string;
}) {
  const t = useT(STR);
  const toast = useToast();
  const { status, refresh: refreshVault, requestUnlock } = useVaultMonitor();
  const unlocked = status?.unlocked ?? false;

  const [state, setState] = useState<State>("unknown");
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!unlocked) {
      setState("unknown");
      return;
    }
    try {
      const [overrides, common] = await Promise.all([
        api.get<OverrideRow[]>(`/api/vault/secrets?agent=${encodeURIComponent(agentName)}`),
        api.get<OverrideRow[]>("/api/vault/secrets"),
      ]);
      const mine = overrides.find((row) => row.name === vaultKey);
      if (mine) {
        setState("override");
        setUpdatedAt(mine.updated_at);
        return;
      }
      // No override: the connector still boots if a shared value exists, but
      // every agent using it authenticates as the same bot — worth saying.
      setState(common.some((row) => row.name === vaultKey) ? "shared" : "missing");
      setUpdatedAt(null);
    } catch {
      setState("unknown");
    }
  }, [agentName, unlocked, vaultKey]);

  useEffect(() => {
    void load();
    setRevealed(null);
  }, [load, reloadSignal]);

  const gate = (action: () => void) => {
    if (!unlocked) {
      requestUnlock(() => {
        void refreshVault().then(() => action());
      });
      return;
    }
    action();
  };

  async function reveal() {
    if (revealed !== null) {
      setRevealed(null);
      return;
    }
    try {
      const res = await api.get<{ value: string }>(
        `/api/vault/secrets/${encodeURIComponent(vaultKey)}/reveal?agent=${encodeURIComponent(agentName)}`,
      );
      setRevealed(res.value);
    } catch {
      toast.push(t("err.reveal"), "error");
    }
  }

  function mint() {
    // 32 random bytes as hex. getRandomValues (unlike crypto.subtle) is not
    // restricted to secure contexts, so this also works on a plain-HTTP LAN
    // dashboard — but a browser without it must say so rather than fall back
    // to Math.random, which would mint a guessable key.
    const bytes = new Uint8Array(32);
    try {
      crypto.getRandomValues(bytes);
    } catch {
      toast.push(t("err.generate"), "error");
      return;
    }
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    // Into the draft, not straight to the vault: the user has to see the value
    // and save the connector, so a mis-click cannot silently rotate a live key.
    setRevealed(null);
    onDraftChange(hex);
    toast.push(t("generated"), "success");
  }

  async function clear() {
    setBusy(true);
    try {
      await api.del(
        `/api/vault/secrets/${encodeURIComponent(vaultKey)}?agent=${encodeURIComponent(agentName)}`,
      );
      setRevealed(null);
      toast.push(t("cleared"), "success");
      await load();
    } catch {
      toast.push(t("err.save"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="field vault-secret-field">
      <label style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {label}
        {/* The key name IS the contract — the runtime resolves exactly this. */}
        <code style={{ fontSize: 11.5 }}>{vaultKey}</code>
        <span className="badge" style={{ padding: "0 6px" }}>
          {t("scope")}
        </span>
      </label>

      {!unlocked ? (
        <div
          className="connector-readonly"
          style={{ display: "flex", alignItems: "center", gap: 8 }}
        >
          <LockKeyhole size={14} strokeWidth={1.75} />
          <div style={{ flex: 1 }}>{t("locked")}</div>
          <button type="button" className="btn" onClick={() => gate(() => undefined)}>
            {t("unlock")}
          </button>
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              className="input"
              type={revealed !== null ? "text" : "password"}
              value={revealed !== null ? revealed : draft}
              onChange={(e) => {
                setRevealed(null);
                onDraftChange(e.target.value);
              }}
              placeholder={state === "override" ? "••••••••••••" : t("placeholder")}
              autoComplete="off"
              spellCheck={false}
              style={{ fontFamily: "var(--font-mono)", flex: 1 }}
            />
            {state === "override" && (
              <button
                type="button"
                className="btn ghost"
                onClick={() => gate(() => void reveal())}
                aria-label={revealed !== null ? t("hide") : t("reveal")}
                aria-pressed={revealed !== null}
                title={revealed !== null ? t("hide") : t("reveal")}
                style={{ padding: 6, lineHeight: 0 }}
              >
                {revealed !== null ? (
                  <EyeOff size={15} strokeWidth={1.75} />
                ) : (
                  <Eye size={15} strokeWidth={1.75} />
                )}
              </button>
            )}
            {/* Copy what is on screen: a freshly generated draft, or a stored
                value the user has revealed. Never a masked one — copying dots
                would hand over the placeholder. */}
            {(revealed !== null || draft.trim()) && (
              <CopyButton value={revealed !== null ? revealed : draft} label={t("copy")} t={t} />
            )}
            {/* Minting is offered while there is nothing to lose: no stored
                value, or a draft the user has not saved. Replacing a live key
                goes through Clear first, so a rotation is deliberate. */}
            {generate && state !== "override" && (
              <button
                type="button"
                className="btn ghost"
                onClick={() => gate(mint)}
                aria-label={t("generate")}
                title={t("generate")}
                style={{ padding: 6, lineHeight: 0 }}
              >
                <Sparkles size={15} strokeWidth={1.75} />
              </button>
            )}
            {state === "override" && !draft.trim() && (
              <button
                type="button"
                className="btn ghost"
                onClick={() => void clear()}
                disabled={busy}
                aria-label={t("clear")}
                title={t("clear")}
                style={{ padding: 6, lineHeight: 0 }}
              >
                <Trash2 size={15} strokeWidth={1.75} />
              </button>
            )}
          </div>
          <StateLine
            state={state}
            pending={Boolean(draft.trim())}
            updatedAt={updatedAt}
            missingHint={missingHint}
            t={t}
          />
        </>
      )}
      {help && <span className="ob-hint">{help}</span>}
    </div>
  );
}

function StateLine({
  state,
  pending,
  updatedAt,
  missingHint,
  t,
}: {
  state: State;
  pending: boolean;
  updatedAt: string | null;
  missingHint?: string;
  t: (k: string, v?: Record<string, string>) => string;
}) {
  // A typed value outranks whatever is stored: it is what the next save puts
  // there, and "Not set, this connector will not start" beside a token the
  // user has just typed reads as if the typing did not count.
  if (pending) {
    return (
      <small
        style={{ display: "flex", alignItems: "center", gap: 5, marginTop: 5, color: "var(--text-3)" }}
      >
        <LockKeyhole size={12} strokeWidth={1.75} />
        <span>{t("pending")}</span>
      </small>
    );
  }
  if (state === "unknown") return null;
  // An absent value is only a defect when the connector has no other way to
  // authenticate; a missingHint marks the connectors where empty is normal.
  const benignMissing = state === "missing" && Boolean(missingHint);
  const tone =
    state === "override"
      ? "var(--success)"
      : state === "shared"
        ? "var(--warn)"
        : benignMissing
          ? "var(--text-3)"
          : "var(--danger)";
  const text =
    state === "override"
      ? `${t("set")}${updatedAt ? ` · ${t("updated", { date: updatedAt.slice(0, 10) })}` : ""}`
      : state === "shared"
        ? `${t("shared")} — ${t("sharedHelp")}`
        : missingHint || t("missing");
  return (
    <small
      style={{ display: "flex", alignItems: "center", gap: 5, marginTop: 5, color: tone }}
    >
      {state === "override" || benignMissing ? (
        <Check size={12} strokeWidth={2} />
      ) : (
        <ShieldAlert size={12} strokeWidth={1.75} />
      )}
      <span>{text}</span>
    </small>
  );
}
