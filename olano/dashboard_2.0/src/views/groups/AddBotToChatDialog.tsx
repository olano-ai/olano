// "Add a bot to this chat" — the one-click path from a 1:1 into a group.
//
// This is deliberately NOT NewGroupDialog with a bot pre-ticked. That dialog
// asks you to name a room and compose a roster from nothing, which is the right
// shape when you are setting one up on purpose and the wrong shape here: the
// roster is already half-decided (the bot you are talking to leads it) and the
// conversation already has a subject, so a required name is pure friction. What
// this dialog asks is the one question that actually has two answers — does the
// conversation come with you — and it answers it in the affirmative by default,
// because arriving in an empty room is exactly the re-explaining people were
// trying to avoid.
//
// The current bot is shown as a fixed row rather than a ticked checkbox: it is
// not a choice, and a checkbox that cannot be unticked reads as a bug.

import { useMemo, useRef, useState } from "react";
import { Check, Search, X } from "lucide-react";
import { useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { useDialogA11y } from "../../ui/dialogA11y";
import { Toggle } from "../../ui/Toggle";
import { useToast } from "../../ui/toast";
import { BotMark, GroupMark } from "../agents/BotMark";
import { createGroupFromThread, groupErrorText, type GroupSummary } from "./groupsStore";
import { GROUP_MAX_MEMBERS } from "./GroupInfoPanel";
import { GROUPS_STR as STR } from "./strings";

export function AddBotToChatDialog({
  agent,
  agents,
  threadId,
  onClose,
  onCreated,
}: {
  /** The bot this conversation is with — it leads the new roster. */
  agent: string;
  /** Every bot this human can reach; the current one is filtered out. */
  agents: Agent[];
  /**
   * The conversation being promoted. Empty for a chat with nothing sent yet —
   * there is no transcript to carry, so the dialog drops the carry-over
   * question rather than offering a switch that would do nothing.
   */
  threadId: string;
  onClose: () => void;
  onCreated: (group: GroupSummary) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const panelRef = useRef<HTMLDivElement>(null);

  const [picked, setPicked] = useState<string[]>([]);
  const [carryOver, setCarryOver] = useState(true);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  useDialogA11y(panelRef, busy ? undefined : onClose);

  // The roster keyed by name, so a group's mark can wear its members' real
  // faces rather than three anonymous beads.
  const byName = useMemo(() => new Map(agents.map((a) => [a.name, a] as const)), [agents]);
  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = agents.filter((a) => a.name !== agent);
    const matched = q
      ? rows.filter(
          (a) =>
            a.name.toLowerCase().includes(q) || (a.description || "").toLowerCase().includes(q),
        )
      : rows;
    return [...matched].sort((a, b) => a.name.localeCompare(b.name));
  }, [agents, agent, query]);

  // The leading bot occupies one seat, so the pick list is bounded one below
  // the roster ceiling.
  const room = GROUP_MAX_MEMBERS - 1;
  const tooMany = picked.length > room;
  const canCreate = picked.length > 0 && !tooMany && !busy;

  function toggle(name: string) {
    setPicked((prev) => (prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]));
  }

  async function create() {
    if (!canCreate) return;
    setBusy(true);
    try {
      const group = await createGroupFromThread(agent, threadId, picked, { carryOver });
      toast.push(t("addbot.created", { name: group.name }));
      onCreated(group);
    } catch (e) {
      // The backend's own refusal ("A group holds 2–6 agents", "no access to
      // 'x'") is the useful sentence — never replace it with a generic one.
      toast.push(groupErrorText(e, t("addbot.failed")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={busy ? undefined : onClose}>
      <div
        className="card modal-card simple-dialog"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("addbot.title", { name: agent })}
        onClick={(e) => e.stopPropagation()}
        data-testid="simple-add-bot"
      >
        <div className="modal-head">
          <div style={{ minWidth: 0, display: "flex", alignItems: "center", gap: 10 }}>
            <GroupMark members={[agent, ...picked]} agentsByName={byName} size={34} />
            <div style={{ minWidth: 0 }}>
              <strong style={{ fontSize: 16 }}>{t("addbot.title", { name: agent })}</strong>
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {t("addbot.subtitle", { name: agent })}
              </div>
            </div>
          </div>
          <button
            type="button"
            className="btn"
            onClick={onClose}
            disabled={busy}
            aria-label={t("addbot.cancel")}
            style={{ padding: "4px 9px", flexShrink: 0 }}
          >
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>

        <div className="modal-body">
          {candidates.length === 0 && !query && (
            <p className="simple-error">{t("addbot.noOthers")}</p>
          )}

          <div className="field">
            <span className="simple-field-label">{t("addbot.who")}</span>
            <p className="simple-hint">{t("addbot.whoHelp", { name: agent, max: String(room) })}</p>

            <div className="simple-search simple-pick-search">
              <Search size={14} strokeWidth={1.75} aria-hidden />
              <input
                className="simple-search-input"
                type="search"
                value={query}
                placeholder={t("newgroup.searchPlaceholder")}
                aria-label={t("newgroup.searchLabel")}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>

            <div className="simple-pick-list">
              {candidates.map((a) => {
                const selected = picked.includes(a.name);
                return (
                  <button
                    key={a.name}
                    type="button"
                    className={`simple-pick${selected ? " is-active" : ""}`}
                    aria-pressed={selected}
                    onClick={() => toggle(a.name)}
                  >
                    <BotMark name={a.name} avatarUrl={a.avatar_url} emoji={a.emoji} size={28} />
                    <span className="simple-pick-text">
                      <span className="simple-pick-name">{a.name}</span>
                      {!!a.description && <span className="simple-pick-desc">{a.description}</span>}
                    </span>
                    {selected && (
                      <span className="simple-pick-check" aria-hidden>
                        <Check size={14} strokeWidth={2.5} />
                      </span>
                    )}
                  </button>
                );
              })}
              {candidates.length === 0 && !!query && (
                <p className="simple-hint">{t("newgroup.noMatches")}</p>
              )}
            </div>

            {tooMany && (
              <p className="simple-error">
                {t("addbot.tooMany", { max: String(GROUP_MAX_MEMBERS), room: String(room) })}
              </p>
            )}
          </div>

          {/* Only a conversation that exists can travel. A chat with nothing
              sent yet has no transcript, so the question is not asked. */}
          {!!threadId && (
            <div className="field simple-group-field">
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                }}
              >
                <span className="simple-field-label">{t("addbot.carry")}</span>
                <Toggle
                  checked={carryOver}
                  disabled={busy}
                  onChange={setCarryOver}
                  ariaLabel={t("addbot.carry")}
                />
              </div>
              <p className="simple-hint">{t("addbot.carryHelp")}</p>
            </div>
          )}
        </div>

        <div className="modal-foot">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t("addbot.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!canCreate}
            onClick={() => void create()}
          >
            {busy ? t("addbot.creating") : t("addbot.create")}
          </button>
        </div>
      </div>
    </div>
  );
}
