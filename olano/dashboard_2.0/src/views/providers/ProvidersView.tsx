import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../core/api";
import { usePersistentState } from "../../core/usePersistentState";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { RefreshIcon } from "../../ui/icons";
import { useToast } from "../../ui/toast";
import { CodexConnectDialog } from "./CodexConnectDialog";
import { fetchOlanoKeyStatus, type OlanoKeyStatus } from "./OlanoCreditsKey";
import { ProviderCard } from "./ProviderCard";
import { STR } from "./strings";
import { matchProvider, type CatalogResponse, type Provider } from "./types";

// Hover lift + aurora accent for the copyable model chips, plus the green
// "ready" tint for providers whose requirements are all met (see
// types.ts:providerReady).
//
// The base colors live here rather than inline on the chip because an inline
// `background` outranks any stylesheet rule: the hover tint below was written
// against a chip that carried `background: var(--surface-2)` inline, so only its
// `color` ever changed on hover. Keeping every color in the cascade lets base,
// ready and hover states layer in the order they're written.
const MODEL_CHIP_CSS = `
.model-chip {
  transition: transform var(--t-fast) var(--ease), color var(--t-fast), background var(--t-fast);
  background: var(--surface-2);
}
.model-chip.ready {
  color: var(--success);
  background: var(--success-soft);
}
.model-chip:hover,
.model-chip.ready:hover {
  transform: translateY(-1px);
  color: var(--accent);
  background: var(--accent-soft);
}
.model-chip:active {
  transform: translateY(0);
}
`;

// Full parity with the original loadProvidersView() / renderProviders():
//   • page head + Refresh + debounced search
//   • per-provider cards (badges, notes, model chips → click-to-copy)
//   • OpenAI Codex device-flow connect/reconnect dialog
//   • re-fetch + re-render after a successful Codex connect
export function ProvidersView() {
  const toast = useToast();
  const t = useT(STR);
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [rawQuery, setRawQuery] = usePersistentState("providers.rawQuery", "");
  const [query, setQuery] = useState("");
  const [codexProvider, setCodexProvider] = useState<Provider | null>(null);
  const [byokLocked, setByokLocked] = useState(false);
  // The Olano account-key status (self-hosted boxes). Fetched beside the
  // catalog, because the catalog's `auth.manageable` only says the card MAY
  // hold a form; the status carries what the form shows (prefix, rejected,
  // the cloud links). Null on a managed deployment or when the admin-only
  // route refuses, in which case the card renders exactly as before.
  const [olanoKey, setOlanoKey] = useState<OlanoKeyStatus | null>(null);

  // Debounce search (80ms) — same cadence as the original debounce(…, 80).
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(rawQuery.trim()), 80);
    return () => window.clearTimeout(timer);
  }, [rawQuery]);

  // The catalog endpoint is GET-only; "force" in the old app merely bypassed an
  // in-memory cache, so here a refresh is just another GET.
  const loadCatalog = useCallback(async () => {
    try {
      const data = await api.get<CatalogResponse>("/api/catalog/providers");
      setProviders(data.providers ?? []);
      setByokLocked(Boolean(data.byok_locked));
      setLoadError(false);
      const olano = (data.providers ?? []).find((p) => p.key === "olano");
      setOlanoKey(olano?.auth?.manageable ? await fetchOlanoKeyStatus() : null);
      return true;
    } catch {
      setProviders((prev) => prev ?? []);
      setLoadError(true);
      toast.push(t("load.error"), "error");
      return false;
    }
  }, [toast, t]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  async function refresh() {
    setRefreshing(true);
    try {
      const ok = await loadCatalog();
      if (ok) toast.push(t("load.refreshed"));
    } finally {
      setRefreshing(false);
    }
  }

  async function copyModel(providerKey: string, model: string) {
    const value = `${providerKey}:${model}`;
    try {
      await navigator.clipboard.writeText(value);
      toast.push(t("copy.done", { value }));
    } catch {
      toast.push(t("copy.error"), "error");
    }
  }

  // After a successful Codex connect, re-pull the catalog so the badge flips to
  // "connected" and the new models appear.
  const onCodexConnected = useCallback(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const visible = useMemo(() => {
    if (!providers) return [];
    return providers.map((p) => matchProvider(p, query)).filter((x) => x.show);
  }, [providers, query]);

  // Lightweight summary line (installed count + total models) — informational,
  // matching the catalog-at-a-glance feel of the original page sub-header.
  const summary = useMemo(() => {
    if (!providers || !providers.length) return null;
    const installed = providers.filter((p) => p.available).length;
    const totalModels = providers.reduce((n, p) => n + p.models.length, 0);
    return t("summary", {
      installed: String(installed),
      total: String(providers.length),
      models: String(totalModels),
    });
  }, [providers, t]);

  // Live count of what the current search matches (providers + model chips).
  const matchCount = useMemo(() => {
    if (!query) return null;
    const matchedModels = visible.reduce((n, x) => n + x.models.length, 0);
    return t("match", {
      providers: String(visible.length),
      ps: visible.length === 1 ? "" : "s",
      models: String(matchedModels),
      ms: matchedModels === 1 ? "" : "s",
    });
  }, [query, visible, t]);

  return (
    <>
      {/* Scoped hover affordance for the click-to-copy model chips — globals.css
          is off-limits, so the rule ships with the view (rendered once). */}
      <style>{MODEL_CHIP_CSS}</style>
      <div className="topbar">
        <BrandWordmark />
        <button className="btn" onClick={() => void refresh()} disabled={refreshing || providers === null}>
          {refreshing ? <span className="spinner" /> : <RefreshIcon size={15} strokeWidth={1.75} />}
          {t("refresh")}
        </button>
      </div>
      <div className="page">
        <h1>{t("title")}</h1>
        <p className="muted">
          {t("desc.before")}
          <code>provider:model</code>
          {t("desc.after")}
          {summary ? (
            <>
              {" "}
              <span style={{ color: "var(--text-2)" }}>{summary}</span>.
            </>
          ) : null}
        </p>

        <div className="row" style={{ marginTop: 16, marginBottom: 16, gap: 10 }}>
          <input
            className="input"
            style={{ maxWidth: 360 }}
            placeholder={t("search.placeholder")}
            value={rawQuery}
            onChange={(e) => setRawQuery(e.target.value)}
          />
          {rawQuery ? (
            <button className="btn" onClick={() => setRawQuery("")}>
              {t("clear")}
            </button>
          ) : null}
          {matchCount ? (
            <span className="muted" style={{ fontSize: 12 }}>
              {matchCount}
            </span>
          ) : null}
        </div>

        {providers === null ? (
          <div className="spinner" style={{ marginTop: 16 }} />
        ) : loadError && providers.length === 0 ? (
          <div className="card" style={{ padding: 16, marginTop: 8 }}>
            <div className="error-text">{t("load.error")}</div>
            <button className="btn" style={{ marginTop: 12 }} onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? <span className="spinner" /> : null}
              {t("load.tryAgain")}
            </button>
          </div>
        ) : visible.length === 0 ? (
          <p className="muted">
            {query ? t("empty.search") : t("empty.none")}
          </p>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            {byokLocked ? (
              <div
                className="card"
                style={{ padding: 14, borderColor: "var(--warn)", display: "grid", gap: 4 }}
              >
                <strong style={{ fontSize: 13 }}>{t("byok.locked.title")}</strong>
                <span className="muted" style={{ fontSize: 12 }}>{t("byok.locked.body")}</span>
              </div>
            ) : null}
            {visible.map(({ provider, models }) => (
              <ProviderCard
                key={provider.key}
                provider={provider}
                models={models}
                byokLocked={byokLocked}
                onCopy={copyModel}
                onConnect={() => setCodexProvider(provider)}
                olanoKey={olanoKey}
                onOlanoKeyChanged={() => void loadCatalog()}
              />
            ))}
          </div>
        )}
      </div>

      {codexProvider ? (
        <CodexConnectDialog
          provider={codexProvider}
          onClose={() => setCodexProvider(null)}
          onConnected={onCodexConnected}
        />
      ) : null}
    </>
  );
}
