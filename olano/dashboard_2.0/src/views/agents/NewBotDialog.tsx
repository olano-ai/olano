// "New bot" — the one-question creation flow, shared by both shells.
//
// The simple shell offers nothing else; the dashboard's sidebar offers it
// beside the full create-agent form (views/agents/AgentForm.tsx), which
// asks for a model, a backend, a toolset and
// a system prompt. None of that is a question a consumer can answer, so this
// asks two: what should it be good at (an archetype from the catalog, or a
// blank bot), and what should it be called. Everything else comes from the
// archetype or from the deployment's own defaults.
//
// The name is a NAME ("Hillary"), not a slug. The bot's permanent handle is
// minted from it by the server (slug, then `-2`, `-3` if taken) and previewed
// under the field — nobody is asked to invent a key. An archetype's own label
// becomes the bot's title (its position), never its name: sending the handle
// alone used to make the installer take the label as the display name, so a
// bot someone called "hillary" introduced herself as "Recruitment Specialist".
//
// One request does both shapes: `POST /api/agents` with an `archetype` installs
// that template under the given name, and without one creates the blank agent
// the endpoint has always created. The server owns the real gate — self-serve
// creation can be off, limited to an allow-list of archetypes, or capped per
// human — so every refusal here is surfaced verbatim rather than second-guessed.
//
// The one technical question it does ask is the model, because it is the one
// choice with a bill attached. The dropdown lists only what this deployment
// can run right now — `usableModelOptions`: the deployment defaults whose
// target is usable, the Olano tiers when the gateway is connected, and any
// provider with a key — the same rule the chat header's picker applies, so a
// model offered here never fails on first use. Default · Turbo is pre-selected
// whenever it is offered; a template's own curated model stays one option away.

import { useEffect, useMemo, useRef, useState } from "react";
import { Search, Sparkles, Users, X } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useDialogA11y } from "../../ui/dialogA11y";
import { useToast } from "../../ui/toast";
import { useArchetypes, type ArchetypeEntry } from "./archetypes";
import { usableModelOptions, useCatalog } from "./catalog";
import { HandlePreview } from "./HandlePreview";
import { slugifyAgentName } from "./helpers";
import { ModelOptionGroups } from "./ModelOptions";
import { NEWBOT_STR as STR } from "./newBotStrings";

// The pre-selected model: the deployment's fast tier, which the server would
// apply anyway when the field is omitted (runtime/api.py `_default_agent_model`).
const PREFERRED_MODEL = "default:turbo";

export function NewBotDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  /** Fires with the created bot's name so the shell can select it. */
  onCreated: (name: string) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const catalog = useArchetypes();
  const models = useCatalog();
  const panelRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  // "" = start from scratch; otherwise the archetype slug to install.
  const [pick, setPick] = useState("");
  const [query, setQuery] = useState("");
  // What the bot is called. Free text; the handle is derived from it.
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // The model. `null` = the person has not touched the dropdown, so the
  // pre-selection below applies; "" = "the template's own model" (an archetype
  // pick only — sent as no `model`, so the installer keeps the curated one).
  const [model, setModel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Only models this deployment can run now. Empty while the catalog loads —
  // and then a create simply omits `model`, so the server applies its default.
  const modelOptions = useMemo(() => usableModelOptions(models.providers), [models.providers]);
  const modelsLoaded = models.providers.length > 0;
  // "Template's own" is only a choice while a template is picked; switching
  // to from-scratch with it selected falls back to the pre-selection rather
  // than leaving the dropdown on a value it no longer offers.
  const chosen = model === "" && !pick ? null : model;
  const modelValue =
    chosen ??
    (modelOptions.some((o) => o.value === PREFERRED_MODEL)
      ? PREFERRED_MODEL
      : (modelOptions[0]?.value ?? ""));

  // The dialog manages its own initial focus (the name field), so the shared
  // hook only handles Escape, the Tab trap and focus restore.
  useDialogA11y(panelRef, busy ? undefined : onClose, { initialFocus: false });
  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const experts = catalog.data.specialists;
  const teams = catalog.data.teams;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = (e: ArchetypeEntry) =>
      !q ||
      e.label.toLowerCase().includes(q) ||
      e.name.toLowerCase().includes(q) ||
      (e.description || "").toLowerCase().includes(q);
    return { experts: experts.filter(match), teams: teams.filter(match) };
  }, [experts, teams, query]);

  // Picking a template never touches the name: the name is the person's to
  // give, and the template's label becomes the bot's title server-side.
  function choose(entry: ArchetypeEntry | null) {
    setPick(entry?.name ?? "");
  }

  const handlePreview = slugifyAgentName(name);
  const canCreate = !!name.trim() && !busy;

  async function create() {
    const clean = name.trim();
    if (!clean) {
      toast.push(t("newbot.nameRequired"), "error");
      return;
    }
    setBusy(true);
    try {
      // No `name` in the body: the server mints the handle from the display
      // name and returns it — `hillary`, or `hillary-2` if that one exists.
      const res = await api.post<{ ok: boolean; name?: string }>("/api/agents", {
        display_name: clean,
        // An archetype create refuses free-form fields (the template owns
        // them), so the description only travels on a from-scratch create.
        ...(pick ? { archetype: pick } : { description: description.trim() }),
        // The model travels on both shapes — the installer takes it as an
        // override of the template's own. Omitted (catalog not loaded, or
        // "template's own" chosen) the server decides, as it always did.
        ...(modelValue ? { model: modelValue } : {}),
      });
      const created = res?.name || handlePreview;
      toast.push(t("newbot.created", { name: clean }));
      onCreated(created);
    } catch (e) {
      // The backend's refusals are the useful text here — "this deployment
      // allows x, y", "you already own 3 agents and the limit is 3" — so they
      // are shown as-is rather than replaced with a generic failure.
      toast.push(e instanceof ApiError ? e.detail : t("newbot.failed"), "error");
    } finally {
      setBusy(false);
    }
  }

  function optionRow(entry: ArchetypeEntry) {
    const selected = pick === entry.name;
    return (
      <button
        key={entry.name}
        type="button"
        className={`simple-pick${selected ? " is-active" : ""}`}
        aria-pressed={selected}
        onClick={() => choose(entry)}
      >
        <span className="simple-pick-face" aria-hidden>
          {entry.emoji || <Sparkles size={15} strokeWidth={1.75} />}
        </span>
        <span className="simple-pick-text">
          <span className="simple-pick-name">{entry.label || entry.name}</span>
          {!!entry.description && <span className="simple-pick-desc">{entry.description}</span>}
        </span>
      </button>
    );
  }

  return (
    <div className="modal-overlay" onClick={busy ? undefined : onClose}>
      <div
        className="card modal-card simple-dialog"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("newbot.title")}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: 16 }}>{t("newbot.title")}</strong>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {t("newbot.subtitle")}
            </div>
          </div>
          <button
            type="button"
            className="btn"
            onClick={onClose}
            disabled={busy}
            aria-label={t("newbot.cancel")}
            style={{ padding: "4px 9px", flexShrink: 0 }}
          >
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>

        <div className="modal-body">
          <div className="field">
            <label htmlFor="simple-newbot-name">{t("newbot.name")}</label>
            <input
              id="simple-newbot-name"
              ref={nameRef}
              className="input"
              value={name}
              placeholder={t("newbot.namePlaceholder")}
              autoComplete="off"
              onChange={(e) => setName(e.target.value)}
            />
            <p className="simple-hint">{t("newbot.nameHelp")}</p>
          </div>

          <HandlePreview
            handle={handlePreview}
            label={t("newbot.handle")}
            pending={pick ? t("newbot.handlePendingArchetype", { slug: pick }) : t("newbot.handlePending")}
            hint={t("newbot.handleHelp")}
          />

          <div className="field">
            <span className="simple-field-label">{t("newbot.kind")}</span>
            <p className="simple-hint">{t("newbot.kindHelp")}</p>

            <button
              type="button"
              className={`simple-pick${pick === "" ? " is-active" : ""}`}
              aria-pressed={pick === ""}
              onClick={() => choose(null)}
            >
              <span className="simple-pick-face" aria-hidden>
                <Sparkles size={15} strokeWidth={1.75} />
              </span>
              <span className="simple-pick-text">
                <span className="simple-pick-name">{t("newbot.blank")}</span>
                <span className="simple-pick-desc">{t("newbot.blankDesc")}</span>
              </span>
            </button>

            <div className="simple-search simple-pick-search">
              <Search size={14} strokeWidth={1.75} aria-hidden />
              <input
                className="simple-search-input"
                type="search"
                value={query}
                placeholder={t("newbot.searchPlaceholder")}
                aria-label={t("newbot.searchLabel")}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>

            {catalog.status === "loading" && experts.length + teams.length === 0 && (
              <div className="spinner simple-rail-spinner" aria-label={t("newbot.loading")} />
            )}
            {catalog.status === "error" && (
              <p className="simple-hint">
                {t("newbot.catalogFailed")}{" "}
                <button type="button" className="btn ghost xs" onClick={catalog.refresh}>
                  {t("newbot.retry")}
                </button>
              </p>
            )}

            <div className="simple-pick-list">
              {filtered.experts.length > 0 && (
                <>
                  <p className="simple-section">
                    <Sparkles size={12} strokeWidth={1.75} /> {t("newbot.experts")}
                  </p>
                  {filtered.experts.map(optionRow)}
                </>
              )}
              {filtered.teams.length > 0 && (
                <>
                  <p className="simple-section">
                    <Users size={12} strokeWidth={1.75} /> {t("newbot.teams")}
                  </p>
                  {filtered.teams.map(optionRow)}
                </>
              )}
              {catalog.status === "ready" &&
                filtered.experts.length + filtered.teams.length === 0 && (
                  <p className="simple-hint">{query.trim() ? t("newbot.noMatches") : t("newbot.catalogEmpty")}</p>
                )}
            </div>
          </div>

          <div className="field">
            <label htmlFor="simple-newbot-model">{t("newbot.model")}</label>
            <select
              id="simple-newbot-model"
              className="input"
              data-testid="newbot-model"
              value={modelValue}
              disabled={busy || modelOptions.length === 0}
              onChange={(e) => setModel(e.target.value)}
            >
              {modelOptions.length === 0 ? (
                <option value="">{modelsLoaded ? t("newbot.modelNone") : t("newbot.modelLoading")}</option>
              ) : (
                <>
                  {pick !== "" && <option value="">{t("newbot.modelTemplateOwn")}</option>}
                  <ModelOptionGroups options={modelOptions} />
                </>
              )}
            </select>
            <p className="simple-hint">{t("newbot.modelHelp")}</p>
          </div>

          {/* Only a from-scratch bot has a description to give: an archetype
              brings its own, and the create endpoint refuses free-form fields
              that would contradict the template. */}
          {pick === "" && (
            <div className="field">
              <label htmlFor="simple-newbot-desc">{t("newbot.description")}</label>
              <textarea
                id="simple-newbot-desc"
                className="input simple-profile-textarea"
                rows={3}
                value={description}
                placeholder={t("newbot.descriptionPlaceholder")}
                onChange={(e) => setDescription(e.target.value)}
              />
              <p className="simple-hint">{t("newbot.descriptionHelp")}</p>
            </div>
          )}
        </div>

        <div className="modal-foot">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t("newbot.cancel")}
          </button>
          <button type="button" className="btn primary" disabled={!canCreate} onClick={() => void create()}>
            {busy ? t("newbot.creating") : t("newbot.create")}
          </button>
        </div>
      </div>
    </div>
  );
}
