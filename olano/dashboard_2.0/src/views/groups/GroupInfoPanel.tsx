// The right-hand panel for a group: what it is called, who is in it, and the
// things you can do about that — rename it, add a bot, remove a bot, delete it.
//
// Renaming lives here as well as on the rail's right-click menu, and that is
// deliberate rather than redundant: a right-click is not a gesture a phone
// has, so a rename that existed only in the context menu would be a desktop
// feature wearing a cross-platform label.
//
// Same shape as BotProfilePanel (a column beside the conversation on a wide
// viewport, a full-screen overlay with a focus trap on a phone) so the shell
// has one panel idiom rather than two.
//
// Every destructive action is confirmed, and the confirmation says what is
// actually lost: removing a member keeps what it already said, deleting the
// group takes the whole conversation and keeps the bots. Both are stated in
// the help text BEFORE the button is pressed, not only in the dialog.
//
// The PEOPLE section is the human half of the room. Agents are the roster;
// people are the creator, anyone the creator shared the group with, and any
// admin who has posted, read off the group's `people`, which the server
// derives from the share table and the transcript (so an admin who spoke is
// listed by having spoken, with no row to keep in step). Sharing is the
// document model: a shared person reads, posts and leaves, and this panel
// shows them exactly those controls. The creator and admins also get the
// picker, which lists only people the server would accept, anyone who can
// already use every agent in the roster, because a share must never be the
// way somebody reaches an agent they were not granted.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LogOut, Pencil, Trash2, UserMinus, X } from "lucide-react";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { useConfirm } from "../../ui/confirm";
import { useDialogA11y } from "../../ui/dialogA11y";
import { useToast } from "../../ui/toast";
import { BotMark, GroupMark } from "../agents/BotMark";
import {
  addMember,
  deleteGroup,
  fetchShareCandidates,
  groupErrorText,
  removeMember,
  renameGroup,
  shareGroup,
  unshareGroup,
  type GroupPerson,
  type GroupSummary,
  type ShareCandidate,
} from "./groupsStore";
import { GROUPS_STR as STR } from "./strings";

// Initials for a person's chip: name first ("Emily Chacon" → EC), else the
// handle. Local rather than imported from the admin views, which would pull
// that whole section into the group bundle for two lines.
function initialsOf(p: { name?: string; username?: string }): string {
  const base = (p.name || p.username || "?").trim();
  const parts = base.split(/[\s@._-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}

/** What a person is called in the list: their name, else their handle. */
function personLabel(p: GroupPerson): string {
  return p.name || p.username || p.principal || p.id;
}

// The roster bounds the feature is designed around (olano/groups/store.py). A
// deployment may narrow them in config, never widen them — so these are the
// numbers the UI explains, and a narrower server-side refusal still arrives as
// its own message, verbatim.
export const GROUP_MIN_MEMBERS = 2;
export const GROUP_MAX_MEMBERS = 6;

export function GroupInfoPanel({
  group,
  agents,
  asOverlay,
  onClose,
  onDeleted,
}: {
  group: GroupSummary;
  /** Every bot this human can reach — the pool "add a bot" draws from. */
  agents: Agent[];
  asOverlay: boolean;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const { user: me, multiUser } = useAuth();
  const panelRef = useRef<HTMLElement>(null);
  const [busy, setBusy] = useState(false);
  const [pick, setPick] = useState("");
  // Inline rename: null when the heading is just a heading.
  const [draft, setDraft] = useState<string | null>(null);

  // Who may change the group itself, rename, roster, shares, delete. The
  // server's `_can_manage` rule, read here only to draw the right controls:
  // a guest who found a hidden button would be refused server-side anyway.
  const canManage = !!me && (me.is_admin || group.created_by === me.id);
  // A shared-in guest: can read, post and leave, nothing else.
  const isGuest = !!me && !canManage && (group.shared_with ?? []).includes(me.id);
  const people = group.people ?? [];

  // The share picker's pool. Fetched only for someone who may share, on a
  // team-accounts deployment, and refetched whenever the roster or the share
  // list moves, both change who qualifies.
  const [shareCandidates, setShareCandidates] = useState<ShareCandidate[]>([]);
  const [sharePick, setSharePick] = useState("");
  const sharesKey = (group.shared_with ?? []).join(",");
  const membersKey = group.members.join(",");
  const loadCandidates = useCallback(async () => {
    if (!canManage || !multiUser) {
      setShareCandidates([]);
      return;
    }
    try {
      setShareCandidates(await fetchShareCandidates(group.id));
    } catch {
      // An older backend has no candidates endpoint; the picker then simply
      // offers nobody, which the hint explains.
      setShareCandidates([]);
    }
  }, [canManage, multiUser, group.id]);
  useEffect(() => {
    void loadCandidates();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the two keys are the roster and share list, which decide who qualifies
  }, [loadCandidates, sharesKey, membersKey]);

  async function share() {
    if (!sharePick) return;
    const who = shareCandidates.find((c) => c.id === sharePick);
    setBusy(true);
    try {
      await shareGroup(group.id, sharePick);
      toast.push(t("groups.sharedWith", { name: who?.name || who?.username || sharePick }));
      setSharePick("");
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.shareFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function unshare(p: GroupPerson) {
    const name = personLabel(p);
    const ok = await confirm({
      message: t("groups.unshareConfirm", { name, group: group.name }),
      confirmLabel: t("groups.unshare", { name }),
      cancelLabel: t("groups.cancel"),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await unshareGroup(group.id, p.id);
      toast.push(t("groups.unshared", { name }));
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.shareFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function leave() {
    if (!me) return;
    const ok = await confirm({
      message: t("groups.leaveConfirm", { name: group.name }),
      confirmLabel: t("groups.leave"),
      cancelLabel: t("groups.cancel"),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await unshareGroup(group.id, me.id);
      toast.push(t("groups.left"));
      onDeleted();
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.leaveFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  // Keyed by name — used both to draw a member row and to give the group's
  // mark its members' real faces rather than three anonymous beads.
  const byName = useMemo(() => new Map(agents.map((a) => [a.name, a])), [agents]);
  const candidates = useMemo(
    () => agents.filter((a) => !group.members.includes(a.name)).map((a) => a.name).sort(),
    [agents, group.members],
  );
  const full = group.members.length >= GROUP_MAX_MEMBERS;
  const atMin = group.members.length <= GROUP_MIN_MEMBERS;

  async function add() {
    if (!pick) return;
    setBusy(true);
    try {
      await addMember(group.id, pick);
      toast.push(t("groups.added", { name: pick }));
      setPick("");
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.memberFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function remove(name: string) {
    const ok = await confirm({
      message: t("groups.removeConfirm", { name, group: group.name }),
      confirmLabel: t("groups.remove", { name }),
      cancelLabel: t("groups.cancel"),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await removeMember(group.id, name);
      toast.push(t("groups.removed", { name }));
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.memberFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function commitRename() {
    const name = (draft ?? "").trim();
    setDraft(null);
    // An unchanged (or emptied) name is not a rename — the server would refuse
    // the empty one anyway, and refusing it here keeps the error for the cases
    // that are actually a problem.
    if (!name || name === group.name) return;
    try {
      await renameGroup(group.id, name);
      toast.push(t("groups.renamed", { name }));
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.renameFailed")), "error");
    }
  }

  async function drop() {
    const ok = await confirm({
      message: t("groups.deleteConfirm", { name: group.name }),
      confirmLabel: t("groups.deleteAction"),
      cancelLabel: t("groups.cancel"),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await deleteGroup(group.id);
      toast.push(t("groups.deleted"));
      onDeleted();
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.deleteFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside
      ref={panelRef}
      className={asOverlay ? "simple-profile is-overlay" : "simple-profile"}
      aria-label={t("groups.infoRegion", { name: group.name })}
      data-testid="simple-group-info"
    >
      {asOverlay && <OverlayA11y panelRef={panelRef} onClose={onClose} />}

      <div className="simple-profile-head">
        <strong>{t("groups.info")}</strong>
        <button
          type="button"
          className="btn ghost xs"
          onClick={onClose}
          aria-label={t("groups.infoClose")}
          title={t("groups.infoClose")}
        >
          <X size={15} strokeWidth={2} />
        </button>
      </div>

      <div className="simple-profile-body">
        <div className="simple-profile-face">
          <span className="simple-profile-mark">
            <GroupMark members={group.members} agentsByName={byName} size={88} />
          </span>
          {draft === null ? (
            <h2 className="simple-profile-name simple-group-title">
              {group.name}
              {canManage && (
                <button
                  type="button"
                  className="simple-group-rename"
                  onClick={() => setDraft(group.name)}
                  aria-label={t("groups.rename")}
                  title={t("groups.renameHelp")}
                >
                  <Pencil size={13} strokeWidth={1.75} />
                </button>
              )}
            </h2>
          ) : (
            <input
              className="simple-group-rename-input"
              autoFocus
              value={draft}
              maxLength={120}
              aria-label={t("groups.rename")}
              placeholder={t("groups.renamePlaceholder")}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void commitRename();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setDraft(null);
                }
              }}
              onBlur={() => void commitRename()}
            />
          )}
          <p className="simple-profile-role">
            {group.members.length === 1
              ? t("groups.membersOne")
              : t("groups.members", { n: String(group.members.length) })}
            {people.length > 1 && (
              <>
                {" · "}
                {t("groups.peopleCount", { n: String(people.length) })}
              </>
            )}
          </p>
        </div>

        {isGuest && <p className="simple-hint simple-group-guest">{t("groups.guestHint")}</p>}

        <h3 className="simple-profile-label">{t("groups.roster")}</h3>
        <p className="simple-hint">{t("groups.rosterHelp")}</p>
        <ul className="simple-member-list">
          {group.members.map((name, i) => (
            <li className="simple-member" key={name}>
              <BotMark
                name={name}
                avatarUrl={byName.get(name)?.avatar_url}
                emoji={byName.get(name)?.emoji}
                size={30}
              />
              <span className="simple-member-text">
                <span className="simple-member-name">{name}</span>
                {i === 0 && <span className="simple-member-tag">{t("groups.answersFirst")}</span>}
              </span>
              {canManage && (
                <button
                  type="button"
                  className="simple-member-del"
                  disabled={busy || atMin}
                  aria-label={t("groups.remove", { name })}
                  title={
                    atMin
                      ? t("groups.removeMin", { min: String(GROUP_MIN_MEMBERS) })
                      : t("groups.remove", { name })
                  }
                  onClick={() => void remove(name)}
                >
                  <UserMinus size={14} strokeWidth={1.75} />
                </button>
              )}
            </li>
          ))}
        </ul>
        {canManage && atMin && (
          <p className="simple-hint">{t("groups.removeMin", { min: String(GROUP_MIN_MEMBERS) })}</p>
        )}

        {canManage && (
          <div className="field simple-group-field">
            <label htmlFor="simple-group-add">{t("groups.add")}</label>
            <div className="simple-group-add">
              <select
                id="simple-group-add"
                className="input"
                value={pick}
                disabled={busy || full || candidates.length === 0}
                onChange={(e) => setPick(e.target.value)}
              >
                <option value="">{t("groups.addPlaceholder")}</option>
                {candidates.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn"
                disabled={busy || full || !pick}
                onClick={() => void add()}
              >
                {t("groups.addAction")}
              </button>
            </div>
            <p className="simple-hint">{t("groups.addHelp", { max: String(GROUP_MAX_MEMBERS) })}</p>
            {full && (
              <p className="simple-hint">{t("groups.addFull", { max: String(GROUP_MAX_MEMBERS) })}</p>
            )}
            {!full && candidates.length === 0 && <p className="simple-hint">{t("groups.addNone")}</p>}
          </div>
        )}

        {/* The people half of the room. Only meaningful with team accounts:
            single-user there is exactly one person, and a list of one "you"
            explains nothing. */}
        {multiUser && (
          <>
            <h3 className="simple-profile-label simple-group-people-label">{t("groups.people")}</h3>
            <p className="simple-hint">{t("groups.peopleHelp")}</p>
            <ul className="simple-member-list" data-testid="simple-group-people">
              {people.map((p) => {
                const self = !!me && p.id === me.id;
                const label = personLabel(p);
                const tag =
                  p.role === "creator"
                    ? t("groups.personCreator")
                    : p.role === "shared"
                      ? t("groups.personShared")
                      : t("groups.personAdmin");
                return (
                  <li className="simple-member" key={p.principal || p.id}>
                    <span className="avatar-sm" aria-hidden>
                      {initialsOf(p)}
                    </span>
                    <span className="simple-member-text">
                      <span className="simple-member-name">
                        {label}
                        {self && <span className="muted"> ({t("groups.personYou")})</span>}
                        {!p.username && !p.name && (
                          <span className="muted"> {t("groups.personGone")}</span>
                        )}
                      </span>
                      <span className="simple-member-tag">{tag}</span>
                    </span>
                    {canManage && p.role === "shared" && (
                      <button
                        type="button"
                        className="simple-member-del"
                        disabled={busy}
                        aria-label={t("groups.unshare", { name: label })}
                        title={t("groups.unshare", { name: label })}
                        onClick={() => void unshare(p)}
                      >
                        <UserMinus size={14} strokeWidth={1.75} />
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>

            {canManage && (
              <div className="field simple-group-field">
                <label htmlFor="simple-group-share">{t("groups.share")}</label>
                <div className="simple-group-add">
                  <select
                    id="simple-group-share"
                    className="input"
                    value={sharePick}
                    disabled={busy || shareCandidates.length === 0}
                    onChange={(e) => setSharePick(e.target.value)}
                  >
                    <option value="">{t("groups.sharePlaceholder")}</option>
                    {shareCandidates.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name || c.username}
                        {c.username && c.name ? ` (@${c.username})` : ""}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="btn"
                    disabled={busy || !sharePick}
                    onClick={() => void share()}
                  >
                    {t("groups.shareAction")}
                  </button>
                </div>
                <p className="simple-hint">{t("groups.shareHelp")}</p>
                {shareCandidates.length === 0 && <p className="simple-hint">{t("groups.shareNone")}</p>}
              </div>
            )}

            {isGuest && (
              <div className="field simple-group-field">
                <button
                  type="button"
                  className="btn simple-group-delete"
                  disabled={busy}
                  onClick={() => void leave()}
                >
                  <LogOut size={14} strokeWidth={1.75} /> {t("groups.leave")}
                </button>
                <p className="simple-hint">{t("groups.leaveHelp")}</p>
              </div>
            )}
          </>
        )}

        {canManage && (
          <div className="field simple-group-field">
            <button
              type="button"
              className="btn danger simple-group-delete"
              disabled={busy}
              onClick={() => void drop()}
            >
              <Trash2 size={14} strokeWidth={1.75} /> {t("groups.delete")}
            </button>
            <p className="simple-hint">{t("groups.deleteHelp")}</p>
          </div>
        )}
      </div>
    </aside>
  );
}

// Hook host for the mobile overlay — mounted only while the panel IS an
// overlay, so Escape, the Tab trap and focus restore apply exactly then.
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
