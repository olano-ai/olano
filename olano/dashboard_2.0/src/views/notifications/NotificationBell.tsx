// The bell: a top-level, always-visible entry point for anything needing a
// human. It sits next to the theme toggle on desktop and in the mobile topbar,
// so an alert is one glance away from every tab — the Overview section is the
// full list, this is the interrupt.

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bell } from "lucide-react";
import { Link } from "react-router-dom";
import { useAnchor } from "../../ui/anchoredMenu";
import { useT, type LocalDict } from "../../core/i18n";
import { NotificationRow } from "./NotificationsSection";
import { markRead, useNotifications } from "./notificationsStore";

const STR: LocalDict = {
  en: {
    "title": "Notifications",
    "empty": "Nothing needs your attention.",
    "viewAll": "View all",
    "open": "Notifications ({n} unread)",
    "openNone": "Notifications",
  },
  zh: {
    "title": "通知",
    "empty": "没有需要处理的事项。",
    "viewAll": "查看全部",
    "open": "通知（{n} 条未读）",
    "openNone": "通知",
  },
  es: {
    "title": "Notificaciones",
    "empty": "No hay nada pendiente.",
    "viewAll": "Ver todas",
    "open": "Notificaciones ({n} sin leer)",
    "openNone": "Notificaciones",
  },
};

const PANEL_LIMIT = 6;
const PANEL_WIDTH = 360;

export function NotificationBell() {
  const t = useT(STR);
  const { notifications, unread, level } = useNotifications();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // The panel is portaled to <body> and positioned from the bell's own rect.
  // It has to be: this bell sits at the left edge of the desktop sidebar, the
  // right edge of the mobile topbar, and — in the simple shell — inside a rail
  // that collapses to a 78px strip with `overflow: hidden`, which sliced an
  // in-flow panel off at the strip's edge. One measured anchor covers all three
  // and clamps the panel into the viewport at every one of them.
  const { rect } = useAnchor(ref, open, "below", PANEL_WIDTH);

  // Dismiss on outside click / Escape — same behavior as the profile menu.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      const target = e.target as Node;
      // The panel lives outside this component's wrapper now, so it has to be
      // excluded explicitly or the first click inside it closes.
      if (ref.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Opening the panel is the read receipt: the user has now seen them, so the
  // badge clears while the rows stay listed.
  const toggle = () => {
    setOpen((wasOpen) => {
      if (!wasOpen && unread > 0) void markRead().catch(() => undefined);
      return !wasOpen;
    });
  };

  const tone =
    level === "error" ? "var(--danger)" : level === "warn" ? "var(--warn)" : "var(--accent)";

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-flex" }}>
      <button
        className="btn ghost sidebar-icon-btn notification-bell"
        onClick={toggle}
        aria-label={unread > 0 ? t("open", { n: String(unread) }) : t("openNone")}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={unread > 0 ? t("open", { n: String(unread) }) : t("openNone")}
        style={{ position: "relative" }}
      >
        <Bell size={17} strokeWidth={1.75} />
        {unread > 0 && (
          <span
            aria-hidden
            style={{
              position: "absolute",
              top: 2,
              right: 2,
              minWidth: 15,
              height: 15,
              padding: "0 3px",
              borderRadius: 8,
              background: tone,
              color: "#fff",
              fontSize: 9.5,
              fontWeight: 700,
              lineHeight: "15px",
              textAlign: "center",
            }}
          >
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open &&
        rect &&
        createPortal(
          <div
            ref={panelRef}
            className="menu notification-panel"
            role="dialog"
            aria-label={t("title")}
            style={{
              position: "fixed",
              left: rect.left,
              top: rect.top,
              width: rect.width,
              maxHeight: "min(60vh, 420px)",
              overflowY: "auto",
              padding: 8,
            }}
          >
            <div className="menu-head" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontWeight: 600 }}>{t("title")}</span>
              <span style={{ flex: 1 }} />
              <Link
                to="/overview"
                onClick={() => setOpen(false)}
                className="muted"
                style={{ fontSize: 12 }}
              >
                {t("viewAll")}
              </Link>
            </div>
            {notifications.length === 0 ? (
              <div className="muted" style={{ padding: "12px 10px", fontSize: 12.5 }}>
                {t("empty")}
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: 4 }}>
                {notifications.slice(0, PANEL_LIMIT).map((item) => (
                  <NotificationRow
                    key={item.id}
                    item={item}
                    compact
                    // Following a row navigates away underneath the panel; leaving
                    // it open would cover the page it just opened.
                    onNavigate={() => setOpen(false)}
                  />
                ))}
              </div>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
