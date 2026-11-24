// The right-hand panel: who this bot is.
//
// Read-only for everyone by default — a name, a face, what it is for. It turns
// into an editor only for a human who may actually administer this bot
// (`useAuth().canAdminAgent`), and that check decides what to DRAW and nothing
// else: the runtime re-checks every write on its own, so a member who somehow
// reached the form still cannot save through it.
//
// On a phone the panel is a full-screen overlay with its own focus trap; on a
// wide viewport it is a column beside the conversation.

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { agentDetailPath } from "../../core/routeUrls";
import { KeyRound, Pencil, Plug, Upload, Wrench, X } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { useDialogA11y } from "../../ui/dialogA11y";
import { useToast } from "../../ui/toast";
import { saveAgentTitle } from "../agents/helpers";
import type { AgentConfig } from "../agents/types";
import { BotMark } from "./BotMark";
import { STR } from "./strings";

// Same ceiling the dashboard's avatar editor enforces before it uploads, so a
// too-large file is refused here instead of after a slow round-trip.
const MAX_AVATAR_BYTES = 10 * 1024 * 1024;

function errText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.detail || fallback;
  return e instanceof Error ? e.message : fallback;
}

// "engineering-frontend-developer" → "Engineering frontend developer". The
// archetype a bot was installed from is the closest thing it has to a job
// title, so it is shown as one when present.
function prettyRole(raw: unknown): string {
  if (typeof raw !== "string" || !raw.trim()) return "";
  const words = raw.replace(/[-_]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function BotProfilePanel({
  agent,
  bot,
  asOverlay,
  onClose,
  onChanged,
}: {
  agent: string;
  bot: Agent | undefined;
  asOverlay: boolean;
  onClose: () => void;
  /** Roster refresh — the rail and the chat header read the same fields. */
  onChanged: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const { canAdminAgent, user, switchUiMode } = useAuth();
  const navigate = useNavigate();
  // Set the URL first, then swap shells: the router sits above the fork, so
  // when the dashboard tree mounts it is already showing the target page.
  const openInDashboard = (path: string) => {
    navigate(path);
    void switchUiMode("advanced");
  };
  const panelRef = useRef<HTMLElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [emoji, setEmoji] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  const mayEdit = canAdminAgent(agent);

  const load = useCallback(() => {
    if (!agent) return;
    setFailed(false);
    api
      .get<AgentConfig>(`/api/agents/${encodeURIComponent(agent)}`)
      .then((c) => setCfg(c))
      .catch(() => setFailed(true));
  }, [agent]);

  // Reload on every bot switch, and drop the previous bot's config first so a
  // slow fetch can never show one bot's profile under another's name.
  useEffect(() => {
    setCfg(null);
    setEditing(false);
    load();
  }, [agent, load]);

  // Opening the editor seeds it from whatever is on screen right now.
  function beginEdit() {
    setEmoji(cfg?.emoji ?? bot?.emoji ?? "");
    setTitle(cfg?.title ?? bot?.title ?? "");
    setDescription(cfg?.description ?? bot?.description ?? "");
    setEditing(true);
  }

  async function save() {
    setBusy(true);
    try {
      await api.put(`/api/agents/${encodeURIComponent(agent)}`, {
        emoji: emoji.trim(),
        description: description.trim(),
      });
      // The title lives in the org block (its own endpoint) — written only
      // when it changed, so an untouched profile edit never rewrites the
      // org chart entry.
      if (title.trim() !== (cfg?.title ?? bot?.title ?? "").trim()) {
        await saveAgentTitle(agent, title);
      }
      toast.push(t("profile.saved"));
      setEditing(false);
      load();
      onChanged();
    } catch (e) {
      toast.push(errText(e, t("profile.saveFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function uploadAvatar(ev: React.ChangeEvent<HTMLInputElement>) {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    if (file.size > MAX_AVATAR_BYTES) {
      toast.push(t("profile.avatarTooBig"), "error");
      return;
    }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api.postForm(`/api/agents/${encodeURIComponent(agent)}/avatar`, fd);
      toast.push(t("profile.avatarUpdated"));
      // The reload is what refreshes the picture: `avatar_url` comes back
      // stamped with the new file's content, so the freshly uploaded image is a
      // different URL to the browser and no cache-busting is needed.
      load();
      onChanged();
    } catch (e) {
      toast.push(errText(e, t("profile.avatarFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  const name = cfg?.name || bot?.name || agent;
  const shownEmoji = cfg?.emoji ?? bot?.emoji ?? "";
  const avatarUrl = cfg?.avatar_url ?? bot?.avatar_url ?? null;
  const shownDescription = (cfg?.description ?? bot?.description ?? "").trim();
  // The line under the name: the bot's title (its position) when it has
  // one; the prettified archetype slug only as a fallback for bots that
  // predate the field.
  const role = (cfg?.title ?? bot?.title ?? "").trim() || prettyRole(cfg?.archetype);
  const online = bot?.running === true && bot?.paused !== true;

  return (
    <aside
      ref={panelRef}
      className={asOverlay ? "simple-profile is-overlay" : "simple-profile"}
      aria-label={t("profile.region", { agent: name })}
      data-testid="simple-profile"
    >
      {/* A full-screen overlay is a dialog: Escape closes it, Tab stays inside,
          and focus goes back where it came from. A docked column is not. */}
      {asOverlay && <OverlayA11y panelRef={panelRef} onClose={onClose} />}

      <div className="simple-profile-head">
        <strong>{t("profile.title")}</strong>
        <button
          type="button"
          className="btn ghost xs"
          onClick={onClose}
          aria-label={t("profile.close")}
          title={t("profile.close")}
        >
          <X size={15} strokeWidth={2} />
        </button>
      </div>

      <div className="simple-profile-body">
        <div className="simple-profile-face">
          {/* The same mark the rail, the header and every reply use — one face
              per bot across the shell, including its fallback. */}
          <span className="simple-profile-mark">
            <BotMark name={name} avatarUrl={avatarUrl} emoji={shownEmoji} size={88} online={online} />
          </span>
          <h2 className="simple-profile-name">{name}</h2>
          {!!role && <p className="simple-profile-role">{role}</p>}
          <p className={`simple-profile-status${online ? " is-online" : ""}`}>
            {online ? t("rail.online") : t("rail.offline")}
          </p>
        </div>

        {failed && <p className="muted simple-hint">{t("profile.loadFailed")}</p>}

        {!editing && (
          <>
            <h3 className="simple-profile-label">{t("profile.about")}</h3>
            <p className="simple-profile-about">{shownDescription || t("profile.noDescription")}</p>
            {mayEdit ? (
              <button type="button" className="btn simple-profile-edit" onClick={beginEdit}>
                <Pencil size={14} strokeWidth={1.75} /> {t("profile.edit")}
              </button>
            ) : (
              <p className="simple-hint">{t("profile.readOnly")}</p>
            )}

            {/* Where configuration lives. The simple view deliberately has no
                settings screens — connectors, MCPs and keys are dashboard
                work — so this section is a door, not a form. Admins get the
                door (switch shells, landing directly on the right page,
                because the URL is set before the swap and the router already
                holds it when the dashboard mounts). Everyone else gets the
                honest sentence: their operator does this, and a member-shaped
                door would open onto pages that refuse them.

                Gated on is_admin, NOT on mayEdit: a per-bot owner who is not
                an admin may edit the face above, but the shell switch itself
                is admin-only server-side, so for them the door cannot open. */}
            <h3 className="simple-profile-label">{t("profile.setup")}</h3>
            {user?.is_admin ? (
              <div className="simple-profile-setup">
                <button type="button" className="btn" onClick={() => openInDashboard(agentDetailPath(agent))} title={t("profile.setup.botHelp")}>
                  <Wrench size={14} strokeWidth={1.75} /> {t("profile.setup.bot")}
                </button>
                <button type="button" className="btn" onClick={() => openInDashboard("/connections")} title={t("profile.setup.connectionsHelp")}>
                  <Plug size={14} strokeWidth={1.75} /> {t("profile.setup.connections")}
                </button>
                <button type="button" className="btn" onClick={() => openInDashboard("/providers")} title={t("profile.setup.keysHelp")}>
                  <KeyRound size={14} strokeWidth={1.75} /> {t("profile.setup.keys")}
                </button>
                <p className="simple-hint">{t("profile.setup.hint")}</p>
              </div>
            ) : (
              <p className="simple-hint">{t("profile.setup.member")}</p>
            )}
          </>
        )}

        {editing && (
          <form
            className="simple-profile-form"
            onSubmit={(e) => {
              e.preventDefault();
              void save();
            }}
          >
            {/* The bot's handle. Read-only on purpose: the name is this bot's
                identity everywhere (its workspace on disk, its conversations,
                how other bots address it), and the runtime refuses to change it
                on this endpoint — so the field explains that instead of
                offering an edit that would silently do nothing. */}
            <div className="field">
              <label htmlFor="simple-bot-handle">{t("profile.handle")}</label>
              <input id="simple-bot-handle" className="input" value={name} readOnly disabled />
              <p className="simple-hint">{t("profile.handleHelp")}</p>
            </div>

            <div className="field">
              <label htmlFor="simple-bot-title">{t("profile.titleField")}</label>
              <input
                id="simple-bot-title"
                className="input"
                value={title}
                maxLength={80}
                placeholder={t("profile.titlePlaceholder")}
                onChange={(e) => setTitle(e.target.value)}
              />
              <p className="simple-hint">{t("profile.titleHelp")}</p>
            </div>

            <div className="field">
              <label htmlFor="simple-bot-emoji">{t("profile.emoji")}</label>
              <input
                id="simple-bot-emoji"
                className="input"
                value={emoji}
                maxLength={8}
                placeholder="🦁"
                onChange={(e) => setEmoji(e.target.value)}
              />
              <p className="simple-hint">{t("profile.emojiHelp")}</p>
            </div>

            <div className="field">
              <label htmlFor="simple-bot-desc">{t("profile.description")}</label>
              <textarea
                id="simple-bot-desc"
                className="input simple-profile-textarea"
                rows={4}
                value={description}
                placeholder={t("profile.descriptionPlaceholder")}
                onChange={(e) => setDescription(e.target.value)}
              />
              <p className="simple-hint">{t("profile.descriptionHelp")}</p>
            </div>

            <div className="field">
              <label htmlFor="simple-bot-avatar">{t("profile.avatar")}</label>
              <input
                id="simple-bot-avatar"
                ref={fileRef}
                type="file"
                accept="image/*"
                hidden
                onChange={(e) => void uploadAvatar(e)}
              />
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => fileRef.current?.click()}
              >
                <Upload size={14} strokeWidth={1.75} /> {t("profile.avatarUpload")}
              </button>
              <p className="simple-hint">{t("profile.avatarHelp")}</p>
            </div>

            <div className="simple-profile-actions">
              <button type="button" className="btn" disabled={busy} onClick={() => setEditing(false)}>
                {t("profile.cancel")}
              </button>
              <button type="submit" className="btn primary" disabled={busy}>
                {busy ? t("profile.saving") : t("profile.save")}
              </button>
            </div>
          </form>
        )}
      </div>
    </aside>
  );
}

// Hook host for the mobile overlay — mounted only while the panel is an
// overlay, so the trap applies exactly then and never to the docked column.
function OverlayA11y({
  panelRef,
  onClose,
}: {
  panelRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  useDialogA11y(panelRef, onClose);
  return null;
}
