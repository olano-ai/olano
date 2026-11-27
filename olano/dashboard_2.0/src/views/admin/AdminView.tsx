import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type FormEvent,
} from "react";
import { QRCodeSVG } from "qrcode.react";
import { usePersistentState } from "../../core/usePersistentState";
import { api, ApiError } from "../../core/api";
import { refreshCapabilities } from "../../core/capabilities";
import { useT } from "../../core/i18n";
import { useAuth } from "../../core/auth";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import {
  AdminIcon,
  BanIcon,
  CopyIcon,
  KeyIcon,
  MailIcon,
  MemberIcon,
  MoreIcon,
  TerminalIcon,
  TrashIcon,
  UserPlusIcon,
} from "../../ui/icons";
import type { User } from "../../core/types";
import { Toggle } from "../agents/Toggle";
import { AccessSection } from "./AccessSection";
import { DeploymentSection } from "./DeploymentSection";
import { Field, Person, SectionHead } from "./shared";
import { HandleField, slugifyHandle } from "../../ui/HandleField";
import { STR } from "./strings";

interface InvitePayload {
  token: string;
  path: string;
  expires_at: string | null;
}
interface CreateUserResponse {
  user: User;
  invite: InvitePayload | null;
}

export function AdminView() {
  const t = useT(STR);
  // Bumped whenever the people list changes (created, invited, edited,
  // removed). The access overview has its own fetch, and it used to keep the
  // roster it loaded on mount, so a person invited a moment ago was missing
  // from its Grant access dialog until the page was reloaded.
  const [peopleRevision, setPeopleRevision] = useState(0);
  const peopleChanged = useCallback(() => setPeopleRevision((n) => n + 1), []);
  return (
    <>
      <div className="topbar">
        <BrandWordmark />
      </div>
      <div className="page" style={{ maxWidth: 920 }}>
        <h1>{t("page.title")}</h1>
        <p className="muted" style={{ margin: "2px 0 0" }}>
          {t("page.subtitle")}
        </p>
        <UsersSection onChanged={peopleChanged} />
        <AccessSection revision={peopleRevision} />
        {/* Rendered again. It was hidden on the reasoning that plan limits were
            "practically unlimited" — and then one bit: an agent refused every
            turn against a monthly token ceiling nothing on the box named, warned
            about, or could move. A cap that can stop the product has to be
            visible where its usage is. (The token ceilings are editable in
            Config → Usage budgets; this card is the read-only posture view.) */}
        <DeploymentSection />
      </div>
    </>
  );
}

function StatusBadge({ active, t }: { active: boolean; t: (k: string) => string }) {
  const color = active ? "var(--success)" : "var(--warn)";
  return (
    <span
      className="badge"
      style={{ gap: 6, background: active ? "var(--success-soft)" : "var(--warn-soft)", color }}
    >
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: "currentColor" }} />
      {active ? t("users.status.active") : t("users.status.pending")}
    </span>
  );
}

interface RowAction {
  icon: ComponentType<{ size?: number | string }>;
  label: string;
  onClick: () => void;
  danger?: boolean;
}

// Per-row "⋮" overflow menu — replaces the old wall of inline buttons. Manages
// its own open state and closes on outside click / action.
function RowActions({ actions, label }: { actions: RowAction[]; label: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-flex" }}>
      <button
        className="icon-action"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <MoreIcon size={17} />
      </button>
      {open && (
        <div className="menu" style={{ top: "100%", right: 0, marginTop: 6, minWidth: 188 }} role="menu">
          {actions.map((a) => {
            const Icon = a.icon;
            return (
              <button
                key={a.label}
                className={a.danger ? "menu-item danger" : "menu-item"}
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  a.onClick();
                }}
              >
                <Icon size={15} />
                {a.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// No z override — .modal-overlay's tokenized --z-modal keeps this in the same
// layer as every other dialog (a stale hardcoded 40 sat below them).
const overlayStyle = {} as const;

function InviteModal({
  invite,
  kind,
  onClose,
}: {
  invite: InvitePayload;
  kind: "invite" | "reset";
  onClose: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const url = window.location.origin + invite.path;
  return (
    <div className="modal-overlay" style={overlayStyle} onClick={onClose}>
      <div
        className="card"
        style={{ padding: 22, width: 420, maxWidth: "90vw", boxShadow: "var(--shadow-lg)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 style={{ marginTop: 0, fontSize: 16 }}>
          {kind === "reset" ? t("modal.reset.title") : t("modal.invite.title")}
        </h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {kind === "reset" ? t("modal.share.reset") : t("modal.share.invite")}
          {invite.expires_at &&
            t("modal.expires", { date: new Date(invite.expires_at).toLocaleDateString() })}
        </p>
        <div
          style={{ display: "flex", justifyContent: "center", padding: 14, background: "#fff", borderRadius: 8 }}
        >
          <QRCodeSVG value={url} size={168} />
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <input
            className="input"
            readOnly
            value={url}
            onFocus={(e) => e.target.select()}
            style={{ fontSize: 12 }}
          />
          <button
            className="btn primary"
            onClick={() => {
              void navigator.clipboard?.writeText(url);
              toast.push(t("toast.linkCopied"));
            }}
          >
            <CopyIcon size={15} />
            {t("modal.copy")}
          </button>
        </div>
        <div className="row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
          <button className="btn" onClick={onClose}>
            {t("modal.done")}
          </button>
        </div>
      </div>
    </div>
  );
}

// Inline editor for a person's title: commits on blur/Enter, only when the
// value actually changed, so tabbing through the table never fires a PATCH.
function TitleCell({
  value,
  ariaLabel,
  placeholder,
  onCommit,
}: {
  value: string;
  ariaLabel: string;
  placeholder: string;
  onCommit: (next: string) => void;
}) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  const commit = () => {
    if (v.trim() !== value.trim()) onCommit(v.trim());
  };
  return (
    <input
      className="input"
      style={{ padding: "4px 6px", fontSize: 12, minWidth: 120 }}
      value={v}
      maxLength={80}
      aria-label={ariaLabel}
      placeholder={placeholder}
      onChange={(e) => setV(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

function UsersSection({ onChanged }: { onChanged?: () => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const t = useT(STR);
  const auth = useAuth();
  const [users, setUsers] = useState<User[]>([]);
  const [email, setEmail] = usePersistentState("admin.newUserEmail", "");
  const [name, setName] = usePersistentState("admin.newUserName", "");
  const [username, setUsername] = usePersistentState("admin.newUserUsername", "");
  const [title, setTitle] = usePersistentState("admin.newUserTitle", "");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = usePersistentState("admin.newUserIsAdmin", false);
  const [linkInfo, setLinkInfo] = useState<{ payload: InvitePayload; kind: "invite" | "reset" } | null>(
    null,
  );

  const load = useCallback(() => {
    api
      .get<User[]>("/api/admin/users")
      .then(setUsers)
      .catch(() => toast.push(t("users.toast.loadFailed"), "error"));
  }, [toast, t]);
  useEffect(load, [load]);
  // After a change to the people list: refresh it here and tell the access
  // overview, which reads the same people.
  const afterChange = useCallback(() => {
    load();
    onChanged?.();
  }, [load, onChanged]);

  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      const res = await api.post<CreateUserResponse>("/api/admin/users", {
        email,
        password: password || undefined, // blank → invite flow
        name,
        username, // blank → server mints one from `name` / the email local-part
        title: title.trim(),
        is_admin: isAdmin,
      });
      setEmail("");
      setName("");
      setUsername("");
      setTitle("");
      setPassword("");
      setIsAdmin(false);
      afterChange();
      if (res.invite) {
        setLinkInfo({ payload: res.invite, kind: "invite" });
      } else {
        toast.push(t("users.toast.created"));
      }
    } catch (err) {
      toast.push(
        err instanceof ApiError && err.detail === "email_exists"
          ? t("users.toast.emailExists")
          : err instanceof ApiError && err.detail === "username_exists"
            ? t("users.toast.usernameExists")
            : t("users.toast.createFailed"),
        "error",
      );
    }
  }

  async function resend(u: User) {
    try {
      const res = await api.post<{ invite: InvitePayload }>(`/api/admin/users/${u.id}/invite`);
      setLinkInfo({ payload: res.invite, kind: "invite" });
    } catch {
      toast.push(t("users.toast.inviteFailed"), "error");
    }
  }

  async function resetPassword(u: User) {
    try {
      const res = await api.post<{ reset: InvitePayload }>(`/api/admin/users/${u.id}/reset`);
      setLinkInfo({ payload: res.reset, kind: "reset" });
    } catch {
      toast.push(t("users.toast.resetFailed"), "error");
    }
  }

  async function patch(u: User, body: Record<string, unknown>) {
    try {
      await api.patch(`/api/admin/users/${u.id}`, body);
      if ("can_terminal" in body || "is_admin" in body || "is_active" in body) refreshCapabilities();
      // ui_mode decides which whole shell renders, and it is delivered on
      // /api/auth/me rather than in capabilities. An admin switching their OWN
      // interface would otherwise stay in the old one until a manual reload.
      // Langfuse links ride /api/auth/me the same way.
      if (("ui_mode" in body || "langfuse_links" in body) && u.id === auth.user?.id) void auth.refresh();
      afterChange();
    } catch (err) {
      toast.push(
        err instanceof ApiError && err.detail === "last_admin"
          ? t("users.toast.lastAdminDemote")
          : t("users.toast.updateFailed"),
        "error",
      );
    }
  }

  async function remove(u: User) {
    const ok = await confirm({
      title: t("users.confirm.title"),
      message: t("users.confirm.delete", { email: u.email }),
      confirmLabel: t("users.btn.delete"),
      cancelLabel: t("users.confirm.cancel"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/admin/users/${u.id}`);
      afterChange();
    } catch (err) {
      toast.push(
        err instanceof ApiError && err.detail === "last_admin"
          ? t("users.toast.lastAdminDelete")
          : t("users.toast.deleteFailed"),
        "error",
      );
    }
  }

  function actionsFor(u: User): RowAction[] {
    return [
      u.is_active
        ? { icon: KeyIcon, label: t("users.btn.resetPassword"), onClick: () => void resetPassword(u) }
        : { icon: MailIcon, label: t("users.btn.inviteLink"), onClick: () => void resend(u) },
      {
        icon: u.is_admin ? MemberIcon : AdminIcon,
        label: u.is_admin ? t("users.btn.makeMember") : t("users.btn.makeAdmin"),
        onClick: () => void patch(u, { is_admin: !u.is_admin }),
      },
      ...(u.is_active
        ? [{ icon: BanIcon, label: t("users.btn.disable"), onClick: () => void patch(u, { is_active: false }) }]
        : []),
      { icon: TrashIcon, label: t("users.btn.delete"), onClick: () => void remove(u), danger: true },
    ];
  }

  return (
    <section className="card" style={{ padding: 18, marginTop: 16 }}>
      <SectionHead
        title={t("users.title")}
        subtitle={t("users.subtitle")}
        right={users.length > 0 && <span className="badge">{users.length}</span>}
      />

      <table className="table admin-table" style={{ marginTop: 12 }}>
        <thead>
          <tr>
            <th>{t("users.col.user")}</th>
            <th>{t("users.col.title")}</th>
            <th>{t("users.col.role")}</th>
            <th>{t("users.col.status")}</th>
            <th>{t("users.col.terminal")}</th>
            <th>{t("users.col.uiMode")}</th>
            <th>{t("users.col.langfuse")}</th>
            <th style={{ width: 1 }}></th>
          </tr>
        </thead>
        <tbody>
          {users.length === 0 && (
            <tr>
              <td colSpan={8} className="muted" style={{ padding: "20px 12px", textAlign: "center" }}>
                {t("users.empty")}
              </td>
            </tr>
          )}
          {users.map((u) => (
            <tr key={u.id}>
              <td>
                <Person user={u} showTitle={false} />
              </td>
              <td>
                <TitleCell
                  value={u.title || ""}
                  ariaLabel={t("users.title.aria", { name: u.name || u.email })}
                  placeholder={t("users.placeholder.title")}
                  onCommit={(next) => void patch(u, { title: next })}
                />
              </td>
              <td>
                {u.is_admin ? (
                  <span className="badge admin">{t("users.role.admin")}</span>
                ) : (
                  <span className="badge">{t("users.role.member")}</span>
                )}
              </td>
              <td>
                <StatusBadge active={u.is_active} t={t} />
              </td>
              <td>
                {u.is_admin ? (
                  <span className="row" style={{ gap: 6, color: "var(--text-3)", fontSize: 12 }} title={t("users.terminalAdmin")}>
                    <TerminalIcon size={13} /> {t("users.terminalAlways")}
                  </span>
                ) : (
                  <Toggle checked={!!u.can_terminal} onChange={() => void patch(u, { can_terminal: !u.can_terminal })} />
                )}
              </td>
              <td>
                <select
                  className="input"
                  style={{ padding: "4px 6px", fontSize: 12 }}
                  value={u.ui_mode === "simple" ? "simple" : "advanced"}
                  aria-label={t("users.uiMode.aria")}
                  title={t("users.uiModeHelp")}
                  onChange={(e) => void patch(u, { ui_mode: e.target.value })}
                >
                  <option value="advanced">{t("users.uiMode.advanced")}</option>
                  <option value="simple">{t("users.uiMode.simple")}</option>
                </select>
              </td>
              <td>
                {u.is_admin ? (
                  <span data-testid="user-langfuse-links">
                    <Toggle
                      checked={!!u.langfuse_links}
                      title={t("users.langfuseHelp")}
                      ariaLabel={t("users.langfuse.aria")}
                      onChange={() => void patch(u, { langfuse_links: !u.langfuse_links })}
                    />
                  </span>
                ) : (
                  <span className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }} title={t("users.langfuseMember")}>
                    {t("users.langfuseAdminsOnly")}
                  </span>
                )}
              </td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <RowActions actions={actionsFor(u)} label={t("users.actions")} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="muted admin-form-hint" style={{ marginTop: 10 }}>
        {t("users.uiModeHelp")}
      </p>
      <p className="muted admin-form-hint" style={{ marginTop: 6 }}>
        {t("users.langfuseHelp")}
      </p>

      <div className="admin-form">
        <h3 className="admin-form-title">{t("users.invite.title")}</h3>
        <p className="muted admin-form-hint">{t("users.invite.hint")}</p>
        <form onSubmit={create} className="admin-form-row">
          <Field label={t("users.col.name")} style={{ flex: "1 1 150px" }}>
            <input
              className="input"
              placeholder={t("users.placeholder.name")}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field
            label={t("users.col.username")}
            hint={t("users.label.optional")}
            style={{ flex: "1 1 150px" }}
          >
            <HandleField
              value={username}
              onChange={setUsername}
              placeholder={slugifyHandle(name) || t("users.placeholder.username")}
              messages={{
                available: t("users.handle.available"),
                taken: t("users.handle.taken"),
                savedAs: t("users.handle.savedAs"),
                checking: t("users.handle.checking"),
              }}
            />
          </Field>
          <Field label={t("users.col.title")} hint={t("users.label.optional")} style={{ flex: "1 1 150px" }}>
            <input
              className="input"
              placeholder={t("users.placeholder.title")}
              value={title}
              maxLength={80}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label={t("users.col.email")} style={{ flex: "2 1 220px" }}>
            <input
              className="input"
              type="email"
              placeholder={t("users.placeholder.email")}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field
            label={t("users.label.password")}
            hint={t("users.label.optional")}
            style={{ flex: "1 1 180px" }}
          >
            <input
              className="input"
              type="password"
              placeholder={t("users.placeholder.passwordOpt")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <div className="admin-form-actions">
            <label className="admin-check">
              <input
                type="checkbox"
                checked={isAdmin}
                onChange={(e) => setIsAdmin(e.target.checked)}
              />
              {t("users.label.admin")}
            </label>
            <button className="btn primary" disabled={!email}>
              <UserPlusIcon size={15} />
              {password ? t("users.btn.addUser") : t("users.btn.sendInvite")}
            </button>
          </div>
        </form>
        {/* Full-width: a paragraph inside the Handle column would render as a
            twenty-line ribbon. Named so it stays attached to its field. */}
        <p className="admin-form-note">
          <strong>{t("users.col.username")}</strong> — {t("users.hint.username")}
        </p>
        <p className="admin-form-note">
          <strong>{t("users.col.title")}</strong> — {t("users.hint.title")}
        </p>
      </div>

      {linkInfo && (
        <InviteModal
          invite={linkInfo.payload}
          kind={linkInfo.kind}
          onClose={() => setLinkInfo(null)}
        />
      )}
    </section>
  );
}
