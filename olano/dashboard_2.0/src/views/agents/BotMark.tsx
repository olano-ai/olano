/**
 * BotMark — the face of a bot, in either shell.
 *
 * Born in the simple shell; moved here (views/agents/) the day the dashboard's
 * sidebar listed bots too. Its base styles already lived in globals.css.
 *
 * Every bot needs an identity a non-technical person can recognise at a glance
 * in a rail of eight, and most bots will never have an uploaded avatar. The
 * fallback therefore has to be *good*, not a grey circle with two letters.
 *
 * So: a glossy bead. A saturated disc lit from the upper left, with a crescent
 * catchlight, drawn as inline SVG. It is generated from the bot's name, which
 * buys three things a random or sequential colour cannot:
 *
 *   - **Stable.** The same bot is the same colour on every device, in every
 *     session, for every teammate, with nothing stored and nothing to migrate.
 *   - **Distinct.** The palette is hand-picked and evenly spaced around the
 *     wheel, and the hash carries an avalanche step so near-identical names
 *     ("sales-1"/"sales-2") land far apart rather than adjacent — see
 *     paletteIndex, where that is the entire point of the finalizer.
 *   - **Free.** No image endpoint, no model call, no network, no layout shift
 *     while something loads. It paints in the first frame.
 *
 * Precedence is uploaded image → emoji → bead, with two exceptions that both
 * exist because the fallback must never be *worse* than the thing it replaces:
 * a **broken** image falls back to the bead (`onError`) rather than leaving the
 * alt-text box a dead avatar URL prints into the rail, and the **inherited
 * default** emoji is treated as absent, because an emoji every agent shares
 * identifies none of them.
 *
 * Groups get the same mark, stacked: two or three member beads overlapped with
 * a ring punched out in the surface colour, so they read as several bots rather
 * than one muddy disc.
 */
import { useMemo, useState } from "react";

// Twelve hues, hand-picked rather than generated: evenly spaced around the
// wheel, all legible on both the light and dark surfaces, none of them the
// muddy yellow-green or the brown that a naive HSL sweep produces. `from` is
// the lit top-left, `to` the shadowed lower-right.
const PALETTE: ReadonlyArray<{ from: string; to: string }> = [
  { from: "#5EEAD4", to: "#14B8A6" }, // teal
  { from: "#FBBF24", to: "#F59E0B" }, // amber
  { from: "#818CF8", to: "#4F46E5" }, // indigo
  { from: "#C084FC", to: "#9333EA" }, // violet
  { from: "#60A5FA", to: "#2563EB" }, // blue
  { from: "#FB923C", to: "#EA580C" }, // orange
  { from: "#F472B6", to: "#DB2777" }, // pink
  { from: "#4ADE80", to: "#16A34A" }, // green
  { from: "#22D3EE", to: "#0891B2" }, // cyan
  { from: "#FB7185", to: "#E11D48" }, // rose
  { from: "#A3E635", to: "#65A30D" }, // lime
  { from: "#A78BFA", to: "#7C3AED" }, // purple
];

/**
 * FNV-1a over the name, then an fmix32 avalanche.
 *
 * The finalizer is the whole reason this is not a one-liner, and it is not
 * optional. FNV-1a alone ends on a multiply, which leaves its LOW bits barely
 * mixed — and the low bits are exactly what `% PALETTE.length` reads. Names
 * that differ in one trailing character therefore come out *sequential*:
 * sales-1…sales-6 landed on palette 2,3,4,5,6,7, six adjacent hues, for the
 * six bots most likely to sit next to each other in one rail.
 *
 * (Folding the name's length into the seed does not save it. That term is
 * constant across same-length names, which is precisely the colliding case.)
 *
 * fmix32 pushes every input bit through the high bits and back down, so a
 * one-character change redistributes the whole word: the same six names now
 * land on 3,8,11,9,2,4.
 *
 * This package ships no JS test runner, so that property is documented here
 * rather than pinned by a test. If you touch this function, re-check it by
 * hand — feed it sales-1…sales-6 and confirm the results are not consecutive.
 * A regression here is silent: the rail still renders, it just goes gradient.
 */
function paletteIndex(name: string): number {
  let h = 0x811c9dc5 ^ (name.length * 0x01000193);
  for (let i = 0; i < name.length; i += 1) {
    h ^= name.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  h ^= h >>> 16;
  h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return Math.abs(h) % PALETTE.length;
}

export function markColors(name: string): { from: string; to: string } {
  return PALETTE[paletteIndex(name || "?")];
}

/** The bead itself, with no wrapper — used alone and stacked into groups. */
function Bead({ name, size, idSuffix = "" }: { name: string; size: number; idSuffix?: string }) {
  const { from, to } = markColors(name);
  // Gradient and mask ids must be unique per instance: two beads on one page
  // sharing an id makes the second silently reuse the first's fill, which is
  // exactly what happens in a stacked group avatar.
  const uid = useMemo(
    () => `bm-${paletteIndex(name)}-${name.replace(/[^a-z0-9]/gi, "")}-${idSuffix}`,
    [name, idSuffix],
  );
  return (
    <svg viewBox="0 0 40 40" width={size} height={size} aria-hidden focusable="false">
      <defs>
        <linearGradient id={`${uid}-g`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={from} />
          <stop offset="100%" stopColor={to} />
        </linearGradient>
        {/* The catchlight: a disc with a second disc bitten out of it, offset
            down-right. Two circles in a mask is far crisper at 24px than a
            blurred radial gradient, which turns to mush at rail size. */}
        <mask id={`${uid}-m`}>
          <rect width="40" height="40" fill="black" />
          <circle cx="14.5" cy="13" r="7.6" fill="white" />
          <circle cx="18.6" cy="16.4" r="7.2" fill="black" />
        </mask>
      </defs>
      <circle cx="20" cy="20" r="20" fill={`url(#${uid}-g)`} />
      <circle cx="20" cy="20" r="20" fill="#fff" opacity="0.92" mask={`url(#${uid}-m)`} />
    </svg>
  );
}

/**
 * The emoji every agent gets when nobody picks one (`AgentConfig.emoji`
 * defaults to it in olano/config/agent_config.py).
 *
 * It is treated as *absent* here, and that is the whole point. Precedence is
 * image → emoji → bead, so an inherited default silently outranked the bead
 * for every agent that never set one — a rail of six bots rendered six
 * identical lions, which is exactly the "cannot tell my bots apart" problem
 * the bead exists to solve. A shared default is not an identity.
 *
 * The trade is deliberate and one-directional: an operator who *deliberately*
 * picks this particular emoji gets a bead instead. That costs one person a
 * lion. Honouring it costs everyone else a distinguishable rail.
 */
const INHERITED_DEFAULT_EMOJI = "🦁";

export interface BotMarkProps {
  /** Agent name — the identity the colour is derived from. */
  name: string;
  /** Uploaded avatar, when the bot has one. Wins over the emoji and the bead. */
  avatarUrl?: string | null;
  /** Emoji from the agent config. Used when there is no uploaded image. */
  emoji?: string | null;
  size?: number;
  /** Draws the online/offline pip on the lower-right. Omit for no pip. */
  online?: boolean;
  /**
   * A finer pip than `online` (see presence.ts `pipTone`): amber for an agent
   * starting, stopping or blocked, red for one whose boot failed. Wins over
   * `online` when both are passed; implies a pip.
   */
  tone?: "online" | "warn" | "error" | "off";
}

/** One bot's face at any size. */
export function BotMark({ name, avatarUrl, emoji, size = 34, online, tone }: BotMarkProps) {
  const pip = tone ?? (online === undefined ? undefined : online ? "online" : "off");
  const [broken, setBroken] = useState(false);
  const box = { width: size, height: size } as const;

  let inner;
  if (avatarUrl && !broken) {
    inner = (
      <img
        className="botmark-img"
        src={avatarUrl}
        alt=""
        style={box}
        // A dead avatar URL must degrade to the bead, not to a broken-image
        // glyph sitting in the rail.
        onError={() => setBroken(true)}
        aria-hidden
      />
    );
  } else if (emoji && emoji !== INHERITED_DEFAULT_EMOJI) {
    inner = (
      <span className="botmark-emoji" style={{ ...box, fontSize: size * 0.55 }} aria-hidden>
        {emoji}
      </span>
    );
  } else {
    inner = <Bead name={name} size={size} />;
  }

  return (
    <span className="botmark" style={box}>
      {inner}
      {pip !== undefined && (
        <span
          className={`botmark-pip${pip === "off" ? "" : ` is-${pip}`}`}
          // Scale with the mark so the pip stays proportional from the 24px
          // rail row up to the 96px profile header.
          style={{ width: Math.max(7, size * 0.26), height: Math.max(7, size * 0.26) }}
        />
      )}
    </span>
  );
}

/**
 * A group's face: its members' marks, overlapped.
 *
 * Each member wears the SAME face it wears everywhere else — its uploaded
 * avatar first, then its emoji, then the generated bead — because that face is
 * how a person recognises the bot, and a group that redraws its members as
 * anonymous coloured discs answers "which group is this?" with nothing. That
 * needs the roster (`agentsByName`): the group summary carries member *names*
 * only, so without it there is no avatar to find and the bead is all there is.
 * The prop is therefore optional and the bead remains the fallback — a caller
 * that has no roster still renders, exactly as it did before.
 *
 * Capped at three. A fourth adds no information at rail size — it is a sliver
 * of colour — while stealing width from the name beside it, which is what a
 * person actually reads.
 */
export function GroupMark({
  members,
  agentsByName,
  size = 34,
}: {
  members: string[];
  /** The roster, so a member can be drawn with its real avatar/emoji. */
  agentsByName?: Map<string, { avatar_url?: string | null; emoji?: string | null }>;
  size?: number;
}) {
  const shown = members.slice(0, 3);
  // Each bead is smaller than the box so the stack still fits the same
  // footprint as a single bot's mark — a rail whose rows jump width depending
  // on row type reads as broken.
  const bead = Math.round(size * 0.64);
  const step = Math.round(size * 0.3);
  return (
    <span className="botmark is-group" style={{ width: size, height: size }}>
      {shown.map((m, i) => {
        const bot = agentsByName?.get(m);
        return (
          <span
            key={m}
            className="groupmark-bead"
            style={{
              left: i * step,
              top: i % 2 === 0 ? 0 : Math.round(size * 0.3),
              width: bead,
              height: bead,
              zIndex: shown.length - i,
            }}
          >
            <MemberFace
              name={m}
              avatarUrl={bot?.avatar_url}
              emoji={bot?.emoji}
              size={bead}
              idSuffix={`g${i}`}
            />
          </span>
        );
      })}
    </span>
  );
}

/**
 * One member inside a group mark.
 *
 * Deliberately not `BotMark`: that component owns the presence pip and its
 * surface-coloured ring, and three pips stacked inside a 34px group mark are
 * three specks of noise, not three statuses. Everything else — the
 * image → emoji → bead precedence, the broken-image fallback, the inherited
 * default emoji treated as absent — is the same, and is the whole reason this
 * lives beside `BotMark` rather than being reimplemented at the call site.
 */
function MemberFace({
  name,
  avatarUrl,
  emoji,
  size,
  idSuffix,
}: {
  name: string;
  avatarUrl?: string | null;
  emoji?: string | null;
  size: number;
  idSuffix: string;
}) {
  const [broken, setBroken] = useState(false);
  const box = { width: size, height: size } as const;
  if (avatarUrl && !broken) {
    return (
      <img
        className="botmark-img"
        src={avatarUrl}
        alt=""
        style={box}
        onError={() => setBroken(true)}
        aria-hidden
      />
    );
  }
  if (emoji && emoji !== INHERITED_DEFAULT_EMOJI) {
    return (
      <span className="botmark-emoji" style={{ ...box, fontSize: size * 0.55 }} aria-hidden>
        {emoji}
      </span>
    );
  }
  return <Bead name={name} size={size} idSuffix={idSuffix} />;
}
