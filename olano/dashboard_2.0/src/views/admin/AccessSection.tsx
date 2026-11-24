// Agent access, agent-major.
//
// The previous version was agent-at-a-time: a dropdown picked one agent and
// the table showed only that agent's grants, so "who can reach what?" cost one
// round-trip and one click per agent. This renders every agent as a row and
// drills in for edits.
//
// Two things drive the shape of this file:
//
//  1. Platform admins own *every* agent implicitly — `can_admin_agent` returns
//     true for `is_admin` without reading an `agent_access` row. Enumerating
//     them per row would repeat the same avatars on every line and say nothing;
//     they are stated once in a banner, and the OWNERS column carries only the
//     explicit, agent-specific grants that actually vary per row.
//  2. It has to survive a large deployment. Avatars cap at a stack of three
//     plus a count, people/agent pickers are filterable checkbox lists rather
//     than <select> walls of <option>, the whole table loads in one request,
//     and granting is multi-select (one person onto eight agents is one call).
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { useDialogA11y } from "../../ui/dialogA11y";
import {
  AdminIcon,
  CheckIcon,
  ChevronDownIcon,
  CloseIcon,
  SearchIcon,
  TrashIcon,
  UserPlusIcon,
} from "../../ui/icons";
import type { AccessOverview, User } from "../../core/types";
import { Field, Person, SectionHead, initials } from "./shared";
import { STR } from "./strings";

type Role = "owner" | "member";

// How many avatars a stack shows before collapsing into "+N".
const STACK_MAX = 3;
// Above this, a list gets its own filter box.
const FILTER_AT = 8;

function byName(a: User, b: User) {
  return (a.name || a.email).localeCompare(b.name || b.email);
}

function matches(u: User, q: string) {
  const n = q.trim().toLowerCase();
  if (!n) return true;
  return u.name.toLowerCase().includes(n) || u.email.toLowerCase().includes(n);
}

// Overlapping avatar row, capped. Order is caller-supplied and stable, so the
// faces don't reshuffle between polls.
function AvatarStack({ users, max = STACK_MAX }: { users: User[]; max?: number }) {
  if (users.length === 0) return <span className="muted">—</span>;
  const shown = users.slice(0, max);
  const rest = users.length - shown.length;
  return (
    <div className="avatar-stack" title={users.map((u) => u.name || u.email).join(", ")}>
      {shown.map((u) => (
        <span key={u.id} className="avatar-sm">
          {initials(u)}
        </span>
      ))}
      {rest > 0 && <span className="avatar-sm avatar-more">+{rest}</span>}
    </div>
  );
}

// Filterable multi-select. Used for both people and agents — anything where the
// list can realistically run to hundreds and a native <select> would collapse.
function CheckList<T extends { key: string; label: string; sub?: string; note?: string }>({
  items,
  selected,
  onToggle,
  placeholder,
  emptyText,
}: {
  items: T[];
  selected: Set<string>;
  onToggle: (key: string) => void;
  placeholder: string;
  emptyText: string;
}) {
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const shown = needle
    ? items.filter(
        (i) => i.label.toLowerCase().includes(needle) || (i.sub || "").toLowerCase().includes(needle),
      )
    : items;
  return (
    <div className="picker">
      {items.length > FILTER_AT && (
        <div className="picker-search">
          <SearchIcon size={14} />
          <input
            className="picker-search-input"
            placeholder={placeholder}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      )}
      <div className="picker-list" role="listbox" aria-multiselectable="true">
        {shown.length === 0 && <div className="picker-empty muted">{emptyText}</div>}
        {shown.map((i) => {
          const on = selected.has(i.key);
          return (
            <button
              key={i.key}
              type="button"
              role="option"
              aria-selected={on}
              className={on ? "picker-item on" : "picker-item"}
              onClick={() => onToggle(i.key)}
            >
              <span className="picker-check">{on && <CheckIcon size={13} />}</span>
              <span className="picker-text">
                <span className="picker-label">{i.label}</span>
                {i.sub && <span className="picker-sub muted">{i.sub}</span>}
              </span>
              {i.note && <span className="badge">{i.note}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// What opened the grant modal: which agent is preselected (null = none) and
// which role the opener promised — "Set an owner" must not default to User.
export interface GrantPreset {
  agent: string | null;
  role: Role;
}

function GrantModal({
  users,
  agents,
  adminIds,
  preset,
  onClose,
  onDone,
}: {
  users: User[];
  agents: string[];
  adminIds: Set<string>;
  preset: GrantPreset;
  onClose: () => void;
  onDone: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const panelRef = useRef<HTMLDivElement>(null);
  // Escape-to-close + focus trap + focus restore, same as every other dialog.
  useDialogA11y(panelRef, onClose);
  const [people, setPeople] = useState<Set<string>>(new Set());
  const [targets, setTargets] = useState<Set<string>>(
    new Set(preset.agent ? [preset.agent] : []),
  );
  const [role, setRole] = useState<Role>(preset.role);
  const [busy, setBusy] = useState(false);

  // Selecting an admin is a no-op grant — they already own every agent by rule,
  // so the row would change nothing. Say so rather than writing dead rows.
  const pickedAdmins = [...people].filter((id) => adminIds.has(id));

  function toggle(set: Set<string>, key: string, apply: (s: Set<string>) => void) {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    apply(next);
  }

  async function submit() {
    if (people.size === 0 || targets.size === 0) return;
    setBusy(true);
    try {
      await api.post("/api/admin/access/bulk", {
        user_ids: [...people],
        agent_names: [...targets],
        role,
      });
      toast.push(t("access.toast.granted"));
      onDone();
      onClose();
    } catch {
      toast.push(t("access.toast.grantFailed"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        ref={panelRef}
        className="card access-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={t("access.grant.title")}
      >
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 16 }}>{t("access.grant.title")}</h2>
            <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5 }}>
              {t("access.grant.hint")}
            </p>
          </div>
          <button className="icon-action" aria-label={t("access.grant.close")} onClick={onClose}>
            <CloseIcon size={16} />
          </button>
        </div>

        <div className="access-modal-cols">
          <Field label={t("access.grant.people", { n: String(people.size) })}>
            <CheckList
              items={users.map((u) => ({
                key: u.id,
                label: u.name || u.email,
                sub: u.email,
                note: u.is_admin ? t("users.role.admin") : undefined,
              }))}
              selected={people}
              onToggle={(k) => toggle(people, k, setPeople)}
              placeholder={t("access.grant.searchPeople")}
              emptyText={t("access.grant.noMatch")}
            />
          </Field>
          <Field label={t("access.grant.agents", { n: String(targets.size) })}>
            <CheckList
              items={agents.map((a) => ({ key: a, label: a }))}
              selected={targets}
              onToggle={(k) => toggle(targets, k, setTargets)}
              placeholder={t("access.grant.searchAgents")}
              emptyText={t("access.grant.noMatch")}
            />
          </Field>
        </div>

        <div className="admin-form-row" style={{ marginTop: 16 }}>
          <Field label={t("access.col.role")} style={{ flex: "0 1 170px" }}>
            <select
              className="input"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              <option value="member">{t("access.role.user")}</option>
              <option value="owner">{t("access.role.owner")}</option>
            </select>
          </Field>
          <div className="admin-form-actions" style={{ marginLeft: "auto" }}>
            <button className="btn" onClick={onClose}>
              {t("access.grant.cancel")}
            </button>
            <button
              className="btn primary"
              disabled={busy || people.size === 0 || targets.size === 0}
              onClick={() => void submit()}
            >
              <UserPlusIcon size={15} />
              {/* "Grant to 0 × 0" reads like a bug on the empty state, which is
                  what the dialog opens in. Only show the arithmetic once both
                  sides have a selection to multiply. */}
              {people.size && targets.size
                ? t("access.grant.submit", {
                    people: String(people.size),
                    agents: String(targets.size),
                  })
                : t("access.btn.grant")}
            </button>
          </div>
        </div>

        {pickedAdmins.length > 0 && (
          <p className="muted access-note" style={{ marginBottom: 0 }}>
            <AdminIcon size={13} /> {t("access.grant.adminNote", { n: String(pickedAdmins.length) })}
          </p>
        )}
      </div>
    </div>
  );
}

// The expanded row: the editable list for one agent, split by role.
function DrillIn({
  owners,
  members,
  onRole,
  onRevoke,
  onAdd,
}: {
  owners: User[];
  members: User[];
  onRole: (u: User, role: Role) => void;
  onRevoke: (u: User) => void;
  onAdd: () => void;
}) {
  const t = useT(STR);
  const [q, setQ] = useState("");
  const total = owners.length + members.length;

  function group(label: string, list: User[], role: Role) {
    const shown = list.filter((u) => matches(u, q));
    if (list.length === 0 && q) return null;
    return (
      <div className="access-group">
        <div className="access-group-head">
          {label} <span className="badge">{list.length}</span>
        </div>
        {list.length === 0 && <div className="muted access-empty">{t("access.group.none")}</div>}
        {/* Non-empty group, but the filter matched no one in it — say so
            instead of leaving the header dangling over nothing. */}
        {list.length > 0 && shown.length === 0 && (
          <div className="muted access-empty">{t("access.grant.noMatch")}</div>
        )}
        {shown.map((u) => (
          <div key={u.id} className="access-person">
            <Person user={u} />
            <div className="row" style={{ gap: 8 }}>
              <select
                className="input access-role-select"
                value={role}
                aria-label={t("access.col.role")}
                onChange={(e) => onRole(u, e.target.value as Role)}
              >
                <option value="member">{t("access.role.user")}</option>
                <option value="owner">{t("access.role.owner")}</option>
              </select>
              <button
                className="icon-action danger"
                title={t("access.btn.revoke")}
                aria-label={t("access.btn.revoke")}
                onClick={() => onRevoke(u)}
              >
                <TrashIcon size={16} />
              </button>
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="access-drill">
      <div className="row access-drill-head">
        {total > FILTER_AT ? (
          <div className="picker-search" style={{ flex: "1 1 200px", maxWidth: 300 }}>
            <SearchIcon size={14} />
            <input
              className="picker-search-input"
              placeholder={t("access.drill.filter")}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        ) : (
          <span />
        )}
        <button className="btn access-add-btn" onClick={onAdd}>
          <UserPlusIcon size={15} />
          {t("access.drill.add")}
        </button>
      </div>
      {group(t("access.col.owners"), owners, "owner")}
      {group(t("access.col.users"), members, "member")}
    </div>
  );
}

export function AccessSection() {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [data, setData] = useState<AccessOverview | null>(null);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [grant, setGrant] = useState<GrantPreset | null>(null);

  const load = useCallback(() => {
    api
      .get<AccessOverview>("/api/admin/access-overview")
      .then(setData)
      .catch(() => toast.push(t("access.toast.loadFailed"), "error"));
  }, [toast, t]);
  useEffect(load, [load]);

  const byId = useMemo(() => {
    const m = new Map<string, User>();
    for (const u of data?.users ?? []) m.set(u.id, u);
    return m;
  }, [data]);

  const adminIds = useMemo(() => new Set(data?.admin_ids ?? []), [data]);
  const admins = useMemo(
    () => (data?.admin_ids ?? []).map((id) => byId.get(id)).filter((u): u is User => !!u).sort(byName),
    [data, byId],
  );

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.agents ?? [])
      .filter((a) => !needle || a.name.toLowerCase().includes(needle))
      .map((a) => ({
        name: a.name,
        owners: a.owners.map((id) => byId.get(id)).filter((u): u is User => !!u),
        users: a.users.map((id) => byId.get(id)).filter((u): u is User => !!u),
      }));
  }, [data, byId, q]);

  async function setRole(agent: string, u: User, role: Role) {
    try {
      await api.post(`/api/agents/${agent}/access`, { user_id: u.id, role });
      load();
    } catch {
      toast.push(t("access.toast.grantFailed"), "error");
    }
  }

  async function revoke(agent: string, u: User) {
    const ok = await confirm({
      title: t("access.confirm.title"),
      message: t("access.confirm.revoke", { name: u.name || u.email, agent }),
      confirmLabel: t("access.btn.revoke"),
      cancelLabel: t("users.confirm.cancel"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/agents/${agent}/access/${u.id}`);
      load();
    } catch {
      toast.push(t("access.toast.revokeFailed"), "error");
    }
  }

  const agentNames = (data?.agents ?? []).map((a) => a.name);

  return (
    <section className="card" style={{ padding: 18, marginTop: 16 }}>
      <SectionHead
        title={t("access.title")}
        subtitle={t("access.subtitle")}
        right={
          <button
            className="btn primary"
            onClick={() => setGrant({ agent: null, role: "member" })}
          >
            <UserPlusIcon size={15} />
            {t("access.btn.grant")}
          </button>
        }
      />

      {/* Implicit owners, stated once. Repeating these on every row would be
          the same faces N times over and hide the grants that actually vary.
          A fresh deployment has exactly one admin, so the singular phrasing is
          the common case, not the edge. */}
      {admins.length > 0 && (
        <div className="access-banner">
          <AdminIcon size={14} />
          <span>
            {admins.length === 1
              ? t("access.adminOwnsAll")
              : t("access.adminsOwnAll", { n: String(admins.length) })}
          </span>
          <AvatarStack users={admins} max={5} />
        </div>
      )}

      {agentNames.length > FILTER_AT && (
        <div className="picker-search" style={{ marginTop: 12, maxWidth: 320 }}>
          <SearchIcon size={14} />
          <input
            className="picker-search-input"
            placeholder={t("access.searchAgents")}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      )}

      <table className="table admin-table access-table" style={{ marginTop: 12 }}>
        <thead>
          <tr>
            <th>{t("access.col.agent")}</th>
            <th>{t("access.col.owners")}</th>
            <th>{t("access.col.users")}</th>
            <th style={{ width: 1 }}></th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={4} className="muted" style={{ padding: "20px 12px", textAlign: "center" }}>
                {data === null ? t("access.loading") : t("access.empty")}
              </td>
            </tr>
          )}
          {rows.map((r) => {
            const isOpen = open === r.name;
            return (
              <Fragment key={r.name}>
                <tr
                  className={isOpen ? "access-row open" : "access-row"}
                  onClick={() => setOpen(isOpen ? null : r.name)}
                >
                  <td data-label={t("access.col.agent")}>
                    {/* A real button inside the row: the tr's onClick is the
                        mouse target, this is the keyboard/screen-reader one —
                        a bare tr is not focusable. stopPropagation so one
                        keyboard-triggered click doesn't toggle twice. */}
                    <button
                      type="button"
                      className="access-row-toggle"
                      aria-expanded={isOpen}
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpen(isOpen ? null : r.name);
                      }}
                    >
                      <span className={isOpen ? "access-caret open" : "access-caret"}>
                        <ChevronDownIcon size={14} />
                      </span>
                      <span style={{ fontWeight: 500 }}>{r.name}</span>
                    </button>
                  </td>
                  <td data-label={t("access.col.owners")}>
                    {r.owners.length === 0 ? (
                      <button
                        className="link-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          setGrant({ agent: r.name, role: "owner" });
                        }}
                      >
                        {t("access.setOwner")}
                      </button>
                    ) : (
                      <div className="row" style={{ gap: 8 }}>
                        <AvatarStack users={r.owners} />
                        <span className="muted access-count">{r.owners.length}</span>
                      </div>
                    )}
                  </td>
                  <td data-label={t("access.col.users")}>
                    <div className="row" style={{ gap: 8 }}>
                      <AvatarStack users={r.users} />
                      {r.users.length > 0 && (
                        <span className="muted access-count">{r.users.length}</span>
                      )}
                    </div>
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      className="icon-action"
                      aria-label={t("access.drill.add")}
                      title={t("access.drill.add")}
                      onClick={(e) => {
                        e.stopPropagation();
                        setGrant({ agent: r.name, role: "member" });
                      }}
                    >
                      <UserPlusIcon size={16} />
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="access-drill-row">
                    <td colSpan={4}>
                      <DrillIn
                        owners={r.owners}
                        members={r.users}
                        onRole={(u, role) => void setRole(r.name, u, role)}
                        onRevoke={(u) => void revoke(r.name, u)}
                        onAdd={() => setGrant({ agent: r.name, role: "member" })}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>

      {grant && (
        <GrantModal
          users={[...(data?.users ?? [])].sort(byName)}
          agents={agentNames}
          adminIds={adminIds}
          preset={grant}
          onClose={() => setGrant(null)}
          onDone={load}
        />
      )}
    </section>
  );
}
