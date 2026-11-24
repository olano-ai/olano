// "New group" — a name and a multi-select, and nothing else.
//
// The roster size is the one rule a person can get wrong here, so it is stated
// three times over: in the help text before they start, in a live "2 of 6
// chosen" counter, and as an inline message the moment the selection leaves the
// range. The create button stays disabled until it is valid — but disabled with
// a visible reason, never a button that silently does nothing.
//
// Order matters and is not cosmetic: the FIRST bot picked answers any message
// that names nobody, so the list keeps pick order rather than sorting, and the
// leader is badged.

import { useMemo, useRef, useState } from "react";
import { Check, Search, X } from "lucide-react";
import { useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { useDialogA11y } from "../../ui/dialogA11y";
import { useToast } from "../../ui/toast";
import { BotMark, GroupMark } from "../agents/BotMark";
import { createGroup, groupErrorText, type GroupSummary } from "./groupsStore";
import { GROUP_MAX_MEMBERS, GROUP_MIN_MEMBERS } from "./GroupInfoPanel";
import { GROUPS_STR as STR } from "./strings";

const MAX_NAME = 60;

export function NewGroupDialog({
  agents,
  onClose,
  onCreated,
}: {
  agents: Agent[];
  onClose: () => void;
  onCreated: (group: GroupSummary) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const panelRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  // The dialog seeds its own focus into the name field, so the shared hook only
  // owns Escape, the Tab trap and focus restore.
  useDialogA11y(panelRef, busy ? undefined : onClose, { initialFocus: false });

  // The roster keyed by name, so a group's mark can wear its members' real
  // faces rather than three anonymous beads.
  const byName = useMemo(() => new Map(agents.map((a) => [a.name, a] as const)), [agents]);
  const listed = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = q
      ? agents.filter(
          (a) =>
            a.name.toLowerCase().includes(q) || (a.description || "").toLowerCase().includes(q),
        )
      : agents;
    return [...rows].sort((a, b) => a.name.localeCompare(b.name));
  }, [agents, query]);

  function toggle(agent: string) {
    setPicked((prev) =>
      prev.includes(agent) ? prev.filter((n) => n !== agent) : [...prev, agent],
    );
  }

  const tooFew = picked.length < GROUP_MIN_MEMBERS;
  const tooMany = picked.length > GROUP_MAX_MEMBERS;
  const named = name.trim().length > 0;
  const canCreate = named && !tooFew && !tooMany && !busy;
  // Not enough bots exist to form a group at all — say that instead of showing
  // a picker that can never satisfy its own rule.
  const impossible = agents.length < GROUP_MIN_MEMBERS;

  async function create() {
    if (!canCreate) return;
    setBusy(true);
    try {
      const group = await createGroup(name.trim(), picked);
      toast.push(t("newgroup.created", { name: group.name }));
      onCreated(group);
    } catch (e) {
      // The backend's own refusal ("A group holds 2–6 agents", "no access to
      // 'x'") is the useful sentence — never replace it with a generic one.
      toast.push(groupErrorText(e, t("newgroup.failed")), "error");
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
        aria-label={t("newgroup.title")}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div style={{ minWidth: 0, display: "flex", alignItems: "center", gap: 10 }}>
            {picked.length > 0 && <GroupMark members={picked} agentsByName={byName} size={34} />}
            <div style={{ minWidth: 0 }}>
              <strong style={{ fontSize: 16 }}>{t("newgroup.title")}</strong>
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {t("newgroup.subtitle")}
              </div>
            </div>
          </div>
          <button
            type="button"
            className="btn"
            onClick={onClose}
            disabled={busy}
            aria-label={t("newgroup.cancel")}
            style={{ padding: "4px 9px", flexShrink: 0 }}
          >
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>

        <div className="modal-body">
          {impossible && (
            <p className="simple-error">
              {t("newgroup.noBots", { min: String(GROUP_MIN_MEMBERS) })}
            </p>
          )}

          <div className="field">
            <label htmlFor="simple-newgroup-name">{t("newgroup.name")}</label>
            <input
              id="simple-newgroup-name"
              ref={nameRef}
              className="input"
              value={name}
              maxLength={MAX_NAME}
              autoFocus
              placeholder={t("newgroup.namePlaceholder")}
              onChange={(e) => setName(e.target.value)}
            />
            <p className="simple-hint">{t("newgroup.nameHelp")}</p>
          </div>

          <div className="field">
            <span className="simple-field-label">{t("newgroup.members")}</span>
            <p className="simple-hint">
              {t("newgroup.membersHelp", {
                min: String(GROUP_MIN_MEMBERS),
                max: String(GROUP_MAX_MEMBERS),
              })}
            </p>

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
              {listed.map((a) => {
                const at = picked.indexOf(a.name);
                const selected = at !== -1;
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
                      <span className="simple-pick-name">
                        {a.name}
                        {at === 0 && (
                          <span className="simple-member-tag">{t("newgroup.first")}</span>
                        )}
                      </span>
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
              {listed.length === 0 && <p className="simple-hint">{t("newgroup.noMatches")}</p>}
            </div>

            <p className="simple-count-line">
              {t("newgroup.count", {
                n: String(picked.length),
                max: String(GROUP_MAX_MEMBERS),
              })}
            </p>
            {tooFew && !impossible && (
              <p className="simple-error">
                {t("newgroup.tooFew", { min: String(GROUP_MIN_MEMBERS) })}
              </p>
            )}
            {tooMany && (
              <p className="simple-error">
                {t("newgroup.tooMany", { max: String(GROUP_MAX_MEMBERS) })}
              </p>
            )}
          </div>
        </div>

        <div className="modal-foot">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t("newgroup.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!canCreate}
            onClick={() => void create()}
          >
            {busy ? t("newgroup.creating") : t("newgroup.create")}
          </button>
        </div>
      </div>
    </div>
  );
}
