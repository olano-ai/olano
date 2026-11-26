// Sub-resource editors used by the agent detail tabs: add/edit a subagent,
// add an MCP server, add a connector (channel). Each POSTs/PUTs to the agent's
// nested CRUD endpoints and calls onDone() so the detail view re-fetches.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Modal, Field, ToolPicker, GeneratePromptButton } from "./Modal";
import { useCatalog, modelOptions, modelValueLabel } from "./catalog";
import { ModelOptionGroups } from "./ModelOptions";
import { useArchetypes } from "./archetypes";
import { CatalogPicker, Segmented } from "./CatalogPicker";
import type { McpServer, McpServerStatus, SubagentEntry } from "./types";
import { McpOAuthDialog } from "./McpOAuthDialog";
import { Toggle } from "./Toggle";
import { ChevronLeft, ChevronRight, KeyRound, Plug, BookOpen } from "lucide-react";
import { type McpPreset } from "./mcpPresets";
import { useMcpDirectory, type McpExample } from "./mcpDirectory";
import { STR } from "./strings";
import { ConnectorForm, makeConnectorPayload, makeInitialConnectorValues } from "./ConnectorForm";
import { WhatsAppPairModal } from "./WhatsAppPairModal";
import { WeixinPairModal } from "./WeixinPairModal";
import { SignalSetupModal } from "./SignalSetupModal";
import {
  CONNECTOR_DEFINITIONS,
  connectorDefinitionForConfig,
  getConnectorDefinition,
  applyConnectorToolSwitches,
  connectorToolSwitches,
  getConnectorDefinitionById,
  pendingVaultSecrets,
  supportedCatalogChannels,
  type CatalogChannel,
  type CatalogChannelsResponse,
  type ConnectorDefinition,
  type ConnectorFormValues,
} from "./connectorCatalog";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

type SubSource = "custom" | "expert" | "integration";

// ── Subagent ─────────────────────────────────────────────────────────────
export function SubagentModal({
  agentName,
  existing,
  onClose,
  onDone,
}: {
  agentName: string;
  existing: SubagentEntry | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const cat = useCatalog();
  const arch = useArchetypes();
  const models = modelOptions(cat.providers);
  const [source, setSource] = useState<SubSource>("custom");
  const [pick, setPick] = useState<string | null>(null);
  const [name, setName] = useState(existing?.name || "");
  const [description, setDescription] = useState(existing?.description || "");
  const [systemPrompt, setSystemPrompt] = useState(existing?.system_prompt || "");
  const [model, setModel] = useState(existing?.model || "");
  const [tools, setTools] = useState<string[]>(existing?.tools || []);
  const [busy, setBusy] = useState(false);
  const isEdit = !!existing;

  async function submitCustom() {
    if (!name.trim() || !description.trim() || !systemPrompt.trim()) {
      toast.push(t("toast.subFieldsRequired"), "error");
      return;
    }
    setBusy(true);
    const payload = {
      name: name.trim(),
      description: description.trim(),
      system_prompt: systemPrompt,
      model: model || null,
      tools,
      skills: existing?.skills || [],
    };
    try {
      if (existing) {
        await api.put(`/api/agents/${encodeURIComponent(agentName)}/subagents/${encodeURIComponent(existing.name)}`, payload);
        toast.push(t("toast.subUpdated"));
      } else {
        await api.post(`/api/agents/${encodeURIComponent(agentName)}/subagents`, payload);
        toast.push(t("toast.subAdded"));
      }
      onDone();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.subSaveFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function submitCatalog(path: string, key: string) {
    if (!pick) return;
    setBusy(true);
    try {
      await api.post(`/api/agents/${encodeURIComponent(agentName)}/subagents/${path}`, { [key]: pick });
      toast.push(t("toast.subAdded"));
      onDone();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.subSaveFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  function onPrimary() {
    if (source === "custom") return void submitCustom();
    if (source === "expert") return void submitCatalog("from-archetype", "archetype");
    return void submitCatalog("from-integration", "toolkit_agent");
  }

  const specialistItems = arch.data.specialists.map((s) => ({ name: s.name, label: s.label, description: s.description, color: s.color }));
  const integrationItems = arch.data.integrations.map((i) => ({ name: i.name, label: i.label, description: i.description, badge: i.toolkit }));
  // Shared load state for both catalog tabs — a picker must never render an
  // in-flight or failed fetch as an empty catalog.
  const pickerState = {
    status: arch.status,
    errorText: arch.error ? t("form.catalogError", { detail: arch.error }) : t("form.catalogErrorGeneric"),
    retryText: t("btn.retry"),
    onRetry: arch.refresh,
  };
  const canSubmit = source === "custom" ? true : !!pick;
  const primaryLabel = busy
    ? t("btn.saving")
    : isEdit
      ? t("btn.save")
      : source === "custom"
        ? t("submodal.add")
        : t("submodal.addSelected");

  return (
    <Modal
      title={existing ? t("submodal.editTitle", { name: existing.name }) : t("submodal.addTitle")}
      subtitle={t("submodal.subtitle")}
      onClose={onClose}
      width={600}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("btn.cancel")}
          </button>
          <button className="btn primary" onClick={onPrimary} disabled={busy || !canSubmit}>
            {primaryLabel}
          </button>
        </>
      }
    >
      {!isEdit && (
        <Segmented
          value={source}
          onChange={(v) => {
            setSource(v);
            setPick(null);
          }}
          options={[
            { value: "custom", label: t("submodal.src.custom") },
            { value: "expert", label: t("submodal.src.expert") },
            { value: "integration", label: t("submodal.src.integration") },
          ]}
        />
      )}

      {source === "expert" && !isEdit ? (
        <CatalogPicker
          items={specialistItems}
          selected={pick}
          onSelect={setPick}
          searchPlaceholder={t("submodal.searchExperts")}
          emptyText={t("submodal.noExperts")}
          {...pickerState}
        />
      ) : source === "integration" && !isEdit ? (
        <CatalogPicker
          items={integrationItems}
          selected={pick}
          onSelect={setPick}
          searchPlaceholder={t("submodal.searchIntegrations")}
          emptyText={t("submodal.noIntegrations")}
          {...pickerState}
        />
      ) : (
        <>
          <Field label={t("form.name")}>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoFocus disabled={!!existing} />
          </Field>
          <Field label={t("form.description")}>
            <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <Field label={t("form.model")} hint={t("submodal.modelHint")}>
            <select className="input" value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">{t("submodal.inherit")}</option>
              {model && !models.some((m) => m.value === model) && (
                <option value={model}>{modelValueLabel(model)}</option>
              )}
              <ModelOptionGroups options={models} />
            </select>
          </Field>
          <Field
            label={t("form.systemPrompt")}
            action={
              <GeneratePromptButton
                name={name}
                description={description}
                tools={tools}
                model={model}
                agent={agentName}
                kind="subagent"
                onGenerated={setSystemPrompt}
              />
            }
          >
            <textarea
              className="input"
              style={{ minHeight: 130, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 13 }}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
          </Field>
          <Field label={t("form.tools")}>
            <ToolPicker all={cat.toolNames} selected={tools} onChange={setTools} />
          </Field>
        </>
      )}
    </Modal>
  );
}

// Read-only detail view for a subagent: full system prompt, model, and complete
// tool/skill lists. The row card only ever showed a name, an 8-tool preview,
// and a model badge — catalog-sourced subagents (builtin/archetype) have no
// edit UI at all, so this is often the only place their real configuration is
// visible.
export function SubagentDetailModal({
  sub,
  parentModel,
  onClose,
  onEdit,
}: {
  sub: SubagentEntry;
  parentModel?: string;
  onClose: () => void;
  // The caller routes catalog-sourced (builtin/archetype) subagents to the
  // overrides editor and inline ones to the free-form editor.
  onEdit?: () => void;
}) {
  const t = useT(STR);
  const tools = [...(sub.tools || [])].sort();
  const skills = sub.skills || [];
  const sourceLabel =
    sub.source === "builtin"
      ? t("subdetail.sourceBuiltin")
      : sub.source === "archetype"
        ? t("subdetail.sourceArchetype")
        : t("subdetail.sourceInline");

  return (
    <Modal
      title={sub.name}
      subtitle={sub.description}
      onClose={onClose}
      width={640}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            {t("card.close")}
          </button>
          {onEdit && (
            <button className="btn primary" onClick={onEdit}>
              {t("btn.edit")}
            </button>
          )}
        </>
      }
    >
      <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 16 }}>
        <span className="badge">{sourceLabel}</span>
        {sub.from_builtin && (
          <span className="badge row" style={{ gap: 4 }}>
            <Plug size={12} strokeWidth={1.75} /> {sub.from_builtin}
          </span>
        )}
        {sub.from_archetype && (
          <span className="badge row" style={{ gap: 4 }}>
            <BookOpen size={12} strokeWidth={1.75} /> {sub.from_archetype}
          </span>
        )}
        {sub.enabled === false && (
          <span className="badge" style={{ color: "var(--warn)" }}>
            {t("subdetail.disabled")}
          </span>
        )}
        {sub.backend && <span className="badge">{t("subdetail.ownBackend")}</span>}
      </div>

      <Field label={t("form.model")}>
        <div style={{ fontSize: 13, fontFamily: sub.model ? "var(--font-mono)" : undefined }}>
          {sub.model || (parentModel ? t("subdetail.inheritsModel", { model: parentModel }) : t("submodal.inherit"))}
        </div>
      </Field>

      <Field label={`${t("form.tools")} (${tools.length})`}>
        {tools.length ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {tools.map((tool) => (
              <span key={tool} className="badge">
                {tool}
              </span>
            ))}
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            {t("subdetail.noTools")}
          </div>
        )}
      </Field>

      {skills.length > 0 && (
        <Field label={`${t("form.skills")} (${skills.length})`}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {skills.map((skill) => (
              <span key={skill} className="badge">
                {skill}
              </span>
            ))}
          </div>
        </Field>
      )}

      <Field label={t("form.systemPrompt")}>
        {sub.system_prompt ? (
          <pre
            style={{
              margin: 0,
              padding: "10px 12px",
              background: "var(--surface-2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              fontFamily: "var(--font-mono)",
              fontSize: 12.5,
              lineHeight: 1.55,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 320,
              overflow: "auto",
            }}
          >
            {sub.system_prompt}
          </pre>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            {t("subdetail.noPrompt")}
          </div>
        )}
      </Field>
    </Modal>
  );
}

// Override editor for CATALOG-SOURCED subagents (archetype experts and builtin
// integrations). Unlike SubagentModal's free-form editor, this writes only the
// override set stored next to the ref in agent.json5 — the ref stays live, so
// catalog updates to the specialist keep landing. Two prompt affordances:
// "append" (system_prompt_extension — base prompt stays live, owner text rides
// on top) and "detach" (a full system_prompt override that freezes the prompt
// against catalog updates — the explicit, warned-about choice).
export function SubagentOverridesModal({
  agentName,
  sub,
  onClose,
  onDone,
}: {
  agentName: string;
  sub: SubagentEntry;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const cat = useCatalog();
  const models = modelOptions(cat.providers);
  const overrides = sub.overrides || {};
  const basePrompt = sub.system_prompt_base ?? sub.system_prompt ?? "";
  const [model, setModel] = useState(overrides.model || "");
  const [extension, setExtension] = useState(overrides.system_prompt_extension || "");
  const [detached, setDetached] = useState(!!overrides.system_prompt);
  const [fullPrompt, setFullPrompt] = useState(overrides.system_prompt || basePrompt);
  const [showBase, setShowBase] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = sub.ref || sub.from_archetype || sub.from_builtin || sub.name;

  async function submit() {
    setBusy(true);
    // PUT semantics: an omitted field CLEARS that override, so the tool
    // overrides already stored on the stub must travel back unchanged — this
    // modal edits model + prompt only.
    const payload: Record<string, unknown> = {
      model: model || undefined,
      system_prompt: detached ? fullPrompt : undefined,
      system_prompt_extension: extension.trim() || undefined,
      tools: overrides.tools,
      extra_tools: overrides.extra_tools,
      disabled_tools: overrides.disabled_tools,
    };
    try {
      await api.put(
        `/api/agents/${encodeURIComponent(agentName)}/subagents/${encodeURIComponent(ref)}/overrides`,
        payload,
      );
      toast.push(t("toast.subUpdated"));
      onDone();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.subSaveFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("suboverride.title", { name: sub.name })}
      subtitle={t("suboverride.subtitle")}
      onClose={onClose}
      width={640}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("btn.cancel")}
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={busy}>
            {busy ? t("btn.saving") : t("btn.save")}
          </button>
        </>
      }
    >
      <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 14 }}>
        {sub.from_archetype && (
          <span className="badge row" style={{ gap: 4 }}>
            <BookOpen size={12} strokeWidth={1.75} /> {sub.from_archetype}
          </span>
        )}
        {sub.from_builtin && (
          <span className="badge row" style={{ gap: 4 }}>
            <Plug size={12} strokeWidth={1.75} /> {sub.from_builtin}
          </span>
        )}
      </div>

      <Field label={t("form.model")} hint={t("suboverride.modelHint")}>
        <select className="input" value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">
            {sub.system_prompt_base !== undefined && !overrides.model && sub.model
              ? t("suboverride.inheritCurrent", { model: modelValueLabel(sub.model) })
              : t("submodal.inherit")}
          </option>
          {model && !models.some((m) => m.value === model) && (
            <option value={model}>{modelValueLabel(model)}</option>
          )}
          <ModelOptionGroups options={models} />
        </select>
      </Field>

      <Field label={t("suboverride.basePrompt")} hint={t("suboverride.basePromptHint")}>
        <button className="btn" style={{ alignSelf: "flex-start" }} onClick={() => setShowBase((v) => !v)}>
          {showBase ? t("suboverride.hideBase") : t("suboverride.showBase")}
        </button>
        {showBase && (
          <pre
            style={{
              margin: "8px 0 0",
              padding: "10px 12px",
              background: "var(--surface-2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              fontFamily: "var(--font-mono)",
              fontSize: 12.5,
              lineHeight: 1.55,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 260,
              overflow: "auto",
            }}
          >
            {basePrompt || t("subdetail.noPrompt")}
          </pre>
        )}
      </Field>

      <Field label={t("suboverride.append")} hint={t("suboverride.appendHint")}>
        <textarea
          className="input"
          style={{ minHeight: 110, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 13 }}
          placeholder={t("suboverride.appendPlaceholder")}
          value={extension}
          onChange={(e) => setExtension(e.target.value)}
        />
      </Field>

      <Field label={t("suboverride.detach")} hint={t("suboverride.detachHint")}>
        <label className="row" style={{ gap: 8, alignItems: "center", fontSize: 13 }}>
          <input
            type="checkbox"
            checked={detached}
            onChange={(e) => {
              setDetached(e.target.checked);
              if (e.target.checked && !fullPrompt) setFullPrompt(basePrompt);
            }}
          />
          {t("suboverride.detachLabel")}
        </label>
        {detached && (
          <textarea
            className="input"
            style={{
              marginTop: 8,
              minHeight: 160,
              resize: "vertical",
              fontFamily: "var(--font-mono)",
              fontSize: 13,
            }}
            value={fullPrompt}
            onChange={(e) => setFullPrompt(e.target.value)}
          />
        )}
      </Field>
    </Modal>
  );
}

function splitLines(value: string): string[] {
  return value
    .split("\n")
    .map((v) => v.trim())
    .filter(Boolean);
}

function parseEnvLines(value: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of splitLines(value)) {
    const idx = line.indexOf("=");
    if (idx <= 0) throw new Error(`Invalid env line: ${line}`);
    env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return env;
}

// True when any KEY=value line is missing its value — e.g. a preset/example
// prefilled the required variable NAMES (API_KEY=) but the user hasn't supplied
// the secret yet. Drives the "this field still needs input" highlight.
function hasEmptyEnvValue(value: string): boolean {
  for (const line of splitLines(value)) {
    if (line.startsWith("#")) continue;
    const idx = line.indexOf("=");
    if (idx > 0 && !line.slice(idx + 1).trim()) return true;
  }
  return false;
}

// HTTP headers accept either "Key: Value" (natural) or "Key=Value" (env-style).
function parseHeaderLines(value: string): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const line of splitLines(value)) {
    const colon = line.indexOf(":");
    const eq = line.indexOf("=");
    const idx = colon > 0 && (eq < 0 || colon < eq) ? colon : eq;
    if (idx <= 0) throw new Error(`Invalid header line: ${line}`);
    headers[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return headers;
}

function mcpToJson(value: McpServer): string {
  return JSON.stringify(
    {
      url: value.url || "",
      transport: value.transport || "sse",
      enabled: value.enabled ?? true,
      ...(value.command ? { command: value.command } : {}),
      ...(value.args?.length ? { args: value.args } : {}),
      ...(value.env && Object.keys(value.env).length ? { env: value.env } : {}),
      ...(value.headers && Object.keys(value.headers).length ? { headers: value.headers } : {}),
      ...(value.auth && value.auth !== "none" ? { auth: value.auth } : {}),
      ...(value.oauth_scope ? { oauth_scope: value.oauth_scope } : {}),
      ...(value.oauth_client_id ? { oauth_client_id: value.oauth_client_id } : {}),
      ...(value.oauth_client_secret ? { oauth_client_secret: value.oauth_client_secret } : {}),
      ...(value.disabled_tools?.length ? { disabled_tools: value.disabled_tools } : {}),
    },
    null,
    2,
  );
}

function unwrapMcpPayload(input: unknown): unknown {
  if (!input || typeof input !== "object" || Array.isArray(input)) return input;
  const raw = input as Record<string, unknown>;
  const wrapped =
    raw.mcpServers && typeof raw.mcpServers === "object" && !Array.isArray(raw.mcpServers)
      ? (raw.mcpServers as Record<string, unknown>)
      : raw.servers && typeof raw.servers === "object" && !Array.isArray(raw.servers)
        ? (raw.servers as Record<string, unknown>)
        : null;
  if (!wrapped) return input;
  const entries = Object.entries(wrapped);
  if (entries.length !== 1) throw new Error("Paste exactly one MCP server at a time.");
  const [serverName, serverConfig] = entries[0];
  if (!serverConfig || typeof serverConfig !== "object" || Array.isArray(serverConfig)) {
    throw new Error("MCP server config must be a JSON object.");
  }
  return { url: serverName, ...(serverConfig as Record<string, unknown>) };
}

function normalizeTransportLabel(value: string): string {
  const v = value.trim().toLowerCase();
  if (v === "streamable_http" || v === "streamable-http" || v === "streamablehttp" || v === "shttp") return "http";
  return v;
}

// `npx mcp-remote <url>` is a stdio bridge for clients that can't speak HTTP
// themselves. Olano can, so a pasted bridge config is imported as the native
// remote server instead — no Node/npx child left to hang on a cold npx cache or
// die on a host without Node (the classic "works on localhost, times out on the
// server" failure). Only exact, recognized shapes are unwrapped; a config with
// an `env` block, `${VAR}` header expansion, or unknown flags imports verbatim
// because mcp-remote resolves those itself.
function unwrapMcpRemoteBridge(raw: Record<string, unknown>): Record<string, unknown> {
  const command = typeof raw.command === "string" ? raw.command.trim() : "";
  if (!["npx", "pnpx", "bunx"].includes(command)) return raw;
  if (raw.env && typeof raw.env === "object" && Object.keys(raw.env).length) return raw;
  const rest = Array.isArray(raw.args) ? raw.args.map(String) : [];
  while (rest[0] === "-y" || rest[0] === "--yes") rest.shift();
  const pkg = rest.shift() ?? "";
  if (pkg !== "mcp-remote" && !pkg.startsWith("mcp-remote@")) return raw;
  const url = rest.shift() ?? "";
  if (!/^https?:\/\//.test(url)) return raw;
  let transport = "http"; // mcp-remote's default strategy is http-first
  const headers: Record<string, string> = {};
  while (rest.length) {
    const flag = rest.shift();
    if (flag === "--transport") {
      const value = rest.shift() ?? "";
      if (value === "http-only" || value === "http-first") transport = "http";
      else if (value === "sse-only" || value === "sse-first") transport = "sse";
      else return raw;
    } else if (flag === "--header") {
      const value = rest.shift() ?? "";
      const idx = value.indexOf(":");
      if (idx <= 0 || value.includes("${")) return raw;
      headers[value.slice(0, idx).trim()] = value.slice(idx + 1).trim();
    } else if (flag === "--allow-http") {
      continue;
    } else {
      return raw;
    }
  }
  return {
    url,
    transport,
    ...(Object.keys(headers).length ? { headers } : {}),
    ...(typeof raw.enabled === "boolean" ? { enabled: raw.enabled } : {}),
  };
}

function normalizeMcpPayload(input: unknown): McpServer {
  input = unwrapMcpPayload(input);
  if (input && typeof input === "object" && !Array.isArray(input)) {
    input = unwrapMcpRemoteBridge(input as Record<string, unknown>);
  }
  if (!input || typeof input !== "object") throw new Error("MCP config must be a JSON object.");
  const raw = input as Record<string, unknown>;
  const url = typeof raw.url === "string" ? raw.url.trim() : "";
  const inferredTransport = typeof raw.command === "string" && raw.command.trim() ? "stdio" : "sse";
  // Docs-style configs use `type` (e.g. Stripe's {"type":"http"}); accept it as
  // an alias for `transport`.
  const rawTransport =
    typeof raw.transport === "string"
      ? raw.transport
      : typeof raw.type === "string"
        ? raw.type
        : inferredTransport;
  const transport = normalizeTransportLabel(rawTransport);
  const command = typeof raw.command === "string" ? raw.command.trim() : undefined;
  const args = Array.isArray(raw.args) ? raw.args.map(String) : [];
  const env =
    raw.env && typeof raw.env === "object" && !Array.isArray(raw.env)
      ? Object.fromEntries(Object.entries(raw.env as Record<string, unknown>).map(([k, v]) => [k, String(v)]))
      : {};
  const headers =
    raw.headers && typeof raw.headers === "object" && !Array.isArray(raw.headers)
      ? Object.fromEntries(Object.entries(raw.headers as Record<string, unknown>).map(([k, v]) => [k, String(v)]))
      : {};
  const auth = typeof raw.auth === "string" ? raw.auth.trim().toLowerCase() : "none";
  const oauthScope = typeof raw.oauth_scope === "string" ? raw.oauth_scope.trim() : undefined;
  const oauthClientId = typeof raw.oauth_client_id === "string" ? raw.oauth_client_id.trim() : undefined;
  const oauthClientSecret =
    typeof raw.oauth_client_secret === "string" ? raw.oauth_client_secret.trim() : undefined;
  const disabledTools = Array.isArray(raw.disabled_tools) ? raw.disabled_tools.map(String) : [];
  return {
    url,
    transport: transport || "sse",
    enabled: typeof raw.enabled === "boolean" ? raw.enabled : true,
    ...(command ? { command } : {}),
    ...(args.length ? { args } : {}),
    ...(Object.keys(env).length ? { env } : {}),
    ...(Object.keys(headers).length ? { headers } : {}),
    ...(auth && auth !== "none" ? { auth } : {}),
    ...(oauthScope ? { oauth_scope: oauthScope } : {}),
    ...(oauthClientId ? { oauth_client_id: oauthClientId } : {}),
    ...(oauthClientSecret ? { oauth_client_secret: oauthClientSecret } : {}),
    ...(disabledTools.length ? { disabled_tools: disabledTools } : {}),
  };
}

// Copy-paste JSON templates for the "MCP JSON" editor, one per common way to
// declare a server. All use the docs-style {"mcpServers":{"name":{...}}} shape
// the backend accepts (normalizeMcpPayload unwraps it), so a user can pick the
// tab matching their server and edit in place.
type JsonTemplateKey = "http" | "sse" | "oauth" | "stdio" | "npx" | "python";

const JSON_TEMPLATES: { key: JsonTemplateKey; hint: string; json: string }[] = [
  {
    key: "http",
    hint: "Remote server over streamable HTTP, addressed by URL.",
    json: JSON.stringify({ mcpServers: { serverName: { type: "http", url: "https://example.com/mcp" } } }, null, 2),
  },
  {
    key: "sse",
    hint: "Remote server over the legacy SSE transport.",
    json: JSON.stringify({ mcpServers: { serverName: { type: "sse", url: "https://example.com/sse" } } }, null, 2),
  },
  {
    key: "oauth",
    hint: "Remote HTTP server that requires OAuth — Connect runs the sign-in for you.",
    json: JSON.stringify(
      { mcpServers: { stripe: { type: "http", url: "https://mcp.stripe.com", auth: "oauth" } } },
      null,
      2,
    ),
  },
  {
    key: "stdio",
    hint: "Server launched as a subprocess (command + args). On managed deployments it runs inside an isolated sandbox container.",
    json: JSON.stringify({ mcpServers: { serverName: { command: "server-binary", args: ["--flag", "value"] } } }, null, 2),
  },
  {
    key: "npx",
    hint: "Node package via npx — runs in an isolated sandbox container on managed deployments. For mcp-remote wrappers, add the remote URL directly with transport http instead.",
    json: JSON.stringify(
      {
        mcpServers: {
          serverName: {
            command: "npx",
            args: [
              "mcp-remote",
              "https://olano-929869942.zohomcp.com/mcp/f7d3c3ed74194a8cf8d868f34f1dca00/message",
              "--transport",
              "http-only",
            ],
          },
        },
      },
      null,
      2,
    ),
  },
  {
    key: "python",
    hint: "Python server via uvx (or python -m ...).",
    json: JSON.stringify(
      { mcpServers: { time: { command: "uvx", args: ["mcp-server-time", "--local-timezone", "Asia/Singapore"] } } },
      null,
      2,
    ),
  },
];

function jsonTemplate(key: JsonTemplateKey): string {
  return JSON_TEMPLATES.find((tpl) => tpl.key === key)?.json ?? "";
}

// Catalog chip logo with a generic fallback: some provider favicons 404, and a
// few directory rows may lack one — show a neutral plug icon instead of a gap.
function ServerLogo({ src, size = 14 }: { src?: string | null; size?: number }) {
  const [broken, setBroken] = useState(false);
  if (!src || broken) return <Plug size={size} strokeWidth={1.75} style={{ opacity: 0.65, flexShrink: 0 }} />;
  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      style={{ borderRadius: 3, flexShrink: 0 }}
      onError={() => setBroken(true)}
    />
  );
}

// ── MCP server ───────────────────────────────────────────────────────────
export function McpModal({
  agentName,
  existing,
  onClose,
  onDone,
}: {
  agentName: string;
  existing?: McpServer | null;
  onClose: () => void;
  // Called after save/delete. On save it receives the fresh probe result so
  // the MCP tab can render status + tools immediately, without a manual refresh.
  onDone: (status?: McpServerStatus) => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const { presets, examples } = useMcpDirectory();
  const [mode, setMode] = useState<"guided" | "json">("guided");
  const [url, setUrl] = useState(existing?.url || "");
  const [transport, setTransport] = useState(existing?.transport || "auto");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [command, setCommand] = useState(existing?.command || "");
  const [argsText, setArgsText] = useState((existing?.args || []).join("\n"));
  const [envText, setEnvText] = useState(
    Object.entries(existing?.env || {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
  );
  const [auth, setAuth] = useState(existing?.auth || "none");
  const [oauthScope, setOauthScope] = useState(existing?.oauth_scope || "");
  const [oauthClientId, setOauthClientId] = useState(existing?.oauth_client_id || "");
  const [oauthClientSecret, setOauthClientSecret] = useState(existing?.oauth_client_secret || "");
  const [headersText, setHeadersText] = useState(
    Object.entries(existing?.headers || {})
      .map(([k, v]) => `${k}: ${v}`)
      .join("\n"),
  );
  const [jsonText, setJsonText] = useState(mcpToJson(existing || { url: "", transport: "sse", enabled: true }));
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<McpServerStatus | null>(null);
  const [showLogs, setShowLogs] = useState(false);
  const [oauthOpen, setOauthOpen] = useState(false);
  const [authorized, setAuthorized] = useState(false);
  const [presetId, setPresetId] = useState<string>(existing ? "custom" : "");
  // Per-tool selection made after Connect; persisted as disabled_tools on save.
  const [disabledTools, setDisabledTools] = useState<string[]>(existing?.disabled_tools || []);
  const [toolsTouched, setToolsTouched] = useState(!!existing?.disabled_tools?.length);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [dirQuery, setDirQuery] = useState("");
  const [dirPage, setDirPage] = useState(0);
  const [catalogW, setCatalogW] = useState(0);
  const [jsonTab, setJsonTab] = useState<JsonTemplateKey>("http");
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const catalogRo = useRef<ResizeObserver | null>(null);
  useEffect(() => () => { if (copyTimer.current) clearTimeout(copyTimer.current); }, []);
  // Measure the service grid so it paginates to ~4 rows at any viewport. The old
  // unpaginated list ran long enough to bury the credential fields below the fold,
  // so users picked a service without noticing the secrets they still had to enter.
  const measureCatalog = useCallback((el: HTMLDivElement | null) => {
    catalogRo.current?.disconnect();
    catalogRo.current = null;
    if (!el) return;
    setCatalogW(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setCatalogW(Math.round(w));
    });
    ro.observe(el);
    catalogRo.current = ro;
  }, []);
  useEffect(() => () => catalogRo.current?.disconnect(), []);
  // Redirect URI to register with the provider (HubSpot, etc.). Resolved by the
  // runtime with relay→origin precedence — a relay-backed deployment shows the
  // shared https callback (e.g. https://auth.olano.ai/mcp/callback) rather than
  // this dashboard's localhost origin, which a provider rejects / can't reach.
  const [redirectUri, setRedirectUri] = useState(`${window.location.origin}/api/mcp/oauth/callback`);
  useEffect(() => {
    let cancelled = false;
    api
      .get<{ redirect_uri: string }>(
        `/api/mcp/oauth/redirect-uri?redirect_base=${encodeURIComponent(window.location.origin)}`,
      )
      .then((r) => {
        if (!cancelled && r?.redirect_uri) setRedirectUri(r.redirect_uri);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  const selectedPreset: McpPreset | undefined = presets.find((p) => p.id === presetId);
  const isPresetSelected = presetId !== "" && presetId !== "custom";
  const advancedOpen = presetId === "custom" || presetId === "" || showAdvanced;
  // Modal phase: a successful probe (OAuth completed if the server demanded it)
  // means "connected"; `existing` means the server is already saved. The footer
  // renders one action set per phase so Connect and Save never appear together.
  const connected = !!testResult?.ok;
  const oauthish = auth === "oauth" || !!testResult?.needs_oauth || selectedPreset?.auth === "oauth";
  // Directory filter over the unified catalog (presets + local starters).
  const dirNeedle = dirQuery.trim().toLowerCase();
  const filteredPresets = dirNeedle
    ? presets.filter((p) =>
        `${p.title} ${p.category} ${p.notes ? t(p.notes) : ""}`.toLowerCase().includes(dirNeedle),
      )
    : presets;
  const filteredExamples = dirNeedle
    ? examples.filter((e) => `${e.title} ${e.badge} ${t(e.description)}`.toLowerCase().includes(dirNeedle))
    : examples;

  // Unified, paginated service catalog: presets first, then local starters
  // (hidden when editing an existing server). "Custom" is pinned outside the
  // pager, so the escape hatch is always one click away regardless of page.
  const catalogItems: Array<
    { kind: "preset"; preset: McpPreset } | { kind: "example"; example: McpExample }
  > = [
    ...filteredPresets.map((preset) => ({ kind: "preset" as const, preset })),
    ...(existing ? [] : filteredExamples.map((example) => ({ kind: "example" as const, example }))),
  ];
  const CATALOG_ROWS = 4;
  // ~150px per chip; fall back to a 4-column desktop guess before the first measure.
  const catalogCols = Math.max(1, Math.floor((catalogW || 640) / 150));
  const catalogPageSize = catalogCols * CATALOG_ROWS;
  const catalogPageCount = Math.max(1, Math.ceil(catalogItems.length / catalogPageSize));
  const catalogPage = Math.min(dirPage, catalogPageCount - 1);
  const pagedCatalog = catalogItems.slice(
    catalogPage * catalogPageSize,
    catalogPage * catalogPageSize + catalogPageSize,
  );
  // Reset to the first page whenever the search filter changes.
  useEffect(() => {
    setDirPage(0);
  }, [dirNeedle]);

  function guidedPayload(): McpServer {
    const env = parseEnvLines(envText);
    const args = splitLines(argsText);
    const headers = transport !== "stdio" ? parseHeaderLines(headersText) : {};
    const useOauth = transport !== "stdio" && auth === "oauth";
    return {
      url: url.trim(),
      transport,
      enabled,
      ...(transport === "stdio" && command.trim() ? { command: command.trim() } : {}),
      ...(args.length ? { args } : {}),
      ...(Object.keys(env).length ? { env } : {}),
      ...(Object.keys(headers).length ? { headers } : {}),
      ...(useOauth ? { auth: "oauth" } : {}),
      ...(useOauth && oauthScope.trim() ? { oauth_scope: oauthScope.trim() } : {}),
      ...(useOauth && oauthClientId.trim() ? { oauth_client_id: oauthClientId.trim() } : {}),
      ...(useOauth && oauthClientSecret.trim() ? { oauth_client_secret: oauthClientSecret.trim() } : {}),
      ...(disabledTools.length ? { disabled_tools: [...disabledTools].sort() } : {}),
    };
  }

  function payload(): McpServer {
    const base = mode === "json" ? normalizeMcpPayload(JSON.parse(jsonText)) : guidedPayload();
    // The auto-detected OAuth escalation (401 → authorize flow) lives in
    // component state, not in user-pasted JSON. Overlay it so the
    // post-authorize re-probe AND the saved entry both carry auth: oauth —
    // otherwise the re-probe reconnects unauthenticated, hits the same 401,
    // and the modal never advances past "Connect & Authorize".
    if (auth === "oauth" && base.transport !== "stdio" && !base.auth) {
      base.auth = "oauth";
      if (oauthScope.trim() && !base.oauth_scope) base.oauth_scope = oauthScope.trim();
      if (oauthClientId.trim() && !base.oauth_client_id) base.oauth_client_id = oauthClientId.trim();
      if (oauthClientSecret.trim() && !base.oauth_client_secret) base.oauth_client_secret = oauthClientSecret.trim();
    }
    // Once the user has touched the toggle list shown after Connect, it is the
    // source of truth for tools — overlay it on either mode's payload.
    if (toolsTouched) {
      if (disabledTools.length) base.disabled_tools = [...disabledTools].sort();
      else delete base.disabled_tools;
    }
    return base;
  }

  // Editing the connection target invalidates a previous Connect result.
  function invalidateConnection() {
    if (testResult || authorized) {
      setTestResult(null);
      setAuthorized(false);
    }
    setConfirmDelete(false);
  }

  // "Save changes" appears only when the edited config differs from the saved one.
  const initialSig = useRef(existing ? mcpToJson(existing) : "");
  let dirty = !existing;
  if (existing) {
    try {
      dirty = mcpToJson(payload()) !== initialSig.current;
    } catch {
      dirty = true;
    }
  }

  function applyExample(example: McpExample) {
    const config = example.config;
    setUrl(config.url);
    setTransport(config.transport || "auto");
    setEnabled(config.enabled ?? true);
    setCommand(config.command || "");
    setArgsText((config.args || []).join("\n"));
    setEnvText(
      Object.entries(config.env || {})
        .map(([k, v]) => `${k}=${v}`)
        .join("\n"),
    );
    setAuth(config.auth || "none");
    setOauthScope(config.oauth_scope || "");
    setOauthClientId(config.oauth_client_id || "");
    setOauthClientSecret(config.oauth_client_secret || "");
    setHeadersText(
      Object.entries(config.headers || {})
        .map(([k, v]) => `${k}: ${v}`)
        .join("\n"),
    );
    setJsonText(example.json || mcpToJson(config));
    setPresetId(`example:${example.id}`);
    // Local starters usually need env values filled in — open the fields.
    setShowAdvanced(true);
    setMode("guided");
    setTestResult(null);
    setAuthorized(false);
    setDisabledTools([]);
    setToolsTouched(false);
  }

  function applyPreset(preset: McpPreset) {
    setPresetId(preset.id);
    setShowAdvanced(false);
    setUrl(preset.url);
    setTransport(preset.transport);
    setAuth(preset.auth);
    setMode("guided");
    setTestResult(null);
    setAuthorized(false);
    setDisabledTools([]);
    setToolsTouched(false);
  }

  // "Connect" — probe the server and auto-handle whatever it needs. The backend
  // probe auto-detects the transport requirement and whether the server demands
  // OAuth (even when Auth is left on "None"). If it does, we flip Auth to OAuth
  // and launch the authorization flow automatically — the user never has to know
  // the server uses OAuth or pick it manually.
  async function testConnection() {
    let body: McpServer;
    try {
      body = payload();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("mcpmodal.invalidConfig"), "error");
      return;
    }
    if (!body.url.trim() && body.transport !== "stdio") {
      toast.push(t("toast.urlRequired"), "error");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.post<McpServerStatus>(`/api/agents/${encodeURIComponent(agentName)}/mcp/test`, body);
      setTestResult(result);
      setAuthorized(!!result.authorized);
      // Auto-detect: the probe reports the transport that actually reached the
      // server (it tries the alternate when the chosen one fails). Adopt it so
      // the field — and what gets saved — reflects reality.
      if (result.transport && result.transport !== "stdio" && result.transport !== transport) {
        setTransport(result.transport);
      }
      if (result.status === "auth_required") {
        // Auto-escalate: select OAuth and open the authorization dialog now.
        setAuth("oauth");
        toast.push(t("mcpmodal.authDetected"));
        setOauthOpen(true);
      } else if (result.ok) {
        toast.push(result.message || t("mcp.connectedGeneric"));
      } else {
        toast.push(result.message || t("mcp.connectFailed"), "error");
      }
    } catch (e) {
      const message = errText(e, t("mcp.connectFailed"));
      setTestResult({ url: body.url, transport: body.transport, ok: false, status: "error", message });
      toast.push(message, "error");
    } finally {
      setTesting(false);
    }
  }

  async function submit() {
    let body: McpServer;
    try {
      body = payload();
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("mcpmodal.invalidConfig"), "error");
      return;
    }
    if (!body.url.trim()) {
      toast.push(t("toast.urlRequired"), "error");
      return;
    }
    setBusy(true);
    try {
      if (existing) {
        await api.put(`/api/agents/${encodeURIComponent(agentName)}/mcp?url=${encodeURIComponent(existing.url)}`, body);
        toast.push(t("mcpmodal.updated"));
      } else {
        await api.post(`/api/agents/${encodeURIComponent(agentName)}/mcp`, body);
        toast.push(t("toast.mcpAdded"));
      }
      // Hand the fresh probe result to the caller so the MCP tab renders the
      // server's status and tools immediately.
      onDone(
        testResult?.ok
          ? { ...testResult, url: body.url, transport: body.transport, enabled: body.enabled ?? true }
          : undefined,
      );
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.mcpAddFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  // Disconnect: clear any stored OAuth tokens and drop the probe result. For a
  // not-yet-saved server this simply returns the modal to the disconnected state.
  async function disconnect() {
    const target = (existing?.url || url).trim();
    if (oauthish && target) {
      try {
        await api.del(`/api/agents/${encodeURIComponent(agentName)}/mcp/oauth?url=${encodeURIComponent(target)}`);
        toast.push(t("mcpmodal.disconnected"));
      } catch {
        // No stored tokens is fine — still reset the local connection state.
      }
    }
    setTestResult(null);
    setAuthorized(false);
    setConfirmDelete(false);
  }

  // Delete (saved servers only) — two-click confirm, then remove and close.
  async function deleteServer() {
    if (!existing) return;
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setDeleting(true);
    try {
      await api.del(`/api/agents/${encodeURIComponent(agentName)}/mcp?url=${encodeURIComponent(existing.url)}`);
      toast.push(t("toast.mcpRemoved"));
      onDone();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.mcpRemoveFailed")), "error");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <Modal
      title={existing ? t("mcp.editAria") : t("mcpmodal.title")}
      subtitle={t("mcpmodal.formSubtitle")}
      onClose={onClose}
      width={720}
      footer={
        // One action set per phase — Connect and Save are never shown together.
        existing ? (
          // Saved server: manage auth/lifecycle; Save appears only when edited.
          <>
            {oauthish && (
              <button
                className="btn"
                disabled={busy || testing || deleting}
                onClick={() => {
                  setAuth("oauth");
                  setOauthOpen(true);
                }}
              >
                {t("mcpmodal.reauth")}
              </button>
            )}
            {(oauthish || connected) && (
              <button className="btn" onClick={() => void disconnect()} disabled={busy || testing || deleting}>
                {t("mcpmodal.disconnect")}
              </button>
            )}
            <button className="btn danger" onClick={() => void deleteServer()} disabled={busy || testing || deleting}>
              {deleting ? t("btn.saving") : confirmDelete ? t("mcpmodal.confirmDelete") : t("mcpmodal.deleteServer")}
            </button>
            <button className="btn" onClick={onClose} disabled={busy || deleting}>
              {t("btn.cancel")}
            </button>
            {dirty && (
              <button className="btn primary" onClick={() => void submit()} disabled={busy || deleting}>
                {busy ? t("btn.saving") : t("mcpmodal.saveChanges")}
              </button>
            )}
          </>
        ) : connected ? (
          // Connected but not saved: pick tools above, then save.
          <>
            {oauthish && (
              <button
                className="btn"
                disabled={busy || testing}
                onClick={() => {
                  setAuth("oauth");
                  setOauthOpen(true);
                }}
              >
                {t("mcpmodal.reauth")}
              </button>
            )}
            <button className="btn" onClick={() => void disconnect()} disabled={busy || testing}>
              {t("mcpmodal.disconnect")}
            </button>
            <button className="btn" onClick={onClose} disabled={busy}>
              {t("btn.cancel")}
            </button>
            <button className="btn primary" onClick={() => void submit()} disabled={busy}>
              {busy ? t("mcpmodal.adding") : t("mcpmodal.saveServer")}
            </button>
          </>
        ) : (
          // Nothing connected yet: the only forward action is Connect.
          <>
            <button className="btn" onClick={onClose} disabled={busy || testing}>
              {t("btn.cancel")}
            </button>
            <button className="btn primary" onClick={() => void testConnection()} disabled={busy || testing}>
              {testing ? t("mcpmodal.connecting") : oauthish ? t("mcpmodal.connectAndAuth") : t("mcpmodal.connect")}
            </button>
          </>
        )
      }
    >
      <div className="row" style={{ gap: 8, marginBottom: 14 }}>
        <button className={`btn ${mode === "guided" ? "primary" : ""}`} onClick={() => setMode("guided")}>
          {t("mcpmodal.guidedTab")}
        </button>
        <button
          className={`btn ${mode === "json" ? "primary" : ""}`}
          onClick={() => {
            if (mode === "guided") {
              try {
                const gp = guidedPayload();
                // Fresh/empty guided form → show a real mcpServers template so
                // the JSON editor isn't a bare {"url":""} stub.
                setJsonText(!gp.url && !gp.command ? jsonTemplate(jsonTab) : mcpToJson(gp));
              } catch {
                // Keep user's JSON untouched if guided fields are mid-edit.
              }
            }
            setMode("json");
          }}
        >
          {t("mcpmodal.jsonTab")}
        </button>
        <label className="row muted" style={{ gap: 8, marginLeft: "auto", fontSize: 13 }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> {t("mcpmodal.enabledLabel")}
        </label>
      </div>

      {/* The old standalone "Examples" grid is gone — local starters now render
          in the same catalog below, with logos and a "local" tag. For saved
          servers offer a probe here, since the footer has no Connect action. */}
      {existing && !connected && (
        <div style={{ marginBottom: 12 }}>
          <button className="btn" onClick={() => void testConnection()} disabled={testing || busy}>
            {testing ? t("mcpmodal.connecting") : t("mcpmodal.testConnection")}
          </button>
        </div>
      )}

      {mode === "json" ? (
        <Field label={t("mcpmodal.mcpJsonLabel")} hint={t("mcpmodal.mcpJsonHint")}>
          <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 6, alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 11, marginRight: 2 }}>
              {t("mcpmodal.jsonExamples")}
            </span>
            {JSON_TEMPLATES.map((tpl) => (
              <button
                key={tpl.key}
                type="button"
                className={`btn ${jsonTab === tpl.key ? "primary" : ""}`}
                onClick={() => {
                  setJsonTab(tpl.key);
                  setJsonText(tpl.json);
                }}
                style={{ fontSize: 11, padding: "3px 9px" }}
                title={tpl.hint}
              >
                {tpl.key}
              </button>
            ))}
          </div>
          <div className="muted" style={{ fontSize: 11, marginBottom: 8, lineHeight: 1.4 }}>
            {JSON_TEMPLATES.find((tpl) => tpl.key === jsonTab)?.hint}
          </div>
          <textarea
            className="input"
            value={jsonText}
            onChange={(e) => {
              setJsonText(e.target.value);
              invalidateConnection();
            }}
            style={{ minHeight: 280, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 12, lineHeight: 1.55 }}
          />
        </Field>
      ) : (
        <>
          <div style={{ marginBottom: 14 }}>
            <div className="row" style={{ justifyContent: "space-between", gap: 8, marginBottom: 8, alignItems: "center" }}>
              <span className="muted" style={{ fontSize: 12 }}>{t("mcpmodal.connectService")}</span>
              <input
                className="input"
                value={dirQuery}
                onChange={(e) => setDirQuery(e.target.value)}
                placeholder={t("mcpmodal.searchDirectory")}
                // Focus the service search on open so the user can immediately
                // type a platform/connector name (only for the new-server flow —
                // editing an existing server lands on its config, not the picker).
                autoFocus={!existing}
                style={{ maxWidth: 200, fontSize: 12, padding: "4px 8px" }}
              />
            </div>
            {/* Paginated so the grid stays ~4 rows tall and the credential
                fields for the selected service remain visible below it. */}
            <div ref={measureCatalog} style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {pagedCatalog.map((item) =>
                item.kind === "preset" ? (
                  <button
                    key={`preset:${item.preset.id}`}
                    type="button"
                    className={`btn ${presetId === item.preset.id ? "primary" : ""}`}
                    onClick={() => applyPreset(item.preset)}
                    style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
                    title={(item.preset.description && t(item.preset.description)) || (item.preset.notes && t(item.preset.notes)) || item.preset.title}
                  >
                    <ServerLogo src={item.preset.logoUrl} />
                    {item.preset.title}
                    {/* Preregistered servers need a Client ID/secret you create —
                        flag it up front so the requirement isn't a surprise. */}
                    {item.preset.oauthClient === "preregistered" && (
                      <span title={t("mcpmodal.requiresCreds")} style={{ display: "inline-flex" }}>
                        <KeyRound size={11} strokeWidth={2} style={{ opacity: 0.7 }} />
                      </span>
                    )}
                  </button>
                ) : (
                  <button
                    key={`example:${item.example.id}`}
                    type="button"
                    className={`btn ${presetId === `example:${item.example.id}` ? "primary" : ""}`}
                    onClick={() => applyExample(item.example)}
                    style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}
                    title={`${t(item.example.description)}${item.example.runCommand ? ` — start first: ${item.example.runCommand}` : ""} (${item.example.badge})`}
                  >
                    <ServerLogo src={item.example.logoUrl} />
                    {item.example.title}
                    <span className="muted" style={{ fontSize: 10 }}>
                      · {t("mcpmodal.localExample")}
                    </span>
                  </button>
                ),
              )}
            </div>
            <div
              className="row"
              style={{ justifyContent: "space-between", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}
            >
              {/* Custom is pinned here so it's reachable from any catalog page. */}
              <button
                type="button"
                className={`btn ${presetId === "custom" ? "primary" : ""}`}
                onClick={() => setPresetId("custom")}
                style={{ fontSize: 12 }}
              >
                {t("mcpmodal.customServer")}
              </button>
              {catalogPageCount > 1 && (
                <div className="row" style={{ gap: 6, alignItems: "center", flexShrink: 0 }}>
                  <button
                    type="button"
                    className="btn"
                    aria-label={t("mcpmodal.prevPage")}
                    title={t("mcpmodal.prevPage")}
                    disabled={catalogPage === 0}
                    onClick={() => setDirPage((p) => Math.max(0, p - 1))}
                    style={{ padding: "4px 8px" }}
                  >
                    <ChevronLeft size={15} strokeWidth={2} />
                  </button>
                  <span className="muted" style={{ fontSize: 11, minWidth: 34, textAlign: "center" }}>
                    {catalogPage + 1}/{catalogPageCount}
                  </span>
                  <button
                    type="button"
                    className="btn"
                    aria-label={t("mcpmodal.nextPage")}
                    title={t("mcpmodal.nextPage")}
                    disabled={catalogPage >= catalogPageCount - 1}
                    onClick={() => setDirPage((p) => Math.min(catalogPageCount - 1, p + 1))}
                    style={{ padding: "4px 8px" }}
                  >
                    <ChevronRight size={15} strokeWidth={2} />
                  </button>
                </div>
              )}
            </div>
            {dirNeedle && catalogItems.length === 0 && (
              <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
                {t("mcpmodal.noDirectoryMatches")}
              </div>
            )}
          </div>

          {selectedPreset?.oauthClient === "preregistered" && (
            <div className="card" style={{ padding: 12, marginBottom: 14 }}>
              <div style={{ fontSize: 13, marginBottom: 8 }}>{t("mcpmodal.needsApp")}</div>
              {selectedPreset.appConsoleUrl && (
                <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
                  {t("mcpmodal.createAppAt")}{" "}
                  <a href={selectedPreset.appConsoleUrl} target="_blank" rel="noreferrer">
                    {selectedPreset.appConsoleLabel || selectedPreset.appConsoleUrl}
                  </a>
                </div>
              )}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Field
                  label={t("mcpmodal.clientId")}
                  hint={t("mcpmodal.clientIdHint")}
                  action={
                    !oauthClientId.trim() ? (
                      <span style={{ fontSize: 11, color: "var(--danger)", fontWeight: 500 }}>
                        {t("mcpmodal.required")}
                      </span>
                    ) : undefined
                  }
                >
                  <input
                    className="input"
                    value={oauthClientId}
                    onChange={(e) => setOauthClientId(e.target.value)}
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: 13,
                      ...(!oauthClientId.trim() ? { borderColor: "var(--danger)" } : {}),
                    }}
                  />
                </Field>
                <Field label={t("mcpmodal.clientSecret")} hint={t("mcpmodal.clientSecretHint")}>
                  <input
                    className="input"
                    type="password"
                    value={oauthClientSecret}
                    onChange={(e) => setOauthClientSecret(e.target.value)}
                    style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}
                  />
                </Field>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                {t("mcpmodal.redirectHint")} <code>{redirectUri}</code>{" "}
                <button
                  type="button"
                  className="btn"
                  style={{ fontSize: 11, padding: "2px 8px" }}
                  onClick={() => {
                    void navigator.clipboard?.writeText(redirectUri);
                    setCopied(true);
                    if (copyTimer.current) clearTimeout(copyTimer.current);
                    copyTimer.current = setTimeout(() => setCopied(false), 1500);
                  }}
                >
                  {copied ? t("mcpmodal.copied") : t("mcpmodal.copy")}
                </button>
              </div>
            </div>
          )}
          {selectedPreset?.oauthClient === "dcr" && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 14 }}>{t("mcpmodal.noCredsNeeded")}</div>
          )}

          {isPresetSelected && (
            <button
              type="button"
              className="btn"
              style={{ fontSize: 12, marginBottom: 10 }}
              onClick={() => setShowAdvanced((v) => !v)}
            >
              {advancedOpen ? "▾ " : "▸ "}
              {t("mcpmodal.advanced")}
            </button>
          )}

          {advancedOpen && (
            <>
          <div style={{ display: "grid", gridTemplateColumns: "1.4fr 0.6fr", gap: 12 }}>
            <Field label={transport === "stdio" ? t("mcpmodal.serverNameLabel") : t("mcpmodal.url")}>
              <input
                className="input"
                placeholder={transport === "stdio" ? "time" : "https://example.com/mcp"}
                value={url}
                onChange={(e) => {
                  setUrl(e.target.value);
                  invalidateConnection();
                }}
                style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}
              />
            </Field>
            <Field label={t("mcpmodal.transport")} hint={t("mcpmodal.transportHint")}>
              <select
                className="input"
                value={transport}
                onChange={(e) => {
                  setTransport(e.target.value);
                  invalidateConnection();
                }}
              >
                <option value="auto">auto-detect</option>
                <option value="sse">sse</option>
                <option value="http">http</option>
                <option value="stdio">stdio</option>
              </select>
            </Field>
          </div>

          {transport === "stdio" ? (
            <>
              <Field label={t("mcpmodal.commandLabel")} hint={t("mcpmodal.commandHint")}>
                <input className="input" placeholder="uvx" value={command}
                  onChange={(e) => {
                    setCommand(e.target.value);
                    invalidateConnection();
                  }} />
              </Field>
              <Field label={t("mcpmodal.argumentsLabel")} hint={t("mcpmodal.argumentsHint")}>
                <textarea
                  className="input"
                  value={argsText}
                  onChange={(e) => {
                    setArgsText(e.target.value);
                    invalidateConnection();
                  }}
                  placeholder={"-y\n@modelcontextprotocol/server-everything"}
                  style={{ minHeight: 86, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 12 }}
                />
              </Field>
            </>
          ) : (
            <>
              <div className="muted" style={{ fontSize: 12, lineHeight: 1.5, margin: "-2px 0 12px" }}>
                {t("mcpmodal.httpSsePrefix")}<code>/sse</code>{t("mcpmodal.httpSseMid")}<code>/mcp</code>{t("mcpmodal.httpSseSuffix")}
              </div>

              <Field label={t("mcpmodal.auth")} hint={t("mcpmodal.authHint")}>
                <select className="input" value={auth} onChange={(e) => setAuth(e.target.value)}>
                  <option value="none">{t("mcpmodal.authAuto")}</option>
                  <option value="oauth">{t("mcpmodal.authOauth")}</option>
                </select>
              </Field>

              {auth === "oauth" && (presetId === "custom" || presetId === "") && (
                <>
                  <Field label={t("mcpmodal.scope")} hint={t("mcpmodal.scopeHint")}>
                    <input
                      className="input"
                      placeholder="read write"
                      value={oauthScope}
                      onChange={(e) => setOauthScope(e.target.value)}
                      style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}
                    />
                  </Field>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                    <Field label={t("mcpmodal.clientId")} hint={t("mcpmodal.clientIdHint")}>
                      <input
                        className="input"
                        placeholder="abc123-..."
                        value={oauthClientId}
                        onChange={(e) => setOauthClientId(e.target.value)}
                        style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}
                      />
                    </Field>
                    <Field label={t("mcpmodal.clientSecret")} hint={t("mcpmodal.clientSecretHint")}>
                      <input
                        className="input"
                        type="password"
                        placeholder="••••••••"
                        value={oauthClientSecret}
                        onChange={(e) => setOauthClientSecret(e.target.value)}
                        style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}
                      />
                    </Field>
                  </div>
                  <div className="muted" style={{ fontSize: 12, lineHeight: 1.5, margin: "-4px 0 12px" }}>
                    {t("mcpmodal.redirectHint")}{" "}
                    <code>{redirectUri}</code>
                  </div>
                </>
              )}

              <Field label={t("mcpmodal.headers")} hint={t("mcpmodal.headersHint")}>
                <textarea
                  className="input"
                  value={headersText}
                  onChange={(e) => {
                    setHeadersText(e.target.value);
                    invalidateConnection();
                  }}
                  placeholder={"Authorization: Bearer sk-...\nX-Tenant: acme"}
                  style={{ minHeight: 64, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 12 }}
                />
              </Field>
            </>
          )}

          <Field
            label={t("mcpmodal.environmentLabel")}
            hint={
              hasEmptyEnvValue(envText)
                ? t("mcpmodal.envNeedsValues")
                : t("mcpmodal.environmentHint")
            }
            action={
              hasEmptyEnvValue(envText) ? (
                <span style={{ fontSize: 11, color: "var(--danger)", fontWeight: 500 }}>
                  {t("mcpmodal.required")}
                </span>
              ) : undefined
            }
          >
            <textarea
              className="input"
              value={envText}
              onChange={(e) => {
                setEnvText(e.target.value);
                invalidateConnection();
              }}
              placeholder={"API_KEY=...\nNODE_ENV=production"}
              style={{
                minHeight: 82,
                resize: "vertical",
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                ...(hasEmptyEnvValue(envText) ? { borderColor: "var(--danger)" } : {}),
              }}
            />
          </Field>
            </>
          )}
        </>
      )}

      {testResult &&
        (() => {
          const needsAuth = testResult.status === "auth_required";
          const accent = testResult.ok
            ? "rgba(72, 211, 137, %)"
            : needsAuth
              ? "rgba(245, 191, 79, %)"
              : "rgba(244, 99, 99, %)";
          const title = testResult.ok
            ? t("mcpmodal.connectionReady")
            : needsAuth
              ? t("mcpmodal.authRequired")
              : t("mcpmodal.connectionFailed");
          const logs = testResult.logs || [];
          return (
            <div
              className="card"
              style={{
                padding: 12,
                marginTop: 14,
                borderColor: accent.replace("%", ".35"),
                background: accent.replace("%", ".06"),
              }}
            >
              <div className="row" style={{ justifyContent: "space-between", gap: 12 }}>
                <strong>{title}</strong>
                <span className="muted" style={{ fontSize: 12 }}>
                  {(testResult.tools || []).length} tools
                </span>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {testResult.message}
              </div>

              {needsAuth && transport !== "stdio" && (
                <button
                  className="btn primary"
                  style={{ marginTop: 10 }}
                  onClick={() => {
                    setAuth("oauth");
                    setOauthOpen(true);
                  }}
                >
                  {t("mcpmodal.authorize")}
                </button>
              )}
              {testResult.ok && testResult.needs_oauth && authorized && (
                <div className="row" style={{ gap: 6, marginTop: 8, fontSize: 12, color: "var(--success)" }}>
                  ✓ {t("mcpmodal.authorizedHint")}
                </div>
              )}

              {/* Pick tools right after Connect, before saving — the selection
                  is persisted as disabled_tools with the server. */}
              {!!testResult.tools?.length && (
                <div style={{ marginTop: 10 }}>
                  <div className="row" style={{ justifyContent: "space-between", gap: 8, alignItems: "center" }}>
                    <span className="muted" style={{ fontSize: 12 }}>{t("mcpmodal.chooseTools")}</span>
                    <div className="row" style={{ gap: 6, flexShrink: 0 }}>
                      <button
                        type="button"
                        className="btn"
                        style={{ fontSize: 11, padding: "2px 8px" }}
                        onClick={() => {
                          setDisabledTools([]);
                          setToolsTouched(true);
                        }}
                      >
                        {t("mcpmodal.enableAll")}
                      </button>
                      <button
                        type="button"
                        className="btn"
                        style={{ fontSize: 11, padding: "2px 8px" }}
                        onClick={() => {
                          setDisabledTools((testResult.tools || []).map((tool) => tool.name).sort());
                          setToolsTouched(true);
                        }}
                      >
                        {t("mcpmodal.disableAll")}
                      </button>
                    </div>
                  </div>
                  <div style={{ maxHeight: 240, overflowY: "auto", marginTop: 6 }}>
                    {testResult.tools.map((tool) => {
                      const on = !disabledTools.includes(tool.name);
                      return (
                        <div
                          key={tool.name}
                          className="row"
                          style={{
                            justifyContent: "space-between",
                            gap: 12,
                            padding: "6px 2px",
                            borderBottom: "1px solid var(--border)",
                          }}
                        >
                          <div style={{ minWidth: 0 }}>
                            <code style={{ fontSize: 12 }}>{tool.name}</code>
                            {tool.description && (
                              <div
                                className="muted"
                                style={{ fontSize: 11, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                              >
                                {tool.description}
                              </div>
                            )}
                          </div>
                          <Toggle
                            checked={on}
                            onChange={(next) => {
                              setToolsTouched(true);
                              setDisabledTools((prev) =>
                                next ? prev.filter((n) => n !== tool.name) : [...prev, tool.name].sort(),
                              );
                            }}
                          />
                        </div>
                      );
                    })}
                  </div>
                  <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
                    {t("mcpmodal.toolsEnabled", {
                      on: String(testResult.tools.filter((tool) => !disabledTools.includes(tool.name)).length),
                      total: String(testResult.tools.length),
                    })}
                  </div>
                </div>
              )}

              {!!logs.length && (
                <div style={{ marginTop: 10 }}>
                  <button
                    className="btn"
                    style={{ fontSize: 12, padding: "3px 9px" }}
                    onClick={() => setShowLogs((v) => !v)}
                  >
                    {showLogs ? t("mcpmodal.hideLogs") : t("mcpmodal.showLogs")}
                  </button>
                  {showLogs && (
                    <pre
                      style={{
                        marginTop: 8,
                        padding: 10,
                        borderRadius: "var(--r-sm)",
                        background: "var(--code-bg)",
                        border: "1px solid var(--code-border)",
                        fontFamily: "var(--font-mono)",
                        fontSize: 11,
                        color: "var(--text-3)",
                        maxHeight: 200,
                        overflow: "auto",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {logs
                        .map((l) => `${(l.level || "info").toUpperCase().padEnd(5)} ${l.message}`)
                        .join("\n")}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })()}

      {oauthOpen && (
        <McpOAuthDialog
          agentName={agentName}
          server={(() => {
            try {
              return payload();
            } catch {
              return { url: url.trim(), transport, auth: "oauth" };
            }
          })()}
          onClose={() => setOauthOpen(false)}
          onAuthorized={() => {
            setAuthorized(true);
            setAuth("oauth");
            void testConnection();
          }}
        />
      )}
    </Modal>
  );
}

export function ChannelModal({
  agentName,
  existing,
  initialConfig,
  onClose,
  onDone,
}: {
  agentName: string;
  existing: string | null;
  initialConfig: Record<string, unknown>;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [catalog, setCatalog] = useState<CatalogChannel[] | null>(null);
  // The dropdown selects a picker ID, not a wire type: a variant row (Lark)
  // shares its type with its base row (Feishu), so an existing connector is
  // matched to its row by config, `platform:feishu` + `extra.domain: "lark"`
  // opens as Lark. The saved payload always carries `definition.type`.
  const [selected, setSelected] = useState(() =>
    existing ? (connectorDefinitionForConfig(existing, initialConfig)?.id ?? existing) : "platform:telegram",
  );
  const [values, setValues] = useState<ConnectorFormValues>(() => {
    const def = (existing ? connectorDefinitionForConfig(existing, initialConfig) : getConnectorDefinition("platform:telegram")) || CONNECTOR_DEFINITIONS[0];
    return makeInitialConnectorValues(def, initialConfig);
  });
  const [error, setError] = useState<string | null>(null);
  const [pairing, setPairing] = useState(false);
  const [weixinPairing, setWeixinPairing] = useState(false);
  const [signalSetup, setSignalSetup] = useState(false);
  const [busy, setBusy] = useState(false);
  // Set once a NEW connector has been persisted mid-session (e.g. the Weixin
  // pair flow auto-saves on link). Later saves must PUT that type, not POST a
  // second copy — otherwise the footer's "Add" would 409 on a duplicate.
  const [createdType, setCreatedType] = useState<string | null>(null);
  // The Weixin pair flow saved the connector. Closing that dialog then ends
  // the whole edit — same as a manual Save — and the restart prompt happens
  // outside, as a toast against the agent page's own Restart button.
  const [weixinAutoSaved, setWeixinAutoSaved] = useState(false);
  // Bumped after Save stores a typed credential, so the vault fields re-read
  // what is set instead of still saying "Not set" while the modal stays open.
  const [vaultVersion, setVaultVersion] = useState(0);
  // The connector's tool switches as the agent has them now (null until the
  // agent is read, or when it cannot be), and as they were when loaded, so
  // Save writes `tools`/`disabled_tools` only when one was flipped.
  const [toolState, setToolState] = useState<Record<string, boolean> | null>(null);
  const initialToolState = useRef<Record<string, boolean> | null>(null);

  const options = supportedCatalogChannels(catalog, existing || undefined);
  const definition = getConnectorDefinitionById(selected);
  const type = definition?.type ?? selected;

  useEffect(() => {
    api
      .get<CatalogChannelsResponse>("/api/catalog/channels")
      .then((res) => setCatalog(res.channels || []))
      .catch(() => setCatalog(null));
  }, []);

  const toolDefs = definition?.tools || [];
  useEffect(() => {
    if (!toolDefs.length) return;
    let alive = true;
    setToolState(null);
    initialToolState.current = null;
    api
      .get<{ tools?: string[]; disabled_tools?: string[] }>(`/api/agents/${encodeURIComponent(agentName)}`)
      .then((cfg) => {
        if (!alive) return;
        const state = connectorToolSwitches(toolDefs, cfg);
        initialToolState.current = state;
        setToolState(state);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
    // The definition's identity is the type: a new type is a new tool list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, definition?.id]);

  function selectType(next: string) {
    const def = getConnectorDefinitionById(next);
    setSelected(next);
    setError(null);
    setValues(def ? makeInitialConnectorValues(def, existing === def.type ? initialConfig : {}) : {});
  }

  // Persist the connector from a given set of form values. Returns true on
  // success. `silent` suppresses the toast + modal-close so an internal caller
  // (the Weixin auto-save on link) can save without tearing the dialog down.
  async function persist(nextValues: ConnectorFormValues, silent = false): Promise<boolean> {
    if (!definition) {
      setError(t("chmodal.unsupported"));
      return false;
    }
    // Credential fields never ride in the channel payload: what the user typed
    // into one is stored in this agent's vault override first, below.
    const payload = makeConnectorPayload(definition, nextValues, t, { omitVaultSecrets: true });
    if (payload.error || !payload.config) {
      setError(payload.error || t("chmodal.unsupported"));
      return false;
    }
    const secrets = pendingVaultSecrets(definition, nextValues);
    setBusy(true);
    try {
      // Credentials before the connector. The other order is the failure this
      // exists for: a connector that reads as saved and then does not start,
      // because the token typed into it went nowhere. A token stored for a
      // connector whose own save then fails is harmless, and the next Save
      // reuses it.
      for (const secret of secrets) {
        try {
          await api.post("/api/vault/secrets", { name: secret.vaultKey, value: secret.value, agent: agentName });
        } catch (e) {
          const reason = errText(e, "");
          toast.push(`${t("toast.connVaultFailed", { key: secret.vaultKey })}${reason ? ` ${reason}` : ""}`, "error");
          return false;
        }
      }
      if (secrets.length) {
        // Stored: no longer drafts, so a retry after a failed save below does
        // not write them again, and the fields re-read their state.
        const stored = { ...nextValues };
        for (const secret of secrets) stored[secret.field.key] = "";
        setValues(stored);
        setVaultVersion((n) => n + 1);
      }
      const vaulted = secrets.map((secret) => secret.vaultKey).join(", ");
      // `existing` = editing a saved connector; `createdType` = we already
      // created it earlier this session (the pair auto-save). Either means PUT.
      const editType = existing || createdType;
      if (editType) {
        await api.put(`/api/agents/${encodeURIComponent(agentName)}/channels/${encodeURIComponent(editType)}`, { type, config: payload.config });
        if (!silent) toast.push(vaulted ? t("toast.connUpdatedVaulted", { keys: vaulted }) : t("toast.connUpdated"));
      } else {
        await api.post(`/api/agents/${encodeURIComponent(agentName)}/channels`, { type, config: payload.config });
        setCreatedType(type);
        if (!silent) toast.push(vaulted ? t("toast.connAddedVaulted", { keys: vaulted }) : t("toast.connAdded"));
      }
      // The tool switches, after the connector: they live in the agent's
      // `tools`/`disabled_tools`, applied to a copy fetched right now so a
      // Tools-tab edit made meanwhile survives. A failure here keeps the
      // modal open with the switches as set, so Save again retries them.
      const initial = initialToolState.current;
      if (toolState && initial && toolDefs.some((d) => toolState[d.name] !== initial[d.name])) {
        try {
          const fresh = await api.get<{ tools?: string[]; disabled_tools?: string[] }>(
            `/api/agents/${encodeURIComponent(agentName)}`,
          );
          await api.put(
            `/api/agents/${encodeURIComponent(agentName)}`,
            applyConnectorToolSwitches(toolDefs, fresh, toolState),
          );
          initialToolState.current = { ...toolState };
          if (!silent) toast.push(t("toast.connToolsSaved"));
        } catch (e) {
          toast.push(errText(e, t("toast.connToolsFailed")), "error");
          return false;
        }
      }
      return true;
    } catch (e) {
      toast.push(errText(e, t("toast.connSaveFailed")), "error");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (await persist(values)) {
      onDone();
      onClose();
    }
  }

  // The Weixin QR flow linked an account. Pin it into this connector's bot_id
  // and save immediately, so the login is bound to THIS agent even when the
  // connector was still unsaved (a new connector being created) — the
  // server-side pin can only touch an already-saved entry. Returns whether the
  // save landed, so the pair modal can show Done only once it's persisted.
  async function onWeixinLinked(accountId: string): Promise<boolean> {
    const nextValues = { ...values, bot_id: accountId };
    setValues(nextValues);
    const ok = await persist(nextValues, true);
    if (ok) setWeixinAutoSaved(true);
    return ok;
  }

  // The connector's human name, not its wire type: "Edit platform:api_server"
  // printed an internal identifier directly above a dropdown reading
  // "OpenAI-compatible API". Falls back to the raw type only for a type this
  // build has no definition for.
  const editName = definition ? t(definition.label) : existing || "";

  return (
    <Modal
      title={existing ? t("chmodal.editTitle", { name: editName }) : t("chmodal.addTitle")}
      subtitle={t("chmodal.subtitle")}
      onClose={onClose}
      width={980}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("btn.cancel")}
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={busy}>
            {busy ? t("btn.saving") : existing ? t("btn.save") : t("chmodal.add")}
          </button>
        </>
      }
    >
      <Field label={t("chmodal.type")}>
        <select className="input" value={selected} onChange={(e) => selectType(e.target.value)} disabled={!!existing}>
          {selectOptions(options, existing, definition).map((item) => (
            <option key={item.id} value={item.id}>
              {t(item.label)}
            </option>
          ))}
        </select>
      </Field>
      {definition ? (
        <ConnectorForm
          definition={definition}
          agentName={agentName}
          vaultReloadSignal={vaultVersion}
          tools={
            toolDefs.length
              ? {
                  state: toolState,
                  onChange: (name, on) => setToolState((s) => ({ ...(s || {}), [name]: on })),
                }
              : undefined
          }
          values={values}
          onChange={(next) => {
            setError(null);
            setValues(next);
          }}
          error={error}
          onPairWhatsApp={() => setPairing(true)}
          onPairWeixin={() => setWeixinPairing(true)}
          onSetupSignal={() => setSignalSetup(true)}
        />
      ) : (
        <div className="connector-error">{t("chmodal.unsupported")}</div>
      )}
      {pairing && (
        <WhatsAppPairModal
          agentName={agentName}
          port={whatsAppPort(values)}
          onClose={() => setPairing(false)}
          onRestarted={onDone}
        />
      )}
      {weixinPairing && (
        <WeixinPairModal
          agentName={agentName}
          onLinked={onWeixinLinked}
          onClose={() => {
            setWeixinPairing(false);
            // The link already saved the connector — finish exactly like a
            // manual Save: close the form and prompt for the restart from
            // the agent page (its own Restart button applies it).
            if (weixinAutoSaved) {
              toast.push(t("toast.connSavedRestart"));
              onDone();
              onClose();
            }
          }}
        />
      )}
      {signalSetup && (
        <SignalSetupModal
          agentName={agentName}
          initialAccount={typeof values.account === "string" ? values.account : ""}
          httpUrl={typeof values.http_url === "string" && values.http_url ? values.http_url : "http://127.0.0.1:8080"}
          onClose={() => setSignalSetup(false)}
          onRestarted={onDone}
        />
      )}
    </Modal>
  );
}

function selectOptions(options: ConnectorDefinition[], existing: string | null, definition: ConnectorDefinition | undefined): ConnectorDefinition[] {
  if (existing && definition && !options.some((item) => item.id === definition.id)) return [definition, ...options];
  if (!existing || definition) return options;
  const existingDef = getConnectorDefinition(existing);
  return existingDef ? [existingDef, ...options.filter((item) => item.type !== existing)] : options;
}

function whatsAppPort(values: ConnectorFormValues): number | undefined {
  const raw = values.bridge_port;
  if (typeof raw !== "string" || !raw.trim()) return undefined;
  const n = Number(raw.trim());
  return Number.isFinite(n) ? n : undefined;
}
