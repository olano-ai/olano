import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { CodexConnectDialog } from "../providers/CodexConnectDialog";
import {
  fetchOlanoKeyStatus,
  OlanoCreditsKey,
  type OlanoKeyConnectResult,
  type OlanoKeyStatus,
} from "../providers/OlanoCreditsKey";
import { CODEX_KEY, type CatalogResponse, type Provider } from "../providers/types";
import { PROVIDER_KEY_URLS } from "../providers/keyUrls";
import { PasswordField } from "../vault/components";
import { ExtLink, StepNav, StepShell } from "./chrome";
import {
  consoleUrl,
  deploymentFeatureOn,
  includedOlanoServices,
  isManagedDeployment,
  SELF_HOSTED_OLANO_SERVICES,
  useDeployment,
} from "./cloudDeployment";
import { ArtSpark } from "./illustrations";
import { saveSecret, VaultUnlock } from "./secrets";
import { STR } from "./strings";
import type { StepProps } from "./types";

// Featured providers on the provider step; the rest are in the dropdown.
// openai-codex is OAuth (ChatGPT subscription), not an API key — it gets the
// device-flow dialog instead of a key input.
const FEATURED_PROVIDERS = [
  "anthropic",
  "openai",
  CODEX_KEY,
  "google_genai",
  "groq",
  "openrouter",
];

/**
 * "Your plan already covers this" panel — the credits half of the provider
 * step. Shown on every managed deployment, because credits pay for the models
 * whether or not the plan also allows the operator's own keys.
 *
 * `condensed` drops the per-service descriptions, for the plan that shows this
 * above a live BYOK form rather than as the whole step.
 */
function IncludedCreditsPanel({
  services,
  creditsLeft,
  byokLocked,
  condensed,
  upgradeUrl,
}: {
  services: string[];
  creditsLeft: number | null;
  byokLocked: boolean;
  condensed: boolean;
  upgradeUrl: string | null;
}) {
  const t = useT(STR);
  return (
    <>
      <ul
        className={`ob-included${condensed ? " condensed" : ""}`}
        aria-label={t("ob.prov.inc.a11y")}
      >
        {services.map((id) => (
          <li key={id}>
            <span className="tick" aria-hidden>
              ✓
            </span>
            <div className="ob-inc-body">
              <strong>{t(`ob.prov.inc.${id}`)}</strong>
              {!condensed && <span>{t(`ob.prov.inc.${id}.d`)}</span>}
            </div>
          </li>
        ))}
      </ul>
      {creditsLeft !== null && (
        <span className="ob-credits-chip">
          {t("ob.prov.inc.credits", { n: Math.round(creditsLeft).toLocaleString() })}
        </span>
      )}
      {byokLocked && (
        <p className="ob-hint">
          {t("ob.prov.inc.byokUpsell")}
          {upgradeUrl && (
            <>
              {" "}
              <a href={upgradeUrl} target="_blank" rel="noreferrer noopener">
                {t("ob.prov.inc.byokUpgrade")} ↗
              </a>
            </>
          )}
        </p>
      )}
    </>
  );
}

// The catalog's codex name ("OpenAI Codex (ChatGPT OAuth)") is too wordy for a
// picker card.
function providerLabel(p: Provider): string {
  return p.key === CODEX_KEY ? "OpenAI Codex" : p.name;
}

// env_set is meaningless for codex (its env_var is null → always "set");
// connected state lives on the OAuth auth object instead.
function providerReady(p: Provider): boolean {
  return p.key === CODEX_KEY ? Boolean(p.auth?.connected) : Boolean(p.env_var && p.env_set);
}

/**
 * Wizard step 3 — where the agents' models come from. Three shapes, decided by
 * what the deployment is actually entitled to:
 *
 *   A. managed, credits only  → the included-services panel, nothing to do
 *   B. managed, credits+BYOK  → that panel, then the key form as an *extra*
 *   C. self-hosted            → an "Olano credits" card (paste an Olano API key
 *                               from cloud.olano.ai) above the BYOK key form;
 *                               either route finishes the step
 *
 * A and B count as done the moment they render: model access is configured, by
 * the plan. Telling a customer whose plan includes six managed AI services to
 * "skip this step" was the bug this shape replaces. In C the credits card is
 * offered, never assumed: nothing is included until a key is connected.
 */
export function ProviderStep(p: StepProps) {
  const t = useT(STR);
  const deployment = useDeployment();
  const [providers, setProviders] = useState<Provider[]>([]);
  // The catalog fetch is the first thing on this step that can visibly stall,
  // so it gets a real state machine: an empty grid with no explanation reads
  // as a broken page, and a swallowed error left it empty forever.
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [slow, setSlow] = useState(false);
  // Two different denials, deliberately kept apart: `keyEntryLocked` is the
  // backend's own "no key entry here" (model_selection folded in with byok),
  // while `catalogByokLocked` is specifically the plan entitlement — the only
  // one an upgrade fixes, and so the only one that may say so.
  const [keyEntryLocked, setKeyEntryLocked] = useState(false);
  const [catalogByokLocked, setCatalogByokLocked] = useState(false);
  const [selected, setSelected] = useState<string>("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [ok, setOk] = useState(p.done);
  const [already, setAlready] = useState(false);
  const [locked, setLocked] = useState(false);
  const [codexOpen, setCodexOpen] = useState(false);
  const [codexOk, setCodexOk] = useState(false);
  const [err, setErr] = useState("");
  // Shape C: the Olano account-key status (null while unknown, or when the
  // box may not manage one) and what the connect call reported it bought.
  const [olanoKey, setOlanoKey] = useState<OlanoKeyStatus | null>(null);
  const [olanoVerified, setOlanoVerified] = useState<OlanoKeyConnectResult["verified"] | null>(null);

  const loadCatalog = useCallback(() => {
    // Only a retry after a failure goes back to "loading" — the post-Codex
    // refresh below is a background re-read and must not blank a filled grid
    // back to skeletons.
    setStatus((prev) => (prev === "error" ? "loading" : prev));
    // fields=basic: this step renders provider names + key/auth state only.
    // The model lists and their metadata are what make the full payload slow
    // (per-model reasoning probes, live ollama discovery) and nothing here
    // reads them.
    return api
      .get<CatalogResponse & { model_selection_locked?: boolean }>(
        "/api/catalog/providers?fields=basic",
      )
      .then((res) => {
        setProviders(res.providers.filter((x) => x.env_var || x.key === CODEX_KEY));
        setKeyEntryLocked(Boolean(res.model_selection_locked));
        setCatalogByokLocked(Boolean(res.byok_locked));
        // Something already configured (key in env/vault, or a connected
        // ChatGPT subscription) → the step counts as done even if the admin
        // saves nothing here.
        if (res.providers.some(providerReady)) setAlready(true);
        setStatus("ready");
      })
      .catch(() => setStatus("error"));
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  // The credits card is a self-hosted affordance only, so the lookup waits on
  // the deployment record: a managed box never asks (its wallet is the plan's).
  useEffect(() => {
    if (deployment === undefined || isManagedDeployment(deployment)) return;
    let on = true;
    fetchOlanoKeyStatus().then((s) => {
      if (on) setOlanoKey(s);
    });
    return () => {
      on = false;
    };
  }, [deployment]);

  // Both lookups gate what this step *is*, so the skeleton covers both: paint
  // the BYOK grid first and a managed box would watch it be replaced by the
  // credits panel a round-trip later.
  const loading = status === "loading" || deployment === undefined;

  // A skeleton answers "is it working?" for the first moment; past ~2.5s the
  // honest answer is "yes, but it's taking a while" — say so rather than let
  // the placeholder shimmer on looking stuck.
  useEffect(() => {
    if (!loading) {
      setSlow(false);
      return;
    }
    const timer = window.setTimeout(() => setSlow(true), 2500);
    return () => window.clearTimeout(timer);
  }, [loading]);

  // Stable identity: CodexConnectDialog runs its device flow once per mount and
  // reads this through a ref, but a callback that changes on every render is
  // still a trap for anything that legitimately keys off it. `codexOk` is
  // tracked locally so a successful connect shows even if the catalog re-fetch
  // below fails.
  const onCodexConnected = useCallback(() => {
    setCodexOk(true);
    loadCatalog(); // refresh the Ready badges + auth state
  }, [loadCatalog]);

  const featured = FEATURED_PROVIDERS.map((k) => providers.find((x) => x.key === k)).filter(
    (x): x is Provider => Boolean(x),
  );
  const rest = providers.filter((x) => !FEATURED_PROVIDERS.includes(x.key));
  const chosen = providers.find((x) => x.key === selected) ?? null;

  const services = includedOlanoServices(deployment);
  // Shape C's credits card: offered on a self-hosted box that may hold an
  // account key. Once a key is connected the card turns into the same
  // "included" checklist a managed plan shows, with the account's balance.
  const showOlanoCard = !loading && olanoKey !== null && olanoKey.manageable;
  const olanoConnected = showOlanoCard && olanoKey.connected;
  // The panel carries the whole "you already have AI" message, so an empty
  // service list has to fall through to the plain locked line rather than
  // render a celebration with nothing in it (a quarantined box: managed, but
  // every managed service switched off until it re-licenses).
  const showIncluded = !loading && services.length > 0;
  const upgradeUrl = deployment ? consoleUrl(deployment, window.location.hostname) : null;
  // The plan's BYOK entitlement, read from both sources that carry it: the
  // catalog is the backend's own answer, and the signed deployment record is
  // what survives a failed catalog fetch — without which a credits-only box
  // whose catalog request died would offer a key form its plan forbids. Either
  // saying no means no; a self-hosted box has neither, so both read open.
  const byokLockedByPlan = catalogByokLocked || !deploymentFeatureOn(deployment, "byok");
  // Only the key form is entitlement-locked; the credits panel is the step's
  // content in its own right.
  const showKeyEntry = !loading && !keyEntryLocked && !byokLockedByPlan;

  async function save() {
    if (!chosen?.env_var) return;
    setBusy(true);
    setErr("");
    setLocked(false);
    try {
      await saveSecret(chosen.env_var, key.trim(), t("ob.prov.vaultDesc", { provider: chosen.name }));
      setOk(true);
      setKey("");
    } catch (e2) {
      if (e2 instanceof ApiError && e2.status === 503) setLocked(true);
      else setErr(t("ob.prov.err.generic"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <StepShell
      art={<ArtSpark />}
      title={showIncluded ? t("ob.prov.inc.title") : t("ob.prov.title")}
      lead={
        showIncluded
          ? deployment?.plan
            ? t("ob.prov.inc.lead", { plan: deployment.plan })
            : t("ob.prov.inc.leadNoPlan")
          : t("ob.prov.lead")
      }
    >
      {loading && (
        // Placeholder tiles in the real grid: the picker keeps its final
        // size, so the list fades in instead of shoving the card open.
        // aria-busy + the status line carry the same news to a screen
        // reader, which can't see a shimmer.
        <>
          <div className="ob-provider-grid" aria-hidden="true">
            {FEATURED_PROVIDERS.map((k) => (
              <div key={k} className="ob-provider skeleton" />
            ))}
          </div>
          <p className="ob-hint" role="status" aria-live="polite" aria-busy="true">
            {slow ? t("ob.prov.loadingSlow") : t("ob.prov.loading")}
          </p>
        </>
      )}

      {showIncluded && (
        <IncludedCreditsPanel
          services={services}
          creditsLeft={deployment?.credits_left ?? null}
          byokLocked={byokLockedByPlan}
          condensed={showKeyEntry}
          upgradeUrl={upgradeUrl}
        />
      )}

      {showOlanoCard && (
        <div className="ob-olano-key" data-testid="ob-olano-key">
          <div className="ob-byok-head">
            <strong>{olanoConnected ? t("ob.prov.olano.titleDone") : t("ob.prov.olano.title")}</strong>
            {!olanoConnected && <span className="badge ob-optional">{t("ob.optional")}</span>}
          </div>
          {olanoConnected ? (
            <>
              <div className="ob-ok">✓ {t("ob.prov.olano.done")}</div>
              <IncludedCreditsPanel
                services={[...SELF_HOSTED_OLANO_SERVICES]}
                creditsLeft={olanoVerified?.balance ?? null}
                byokLocked={false}
                condensed
                upgradeUrl={null}
              />
              {olanoVerified && (olanoVerified.balance ?? 0) <= 0 ? (
                <p className="ob-hint" data-testid="ob-olano-zero">
                  {t("ob.prov.olano.zero")}
                </p>
              ) : null}
            </>
          ) : (
            <p className="ob-hint">{t("ob.prov.olano.lead")}</p>
          )}
          <OlanoCreditsKey
            status={olanoKey}
            compact
            onChanged={(next) => {
              setOlanoKey(next);
              if (!next.connected) setOlanoVerified(null);
            }}
            onConnected={(result) => {
              setOlanoVerified(result.verified);
              setOk(true);
            }}
          />
        </div>
      )}

      {/* Locked with nothing to celebrate: a quarantined managed box, or a
          local license with model selection off. Say so plainly and point at
          the console when there is one. */}
      {!loading && keyEntryLocked && !showIncluded && (
        <p className="ob-hint">
          {t("ob.prov.byok")}
          {upgradeUrl && (
            <>
              {" "}
              <a href={upgradeUrl} target="_blank" rel="noreferrer noopener">
                {t("ob.cloud.open")} ↗
              </a>
            </>
          )}
        </p>
      )}

      {showKeyEntry && (
        <>
          {/* On a managed box the keys are an extra, not the task: the split
              says so before the picker appears. */}
          {showIncluded && (
            <div className="ob-byok-split">
              <div className="ob-byok-head">
                <strong>{t("ob.prov.inc.byokHead")}</strong>
                <span className="badge ob-optional">{t("ob.optional")}</span>
              </div>
              <p className="ob-hint">{t("ob.prov.inc.byokLead")}</p>
            </div>
          )}
          {/* Self-hosted with the credits card above: the BYOK picker is the
              other route, and reads as one once a key is connected. */}
          {showOlanoCard && (
            <div className="ob-olano-byok">
              <div className="ob-byok-head">
                <strong>{t("ob.prov.olano.byokHead")}</strong>
                {olanoConnected && <span className="badge ob-optional">{t("ob.optional")}</span>}
              </div>
              <p className="ob-hint">
                {olanoConnected ? t("ob.prov.inc.byokLead") : t("ob.prov.olano.byokLead")}
              </p>
            </div>
          )}
          {ok ? (
            <div className="ob-ok">✓ {t("ob.prov.saved")}</div>
          ) : already && !showIncluded ? (
            <div className="ob-ok">✓ {t("ob.prov.already")}</div>
          ) : null}
          {status === "error" && (
            <div className="ob-retry" role="alert">
              <span>{t("ob.prov.loadErr")}</span>
              <button type="button" className="btn" onClick={() => void loadCatalog()}>
                {t("ob.prov.retry")}
              </button>
            </div>
          )}
          {status === "ready" && (
            <div className="ob-provider-grid" role="listbox" aria-label={t("ob.prov.title")}>
              {featured.map((prov) => (
                <button
                  key={prov.key}
                  type="button"
                  role="option"
                  aria-selected={selected === prov.key}
                  className={`ob-provider ${selected === prov.key ? "on" : ""}`}
                  onClick={() => setSelected(prov.key)}
                >
                  <span>
                    {providerLabel(prov)}
                    {prov.key === CODEX_KEY && (
                      <>
                        {" "}
                        <span className="ok">{t("ob.prov.codexBadge")}</span>
                      </>
                    )}
                  </span>
                  {providerReady(prov) && <span className="ok">✓ {t("ob.prov.ready")}</span>}
                </button>
              ))}
            </div>
          )}
          {rest.length > 0 && (
            <select
              className="input"
              value={FEATURED_PROVIDERS.includes(selected) ? "" : selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">{t("ob.prov.more")}</option>
              {rest.map((prov) => (
                <option key={prov.key} value={prov.key}>
                  {providerLabel(prov)}
                  {providerReady(prov) ? " ✓" : ""}
                </option>
              ))}
            </select>
          )}
          {chosen?.key === CODEX_KEY && (
            <div className="ob-form">
              <p className="ob-hint">{t("ob.prov.codexLead")}</p>
              {codexOk || chosen.auth?.connected ? (
                <div className="ob-ok">✓ {t("ob.prov.codexDone")}</div>
              ) : (
                <button type="button" className="btn primary" onClick={() => setCodexOpen(true)}>
                  {t("ob.prov.codexConnect")}
                </button>
              )}
            </div>
          )}
          {codexOpen && chosen?.key === CODEX_KEY && (
            <CodexConnectDialog
              provider={chosen}
              onClose={() => setCodexOpen(false)}
              onConnected={onCodexConnected}
            />
          )}
          {chosen && chosen.key !== CODEX_KEY && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void save();
              }}
              className="ob-form"
            >
              {PROVIDER_KEY_URLS[chosen.key] && (
                <ExtLink href={PROVIDER_KEY_URLS[chosen.key]}>
                  {t("ob.getKey", { name: chosen.name })}
                </ExtLink>
              )}
              <div className="field">
                <label>{t("ob.prov.keyLabel", { name: chosen.name })}</label>
                <PasswordField
                  value={key}
                  onChange={setKey}
                  autoComplete="off"
                  placeholder={chosen.env_var ?? ""}
                  autoFocus
                />
              </div>
              {err && <div className="error-text">{err}</div>}
              {locked ? (
                <VaultUnlock onUnlocked={() => void save()} />
              ) : (
                <button className="btn primary" disabled={busy || !key.trim()}>
                  {busy ? t("ob.saving") : t("ob.prov.save")}
                </button>
              )}
            </form>
          )}
          <p className="ob-hint">{t("ob.prov.note")}</p>
        </>
      )}
      <StepNav
        // Credits already configure model access, so a managed box arrives at
        // this step finished — the footer offers Continue, and the finish
        // screen records "done" instead of listing AI provider as skipped.
        ok={ok || already || codexOk || showIncluded || olanoConnected}
        busy={busy}
        onBack={p.onBack}
        onSkip={p.onSkip}
        onDone={p.onDone}
      />
    </StepShell>
  );
}
