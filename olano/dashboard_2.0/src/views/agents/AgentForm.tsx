// Create + Edit agent modals. Both drive the same field set; create POSTs to
// /api/agents (name + backend shorthand), edit PUTs a partial patch to
// /api/agents/{name}. Mirrors openAgentCreate / openAgentEdit + their submit
// handlers from the original dashboard.

import { useEffect, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Modal, Field, ToolPicker, ColorPicker, GeneratePromptButton } from "./Modal";
import { useCatalog, modelOptions, modelValueLabel } from "./catalog";
import { ModelOptionGroups } from "./ModelOptions";
import { useArchetypes } from "./archetypes";
import { CatalogPicker, Segmented } from "./CatalogPicker";
import type { EnvShorthand } from "./types";
import { OptionNotes, noteItems } from "./Toggle";
import { STR } from "./strings";
import { ENV_NOTES, slugifyAgentName } from "./helpers";
import { HandlePreview } from "./HandlePreview";

type CreateSource = "custom" | "team" | "expert" | "integration";

// GET /api/archetypes/{name} — the install preview (per-member model table).
interface ArchetypeMember {
  ref: string;
  name: string;
  description?: string;
  model?: string | null;
  source?: string;
}
interface ArchetypeDetail {
  name: string;
  model?: string | null;
  subagents: ArchetypeMember[];
}

const ENV_OPTIONS: { value: EnvShorthand; key: string }[] = [
  { value: "filesystem+shell", key: "env.execute" },
  { value: "filesystem", key: "env.write" },
  { value: "read", key: "env.read" },
  // Virtual (no real host filesystem) backends — a working virtual filesystem with
  // no shell execution that never touches the host disk. "state" is thread-local and
  // ephemeral; "store" persists across threads via the shared KV store.
  { value: "state", key: "env.sandbox" },
  { value: "store", key: "env.virtual" },
];

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

// ── Create ───────────────────────────────────────────────────────────────
export function AgentCreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const toast = useToast();
  const t = useT(STR);
  const cat = useCatalog();
  const arch = useArchetypes();
  const models = modelOptions(cat.providers);
  const [source, setSource] = useState<CreateSource>("custom");
  const [pick, setPick] = useState<string | null>(null);
  // The person names the agent; the server mints its permanent handle from
  // that name (slug, `-2`… on a collision). There is no handle field: the
  // preview under the name shows what the key will be, and the create
  // response carries the real one.
  const [displayName, setDisplayName] = useState("");
  // The position ("Recruitment Specialist") — prefilled from the catalog
  // label on an install, so "Hillary" installed from the Recruitment
  // Specialist archetype keeps that as her title rather than as her name.
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  // "" = the user hasn't picked a model. Resolved against the catalog below;
  // left unset in the POST while the catalog is still loading so the backend
  // applies the same deployment-aware default.
  const [model, setModel] = useState("");
  const [backend, setBackend] = useState<EnvShorthand>("filesystem+shell");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [tools, setTools] = useState<string[]>([]);
  const [color, setColor] = useState("");
  const [busy, setBusy] = useState(false);
  // Install tabs: optional top-level model override ("" = archetype default)
  // and, for teams, per-member model overrides keyed by member ref.
  const [installModel, setInstallModel] = useState("");
  const [memberModels, setMemberModels] = useState<Record<string, string>>({});
  const [archDetail, setArchDetail] = useState<ArchetypeDetail | null>(null);

  const handlePreview = slugifyAgentName(displayName);
  const hasName = !!displayName.trim();

  // Default model: the deployment fast-tier alias. It resolves per box at
  // build time (Config → Default models, or the built-in target), so the old
  // client-side managed-vs-BYOK fork is gone — the backend applies the same
  // alias when the POST omits `model` (runtime/api.py `_default_agent_model`).
  const defaultModel =
    cat.providers.length === 0
      ? ""
      : models.some((m) => m.value === "default:turbo")
        ? "default:turbo"
        : "anthropic:claude-sonnet-4-6";
  const modelValue = model || defaultModel;

  // Team install: fetch the archetype's member list so each member's model can
  // be overridden before installing. Cleared on pick/tab change so a stale
  // team's members never render under a new selection.
  useEffect(() => {
    setArchDetail(null);
    setMemberModels({});
    if (source !== "team" || !pick) return;
    let alive = true;
    void api
      .get<ArchetypeDetail>(`/api/archetypes/${encodeURIComponent(pick)}`)
      .then((d) => {
        if (alive) setArchDetail(d);
      })
      .catch(() => {
        // Preview only — install still works without the member table.
        if (alive) setArchDetail(null);
      });
    return () => {
      alive = false;
    };
  }, [source, pick]);

  async function submitCustom() {
    if (!hasName) {
      toast.push(t("toast.displayNameRequired"), "error");
      return;
    }
    setBusy(true);
    try {
      // No `name`: the server mints the handle from `display_name` and
      // returns it (AgentCreateRequest, olano/runtime/api.py).
      const res = await api.post<{ name?: string }>("/api/agents", {
        display_name: displayName.trim(),
        title: title.trim(),
        emoji: "",
        description: description.trim(),
        model: modelValue || undefined,
        system_prompt: systemPrompt,
        tools,
        backend,
        color: color || null,
      });
      toast.push(t("toast.createdAs", { name: res?.name || handlePreview }));
      onCreated();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.createFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  // Install a team/expert archetype or an integration as a new top-level agent.
  // The person's name for it is required and the handle is minted from it
  // server-side; the title defaults to the catalog label; model and per-member
  // models are optional overrides — left empty, the curated model and prompt
  // come from the archetype itself.
  async function install(endpoint: string, key: string) {
    if (!pick) return;
    if (!hasName) {
      toast.push(t("toast.displayNameRequired"), "error");
      return;
    }
    // Only the members the user actually changed travel — an empty map keeps
    // the archetype's own member models.
    const changedMembers = Object.fromEntries(
      Object.entries(memberModels).filter(([, v]) => (v || "").trim()),
    );
    setBusy(true);
    try {
      const res = await api.post<{ name?: string }>(endpoint, {
        [key]: pick,
        display_name: displayName.trim(),
        title: title.trim() || undefined,
        model: installModel || undefined,
        ...(endpoint === "/api/archetypes/install" && Object.keys(changedMembers).length
          ? { subagent_models: changedMembers }
          : {}),
      });
      toast.push(t("toast.createdAs", { name: res?.name || handlePreview }));
      onCreated();
      onClose();
    } catch (e) {
      toast.push(errText(e, t("toast.createFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  function onPrimary() {
    if (source === "custom") return void submitCustom();
    if (source === "integration") return void install("/api/integrations/install", "toolkit_agent");
    return void install("/api/archetypes/install", "archetype");
  }

  const teamItems = arch.data.teams.map((a) => ({ name: a.name, label: a.label, description: a.description, color: a.color, badge: a.subagent_count ? `${a.subagent_count}` : undefined }));
  const specialistItems = arch.data.specialists.map((a) => ({ name: a.name, label: a.label, description: a.description, color: a.color }));
  const integrationItems = arch.data.integrations.map((i) => ({ name: i.name, label: i.label, description: i.description, badge: i.toolkit }));
  const canSubmit = source === "custom" ? hasName : !!pick && hasName;
  const primaryLabel = busy ? t("form.creating") : source === "custom" ? t("form.createAgent") : t("form.installAgent");

  return (
    <Modal
      title={t("form.newTitle")}
      subtitle={t("form.newSub")}
      onClose={onClose}
      width={620}
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
      <Segmented
        value={source}
        onChange={(v) => {
          setSource(v);
          setPick(null);
        }}
        options={[
          { value: "custom", label: t("form.src.custom") },
          { value: "team", label: t("form.src.team") },
          { value: "expert", label: t("form.src.expert") },
          { value: "integration", label: t("form.src.integration") },
        ]}
      />

      {source !== "custom" ? (
        <>
          <CatalogPicker
            items={source === "team" ? teamItems : source === "expert" ? specialistItems : integrationItems}
            selected={pick}
            onSelect={(picked) => {
              setPick(picked);
              // The picker's human label is the JOB the archetype was written
              // for, so it becomes the title — never the display name. The
              // name is the person's to give: prefilling it with the label is
              // how an agent someone meant to call "Hillary" ended up
              // introducing herself as "Recruitment Specialist".
              const items =
                source === "team"
                  ? teamItems
                  : source === "expert"
                    ? specialistItems
                    : integrationItems;
              const label = (items.find((i) => i.name === picked)?.label || "").trim();
              setTitle(label);
            }}
            searchPlaceholder={t("form.searchCatalog")}
            emptyText={t("form.noCatalog")}
            status={arch.status}
            errorText={arch.error ? t("form.catalogError", { detail: arch.error }) : t("form.catalogErrorGeneric")}
            retryText={t("btn.retry")}
            onRetry={arch.refresh}
          />
          <div style={{ marginTop: 12 }}>
            <Field label={t("form.displayName")} hint={t("form.installDisplayNameHint")}>
              <input
                className="input"
                placeholder={t("form.displayNamePlaceholder")}
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </Field>
            <HandlePreview
              handle={handlePreview}
              label={t("form.handle")}
              pending={pick ? t("form.handlePendingArchetype", { slug: pick }) : t("form.handlePending")}
              hint={t("form.handleHint")}
            />
            <Field label={t("form.title")} hint={t("form.installTitleHint")}>
              <input
                className="input"
                placeholder={t("form.titlePlaceholder")}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </Field>
          </div>
          <Field label={t("form.installModel")} hint={t("form.installModelHint")}>
            <select
              className="input"
              value={installModel}
              onChange={(e) => setInstallModel(e.target.value)}
            >
              <option value="">{t("form.installModelDefaultOption")}</option>
              <ModelOptionGroups options={models} />
            </select>
          </Field>
          {source === "team" && pick && !archDetail && (
            <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
              {t("form.membersLoading")}
            </div>
          )}
          {source === "team" && archDetail && archDetail.subagents.length > 0 && (
            <Field label={t("form.memberModels")} hint={t("form.memberModelsHint")}>
              <div style={{ display: "grid", gap: 8 }}>
                {archDetail.subagents.map((m) => (
                  <div
                    key={m.ref}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                      gap: 8,
                      alignItems: "center",
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {m.name}
                      </div>
                      {m.model && (
                        <div className="muted" style={{ fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {modelValueLabel(m.model)}
                        </div>
                      )}
                    </div>
                    <select
                      className="input"
                      value={memberModels[m.ref] || ""}
                      onChange={(e) =>
                        setMemberModels((prev) => ({ ...prev, [m.ref]: e.target.value }))
                      }
                    >
                      <option value="">{t("form.memberModelDefaultOption")}</option>
                      <ModelOptionGroups options={models} />
                    </select>
                  </div>
                ))}
              </div>
            </Field>
          )}
        </>
      ) : (
        <>
          <Field label={t("form.displayName")} hint={t("form.displayNameCreateHint")}>
        <input
          className="input"
          placeholder={t("form.displayNamePlaceholder")}
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          autoFocus
        />
      </Field>
      <HandlePreview
        handle={handlePreview}
        label={t("form.handle")}
        pending={t("form.handlePending")}
        hint={t("form.handleHint")}
      />
      <Field label={t("form.title")} hint={t("form.titleCreateHint")}>
        <input
          className="input"
          placeholder={t("form.titlePlaceholder")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </Field>
      <Field label={t("form.description")} hint={t("form.descHint")}>
        <input
          className="input"
          placeholder={t("form.descPlaceholder")}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label={t("form.model")} hint={t("form.modelHint")}>
          <select className="input" value={modelValue} onChange={(e) => setModel(e.target.value)}>
            {models.length === 0 && (
              <option value={modelValue}>{modelValue || t("form.modelDefaultOption")}</option>
            )}
            <ModelOptionGroups options={models} />
          </select>
        </Field>
        <Field
          label={t("form.environment")}
          hint={
            <>
              {t("ov.accessHint")}
              <OptionNotes items={noteItems(t, ENV_NOTES)} />
            </>
          }
        >
          <select className="input" value={backend} onChange={(e) => setBackend(e.target.value as EnvShorthand)}>
            {ENV_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {t(o.key)}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Field label={t("form.accentColor")} hint={t("form.accentHint")}>
        <ColorPicker value={color} onChange={setColor} />
      </Field>
      <Field
        label={t("form.systemPrompt")}
        action={
          <GeneratePromptButton
            name={displayName.trim() || handlePreview}
            description={description}
            tools={tools}
            model={modelValue}
            kind="agent"
            onGenerated={setSystemPrompt}
          />
        }
      >
        <textarea
          className="input"
          style={{ minHeight: 120, resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 13 }}
          placeholder={t("form.promptPlaceholder")}
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
        />
      </Field>
      <Field label={t("form.tools")} hint={t("form.toolsHint")}>
        <ToolPicker all={cat.toolNames} selected={tools} onChange={setTools} />
      </Field>
        </>
      )}
    </Modal>
  );
}
