// The app-wide pill switch. Two near-identical copies previously lived in
// agents/Toggle.tsx and tools/ToolsView.tsx with subtly different on/off
// colours — this is the single source both now delegate to, so every boolean
// control looks and behaves the same (role="switch", keyboard operable via
// its native button, aria-checked).

export function Toggle({
  checked,
  onChange,
  disabled,
  busy,
  title,
  ariaLabel,
  variant = "default",
  className,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  // Like disabled, but signals an in-flight mutation (wait cursor).
  busy?: boolean;
  title?: string;
  ariaLabel?: string;
  /**
   * `minimal` drops the track and thumb borders for a quiet surface that is
   * already framed by its container (the profile menu's rows): the thumb reads
   * against the track by its shadow alone, and the on-state by colour alone.
   * `default` keeps the bordered look every form and card uses.
   */
  variant?: "default" | "minimal";
  className?: string;
}) {
  const off = disabled || busy;
  const minimal = variant === "minimal";
  const w = minimal ? 34 : 38;
  const h = minimal ? 20 : 22;
  const thumb = minimal ? 16 : 17;
  const inset = minimal ? 2 : 1.5;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={off}
      title={title}
      className={className}
      data-variant={variant}
      onClick={() => onChange(!checked)}
      style={{
        width: w,
        height: h,
        borderRadius: "var(--r-pill)",
        border: minimal ? "none" : "1px solid var(--border-strong)",
        background: checked
          ? minimal
            ? "var(--accent)"
            : "var(--accent-gradient, var(--accent))"
          : minimal
            ? "var(--border-strong)"
            : "var(--surface-3)",
        position: "relative",
        cursor: busy ? "wait" : off ? "not-allowed" : "pointer",
        opacity: off ? 0.55 : 1,
        padding: 0,
        transition: "background var(--t-fast) var(--ease)",
        flexShrink: 0,
      }}
    >
      <span
        style={{
          position: "absolute",
          top: inset,
          left: checked ? w - thumb - inset : inset,
          width: thumb,
          height: thumb,
          borderRadius: "50%",
          background: "#fff",
          // The border keeps the thumb visible over light-theme off-tracks,
          // where a borderless white thumb all but disappeared. The minimal
          // variant leans on a slightly stronger shadow instead.
          border: minimal ? "none" : "1px solid var(--border-strong)",
          boxSizing: "border-box",
          boxShadow: minimal ? "0 1px 2px rgb(0 0 0 / 0.28)" : "var(--shadow-sm)",
          transition: "left var(--t-fast) var(--ease)",
        }}
      />
    </button>
  );
}
