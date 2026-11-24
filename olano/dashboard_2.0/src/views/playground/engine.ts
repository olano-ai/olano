// ── Playground office engine ────────────────────────────────────────────────
//
// A dependency-free port of the original PixiJS "Playground 2.0" office scene.
// The original used PixiJS for GPU sprite batching + GSAP for tweening; here we
// drive a plain <canvas> 2D context with our own rAF motion loop and a 4 Hz
// behaviour tick. Every behaviour is reproduced:
//
//   • office background drawn `cover` + decor sprites at exact image coords,
//     depth-sorted by foot-y;
//   • per-direction walk cycles (left/right/up/down a–d), idle-per-direction,
//     and stance loops (sleep / coffee / thinking / work) with a flat
//     initial-letter avatar fallback when an agent has no sprite set;
//   • activity halo (blue=talking, agent colour=thinking) with a pulse;
//   • name-pill labels; squash-and-stretch on each stride; seated z-boost so
//     sleepers paint above their cushion;
//   • navmesh routing through waypoints; anchor claiming for desks / mats /
//     coffee stations with collision avoidance; 30–90 s activity dwell; a 14 s
//     "still working" tail after activity stops; legacy-sprite mirroring.
//
// The engine owns no React state. The view feeds it the agent roster + live
// activity map and a click handler; the engine renders and animates.

import {
  BG_URL,
  COFFEE_STATIONS,
  DECOR,
  IMG_H,
  IMG_W,
  REST_ANCHORS,
  WORK_ANCHORS,
  coverParams,
  imageToScreen,
  isImagePointOnDecor,
  isImagePointWalkable,
  randomFloorPoint,
  routeImagePath,
  screenToImage,
  type CoverParams,
  type DecorEntry,
  type ImagePoint,
} from "./layout";

export type Facing = "left" | "right" | "up" | "down";
export type Stance = "sleep" | "coffee" | "thinking" | "work";
export type ActivityStatus = "thinking" | "talking" | "idle";

export interface ActivityState {
  status: ActivityStatus;
  updatedAt: number;
  lastWorkingAt: number;
  // When `status === "talking"`, the agent that initiated the talk_to call.
  // The engine walks the talked-to agent over to stand beside this peer.
  peer?: string | null;
}

export interface AgentLike {
  name: string;
  color?: string | null;
  avatar_url?: string | null;
  running?: boolean;
  sprites?: Record<string, string> | null;
}

const SPRITE_H = 110;
const WALK_FRAME_MS = 110;
const STANCE_FRAME_MS: Record<Stance, number> = { sleep: 500, coffee: 320, thinking: 420, work: 360 };
const SEATED_BOOST = 500;
const WORKING_TAIL_MS = 14000;

// "Talking" pairing — a talked-to agent walks over to stand beside its caller
// and works side-by-side. PAIR_GAP is the image-space offset that keeps the two
// avatars adjacent but not overlapping (the spawn "never shoulder-to-shoulder"
// distance is 90; 84 reads as "right next to" while keeping feet clear).
const PAIR_GAP = 84;
// Screen-px slack for "already standing beside the caller" — avoids re-walking
// on sub-pixel reprojection jitter once settled.
const PAIR_ARRIVE_TOL = 10;

const STANCE_SPEED: Record<Stance | "wander", number> = {
  sleep: 70,
  coffee: 95,
  thinking: 75,
  work: 140,
  wander: 75,
};

const IDLE_ACTION_THRESHOLDS = {
  wander: 0.3,
  thinking: 0.4,
  coffee: 0.75,
} as const;

function dwellMs(): number {
  return 30000 + Math.random() * 60000;
}

// ── theme palette resolved from CSS variables ───────────────────────────────
//
// The scene is drawn to a raw <canvas>, which can't read CSS classes, so we
// snapshot the design-token colours we need (resolved to concrete rgb/hex by
// the browser) and refresh them on resize and whenever the app theme flips.
// This keeps the office, name pills, halos and fallback art reading correctly
// in BOTH the dark and light Aurora themes instead of the old warm-cream hexes.
interface ThemePalette {
  pillBg: string;
  pillText: string;
  shadow: string;
  haloTalking: string;
  floor0: string;
  floor1: string;
  floor2: string;
  decorTable: string;
  decorSit: string;
  decorStation: string;
  decorGlyph: string;
}

function readVar(style: CSSStyleDeclaration, name: string, fallback: string): string {
  const v = style.getPropertyValue(name).trim();
  return v || fallback;
}

function resolveTheme(host: HTMLElement | null): ThemePalette {
  const el = host ?? document.documentElement;
  const s = getComputedStyle(el);
  return {
    // name pill — elevated surface + primary text, like a .badge
    pillBg: readVar(s, "--bg-elev", "#131029"),
    pillText: readVar(s, "--text", "#ece9ff"),
    shadow: "rgba(0,0,0,0.28)",
    // talking halo uses the info/cyan token; thinking uses the agent colour
    haloTalking: readVar(s, "--info", "#36d6e7"),
    // fallback floor (office PNG missing): a soft radial in surface tones
    floor0: readVar(s, "--surface-3", "#272247"),
    floor1: readVar(s, "--surface", "#17142e"),
    floor2: readVar(s, "--bg", "#0d0b1a"),
    // procedural decor hints
    decorTable: readVar(s, "--surface-4", "#322c57"),
    decorSit: readVar(s, "--accent-soft", "#221a3f"),
    decorStation: readVar(s, "--surface-3", "#272247"),
    decorGlyph: readVar(s, "--text-3", "#8b84b8"),
  };
}

// ── texture cache (plain <img>) ─────────────────────────────────────────────
const TEX = new Map<string, HTMLImageElement>();
function loadTex(url: string): HTMLImageElement {
  let img = TEX.get(url);
  if (!img) {
    img = new Image();
    img.decoding = "async";
    img.src = url;
    TEX.set(url, img);
  }
  return img;
}
function evictTextures(urls: Iterable<string>): void {
  for (const u of urls) TEX.delete(u);
}

// ── projected anchor (claimable slot) ───────────────────────────────────────
interface Anchor {
  ix: number;
  iy: number;
  x: number;
  y: number;
  occupiedBy: string | null;
}

// One live character.
interface Char {
  name: string;
  color: string;
  sprites: Record<string, string> | null;
  running: boolean;
  facing: Facing;
  // canvas position (foot anchor) + per-segment target + remaining waypoints
  x: number;
  y: number;
  tx: number;
  ty: number;
  path: ImagePoint[]; // remaining image-space waypoints after the current one
  walking: boolean;
  walkSpeed: number;
  // per-segment easing state — each leg accelerates from rest and decelerates
  // into its target (sine.inOut), reproducing the original GSAP tween instead
  // of a constant-velocity slide.
  segStartX: number;
  segStartY: number;
  segDist: number; // seconds elapsed on the current segment
  segDur: number; // segment duration in seconds (= max(0.4, len/speed))
  walkFrame: number;
  walkFrameAt: number;
  stance: Stance | null;
  stanceFrame: number;
  stanceFrameAt: number;
  desk: Anchor | null;
  mat: Anchor | null;
  coffee: Anchor | null;
  pauseUntil: number;
  seated: boolean;
  haloPulse: number;
  intendedImg: ImagePoint | null; // where this char is heading (collision lookahead)
  postWalkStance: Stance | null; // settle into this on arrival
  postWalkDwell: boolean; // start the 30-90s idle dwell after arrival, not before
  peer: string | null; // caller this char is shadowing (set while talked-to)
}

function decorBox(e: DecorEntry): { x: number; y: number; w: number; h: number } {
  // anchored bottom-centre
  return { x: e.ix - e.iw / 2, y: e.iy - e.ih, w: e.iw, h: e.ih };
}

export interface EngineCallbacks {
  onCharClick(name: string, running: boolean): void;
}

export class PlaygroundEngine {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private cb: EngineCallbacks;
  private bg: HTMLImageElement;
  private decorImgs: HTMLImageElement[];

  private chars = new Map<string, Char>();
  private work: Anchor[] = [];
  private mats: Anchor[] = [];
  private stations: Anchor[] = [];

  private w = 0;
  private h = 0;
  private dpr = Math.min(window.devicePixelRatio || 1, 2);
  private cover: CoverParams = coverParams(1, 1);

  private raf = 0;
  private tickHandle = 0;
  private last = performance.now();

  // live roster + activity, refreshed by the view each render
  private roster: AgentLike[] = [];
  private activity: Record<string, ActivityState> = {};

  // design-token palette, refreshed on resize + theme change
  private theme: ThemePalette;

  constructor(canvas: HTMLCanvasElement, cb: EngineCallbacks) {
    this.canvas = canvas;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("2D canvas context unavailable");
    this.ctx = ctx;
    this.cb = cb;
    this.bg = loadTex(BG_URL);
    this.decorImgs = DECOR.map((d) => loadTex(d.url));
    this.theme = resolveTheme(canvas.parentElement);
    this.buildAnchors();
  }

  /** Re-read the design tokens (call when the app theme toggles). */
  refreshTheme(): void {
    this.theme = resolveTheme(this.canvas.parentElement);
  }

  // ── public lifecycle ──────────────────────────────────────────────────────
  start(): void {
    this.raf = requestAnimationFrame(this.frame);
    this.tickHandle = window.setInterval(this.tick, 250);
  }

  stop(): void {
    cancelAnimationFrame(this.raf);
    window.clearInterval(this.tickHandle);
  }

  resize(w: number, h: number): void {
    this.w = w;
    this.h = h;
    this.canvas.width = Math.floor(w * this.dpr);
    this.canvas.height = Math.floor(h * this.dpr);
    this.canvas.style.width = `${w}px`;
    this.canvas.style.height = `${h}px`;
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this.cover = coverParams(w, h);
    this.refreshTheme();
    this.reproject();
  }

  /** Feed the latest agent roster; spawns/removes/updates live characters. */
  setRoster(agents: AgentLike[]): void {
    this.roster = agents;
    const seen = new Set<string>();
    for (const a of agents) {
      seen.add(a.name);
      let c = this.chars.get(a.name);
      if (!c) {
        c = this.spawn(a);
        this.chars.set(a.name, c);
      } else {
        c.color = a.color || c.color;
        c.running = !!a.running;
        if (a.sprites && a.sprites !== c.sprites) c.sprites = a.sprites;
      }
    }
    for (const [name, c] of this.chars) {
      if (!seen.has(name)) {
        this.releaseAll(c);
        this.chars.delete(name);
      }
    }
  }

  setActivity(act: Record<string, ActivityState>): void {
    this.activity = act;
  }

  /** SSE `agent_sprites_ready` — drop cached textures + hot-swap the live char. */
  swapSprites(name: string, sprites: Record<string, string> | null): void {
    const c = this.chars.get(name);
    if (c?.sprites) evictTextures(Object.values(c.sprites));
    if (sprites) {
      evictTextures(Object.values(sprites));
      for (const u of Object.values(sprites)) loadTex(u); // pre-warm
    }
    if (c) c.sprites = sprites;
    for (const a of this.roster) if (a.name === name) a.sprites = sprites;
  }

  // ── anchors ────────────────────────────────────────────────────────────────
  private buildAnchors(): void {
    this.work = WORK_ANCHORS.map((a) => ({ ix: a.ix, iy: a.iy, x: 0, y: 0, occupiedBy: null }));
    this.mats = REST_ANCHORS.map((a) => ({ ix: a.ix, iy: a.iy, x: 0, y: 0, occupiedBy: null }));
    this.stations = COFFEE_STATIONS.map((a) => ({ ix: a.ix, iy: a.iy, x: 0, y: 0, occupiedBy: null }));
  }

  private reproject(): void {
    for (const a of [...this.work, ...this.mats, ...this.stations]) {
      const s = imageToScreen(a.ix, a.iy, this.cover);
      a.x = s.x;
      a.y = s.y;
    }
    // keep claimed/parked chars glued to their re-projected anchor
    for (const c of this.chars.values()) {
      const ref = c.desk ?? c.mat ?? c.coffee;
      if (ref) {
        if (!c.walking) {
          c.x = ref.x;
          c.y = ref.y;
        }
      } else if (!c.walking) {
        // Pair-following chars hold no anchor, so re-glue them beside their
        // caller (whose desk anchor is already re-projected above) instead of
        // leaving them at a stale, possibly clamped, position until next tick.
        const peer = c.peer ? this.chars.get(c.peer) : null;
        if (peer) {
          const spot = this.sideBySideSpot(c, peer);
          const s = imageToScreen(spot.ix, spot.iy, this.cover);
          c.x = s.x;
          c.y = s.y;
        } else {
          c.x = Math.min(Math.max(c.x, 20), this.w - 20);
          c.y = Math.min(Math.max(c.y, 20), this.h - 20);
        }
      }
    }
  }

  private spawn(a: AgentLike): Char {
    // Spawn inside a walkable zone, clear of decor AND other agents, so a new
    // avatar never appears parked on a table or shoulder-to-shoulder.
    const img = this.randomWalkableAwayFromAgents(a.name);
    const s = imageToScreen(img.ix, img.iy, this.cover);
    if (a.sprites) for (const u of Object.values(a.sprites)) loadTex(u); // pre-warm
    return {
      name: a.name,
      color: a.color || "#c96442",
      sprites: a.sprites ?? null,
      running: !!a.running,
      facing: "down",
      x: s.x || (0.2 + Math.random() * 0.6) * (this.w || 800),
      y: s.y || (0.3 + Math.random() * 0.4) * (this.h || 480),
      tx: s.x,
      ty: s.y,
      path: [],
      walking: false,
      walkSpeed: STANCE_SPEED.wander,
      segStartX: 0,
      segStartY: 0,
      segDist: 0,
      segDur: 0,
      walkFrame: 0,
      walkFrameAt: 0,
      stance: null,
      stanceFrame: 0,
      stanceFrameAt: 0,
      desk: null,
      mat: null,
      coffee: null,
      // NOTE: the behaviour tick clocks `now` off performance.now(), so this
      // initial settle-in pause MUST use the same clock. Using Date.now() here
      // (epoch ms) made `now < pauseUntil` permanently true, freezing every
      // idle agent on its spawn pose — it never reached the action roll.
      pauseUntil: performance.now() + Math.random() * 4000,
      seated: false,
      haloPulse: 0,
      intendedImg: null,
      postWalkStance: null,
      postWalkDwell: false,
      peer: null,
    };
  }

  // ── claiming + collision helpers ────────────────────────────────────────────
  private isPointClearOfAgents(ix: number, iy: number, self: string, minDist = 70): boolean {
    for (const c of this.chars.values()) {
      if (c.name === self) continue;
      const ref = c.intendedImg ?? screenToImage(c.x, c.y, this.cover);
      if (Math.hypot(ref.ix - ix, ref.iy - iy) < minDist) return false;
    }
    return true;
  }

  private randomWalkableAwayFromAgents(self: string, minDist = 90): ImagePoint {
    let last = randomFloorPoint();
    for (let i = 0; i < 16; i++) {
      last = randomFloorPoint();
      if (this.isPointClearOfAgents(last.ix, last.iy, self, minDist)) return last;
    }
    return last;
  }

  // Like isPointClearOfAgents, but also ignores `except` — used when placing a
  // talked-to agent beside its caller: the intentional gap to the caller must
  // not disqualify the spot, while OTHER agents still must be cleared.
  private isPointClearOfPeers(ix: number, iy: number, self: string, except: string, minDist = 60): boolean {
    for (const c of this.chars.values()) {
      if (c.name === self || c.name === except) continue;
      const ref = c.intendedImg ?? screenToImage(c.x, c.y, this.cover);
      if (Math.hypot(ref.ix - ix, ref.iy - iy) < minDist) return false;
    }
    return true;
  }

  // ── talking pairing ──────────────────────────────────────────────────────
  // Resolve the live caller a talked-to agent should stand beside, or null for
  // normal (own-desk) working behaviour. The pairing is asserted while the
  // agent is actively "talking" and held through the brief working-tail so it
  // finishes the exchange beside its caller instead of darting to a desk; a
  // fresh "thinking" (its own work) or going fully idle drops it.
  private pairPeerFor(c: Char): Char | null {
    const act = this.activity[c.name];
    const status: ActivityStatus = act?.status ?? "idle";
    const inTail = !!act && Date.now() - act.lastWorkingAt < WORKING_TAIL_MS;
    if (status === "talking" && act?.peer) {
      c.peer = act.peer;
    } else if (!(status === "idle" && inTail && c.peer)) {
      c.peer = null;
    }
    if (!c.peer) return null;
    const p = this.chars.get(c.peer);
    if (!p || p === c) {
      c.peer = null;
      return null;
    }
    return p;
  }

  // A standing spot ~PAIR_GAP image-px to one side of the caller — side-by-side
  // first (left/right), then diagonals — that is walkable, off decor, and clear
  // of any third agent. Anchors on the caller's settled desk when it has one so
  // the target is stable; otherwise on its live position.
  private sideBySideSpot(c: Char, peer: Char): ImagePoint {
    const base: ImagePoint = peer.desk
      ? { ix: peer.desk.ix, iy: peer.desk.iy }
      : screenToImage(peer.x, peer.y, this.cover);
    const g = PAIR_GAP;
    const cands: ImagePoint[] = [
      { ix: base.ix - g, iy: base.iy },
      { ix: base.ix + g, iy: base.iy },
      { ix: base.ix - g, iy: base.iy + g * 0.5 },
      { ix: base.ix + g, iy: base.iy + g * 0.5 },
      { ix: base.ix - g, iy: base.iy - g * 0.5 },
      { ix: base.ix + g, iy: base.iy - g * 0.5 },
    ];
    for (const p of cands) {
      if (
        isImagePointWalkable(p.ix, p.iy, 10) &&
        !isImagePointOnDecor(p.ix, p.iy, 8) &&
        this.isPointClearOfPeers(p.ix, p.iy, c.name, peer.name, g - 8)
      ) {
        return p;
      }
    }
    return cands[0]; // nothing ideal — navmesh routing in walkTo still finds a path
  }

  private releaseCushion(name: string): void {
    for (const an of this.work) if (an.occupiedBy === name) an.occupiedBy = null;
    for (const an of this.mats) if (an.occupiedBy === name) an.occupiedBy = null;
  }

  private isCushionTaken(ix: number, iy: number, self: string): boolean {
    for (const an of this.work) if (an.ix === ix && an.iy === iy && an.occupiedBy && an.occupiedBy !== self) return true;
    for (const an of this.mats) if (an.ix === ix && an.iy === iy && an.occupiedBy && an.occupiedBy !== self) return true;
    return false;
  }

  private lockCushion(name: string, ix: number, iy: number): void {
    for (const an of this.work) if (an.ix === ix && an.iy === iy) an.occupiedBy = name;
    for (const an of this.mats) if (an.ix === ix && an.iy === iy) an.occupiedBy = name;
  }

  private claimWork(name: string, near: { x: number; y: number }): Anchor | null {
    this.releaseCushion(name);
    let best: Anchor | null = null;
    let bestD = Infinity;
    for (const an of this.work) {
      if (this.isCushionTaken(an.ix, an.iy, name)) continue;
      const d = (an.x - near.x) ** 2 + (an.y - near.y) ** 2;
      if (d < bestD) {
        bestD = d;
        best = an;
      }
    }
    if (best) this.lockCushion(name, best.ix, best.iy);
    return best;
  }

  private claimMat(name: string, near: { x: number; y: number }): Anchor | null {
    this.releaseCushion(name);
    let best: Anchor | null = null;
    let bestD = Infinity;
    for (const an of this.mats) {
      if (this.isCushionTaken(an.ix, an.iy, name)) continue;
      const d = (an.x - near.x) ** 2 + (an.y - near.y) ** 2;
      if (d < bestD) {
        bestD = d;
        best = an;
      }
    }
    if (best) this.lockCushion(name, best.ix, best.iy);
    return best;
  }

  private claimCoffee(name: string): Anchor | null {
    for (const st of this.stations) if (st.occupiedBy === name) st.occupiedBy = null;
    const free = this.stations.filter((s) => !s.occupiedBy);
    if (!free.length) return null;
    const far = free.filter((s) => this.isPointClearOfAgents(s.ix, s.iy, name, 130));
    const pool = far.length ? far : free;
    const pick = pool[Math.floor(Math.random() * pool.length)];
    pick.occupiedBy = name;
    return pick;
  }

  private releaseAll(c: Char): void {
    for (const an of [...this.work, ...this.mats, ...this.stations]) {
      if (an.occupiedBy === c.name) an.occupiedBy = null;
    }
    c.desk = null;
    c.mat = null;
    c.coffee = null;
  }

  // ── route an agent to an anchor (or floor point) through the navmesh ────────
  private walkTo(c: Char, dest: ImagePoint, speed: number, postWalk: Stance | null, dwellOnArrival = false): void {
    const start = screenToImage(c.x, c.y, this.cover);
    c.intendedImg = dest;
    const path = routeImagePath(start, dest);
    c.path = path.slice(1); // remaining waypoints after the first
    const first = path[0];
    const s = imageToScreen(first.ix, first.iy, this.cover);
    c.tx = s.x;
    c.ty = s.y;
    c.walking = Math.hypot(c.tx - c.x, c.ty - c.y) > 2 || c.path.length > 0;
    c.walkSpeed = speed;
    c.postWalkStance = postWalk;
    c.postWalkDwell = dwellOnArrival;
    c.seated = false;
    if (c.walking) {
      this.faceToward(c, c.tx, c.ty);
      this.initSegment(c);
    } else {
      c.intendedImg = null;
      this.finishWalk(c);
    }
  }

  /** Snapshot the current leg (start, length, duration) so `advance` can ease
   *  position with sine.inOut. Duration matches the original GSAP tween:
   *  max(0.4s, length / speed). Call when a new segment (or waypoint) begins. */
  private initSegment(c: Char): void {
    c.segStartX = c.x;
    c.segStartY = c.y;
    const len = Math.hypot(c.tx - c.x, c.ty - c.y);
    c.segDist = 0;
    c.segDur = Math.max(0.4, len / (c.walkSpeed || STANCE_SPEED.wander));
  }

  private faceToward(c: Char, x: number, y: number): void {
    const dx = x - c.x;
    const dy = y - c.y;
    if (Math.abs(dx) > Math.abs(dy)) c.facing = dx < 0 ? "left" : "right";
    else c.facing = dy < 0 ? "up" : "down";
  }

  // Settle the character into a stance. Idempotent: re-asserting the SAME
  // stance is a no-op so the per-frame loop in `advance` keeps cycling.
  // Without this guard, the work/sleep branches (which re-call settle every
  // 250 ms tick while parked at a desk/mat) reset stanceFrame to 0 faster
  // than the frame timer fires, freezing the animation on its first frame.
  private settle(c: Char, stance: Stance | null): void {
    if (c.stance === stance) return;
    c.stance = stance;
    c.stanceFrame = 0;
    c.stanceFrameAt = Date.now();
    c.seated = stance === "sleep";
  }

  private agentFor(name: string): AgentLike | undefined {
    return this.roster.find((a) => a.name === name);
  }

  private isWorking(name: string, running: boolean): boolean {
    const act = this.activity[name];
    const status: ActivityStatus = act?.status ?? "idle";
    const inTail = Date.now() - (act?.lastWorkingAt ?? 0) < WORKING_TAIL_MS;
    return running && (status === "thinking" || status === "talking" || inTail);
  }

  private arrivalStance(c: Char): Stance {
    const running = !!this.agentFor(c.name)?.running;
    if (!running) return "sleep";
    if (this.isWorking(c.name, running)) return "work";
    return c.postWalkDwell ? c.postWalkStance ?? "thinking" : "thinking";
  }

  private finishWalk(c: Char): void {
    const running = !!this.agentFor(c.name)?.running;
    const working = this.isWorking(c.name, running);
    this.settle(c, this.arrivalStance(c));
    if (c.postWalkDwell && running && !working) {
      c.pauseUntil = performance.now() + dwellMs();
    }
    c.postWalkStance = null;
    c.postWalkDwell = false;
  }

  // ── 4 Hz behaviour tick ─────────────────────────────────────────────────────
  private tick = (): void => {
    const now = performance.now();
    for (const a of this.roster) {
      const c = this.chars.get(a.name);
      if (!c) continue;
      if (c.walking) continue;

      const act = this.activity[a.name];
      const status: ActivityStatus = act?.status ?? "idle";
      const inTail = Date.now() - (act?.lastWorkingAt ?? 0) < WORKING_TAIL_MS;
      const running = !!a.running;
      const isWorking = running && (status === "thinking" || status === "talking" || inTail);

      // Stopped → curl up on a mat.
      if (!running) {
        if (c.desk || c.coffee) {
          this.releaseCushion(c.name);
          c.desk = null;
        }
        if (c.coffee) {
          c.coffee.occupiedBy = null;
          c.coffee = null;
        }
        if (!c.mat) c.mat = this.claimMat(a.name, c);
        if (c.mat) {
          const d = Math.hypot(c.mat.x - c.x, c.mat.y - c.y);
          if (d > 6) this.walkTo(c, c.mat, STANCE_SPEED.sleep, "sleep");
          else this.settle(c, "sleep");
        }
        continue;
      }

      // Drop a mat claim only once physically off the cushion.
      if (c.mat && c.stance !== "sleep") {
        if (Math.hypot(c.mat.x - c.x, c.mat.y - c.y) > 30) {
          this.releaseCushion(c.name);
          c.mat = null;
          c.desk = null;
        }
      }

      // Working → either shadow the caller that's talking to us, or claim a desk.
      if (isWorking) {
        if (c.coffee) {
          c.coffee.occupiedBy = null;
          c.coffee = null;
        }
        // Talked-to: walk over and stand beside the caller, then work
        // side-by-side — instead of heading off to a desk of our own.
        const peer = this.pairPeerFor(c);
        if (peer) {
          if (c.desk) {
            this.releaseCushion(c.name);
            c.desk = null;
          }
          const spot = this.sideBySideSpot(c, peer);
          const s = imageToScreen(spot.ix, spot.iy, this.cover);
          const d = Math.hypot(s.x - c.x, s.y - c.y);
          if (d > PAIR_ARRIVE_TOL) {
            this.walkTo(c, spot, STANCE_SPEED.work, "work");
          } else {
            this.settle(c, "work");
            c.facing = peer.facing; // face the screen the same way as the caller
          }
          continue;
        }
        if (!c.desk) c.desk = this.claimWork(a.name, c);
        if (c.desk) {
          const d = Math.hypot(c.desk.x - c.x, c.desk.y - c.y);
          if (d > 6) this.walkTo(c, c.desk, STANCE_SPEED.work, "work");
          else this.settle(c, "work");
        }
        continue;
      }

      // Idle running → drop a stale desk claim once walked off it.
      if (c.desk && Math.hypot(c.desk.x - c.x, c.desk.y - c.y) > 30) {
        this.releaseCushion(c.name);
        c.desk = null;
        c.mat = null;
      }
      if (now < c.pauseUntil) continue;

      const myImg = screenToImage(c.x, c.y, this.cover);
      const onDecor = isImagePointOnDecor(myImg.ix, myImg.iy, 14);
      const overlapping = !this.isPointClearOfAgents(myImg.ix, myImg.iy, c.name, 70);
      const mustWander = overlapping || onDecor;
      const r = mustWander ? 0 : Math.random();

      if (r < IDLE_ACTION_THRESHOLDS.wander) {
        if (c.coffee) {
          c.coffee.occupiedBy = null;
          c.coffee = null;
        }
        const wander = this.randomWalkableAwayFromAgents(c.name);
        this.walkTo(c, wander, STANCE_SPEED.wander, "thinking", true);
      } else if (r < IDLE_ACTION_THRESHOLDS.thinking) {
        if (c.coffee) {
          c.coffee.occupiedBy = null;
          c.coffee = null;
        }
        this.settle(c, "thinking");
        c.pauseUntil = now + dwellMs();
      } else if (r < IDLE_ACTION_THRESHOLDS.coffee) {
        const station = this.claimCoffee(c.name);
        if (station) {
          c.coffee = station;
          this.walkTo(c, station, STANCE_SPEED.coffee, "coffee", true);
        } else {
          this.settle(c, "thinking");
          c.pauseUntil = now + dwellMs();
        }
      } else {
        if (c.coffee) {
          c.coffee.occupiedBy = null;
          c.coffee = null;
        }
        const mat = c.mat ?? this.claimMat(c.name, c);
        if (mat) {
          c.mat = mat;
          const d = Math.hypot(mat.x - c.x, mat.y - c.y);
          if (d > 6) this.walkTo(c, mat, STANCE_SPEED.sleep, "sleep", true);
          else {
            this.settle(c, "sleep");
            c.pauseUntil = now + dwellMs();
          }
        } else {
          this.settle(c, "thinking");
          c.pauseUntil = now + dwellMs();
        }
      }
    }
  };

  // ── render loop ──────────────────────────────────────────────────────────────
  private frame = (t: number): void => {
    const dt = Math.min(0.05, (t - this.last) / 1000);
    this.last = t;
    const now = Date.now();
    const { ctx, w, h } = this;
    ctx.clearRect(0, 0, w, h);

    this.drawBackground();
    this.drawDecorBehind();

    // advance motion + collect for depth sort
    const list: Char[] = [];
    for (const c of this.chars.values()) {
      this.advance(c, dt, now);
      const act = this.activity[c.name];
      const lit = c.running && (act?.status === "thinking" || act?.status === "talking");
      c.haloPulse += ((lit ? 1 : 0) - c.haloPulse) * Math.min(1, dt * 6);
      list.push(c);
    }

    // Depth sort: chars + decor sprites together, by foot-y. Seated chars get
    // a boost so they paint above their cushion. We z-sort decor by its anchor
    // y too, then draw chars interleaved by comparing foot-y.
    const decorDrawn = new Array(DECOR.length).fill(false);
    list.sort((a, b) => a.y + (a.seated ? SEATED_BOOST : 0) - (b.y + (b.seated ? SEATED_BOOST : 0)));

    // draw remaining (foreground) decor + chars interleaved by y
    const sortedDecor = DECOR.map((d, i) => ({ i, y: imageToScreen(d.ix, d.iy, this.cover).y })).sort(
      (a, b) => a.y - b.y,
    );
    let di = 0;
    for (const c of list) {
      const cy = c.y + (c.seated ? SEATED_BOOST : 0);
      while (di < sortedDecor.length && sortedDecor[di].y <= cy) {
        const entry = sortedDecor[di];
        if (!decorDrawn[entry.i]) {
          this.drawDecorSprite(entry.i);
          decorDrawn[entry.i] = true;
        }
        di++;
      }
      this.drawChar(c, now);
    }
    // any decor in front of the last char
    while (di < sortedDecor.length) {
      const entry = sortedDecor[di];
      if (!decorDrawn[entry.i]) this.drawDecorSprite(entry.i);
      di++;
    }

    this.raf = requestAnimationFrame(this.frame);
  };

  private advance(c: Char, dt: number, now: number): void {
    if (c.walking) {
      // Ease the leg with sine.inOut over its duration (accelerate from rest,
      // decelerate into the target) instead of sliding at constant velocity —
      // this is what made the original GSAP build read as "walking" not
      // "gliding". Progress is time-based so mean pace == len/speed.
      c.segDist += dt;
      const p = c.segDur > 0 ? Math.min(1, c.segDist / c.segDur) : 1;
      const e = 0.5 - 0.5 * Math.cos(Math.PI * p); // sine.inOut on [0,1]
      c.x = c.segStartX + (c.tx - c.segStartX) * e;
      c.y = c.segStartY + (c.ty - c.segStartY) * e;
      if (p >= 1) {
        c.x = c.tx;
        c.y = c.ty;
        if (c.path.length) {
          const next = c.path.shift() as ImagePoint;
          const s = imageToScreen(next.ix, next.iy, this.cover);
          c.tx = s.x;
          c.ty = s.y;
          this.faceToward(c, c.tx, c.ty);
          this.initSegment(c); // ease the next leg from rest too
        } else {
          c.walking = false;
          c.intendedImg = null;
          // Resolve from live runtime state at the moment the walk ends, so
          // a state flip mid-route lands in work/sleep instead of a stale pose.
          this.finishWalk(c);
        }
      } else if (now - c.walkFrameAt > WALK_FRAME_MS) {
        c.walkFrame = (c.walkFrame + 1) % 4;
        c.walkFrameAt = now;
      }
    } else if (c.stance) {
      const ms = STANCE_FRAME_MS[c.stance];
      if (now - c.stanceFrameAt > ms) {
        c.stanceFrame = (c.stanceFrame + 1) % 4;
        c.stanceFrameAt = now;
      }
    }
  }

  // ── drawing ──────────────────────────────────────────────────────────────────
  private drawBackground(): void {
    const { ctx, bg, cover, w, h } = this;
    if (bg.complete && bg.naturalWidth > 0) {
      const drawnW = IMG_W * cover.scale;
      const drawnH = IMG_H * cover.scale;
      ctx.drawImage(bg, cover.offsetX, cover.offsetY, drawnW, drawnH);
    } else {
      // graceful fallback when the office PNG isn't reachable (e.g. dev server
      // doesn't proxy /assets): soft radial gradient in the Aurora surface
      // tones so the empty stage still reads as "part of the app" in both
      // dark and light.
      const g = ctx.createRadialGradient(w * 0.5, h * 0.18, 40, w * 0.5, h * 0.5, Math.max(w, h));
      g.addColorStop(0, this.theme.floor0);
      g.addColorStop(0.55, this.theme.floor1);
      g.addColorStop(1, this.theme.floor2);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }
  }

  // When the office PNG is missing we still want a hint of furniture, so we
  // render procedural rugs/cushion shapes for each decor entry in that case.
  private drawDecorBehind(): void {
    if (this.bg.complete && this.bg.naturalWidth > 0) return; // real bg owns the decor
    const { ctx } = this;
    ctx.save();
    for (let i = 0; i < DECOR.length; i++) {
      const e = DECOR[i];
      const box = decorBox(e);
      const tl = imageToScreen(box.x, box.y, this.cover);
      const br = imageToScreen(box.x + box.w, box.y + box.h, this.cover);
      const cw = br.x - tl.x;
      const ch = br.y - tl.y;
      ctx.fillStyle = e.role === "table" ? this.theme.decorTable : this.theme.decorSit;
      roundRect(ctx, tl.x, tl.y, cw, ch, Math.min(cw, ch) * 0.3);
      ctx.fill();
    }
    // coffee stations — a flat drawn cup glyph (no emoji)
    for (const st of this.stations) {
      ctx.fillStyle = this.theme.decorStation;
      roundRect(ctx, st.x - 18, st.y - 22, 36, 30, 6);
      ctx.fill();
      drawCup(ctx, st.x, st.y - 7, this.theme.decorGlyph);
    }
    ctx.restore();
  }

  private drawDecorSprite(i: number): void {
    if (!(this.bg.complete && this.bg.naturalWidth > 0)) return; // procedural mode owns decor
    const img = this.decorImgs[i];
    if (!img.complete || img.naturalWidth === 0) return;
    const e = DECOR[i];
    const box = decorBox(e);
    const tl = imageToScreen(box.x, box.y, this.cover);
    const w = box.w * this.cover.scale;
    const h = box.h * this.cover.scale;
    this.ctx.drawImage(img, tl.x, tl.y, w, h);
  }

  private pickSprite(c: Char): { img: HTMLImageElement; mirror: boolean } | null {
    if (!c.sprites) return null;
    const s = c.sprites;
    let key: string | null;
    let legacyMirror = false;
    if (c.walking) {
      const frame = "abcd"[c.walkFrame % 4];
      key = `walk_${c.facing}_${frame}`;
      if (!(key in s)) {
        // legacy 4-frame profile cycle (faces right; mirror for left)
        key = `walk_${frame}`;
        legacyMirror = c.facing === "left";
      }
    } else if (c.stance) {
      const sf = "abcd"[c.stanceFrame % 4];
      key = `${c.stance}_${sf}`;
      if (!(key in s)) {
        if (c.stance === "thinking" && "idle_thinking" in s) key = "idle_thinking";
        else if (c.stance in s) key = c.stance;
        else key = null;
      }
    } else {
      key = `idle_${c.facing}`;
    }
    let url = key && key in s ? s[key] : null;
    let chosenKey = url ? key : null;
    if (!url) {
      // Fallback ladder. While settled into a stance, NEVER drop down to a
      // walk frame — a static walk pose reads as "frozen mid-step". Restrict
      // the stance ladder to standing/idle poses; if the agent has none,
      // return null so drawChar paints the initial-letter disc + stance prop.
      const order = c.stance
        ? [`idle_${c.facing}`, "idle_down", "idle_right", "idle_left", "idle_up"]
        : [
            `idle_${c.facing}`,
            "idle_down",
            "idle_right",
            "idle_left",
            "idle_up",
            `walk_${c.facing}_a`,
            "walk_down_a",
            "walk_a",
            "work_a",
            "work",
          ];
      for (const f of order) {
        if (f in s) {
          url = s[f];
          chosenKey = f;
          break;
        }
      }
    }
    if (!url) return null;
    // Legacy profile frames (walk_a..d) are drawn facing right — mirror them
    // when the agent heads/faces left, even when reached via the ladder.
    const mirror = legacyMirror || (!!chosenKey && /^walk_[a-d]$/.test(chosenKey) && c.facing === "left");
    return { img: loadTex(url), mirror };
  }

  private drawChar(c: Char, now: number): void {
    const { ctx } = this;
    const footX = c.x;
    const footY = c.y;
    // Continuous breathing squash while walking — a smooth sine bob between
    // 1.0 and 0.96 every 220 ms, matching the original GSAP squash timeline
    // (two 0.11 s sine legs) rather than a hard per-frame snap.
    const squash = c.walking ? 1 - 0.04 * (0.5 - 0.5 * Math.cos(((now % 220) / 220) * 2 * Math.PI)) : 1;
    const hgt = SPRITE_H * squash;
    const topY = footY - hgt;

    // contact shadow
    ctx.save();
    ctx.fillStyle = this.theme.shadow;
    ctx.beginPath();
    ctx.ellipse(footX, footY + 4, 22, 7, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    // activity halo
    if (c.haloPulse > 0.02) {
      const act = this.activity[c.name];
      const talking = act?.status === "talking";
      const pulse = 0.5 + 0.5 * Math.sin(now / 320);
      // Glow scaled to the sprite (~full body), matching the original's
      // SPRITE_H-radius rings — the prior ~47px radius barely lit the feet.
      const r = SPRITE_H * 0.95 + pulse * 10;
      const color = talking ? this.theme.haloTalking : c.color;
      ctx.save();
      ctx.globalAlpha = 0.2 * c.haloPulse * (0.7 + 0.3 * pulse);
      const cy = topY + hgt * 0.45;
      const grad = ctx.createRadialGradient(footX, cy, 4, footX, cy, r);
      grad.addColorStop(0, color);
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(footX, cy, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    // sprite or emoji
    const picked = this.pickSprite(c);
    if (picked && picked.img.complete && picked.img.naturalWidth > 0) {
      const { img, mirror } = picked;
      // preserve aspect: scale to SPRITE_H tall, like the original Pixi build.
      const scale = hgt / img.naturalHeight;
      const dw = img.naturalWidth * scale;
      if (mirror) {
        ctx.save();
        ctx.translate(footX, 0);
        ctx.scale(-1, 1);
        ctx.drawImage(img, -dw / 2, topY, dw, hgt);
        ctx.restore();
      } else {
        ctx.drawImage(img, footX - dw / 2, topY, dw, hgt);
      }
    } else {
      // No sprite set yet → flat initial-letter avatar (a filled disc in the
      // agent colour with the first letter of its name), instead of an emoji.
      ctx.save();
      const r = 26;
      const cy = footY - hgt * 0.42;
      ctx.beginPath();
      ctx.arc(footX, cy, r, 0, Math.PI * 2);
      ctx.fillStyle = c.color;
      ctx.fill();
      ctx.font =
        "600 26px Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#fff";
      ctx.fillText(initialOf(c.name), footX, cy + 1);
      ctx.restore();
    }

    // stance prop (thought bubble / coffee cup / zzz) — makes the idle
    // behaviours read even when an agent has no generated sprites.
    if (!c.walking && c.stance) this.drawStanceProp(c, footX, topY, hgt, now);

    // name pill — styled like a .badge so it reads in dark + light
    ctx.save();
    ctx.font =
      "600 11px Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
    ctx.textAlign = "center";
    const tw = ctx.measureText(c.name).width;
    const padX = 8;
    const pillW = tw + padX * 2;
    const pillH = 16;
    const pillY = footY + 8;
    // soft drop so the pill lifts off busy floor art
    ctx.shadowColor = "rgba(0,0,0,0.35)";
    ctx.shadowBlur = 6;
    ctx.shadowOffsetY = 1;
    ctx.fillStyle = this.theme.pillBg;
    roundRect(ctx, footX - pillW / 2, pillY, pillW, pillH, 8);
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.fillStyle = this.theme.pillText;
    ctx.textBaseline = "middle";
    ctx.fillText(c.name, footX, pillY + pillH / 2 + 0.5);
    ctx.restore();
  }

  // Draw a small canvas prop above/beside the character for its current stance:
  // a thought bubble (thinking / working), a steaming coffee cup, or rising
  // "z"s for sleep. These are purely cosmetic and theme-aware.
  private drawStanceProp(c: Char, footX: number, topY: number, hgt: number, now: number): void {
    const { ctx } = this;
    if (c.stance === "thinking" || c.stance === "work") {
      // thought bubble up-and-right of the head, with cycling dots
      const bx = footX + 16;
      const by = topY + 2;
      ctx.save();
      ctx.fillStyle = this.theme.pillBg;
      ctx.beginPath();
      ctx.arc(bx - 6, by + 13, 2.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(bx - 1, by + 7, 3.2, 0, Math.PI * 2);
      ctx.fill();
      roundRect(ctx, bx, by - 9, 32, 17, 8);
      ctx.fill();
      const phase = Math.floor(now / 320) % 3;
      for (let i = 0; i < 3; i++) {
        ctx.globalAlpha = i === phase ? 1 : 0.34;
        ctx.fillStyle = this.theme.pillText;
        ctx.beginPath();
        ctx.arc(bx + 8 + i * 8, by - 0.5, 2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    } else if (c.stance === "coffee") {
      // a small steaming cup at hand height
      const x = footX + 16;
      const y = topY + hgt * 0.5;
      ctx.save();
      ctx.strokeStyle = this.theme.pillText;
      ctx.globalAlpha = 0.45;
      ctx.lineWidth = 1.4;
      for (let i = 0; i < 2; i++) {
        const sx = x - 2 + i * 5;
        ctx.beginPath();
        ctx.moveTo(sx, y - 2);
        ctx.quadraticCurveTo(sx + 3 * Math.sin(now / 280 + i * 1.6), y - 8, sx, y - 14);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#a9714b";
      roundRect(ctx, x - 5, y, 11, 9, 2.5);
      ctx.fill();
      ctx.strokeStyle = "#a9714b";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(x + 7, y + 4.5, 2.6, -Math.PI / 2, Math.PI / 2);
      ctx.stroke();
      ctx.restore();
    } else if (c.stance === "sleep") {
      // rising, fading z's
      const x = footX + 13;
      const y = topY + 4;
      ctx.save();
      ctx.fillStyle = this.theme.pillText;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (let i = 0; i < 3; i++) {
        const p = (now / 950 + i * 0.34) % 1;
        ctx.globalAlpha = 0.75 * (1 - p);
        ctx.font = `700 ${9 + i * 2}px Inter, -apple-system, sans-serif`;
        ctx.fillText("z", x + p * 11, y - p * 22);
      }
      ctx.restore();
    }
  }

  // ── hit testing ──────────────────────────────────────────────────────────────
  hitTest(x: number, y: number): Char | null {
    let hit: Char | null = null;
    let hitD = Infinity;
    // iterate front-to-back so the topmost char wins
    const list = [...this.chars.values()].sort(
      (a, b) => b.y + (b.seated ? SEATED_BOOST : 0) - (a.y + (a.seated ? SEATED_BOOST : 0)),
    );
    for (const c of list) {
      if (x >= c.x - 36 && x <= c.x + 36 && y >= c.y - SPRITE_H && y <= c.y + 16) {
        const d = Math.abs(x - c.x);
        if (d < hitD) {
          hit = c;
          hitD = d;
        }
      }
    }
    return hit;
  }

  click(x: number, y: number): void {
    const hit = this.hitTest(x, y);
    if (hit) this.cb.onCharClick(hit.name, hit.running);
  }

  hovering(x: number, y: number): boolean {
    return !!this.hitTest(x, y);
  }

  /** Identify the character (if any) under the cursor, with a human-readable
   *  live status — used by the view for the hover tooltip. */
  hoverInfo(x: number, y: number): HoverInfo | null {
    const hit = this.hitTest(x, y);
    if (!hit) return null;
    const act = this.activity[hit.name];
    let status: string;
    if (!hit.running) status = "asleep";
    else if (act?.status === "talking") status = "talking";
    else if (act?.status === "thinking") status = "thinking";
    else if (act && Date.now() - act.lastWorkingAt < WORKING_TAIL_MS) status = "working";
    else status = "idle";
    return { name: hit.name, running: hit.running, status, x: hit.x, y: hit.y - SPRITE_H };
  }
}

export interface HoverInfo {
  name: string;
  running: boolean;
  status: string;
  /** anchor point in canvas pixels (head of the sprite) */
  x: number;
  y: number;
}

/** First visible letter of an agent name, upper-cased, for the avatar disc. */
function initialOf(name: string): string {
  const ch = (name || "?").trim()[0] ?? "?";
  return ch.toUpperCase();
}

/** Flat line-drawn coffee cup (body + handle), centred horizontally at cx with
 *  its base near cy. Replaces the former coffee emoji on the procedural fallback. */
function drawCup(ctx: CanvasRenderingContext2D, cx: number, cy: number, color: string): void {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  // body
  ctx.beginPath();
  ctx.moveTo(cx - 6, cy - 6);
  ctx.lineTo(cx - 5, cy + 5);
  ctx.lineTo(cx + 5, cy + 5);
  ctx.lineTo(cx + 6, cy - 6);
  ctx.closePath();
  ctx.stroke();
  // handle
  ctx.beginPath();
  ctx.arc(cx + 7, cy - 1, 3.5, -Math.PI / 2, Math.PI / 2);
  ctx.stroke();
  ctx.restore();
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}
