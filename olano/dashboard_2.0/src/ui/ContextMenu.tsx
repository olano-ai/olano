// A right-click menu, positioned at the pointer and clamped to the viewport.
//
// Lifted out of the Files view, which grew it for its tree rows, the day the
// sidebar's bot list needed one too: two hand-rolled copies of "clamp to the
// window, close on Escape/resize/outside click, one row per action" is one
// too many. Presentation only — the caller decides what the rows do and owns
// the open/closed state (`menu` is null when closed).
//
// Rows can be disabled with a reason: a menu that simply hides "Delete" on a
// non-empty group leaves the reader guessing why the option comes and goes,
// where a greyed row with a tooltip says exactly what has to happen first.

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  danger?: boolean;
  /** Draw a thin separator line above this item (groups destructive actions). */
  separatorBefore?: boolean;
  /** Greyed out and inert; pair it with `title` so the reader learns why. */
  disabled?: boolean;
  /** Tooltip — on a disabled row, the reason it is disabled. */
  title?: string;
  onClick: () => void;
}

export interface MenuState {
  x: number;
  y: number;
  items: MenuItem[];
  /** Accessible name for the menu ("Actions for merli"). */
  label?: string;
}

export function ContextMenu({ menu, onClose }: { menu: MenuState; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x: menu.x, y: menu.y });
  const [ready, setReady] = useState(false);

  // Clamp the menu inside the viewport once we know its rendered size.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const pad = 8;
    const x = Math.min(menu.x, window.innerWidth - width - pad);
    const y = Math.min(menu.y, window.innerHeight - height - pad);
    setPos({ x: Math.max(pad, x), y: Math.max(pad, y) });
    setReady(true);
  }, [menu]);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onClose);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      onContextMenu={(e) => {
        e.preventDefault();
        onClose();
      }}
      style={{ position: "fixed", inset: 0, zIndex: 90 }}
    >
      <div
        ref={ref}
        role="menu"
        aria-label={menu.label}
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "fixed",
          left: pos.x,
          top: pos.y,
          minWidth: 196,
          padding: 6,
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: "var(--r-md, 10px)",
          boxShadow: "var(--shadow-lg)",
          display: "flex",
          flexDirection: "column",
          gap: 1,
          opacity: ready ? 1 : 0,
          transition: "opacity var(--t-fast)",
        }}
      >
        {menu.items.map((item, i) => (
          <div key={i}>
            {item.separatorBefore && (
              <div
                role="separator"
                style={{ height: 1, background: "var(--border)", margin: "5px 6px" }}
              />
            )}
            <MenuRow item={item} onClose={onClose} />
          </div>
        ))}
      </div>
    </div>
  );
}

export function MenuRow({ item, onClose }: { item: MenuItem; onClose: () => void }) {
  const [hover, setHover] = useState(false);
  const lit = hover && !item.disabled;
  return (
    <button
      role="menuitem"
      aria-disabled={item.disabled || undefined}
      title={item.title}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={() => {
        // A disabled row keeps the menu open: the tooltip on it is the answer
        // the reader came for, and closing would take it away.
        if (item.disabled) return;
        onClose();
        item.onClick();
      }}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        width: "100%",
        textAlign: "left",
        padding: "8px 10px",
        border: "none",
        borderRadius: "var(--r-sm)",
        cursor: item.disabled ? "not-allowed" : "pointer",
        fontSize: 13,
        fontWeight: 450,
        opacity: item.disabled ? 0.45 : 1,
        color: item.danger ? "var(--danger)" : "var(--text)",
        background: lit ? (item.danger ? "var(--danger-soft)" : "var(--surface-2)") : "transparent",
        transition: "background var(--t-fast)",
      }}
    >
      {item.icon && (
        <span
          style={{
            flexShrink: 0,
            display: "flex",
            color: item.danger ? "var(--danger)" : "var(--text-3)",
          }}
        >
          {item.icon}
        </span>
      )}
      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {item.label}
      </span>
    </button>
  );
}
