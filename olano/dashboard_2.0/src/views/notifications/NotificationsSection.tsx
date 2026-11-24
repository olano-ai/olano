// Overview "Notifications" section — everything the deployment needs a human
// for, in one list. Always rendered (like Approvals and Recent activity) so it
// has a stable home; the sidebar bell carries the same count app-wide.

import type { CSSProperties, ReactNode } from "react";
import {
  AlertTriangle,
  Bell,
  Check,
  ChevronRight,
  CircleAlert,
  Info,
  Trash2,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useT, type LocalDict } from "../../core/i18n";
import { absoluteTimestamp, formatRelativeTime, useRelativeNow } from "../../core/relativeTime";
import { useToast } from "../../ui/toast";
import {
  clearRead,
  dismissNotification,
  markRead,
  useNotifications,
  type NotificationLevel,
  type OperatorNotification,
} from "./notificationsStore";

const STR: LocalDict = {
  en: {
    "title": "Notifications",
    "subtitle": "Things this deployment needs you to look at.",
    "empty": "Nothing needs your attention.",
    "markAll": "Mark all read",
    "clearRead": "Clear read",
    "dismiss": "Dismiss",
    "repeated": "seen {n} times",
    "when.last": "Last seen {at}",
    "when.first": "First seen {at}",
    "err.action": "Could not update the notification.",
  },
  zh: {
    "title": "通知",
    "subtitle": "本部署需要你处理的事项。",
    "empty": "没有需要处理的事项。",
    "markAll": "全部标为已读",
    "clearRead": "清除已读",
    "dismiss": "忽略",
    "repeated": "出现 {n} 次",
    "when.last": "最近一次 {at}",
    "when.first": "首次出现 {at}",
    "err.action": "无法更新通知。",
  },
  es: {
    "title": "Notificaciones",
    "subtitle": "Cosas que este despliegue necesita que revises.",
    "empty": "No hay nada pendiente.",
    "markAll": "Marcar todo como leído",
    "clearRead": "Borrar leídas",
    "dismiss": "Descartar",
    "repeated": "vista {n} veces",
    "when.last": "Última vez {at}",
    "when.first": "Primera vez {at}",
    "err.action": "No se pudo actualizar la notificación.",
  },
};

const LEVEL_COLOR: Record<NotificationLevel, string> = {
  error: "var(--danger)",
  warn: "var(--warn)",
  success: "var(--success)",
  info: "var(--text-3)",
};

export function LevelIcon({ level, size = 15 }: { level: NotificationLevel; size?: number }) {
  const Icon = level === "error" ? CircleAlert : level === "warn" ? AlertTriangle : Info;
  return <Icon size={size} strokeWidth={1.75} color={LEVEL_COLOR[level]} />;
}

/** One row, shared by the Overview list and the bell panel. A row carrying an
 *  action is a link over its whole card: "Issue #10 assigned to jamie-rivera"
 *  is only useful if it opens issue #10, and a small button inside an otherwise
 *  inert card left the operator hunting for it in the Issues list by hand. */
export function NotificationRow({
  item,
  compact = false,
  onNavigate,
}: {
  item: OperatorNotification;
  compact?: boolean;
  /** Called after a click that navigates — the bell uses it to close its panel. */
  onNavigate?: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const now = useRelativeNow();
  const dismiss = () =>
    void dismissNotification(item.id).catch(() => toast.push(t("err.action"), "error"));

  const href = item.action?.href ?? null;

  // `updated_at`, not `created_at`: on a deduplicated row those differ, and the
  // question an operator is asking of a *seen 3 times* alert is when it last
  // happened, not when the first one did. The tooltip keeps both — knowing a
  // recurring failure started three days ago is exactly the context the badge
  // implies but cannot itself name.
  const when = formatRelativeTime(item.updated_at, t, now);
  const lastAt = absoluteTimestamp(item.updated_at);
  const firstAt = absoluteTimestamp(item.created_at);
  const stamp =
    item.count > 1 && firstAt && firstAt !== lastAt
      ? `${t("when.last", { at: lastAt })}\n${t("when.first", { at: firstAt })}`
      : lastAt;

  const content: ReactNode = (
    <>
      <span style={{ paddingTop: 2 }}>
        <LevelIcon level={item.level} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span
            className="notification-row-title"
            style={{ fontWeight: item.read ? 500 : 600, fontSize: compact ? 13 : 14 }}
          >
            {item.title}
          </span>
          {item.agent && (
            <span className="muted" style={{ fontSize: 12 }}>
              {item.agent}
            </span>
          )}
          {item.count > 1 && (
            <span className="badge" style={{ padding: "0 6px" }}>
              {t("repeated", { n: String(item.count) })}
            </span>
          )}
          {when && (
            // Last in the header's wrapping row rather than pinned right: these
            // titles run long and wrap at both viewports, and an auto-margin
            // would strand the stamp at the far edge of a half-empty line.
            <time className="notification-row-time" dateTime={item.updated_at} title={stamp}>
              {when}
            </time>
          )}
        </div>
        {item.body && (
          <div
            className="muted"
            style={{ fontSize: 12.5, marginTop: 3, overflowWrap: "anywhere" }}
          >
            {item.body}
          </div>
        )}
        {item.action && (
          // The row is the target now, so this reads as its label rather than a
          // second, competing control — a <button> inside the <a> would also be
          // invalid markup.
          <span className="notification-row-action">
            {item.action.label}
            <ChevronRight size={13} strokeWidth={2} aria-hidden />
          </span>
        )}
      </div>
    </>
  );

  return (
    <div
      className={`card notification-row${href ? " linked" : ""}`}
      style={
        {
          // Unread is the state that matters: an accent bar reads at a glance
          // without changing the row's height between states.
          borderLeft: `3px solid ${item.read ? "transparent" : LEVEL_COLOR[item.level]}`,
          // The link owns the padding so its hit area is the whole row, not an
          // inset patch of it; the dismiss button re-uses the value as margin.
          "--nrow-pad": compact ? "10px 12px" : "14px",
        } as CSSProperties
      }
    >
      {href ? (
        <Link
          to={href}
          className="notification-row-main"
          onClick={() => {
            // Following it is the read receipt — the Overview list has no other
            // one, and a row you just acted on must not keep the badge lit.
            if (!item.read) void markRead([item.id]).catch(() => undefined);
            onNavigate?.();
          }}
        >
          {content}
        </Link>
      ) : (
        <div className="notification-row-main">{content}</div>
      )}
      <button
        className="btn ghost notification-row-dismiss"
        onClick={dismiss}
        aria-label={t("dismiss")}
        title={t("dismiss")}
        style={{ padding: 4, lineHeight: 0 }}
      >
        <X size={14} strokeWidth={1.75} />
      </button>
    </div>
  );
}

export function NotificationsSection() {
  const t = useT(STR);
  const { notifications, unread, total } = useNotifications();
  const toast = useToast();
  const fail = () => toast.push(t("err.action"), "error");

  return (
    <section>
      <div className="row" style={{ gap: 8, marginTop: 28, alignItems: "center" }}>
        <h2 style={{ fontSize: 16, margin: 0 }}>{t("title")}</h2>
        {unread > 0 && <span className="badge">{unread}</span>}
        <span style={{ flex: 1 }} />
        {unread > 0 && (
          <button
            className="btn ghost"
            style={{ padding: "3px 10px", fontSize: 12.5 }}
            onClick={() => void markRead().catch(fail)}
          >
            <Check size={14} /> {t("markAll")}
          </button>
        )}
        {total > unread && (
          <button
            className="btn ghost"
            style={{ padding: "3px 10px", fontSize: 12.5 }}
            onClick={() => void clearRead().catch(fail)}
          >
            <Trash2 size={14} /> {t("clearRead")}
          </button>
        )}
      </div>
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 12.5 }}>
        {t("subtitle")}
      </p>
      {notifications.length === 0 ? (
        <div
          className="muted"
          style={{ display: "flex", alignItems: "center", gap: 8, padding: "16px 0" }}
        >
          <Bell size={15} /> {t("empty")}
        </div>
      ) : (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            marginTop: 12,
            maxWidth: 760,
          }}
        >
          {notifications.map((item) => (
            <NotificationRow key={item.id} item={item} />
          ))}
        </div>
      )}
    </section>
  );
}
