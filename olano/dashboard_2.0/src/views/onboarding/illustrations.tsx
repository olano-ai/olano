/**
 * Inline SVG illustrations for the onboarding wizard — stylized mini
 * "screenshots" of Olano features. Drawn with design tokens only so they
 * follow all three themes (dark / light / noir) with zero extra assets.
 */

import type { ReactNode } from "react";

const frame = {
  fill: "var(--surface)",
  stroke: "var(--border)",
} as const;

const soft = { fill: "var(--surface-2)" } as const;
const accent = { fill: "var(--accent)" } as const;
const accentSoft = { fill: "var(--accent-soft)" } as const;
const line = { fill: "var(--text-5)" } as const;

function Svg({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <svg
      viewBox="0 0 220 120"
      className="ob-art"
      role={label ? "img" : "presentation"}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {children}
    </svg>
  );
}

/** Mini dashboard: sidebar, cards, a chat bubble — the product at a glance. */
export function ArtDashboard() {
  return (
    <Svg>
      <rect x="24" y="10" width="172" height="100" rx="10" {...frame} strokeWidth="1.5" />
      <rect x="32" y="20" width="36" height="80" rx="6" {...soft} />
      <circle cx="50" cy="32" r="6" {...accent} />
      <rect x="38" y="46" width="24" height="5" rx="2.5" {...line} />
      <rect x="38" y="57" width="24" height="5" rx="2.5" {...line} />
      <rect x="38" y="68" width="24" height="5" rx="2.5" {...accentSoft} />
      <rect x="76" y="20" width="54" height="36" rx="6" {...soft} />
      <rect x="82" y="27" width="30" height="5" rx="2.5" {...line} />
      <rect x="82" y="38" width="42" height="11" rx="4" {...accentSoft} />
      <rect x="136" y="20" width="52" height="36" rx="6" {...soft} />
      <polyline
        points="141,48 150,40 158,44 166,32 174,36 182,28"
        fill="none"
        stroke="var(--accent)"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <rect x="76" y="62" width="112" height="38" rx="6" {...soft} />
      <rect x="84" y="70" width="58" height="10" rx="5" {...accent} opacity="0.9" />
      <rect x="104" y="84" width="76" height="10" rx="5" {...line} />
    </Svg>
  );
}

/** Shield + keyhole — login password. */

/** Vault door with dial — vault passphrase. */

/** Two account cards, the generic one handing off to a personal one — the
 * built-in "Admin" account the box was provisioned with, and the account the
 * owner creates for themselves. */
export function ArtPeople() {
  return (
    <Svg>
      {/* The provisioned Admin: greyed out, a placeholder avatar. */}
      <rect x="30" y="30" width="72" height="62" rx="10" {...frame} strokeWidth="2" />
      <circle cx="66" cy="52" r="11" {...soft} stroke="var(--border)" />
      <circle cx="66" cy="49" r="4" {...line} />
      <path d="M58 60 Q66 53 74 60" fill="none" stroke="var(--text-4)" strokeWidth="2.5" />
      <rect x="48" y="72" width="36" height="5" rx="2.5" {...line} />
      {/* Hand-off. */}
      <path
        d="M106 61 h12"
        fill="none"
        stroke="var(--text-4)"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <path
        d="M114 56 l6 5 l-6 5"
        fill="none"
        stroke="var(--text-4)"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* The owner's own account: accented, and carrying the admin key. */}
      <rect x="128" y="24" width="72" height="74" rx="10" {...frame} strokeWidth="2.5" />
      <circle cx="164" cy="50" r="13" {...accentSoft} />
      <circle cx="164" cy="46" r="4.6" {...accent} />
      <path d="M155 58 Q164 49 173 58" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
      <rect x="144" y="70" width="40" height="5" rx="2.5" {...line} />
      <rect x="150" y="81" width="28" height="5" rx="2.5" {...soft} stroke="var(--border)" />
      <circle cx="192" cy="32" r="9" {...accent} />
      <circle cx="192" cy="30" r="2.6" fill="var(--bg)" />
      <rect x="190.8" y="31.5" width="2.4" height="6" rx="1.2" fill="var(--bg)" />
    </Svg>
  );
}

/** Neural chip — LLM provider. */
export function ArtSpark() {
  return (
    <Svg>
      <circle cx="110" cy="60" r="46" {...accentSoft} />
      <rect x="84" y="34" width="52" height="52" rx="10" {...frame} strokeWidth="2.5" />
      <circle cx="110" cy="60" r="11" {...accent} />
      {[
        [84, 46],
        [84, 60],
        [84, 74],
        [136, 46],
        [136, 60],
        [136, 74],
      ].map(([x, y], i) => (
        <line
          key={i}
          x1={x}
          y1={y}
          x2={x + (x < 110 ? -14 : 14)}
          y2={y}
          stroke="var(--text-4)"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
      ))}
      <circle cx="66" cy="46" r="3.5" {...line} />
      <circle cx="66" cy="60" r="3.5" {...accent} />
      <circle cx="66" cy="74" r="3.5" {...line} />
      <circle cx="154" cy="46" r="3.5" {...line} />
      <circle cx="154" cy="60" r="3.5" {...accent} />
      <circle cx="154" cy="74" r="3.5" {...line} />
    </Svg>
  );
}

/** Agent card with a friendly face. */

/** Paper plane — Telegram. */

/** Globe + magnifier — web search. */

/** Two expert-agent cards — archetype install. */
export function ArtTeam() {
  return (
    <Svg>
      <rect x="38" y="24" width="70" height="76" rx="10" {...frame} strokeWidth="2.5" />
      <circle cx="73" cy="48" r="13" {...accentSoft} />
      <circle cx="68.5" cy="46" r="2.4" {...accent} />
      <circle cx="77.5" cy="46" r="2.4" {...accent} />
      <path d="M68 52 Q73 56 78 52" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" />
      <rect x="52" y="70" width="42" height="5" rx="2.5" {...line} />
      <rect x="58" y="81" width="30" height="5" rx="2.5" {...soft} stroke="var(--border)" />
      <rect x="116" y="24" width="70" height="76" rx="10" {...frame} strokeWidth="2.5" />
      <circle cx="151" cy="48" r="13" {...soft} stroke="var(--border)" />
      <circle cx="146.5" cy="46" r="2.4" {...line} />
      <circle cx="155.5" cy="46" r="2.4" {...line} />
      <path d="M146 52 Q151 56 156 52" fill="none" stroke="var(--text-4)" strokeWidth="2" strokeLinecap="round" />
      <rect x="130" y="70" width="42" height="5" rx="2.5" {...line} />
      <rect x="136" y="81" width="30" height="5" rx="2.5" {...soft} stroke="var(--border)" />
      <circle cx="112" cy="60" r="10" {...accent} />
      <line x1="112" y1="55.5" x2="112" y2="64.5" stroke="var(--bg)" strokeWidth="2.4" strokeLinecap="round" />
      <line x1="107.5" y1="60" x2="116.5" y2="60" stroke="var(--bg)" strokeWidth="2.4" strokeLinecap="round" />
    </Svg>
  );
}

/** Big check + confetti — finish. */

