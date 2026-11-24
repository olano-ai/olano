import { Plug } from "lucide-react";
import { useT } from "../../core/i18n";
import { Markdown } from "../../ui/Markdown";
import type { ModelMeta } from "../agents/types";
import { OlanoCreditsKey, type OlanoKeyStatus } from "./OlanoCreditsKey";
import { STR } from "./strings";
import { CODEX_KEY, OLANO_KEY, providerReady, type Provider } from "./types";

// Compact "$in/$out" per-1M price label for a model chip; null when the
// catalog has no pricing (local models, subscription-billed Codex).
function priceLabel(meta: ModelMeta | undefined): string | null {
  const p = meta?.pricing;
  if (!p || p.input == null || p.output == null) return null;
  const fmt = (v: number) => (v >= 10 ? String(Math.round(v)) : String(Math.round(v * 100) / 100));
  return `$${fmt(p.input)}/${fmt(p.output)}`;
}

// Tooltip detail: capabilities + context window from the synced catalog.
function metaTitle(value: string, meta: ModelMeta | undefined, t: (key: string, vars?: Record<string, string>) => string): string {
  const parts = [value];
  if (meta?.context_window) parts.push(`${Math.round(meta.context_window / 1000)}K ctx`);
  const caps = meta?.capabilities;
  if (caps) {
    const names = [
      caps.vision && t("cap.vision"),
      caps.image_gen && t("cap.imageGen"),
      caps.tts && t("cap.tts"),
      caps.stt && t("cap.stt"),
      caps.reasoning && t("cap.reasoning"),
    ].filter(Boolean);
    if (names.length) parts.push(names.join(", "));
  }
  const price = priceLabel(meta);
  if (price) parts.push(t("cap.pricePerMillion", { price }));
  if (meta?.deprecated) parts.push(t("cap.deprecated"));
  return parts.join(" · ");
}

// Small status pill that mirrors the original badge--green / badge--orange /
// badge--red variants using the design tokens. An optional leading dot mirrors
// the original `<span class="dot">` on the green "installed" badge.
function StatusBadge({
  label,
  tone,
  dot = false,
  title,
}: {
  label: string;
  tone: "ok" | "bad" | "warn";
  dot?: boolean;
  title?: string;
}) {
  const color = tone === "ok" ? "var(--success)" : tone === "warn" ? "var(--warn)" : "var(--danger)";
  const bg = tone === "ok" ? "var(--success-soft)" : tone === "warn" ? "var(--warn-soft)" : "var(--danger-soft)";
  return (
    <span
      className="badge"
      title={title}
      style={{ background: bg, color, borderColor: `color-mix(in srgb, ${color} 30%, transparent)`, gap: 5 }}
    >
      {dot ? (
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: color, flex: "0 0 auto" }} />
      ) : null}
      {label}
    </span>
  );
}

// One provider card: header (name + package + env_var), status badges, the
// Codex connect button, notes/auth diagnostics, and the clickable model chips.
export function ProviderCard({
  provider,
  models,
  byokLocked,
  onCopy,
  onConnect,
  olanoKey,
  onOlanoKeyChanged,
}: {
  provider: Provider;
  models: string[];
  // Plan excludes BYOK: key-entry affordances (Codex connect) disable; the
  // ProvidersView banner explains why.
  byokLocked?: boolean;
  onCopy: (providerKey: string, model: string) => void;
  onConnect: () => void;
  // Self-hosted box only: the account-key status the view fetched, which puts
  // the paste/remove form on the Olano card (null on a managed deployment).
  olanoKey?: OlanoKeyStatus | null;
  onOlanoKeyChanged?: () => void;
}) {
  const t = useT(STR);
  const isCodex = provider.key === CODEX_KEY;
  const isOlano = provider.key === OLANO_KEY;
  const connected = provider.auth?.connected ?? false;
  // Installed + authenticated (connected, or key in env/vault) → the models are
  // runnable right now, and the chips say so in green.
  const ready = providerReady(provider);

  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
        <div style={{ minWidth: 0 }}>
          <strong style={{ fontSize: 13.5 }}>{provider.name}</strong>
          <div
            className="muted"
            style={{ marginTop: 4, fontSize: 12, fontFamily: "var(--font-mono)", wordBreak: "break-all" }}
          >
            {provider.package}
            {provider.env_var ? ` · ${provider.env_var}` : ""}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
          <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <StatusBadge
              label={provider.available ? t("badge.installed") : t("badge.notInstalled")}
              tone={provider.available ? "ok" : "bad"}
              dot={provider.available}
            />
            {provider.env_var ? (
              <StatusBadge label={provider.env_set ? t("badge.envSet") : t("badge.envMissing")} tone={provider.env_set ? "ok" : "warn"} />
            ) : null}
            {/* Any provider with an auth object (Codex OAuth, Olano deployment
                token) reports real connection state — the env_set badge above
                is meaningless for them (env_var is null). */}
            {/* `connected` means "usable on this box at all", so a provider
                that is attached but not yet working reads connected=true — the
                model pickers depend on that. `status: "degraded"` is how such a
                provider says so, and it must not render as a green tick. */}
            {provider.auth ? (
              <StatusBadge
                label={
                  provider.auth.status === "degraded"
                    ? t("badge.degraded")
                    : connected
                      ? t("badge.connected")
                      : t("badge.notConnected")
                }
                tone={connected && provider.auth.status !== "degraded" ? "ok" : "warn"}
                dot={connected && provider.auth.status !== "degraded"}
                title={provider.auth.error ?? undefined}
              />
            ) : null}
            <span className="badge">{t("badge.models", { count: String(provider.models.length) })}</span>
          </div>
          {isCodex ? (
            <button
              className="btn primary"
              onClick={onConnect}
              disabled={byokLocked}
              title={byokLocked ? t("byok.locked.title") : undefined}
              style={{ gap: 6 }}
            >
              <Plug size={15} strokeWidth={1.75} />
              {connected ? t("card.reconnect") : t("card.connect")}
            </button>
          ) : null}
        </div>
      </div>

      {provider.notes ? (
        <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          <Markdown text={provider.notes} />
        </div>
      ) : null}

      {/* A self-hosted box runs the Olano tiers on an account API key pasted
          here (the backend verifies it against cloud.olano.ai before storing
          it). Rendered only when the status says the box may manage one: a
          managed deployment bills its own wallet and never sees this. */}
      {isOlano && olanoKey?.manageable && provider.auth?.manageable ? (
        <div style={{ marginTop: 12, display: "grid", gap: 8 }}>
          <strong style={{ fontSize: 12.5 }}>
            {olanoKey.connected ? t("olano.title.connected") : t("olano.title")}
          </strong>
          {!olanoKey.connected ? (
            <p className="muted" style={{ margin: 0, fontSize: 12 }}>
              {t("olano.lead")}
            </p>
          ) : null}
          <OlanoCreditsKey status={olanoKey} onChanged={onOlanoKeyChanged} />
        </div>
      ) : null}

      {/* Codex auth diagnostics (token preview / store / expiry / error). */}
      {isCodex && connected && provider.auth?.token_preview ? (
        <div className="muted" style={{ marginTop: 8, fontSize: 11, fontFamily: "var(--font-mono)" }}>
          {t("card.token", { preview: provider.auth.token_preview })}
          {provider.auth.auth_store ? ` · ${provider.auth.auth_store}` : ""}
        </div>
      ) : null}
      {isCodex && provider.auth?.expires_soon ? (
        <div style={{ marginTop: 8, fontSize: 12, color: "var(--warn)" }}>
          {t("card.expiresSoon")}
        </div>
      ) : null}
      {/* Any provider carrying an auth error explains itself here. Was
          Codex-only, which left an Olano box that could not enrol showing a
          badge and no reason anywhere on the page. A degraded provider is
          still selectable, so the explanation is the whole affordance. */}
      {provider.auth?.error ? (
        <div
          className={provider.auth.status === "degraded" ? "muted" : "error-text"}
          style={{ marginTop: 8, fontSize: 12, ...(provider.auth.status === "degraded" ? { color: "var(--warn)" } : {}) }}
        >
          {provider.auth.error}
        </div>
      ) : null}

      {models.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 12 }}>
          {models.map((m) => {
            const meta = provider.model_meta?.[m];
            const price = priceLabel(meta);
            return (
              <button
                key={m}
                type="button"
                title={t("card.copyModel", { value: metaTitle(`${provider.key}:${m}`, meta, t) })}
                onClick={() => onCopy(provider.key, m)}
                // A deprecated model stays neutral even on a ready provider —
                // green reads as "use this", which is the opposite of what the
                // strikethrough is saying.
                className={`badge model-chip${ready && !meta?.deprecated ? " ready" : ""}`}
                style={{
                  cursor: "copy",
                  fontFamily: "var(--font-mono)",
                  fontWeight: 500,
                  ...(meta?.deprecated
                    ? { textDecoration: "line-through", opacity: 0.6 }
                    : undefined),
                }}
              >
                {meta?.recommended ? "★ " : ""}
                {m}
                {price ? (
                  <span style={{ opacity: 0.55, marginLeft: 5, fontWeight: 400 }}>{price}</span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          {t("card.noModels")}
        </div>
      )}
    </div>
  );
}
