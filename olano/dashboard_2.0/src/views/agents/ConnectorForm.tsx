import { useMemo, useState } from "react";
import { AlertCircle, ChevronDown, Eye, EyeOff, Info, LockKeyhole, Sparkles } from "lucide-react";
import { useT } from "../../core/i18n";
import { Field } from "./Modal";
import { ConnectorGuide } from "./ConnectorGuide";
import { STR } from "./strings";
import { VaultSecretField } from "./VaultSecretField";
import { CopyButton } from "./CopyButton";
import { Toggle } from "../../ui/Toggle";
import { agentApiBaseUrl } from "./helpers";
import {
  CONNECTOR_GROUP_LABELS,
  buildConnectorConfig,
  initialValues,
  isDisplayOnlyKind,
  isSignalGuided,
  isWeixinPairable,
  isWhatsAppPairable,
  type CatalogT,
  type ConnectorDefinition,
  type ConnectorFieldDef,
  type ConnectorFieldGroup,
  type ConnectorFormValues,
  type ConnectorToolDef,
} from "./connectorCatalog";

/**
 * The connector's tool switches, owned by the modal: `state` is each tool's
 * on/off as the agent currently has it (`null` while it loads or when the
 * agent could not be read, which disables the switches), `onChange` records a
 * flip; the modal saves them with the connector.
 */
export interface ConnectorToolsProps {
  state: Record<string, boolean> | null;
  onChange: (name: string, on: boolean) => void;
}

export function ConnectorForm({
  definition,
  values,
  onChange,
  error,
  agentName,
  vaultReloadSignal,
  tools,
  onPairWhatsApp,
  onSetupSignal,
  onPairWeixin,
}: {
  definition: ConnectorDefinition;
  values: ConnectorFormValues;
  onChange: (next: ConnectorFormValues) => void;
  error?: string | null;
  /** Present when the connector lists tools and there is an agent to switch them on. */
  tools?: ConnectorToolsProps;
  /**
   * Whose credentials these are. With an agent, vault-backed fields show that
   * agent's override and keep what the user types as form state, which the
   * form's Save stores in the vault before it saves the connector
   * (`pendingVaultSecrets`); the value never rides in the channel payload.
   * Without one (a form for an agent that doesn't exist yet) they fall back to
   * a plain masked input, and the backend vaults the value on save.
   */
  agentName?: string;
  /** Bumped after the form stores vault values, so vault fields re-read their state. */
  vaultReloadSignal?: number;
  onPairWhatsApp?: () => void;
  onSetupSignal?: () => void;
  onPairWeixin?: () => void;
}) {
  const t = useT(STR);
  const grouped = useMemo(() => groupFields(definition), [definition]);

  return (
    <div className="connector-shell">
      <div className="connector-main">
        {isWhatsAppPairable(definition.type) && onPairWhatsApp ? (
          <div className="connector-live-card">
            <div>
              <strong>{t("cform.wa.title")}</strong>
              <p>{t("cform.wa.body")}</p>
            </div>
            <button type="button" className="btn primary row" onClick={onPairWhatsApp}>
              <Sparkles size={14} strokeWidth={1.75} /> {t("cform.wa.cta")}
            </button>
          </div>
        ) : null}

        {isWeixinPairable(definition.type) && onPairWeixin ? (
          <div className="connector-live-card">
            <div>
              <strong>{t("cform.wx.title")}</strong>
              <p>{t("cform.wx.body")}</p>
            </div>
            <button type="button" className="btn primary row" onClick={onPairWeixin}>
              <Sparkles size={14} strokeWidth={1.75} /> {t("cform.wx.cta")}
            </button>
          </div>
        ) : null}

        {isSignalGuided(definition.type) && onSetupSignal ? (
          <div className="connector-live-card">
            <div>
              <strong>{t("cform.sig.title")}</strong>
              <p>{t("cform.sig.body")}</p>
            </div>
            <button type="button" className="btn primary row" onClick={onSetupSignal}>
              <Sparkles size={14} strokeWidth={1.75} /> {t("cform.sig.cta")}
            </button>
          </div>
        ) : null}

        {error ? (
          <div className="connector-error">
            <AlertCircle size={15} strokeWidth={1.75} />
            {error}
          </div>
        ) : null}

        {grouped.map(([group, fields]) => {
          const body = fields.map((field) => (
            <ConnectorField
              key={field.key}
              field={field}
              t={t}
              agentName={agentName}
              vaultReloadSignal={vaultReloadSignal}
              value={values[field.key]}
              onChange={(value) => onChange({ ...values, [field.key]: value })}
            />
          ));
          // How many knobs in this group the agent actually set. Rendered as a
          // badge so "configured" is readable at a glance instead of requiring
          // a scan of every input for a non-placeholder value.
          const setCount = fields.filter(
            (f) => !isDisplayOnlyKind(f.kind) && isFilled(values[f.key]),
          ).length;
          const counter = setCount ? <span className="connector-fieldset__count">{setCount} set</span> : null;

          // Opt-in reading for groups the happy path never touches: "Advanced
          // routing" (a dozen rarely-used knobs on Telegram et al.) plus any
          // group the connector marks secondary (WhatsApp's bridge transport,
          // which QR pairing bypasses). Rendering them expanded buries the
          // fields that actually need filling in. Collapsed unless the agent
          // already set one, so an existing override is never hidden.
          if (group === "advanced" || definition.secondaryGroups.includes(group)) {
            return (
              <details key={group} className="connector-fieldset connector-fieldset--collapsible" open={setCount > 0}>
                <summary>
                  <h3>{t(CONNECTOR_GROUP_LABELS[group])}</h3>
                  {counter}
                  <ChevronDown size={15} strokeWidth={1.75} aria-hidden />
                </summary>
                <div className="connector-fieldset__body">{body}</div>
              </details>
            );
          }
          return (
            <section key={group} className="connector-fieldset">
              <h3>
                {t(CONNECTOR_GROUP_LABELS[group])}
                {counter}
              </h3>
              {body}
            </section>
          );
        })}
        {definition.tools.length > 0 && tools ? (
          <ConnectorTools definition={definition} tools={tools} t={t} />
        ) : null}
      </div>
      <ConnectorGuide guide={definition.guide} />
    </div>
  );
}

// The connector's tools with their switches: what the agent may do with this
// connector beyond the conversation, in the place an operator looks for it.
// The Tools tab lists the same tools among hundreds; here they are the only
// ones. Everyday tools first (on by default), account administration below.
function ConnectorTools({
  definition,
  tools,
  t,
}: {
  definition: ConnectorDefinition;
  tools: ConnectorToolsProps;
  t: CatalogT;
}) {
  const everyday = definition.tools.filter((d) => !d.admin);
  const admin = definition.tools.filter((d) => d.admin);
  const row = (def: ConnectorToolDef) => (
    <div
      key={def.name}
      className="connector-tool-row"
      data-connector-tool={def.name}
      style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "8px 0", borderBottom: "1px solid var(--border)" }}
    >
      <div style={{ paddingTop: 2, flex: "0 0 auto" }}>
        <Toggle
          checked={tools.state ? Boolean(tools.state[def.name]) : !def.admin}
          disabled={tools.state === null}
          onChange={(on) => tools.onChange(def.name, on)}
          ariaLabel={def.name}
        />
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <code style={{ fontSize: 12.5 }}>{def.name}</code>
        <div className="muted" style={{ fontSize: 12, lineHeight: 1.45, marginTop: 2 }}>
          {t(def.help)}
        </div>
      </div>
    </div>
  );
  const subhead = (text: string, help: string) => (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 12.5 }}>{text}</div>
      <div className="ob-hint" style={{ marginTop: 2 }}>{help}</div>
    </div>
  );
  return (
    <section className="connector-fieldset" data-connector-tools>
      <h3>{t("catalog.group.tools")}</h3>
      <div className="ob-hint">{t("catalog.tools.intro")}</div>
      {subhead(t("catalog.tools.everyday"), t("catalog.tools.everydayHelp"))}
      {everyday.map(row)}
      {admin.length ? subhead(t("catalog.tools.admin"), t("catalog.tools.adminHelp")) : null}
      {admin.map(row)}
      {tools.state === null ? <div className="ob-hint" style={{ marginTop: 8 }}>{t("catalog.tools.loading")}</div> : null}
    </section>
  );
}

export function makeInitialConnectorValues(definition: ConnectorDefinition, initialConfig: Record<string, unknown>): ConnectorFormValues {
  return initialValues(definition, initialConfig);
}

export function makeConnectorPayload(
  definition: ConnectorDefinition,
  values: ConnectorFormValues,
  t: CatalogT,
  opts: { omitVaultSecrets?: boolean } = {},
) {
  return buildConnectorConfig(definition, values, t, opts);
}

function ConnectorField({
  field,
  t,
  agentName,
  vaultReloadSignal,
  value,
  onChange,
}: {
  field: ConnectorFieldDef;
  t: CatalogT;
  agentName?: string;
  vaultReloadSignal?: number;
  value: string | boolean | undefined;
  onChange: (value: string | boolean) => void;
}) {
  const kind = field.kind || "text";
  // Every catalog string is a translation key resolved here via `t()`. `t()`
  // falls back to returning an unknown key unchanged, so this is also safe
  // for the rare literal (non-key) technical placeholder value.
  const label = t(field.label);
  const helpText = field.help ? t(field.help) : undefined;
  const placeholder = field.placeholder ? t(field.placeholder) : undefined;
  const help = helpText
    ? `${helpText}${field.required ? ` ${t("catalog.common.required")}` : ""}`
    : field.required
      ? t("catalog.common.required")
      : undefined;

  // Vault-backed: shows the agent's override; what the user types stays form
  // state until Save stores it. Falls through to the masked input below when
  // there is no agent to scope it to.
  if (kind === "vaultSecret" && field.vaultKey && agentName) {
    return (
      <VaultSecretField
        vaultKey={field.vaultKey}
        agentName={agentName}
        label={label}
        help={helpText}
        draft={typeof value === "string" ? value : ""}
        onDraftChange={onChange}
        reloadSignal={vaultReloadSignal}
        generate={field.generate}
        missingHint={field.missingHint ? t(field.missingHint) : undefined}
      />
    );
  }

  if (kind === "readonly") {
    return (
      <div className="connector-readonly">
        <Info size={15} strokeWidth={1.75} />
        <div>
          <strong>{label}</strong>
          {helpText ? <p>{helpText}</p> : null}
        </div>
      </div>
    );
  }

  // The address this connector is already served on. Resolved, not asked for:
  // the SPA and the API share one origin on every deployment shape, so the
  // browser's own origin plus the agent's name IS the answer — printing a
  // `https://<your host>/…` template instead would make every user do a
  // substitution the page can do for them, and get it wrong sometimes.
  if (kind === "endpointUrl") {
    const url = agentName ? agentApiBaseUrl(agentName) : null;
    return (
      <div className="field">
        <label>{label}</label>
        {url ? (
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              className="input"
              value={url}
              readOnly
              onFocus={(e) => e.currentTarget.select()}
              spellCheck={false}
              style={{ fontFamily: "var(--font-mono)", flex: 1 }}
            />
            <CopyButton value={url} label={t("catalog.common.copy")} t={t} />
          </div>
        ) : (
          // No agent yet (the onboarding path creates one on save), so there
          // is no name to put in the path. Say so rather than show a template.
          <div className="connector-readonly">
            <Info size={15} strokeWidth={1.75} />
            <div>{t("catalog.apiServer.baseUrl.pending")}</div>
          </div>
        )}
        {help && <span className="ob-hint">{help}</span>}
      </div>
    );
  }

  if (kind === "bool") {
    return (
      <label className="connector-toggle">
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span>
          <strong>{label}</strong>
          {help ? <small>{help}</small> : null}
        </span>
      </label>
    );
  }

  if (kind === "select" && field.options?.length) {
    const options = field.options;
    // An untouched field keeps "" in form state (→ key omitted on save) but
    // must *display* the backend default, which is options[0] by contract.
    const current =
      typeof value === "string" && options.some((o) => o.value === value) ? value : options[0].value;
    return (
      <Field label={label} hint={help}>
        <select className="input" value={current} onChange={(e) => onChange(e.target.value)}>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {t(o.label)}
            </option>
          ))}
        </select>
      </Field>
    );
  }

  // Auto/Manual: "" means auto, so choosing Auto simply omits the key and the
  // backend's per-agent allocation applies. The user never has to know a port
  // exists unless they want to pin one.
  if (kind === "autoNumber") {
    const manual = isFilled(value);
    return (
      <Field label={label} hint={help}>
        <div className="seg-control" role="group" aria-label={label}>
          <button
            type="button"
            className={manual ? "" : "active"}
            aria-pressed={!manual}
            onClick={() => onChange("")}
          >
            {field.autoLabel ? t(field.autoLabel) : t("catalog.common.auto")}
          </button>
          <button
            type="button"
            className={manual ? "active" : ""}
            aria-pressed={manual}
            onClick={() => onChange(field.manualDefault || field.placeholder || "")}
          >
            {t("cform.manual")}
          </button>
        </div>
        {manual ? (
          <input
            className="input"
            data-filled
            type="number"
            min={field.min}
            max={field.max}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            style={{ marginTop: 8 }}
          />
        ) : (
          <p className="seg-control__note">{t("cform.autoNote")}</p>
        )}
      </Field>
    );
  }

  const multiline = kind === "list" || kind === "jsonObject" || (field.rows ?? 1) > 1;

  return (
    <Field label={label} hint={help}>
      {multiline ? (
        <textarea
          className="input"
          data-filled={isFilled(value) || undefined}
          rows={field.rows || (kind === "jsonObject" ? 8 : 3)}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          style={{ resize: "vertical", fontFamily: kind === "jsonObject" ? "var(--font-mono)" : undefined, fontSize: kind === "jsonObject" ? 12.5 : undefined }}
        />
      ) : kind === "secret" || kind === "vaultSecret" ? (
        <>
          <SecretInput
            value={typeof value === "string" ? value : ""}
            onChange={onChange}
            placeholder={placeholder}
          />
          {field.vaultKey ? <VaultNote vaultKey={field.vaultKey} value={typeof value === "string" ? value : ""} /> : null}
        </>
      ) : (
        <input
          className="input"
          data-filled={isFilled(value) || undefined}
          type={kind === "number" ? "number" : "text"}
          min={kind === "number" ? field.min : undefined}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
        />
      )}
    </Field>
  );
}

// Tells the user where a typed-in credential actually ends up. Without it the
// field reads like it writes the secret straight into agent.json5 — which is
// what it used to do. The runtime moves the value into the vault on save as a
// per-agent override and leaves a ${vault:KEY} reference behind, so the two
// states worth distinguishing are "you typed a secret, here is where it goes"
// and "this is already a reference, nothing to move".
function VaultNote({ vaultKey, value }: { vaultKey: string; value: string }) {
  const t = useT(STR);
  const isReference = /\$\{[^}]+\}/.test(value);
  return (
    <small style={{ display: "flex", alignItems: "center", gap: 5, marginTop: 5, color: "var(--text-3)" }}>
      <LockKeyhole size={12} strokeWidth={1.75} />
      {isReference ? (
        <span>{t("cform.vault.ref")}</span>
      ) : (
        <span>
          {t("cform.vault.savedPrefix")}
          <code>{vaultKey}</code>
          {t("cform.vault.savedMid")}
          <code>agent.json5</code>
          {t("cform.vault.savedSuffix")}
        </span>
      )}
    </small>
  );
}

// Masked credential input with a reveal ("eye") toggle so the token value can be
// checked without retyping. Defaults to hidden; the eye flips it to plain text.
function SecretInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  const t = useT(STR);
  const [revealed, setRevealed] = useState(false);
  return (
    <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
      <input
        className="input"
        type={revealed ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        style={{ fontFamily: "var(--font-mono)", paddingRight: 38, width: "100%" }}
      />
      <button
        type="button"
        onClick={() => setRevealed((v) => !v)}
        aria-label={revealed ? t("cform.hideValue") : t("cform.showValue")}
        aria-pressed={revealed}
        title={revealed ? t("cform.hideValue") : t("cform.showValue")}
        style={{
          position: "absolute",
          right: 6,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          border: "none",
          background: "none",
          color: "var(--text-3)",
          cursor: "pointer",
          padding: 4,
          lineHeight: 0,
        }}
      >
        {revealed ? <EyeOff size={15} strokeWidth={1.75} /> : <Eye size={15} strokeWidth={1.75} />}
      </button>
    </div>
  );
}

function isFilled(value: string | boolean | undefined): boolean {
  return typeof value === "boolean" ? value : Boolean(value && String(value).trim());
}

function groupFields(definition: ConnectorDefinition): Array<[ConnectorFieldGroup, ConnectorFieldDef[]]> {
  const groups: ConnectorFieldGroup[] = ["creds", "allow", "behavior", "advanced"];
  // Secondary groups sink to the bottom (keeping their relative order) so the
  // fields the happy path needs come first — "advanced" is always last.
  const rank = (group: ConnectorFieldGroup) => (group === "advanced" ? 2 : definition.secondaryGroups.includes(group) ? 1 : 0);
  return groups
    .map((group) => [group, definition.fields.filter((field) => (field.group || "creds") === group)] as [ConnectorFieldGroup, ConnectorFieldDef[]])
    .filter(([, items]) => items.length > 0)
    .sort(([a], [b]) => rank(a) - rank(b));
}
