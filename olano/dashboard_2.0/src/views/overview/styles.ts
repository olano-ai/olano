// Scoped CSS for the Overview view. We can't touch globals.css, so the
// view-local animations (avatar working/talking pulse, selected-card halo,
// detail-panel reveal) live here and are injected once on first import.
//
// All colours come from CSS variables or the per-agent --agent-color the card
// sets inline, so this honours both the dark and light Aurora themes.

const STYLE_ID = "lc-overview-styles";

const CSS = `
@keyframes lc-ov-working-glow {
  0%, 100% {
    box-shadow:
      0 0 0 0 color-mix(in srgb, var(--agent-color, var(--accent)) 55%, transparent),
      0 0 14px 0 color-mix(in srgb, var(--agent-color, var(--accent)) 35%, transparent);
  }
  50% {
    box-shadow:
      0 0 0 4px color-mix(in srgb, var(--agent-color, var(--accent)) 26%, transparent),
      0 0 22px 2px color-mix(in srgb, var(--agent-color, var(--accent)) 55%, transparent);
  }
}
@keyframes lc-ov-talking-glow {
  0%, 100% {
    box-shadow:
      0 0 0 0 color-mix(in srgb, var(--agent-color, var(--accent-2)) 60%, transparent),
      0 0 14px 0 color-mix(in srgb, var(--agent-color, var(--accent-2)) 40%, transparent);
  }
  50% {
    box-shadow:
      0 0 0 4px color-mix(in srgb, var(--agent-color, var(--accent-2)) 32%, transparent),
      0 0 24px 3px color-mix(in srgb, var(--agent-color, var(--accent-2)) 62%, transparent);
  }
}
@keyframes lc-ov-card-halo {
  0%, 100% {
    box-shadow:
      0 0 0 1px var(--agent-color, var(--accent)),
      0 0 18px -2px color-mix(in srgb, var(--agent-color, var(--accent)) 36%, transparent),
      var(--shadow-sm);
  }
  50% {
    box-shadow:
      0 0 0 1px var(--agent-color, var(--accent)),
      0 0 30px -2px color-mix(in srgb, var(--agent-color, var(--accent)) 58%, transparent),
      var(--shadow);
  }
}
@keyframes lc-ov-reveal {
  from { opacity: 0; transform: translateY(-4px); }
  to   { opacity: 1; transform: translateY(0); }
}

.lc-ov-avatar.is-working { animation: lc-ov-working-glow 1.7s ease-in-out infinite; }
.lc-ov-avatar.is-talking { animation: lc-ov-talking-glow 1.5s ease-in-out infinite; }
.lc-ov-card.is-active { animation: lc-ov-card-halo 2.6s ease-in-out infinite; }

/* Quick actions sit hidden until hover/focus/selection — keeps the card calm,
   matching the legacy reveal-on-hover behaviour. Always shown on touch. */
.lc-ov-quick {
  opacity: 0;
  transition: opacity var(--t-fast) var(--ease);
}
.lc-ov-card:hover .lc-ov-quick,
.lc-ov-card:focus-within .lc-ov-quick,
.lc-ov-card.is-active .lc-ov-quick {
  opacity: 1;
}
@media (hover: none) {
  .lc-ov-quick { opacity: 1; }
}

.lc-ov-detail { animation: lc-ov-reveal var(--t) var(--ease); }

/* A stat card that navigates. Overview cards are borderless, so the resting
   state must stay identical to its non-clickable neighbours — the affordance is
   the pointer, a surface lift on hover and the focus ring, never a border that
   would make one tile in the row look misaligned at rest. The anchor reset
   (display/color/text-decoration) is set inline on the element: this sheet is
   injected from an effect, and a frame of blue underlined link is worse than a
   frame with no hover transition. */
.lc-ov-stat-link {
  cursor: pointer;
  transition: background var(--t-fast) var(--ease);
}
.lc-ov-stat-link:hover,
.lc-ov-stat-link:focus-visible {
  background: var(--surface-2);
}
.lc-ov-stat-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .lc-ov-stat-link { transition: none; }
}
`;

export function ensureOverviewStyles(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}
