import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeftRight,
  KeyRound,
  Maximize2,
  Network,
  Plug,
  RotateCcw,
  Search,
  Sparkles,
  Users,
  Workflow,
  X,
  EyeOff,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { agentDetailPath } from "../../core/routeUrls";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { usePrefs } from "../../core/prefs";
import { peekPersistentState, usePersistentState } from "../../core/usePersistentState";
import { useRememberedUrlParam } from "../../core/useUrlParam";
import { subscribeStream, subscribeStreamState, getStreamState } from "../../core/agentStream";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";
import { agentLabel } from "../agents/helpers";
import { ChannelChip, ChannelOverflowChip } from "./ChannelChip";
import {
  CHIP_ROW_H,
  activityMatchesChannel,
  channelDisplayName,
  channelStatus,
  chipLayout,
  worstStatus,
  type OrgChannel,
} from "./channelBrands";

// ── wire types ──────────────────────────────────────────────────────────────
interface OrgHuman {
  id: string;
  name: string;
  email: string;
  // The person's position, under their name — the human half of `title`.
  title?: string;
  is_admin: boolean;
  agents: { agent: string; role: string }[];
}
interface OrgAgent {
  name: string;
  display_name?: string;
  emoji?: string;
  color?: string | null;
  description?: string;
  running?: boolean;
  title?: string | null;
  manager?: string | null;
  reports: string[];
  coworkers?: string[];
  input_agents: string[];
  output_agents: string[];
  input_mode?: string;
  output_mode?: string;
  autogenerate?: boolean;
  subagents: string[];
  /** Configured messaging connectors with their live status (in-memory, no probe). */
  channels?: OrgChannel[];
}
interface OrgEdges {
  access: { user_id: string; agent: string; role: string }[];
  talk: { from: string; to: string }[];
  manager: { manager: string; report: string }[];
  coworker?: { a: string; b: string }[];
}
interface OrgChartData {
  multi_user: boolean;
  humans: OrgHuman[];
  agents: OrgAgent[];
  edges: OrgEdges;
}

// SSE agent_message frames (same shape the Activity view consumes). `kind`
// distinguishes peer talk_to_agent calls from subagent (task tool) delegations.
interface AgentMessageEvent {
  type?: string;
  data?: { kind?: string; phase?: string; from?: string; to?: string };
}
// `thread_activity` frames (core/thread_events.py): `thinking` marks a turn's
// start and `channel` names the surface it arrived on (`whatsapp`, `telegram`…),
// which is what lights the matching connector chip on the agent's card.
interface ThreadActivityEvent {
  type?: string;
  data?: { agent?: string; thread_id?: string; status?: string; channel?: string };
}

// ── layout constants ──────────────────────────────────────────────────────
const NODE_W = 176;
const NODE_H = 62;
const H_NODE_W = 158;
const H_NODE_H = 54;
// A person with a title carries a third line (name, title, badges): the card
// grows by one line so the HUMAN / role badges are never cut off at the bottom.
const H_TITLE_ROW_H = 17;
const humanCardH = (h: { title?: string | null }): number =>
  H_NODE_H + ((h.title || "").trim() ? H_TITLE_ROW_H : 0);
const COL_GAP = 40;
const COL_W = NODE_W + COL_GAP;
const LAYER_GAP = 96;
const HUMAN_BAND_GAP = 66;
const PAD_X = 28;
const PAD_TOP = 16;
const PAD_BOTTOM = 36;
const FLOW_MS = 2600;
// A connector chip pulses from a turn's `thinking` frame until its `idle` one;
// this caps it in case the `idle` frame is lost (a dropped stream).
const CHANNEL_TURN_MAX_MS = 90_000;
// Surfaces a turn can arrive on that are never a connector chip: a turn there
// must not cost the chart a re-render.
const NON_CONNECTOR_CHANNELS: ReadonlySet<string> = new Set(["dashboard", "cli", "scheduler", "local"]);
// Quiet re-fetch of /api/org-chart while the tab is visible, so connector
// status stays current. Its connector status is read from runtime memory,
// never probed over the network.
const CHART_REFRESH_MS = 30_000;

// Subagents render as a labeled footer strip *inside* the parent card (not as
// circles on the canvas), so they can never sit on the reporting connectors —
// one subagent per row. The footer reserves a fixed pad (top border + padding)
// plus one SUB_ROW_H per subagent, and `cardH` grows the card to match so a row
// is never clipped by the auto-layout.
const SUB_FOOTER_PAD = 7;
const SUB_ROW_H = 16;
const subsFooterH = (count: number): number => (count > 0 ? SUB_FOOTER_PAD + count * SUB_ROW_H : 0);

const MIN_SCALE = 0.2;
const MAX_SCALE = 2.5;
const HUMAN_COLOR = "#6b8afd";

// Manual-placement grid. Drags and resizes snap to GRID; a faint dot grid is
// shown on the stage while a gesture is in flight. Boxes can't shrink past the
// minimums (keeps the name + a line of wrapped text readable).
const GRID = 16;
const MIN_NODE_W = 112;
const MIN_NODE_H = 44;
// Pointer travel (screen px) before a press on a card becomes a drag rather
// than a tap — below this it selects/opens instead of nudging the node.
const DRAG_THRESHOLD = 4;
const snap = (v: number): number => Math.round(v / GRID) * GRID;
// Connectors snap to a finer lattice than cards — a hand-placed segment wants
// more precision than a card corner does.
const EDGE_LANE = 8;
const snapLane = (v: number): number => Math.round(v / EDGE_LANE) * EDGE_LANE;

interface Pos {
  x: number;
  y: number;
  w: number;
  h: number;
}
type Pt = { x: number; y: number };
interface View {
  scale: number;
  tx: number;
  ty: number;
}

const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v));

/** Keep only well-formed {x,y,w,h} entries from a (possibly stale/tampered)
 *  stored layout, so a bad value can never push a node to NaN px. */
function sanitizeOverrides(raw: unknown): Record<string, Pos> {
  const out: Record<string, Pos> = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [id, v] of Object.entries(raw as Record<string, unknown>)) {
    const p = v as Partial<Pos> | null;
    if (
      p &&
      Number.isFinite(p.x) &&
      Number.isFinite(p.y) &&
      Number.isFinite(p.w) &&
      Number.isFinite(p.h)
    ) {
      out[id] = { x: p.x as number, y: p.y as number, w: p.w as number, h: p.h as number };
    }
  }
  return out;
}

/** New box for a move/resize gesture: snapped to the grid, clamped to minimums
 *  (resize) and to ≥0 (move), keeping the untouched axis from the gesture base. */
function applyDrag(base: Pos, mode: "move" | "resize", dx: number, dy: number): Pos {
  if (mode === "move") {
    return { ...base, x: Math.max(0, snap(base.x + dx)), y: Math.max(0, snap(base.y + dy)) };
  }
  return {
    ...base,
    w: Math.max(MIN_NODE_W, snap(base.w + dx)),
    h: Math.max(MIN_NODE_H, snap(base.h + dy)),
  };
}

// ── pure layout ─────────────────────────────────────────────────────────────
// Tidy-tree placement. Humans sit across the top; each agent then lands in the
// row for its reporting depth (row 1 = no manager, row 2 = their reports, …) and
// is packed so a manager's reports are grouped *directly beneath it* with the
// manager centred over them. That parent-over-children packing — not the old
// alphabetical spread — is what makes the reporting hierarchy read at a glance.
function computeLayout(
  humans: OrgHuman[],
  agents: OrgAgent[],
  managerEdges: OrgEdges["manager"],
  showSubs: boolean,
  showChannels = false,
) {
  const names = agents.map((a) => a.name);
  const nameSet = new Set(names);
  const subsByName = new Map(agents.map((a) => [a.name, a.subagents || []]));
  const chanCountByName = new Map(agents.map((a) => [a.name, (a.channels || []).length]));

  // Single-parent reporting tree (first manager wins) and its inverse.
  const parentOf = new Map<string, string>();
  for (const e of managerEdges) {
    if (nameSet.has(e.manager) && nameSet.has(e.report) && !parentOf.has(e.report)) {
      parentOf.set(e.report, e.manager);
    }
  }
  const childrenOf = new Map<string, string[]>();
  for (const n of names) {
    const p = parentOf.get(n);
    if (!p) continue;
    const arr = childrenOf.get(p);
    if (arr) arr.push(n);
    else childrenOf.set(p, [n]);
  }
  for (const arr of childrenOf.values()) arr.sort((a, b) => a.localeCompare(b));

  const depthCache = new Map<string, number>();
  const depthOf = (n: string, seen: Set<string> = new Set()): number => {
    const cached = depthCache.get(n);
    if (cached !== undefined) return cached;
    const p = parentOf.get(n);
    let d: number;
    if (!p || seen.has(n)) d = 0;
    else {
      seen.add(n);
      d = depthOf(p, seen) + 1;
    }
    depthCache.set(n, d);
    return d;
  };

  // Horizontal packing: every leaf takes one column; a parent's column is the
  // midpoint of its children's. Roots (and any cycle remnants) get fresh columns
  // left→right, alphabetically, so siblings never interleave between parents.
  const roots = names.filter((n) => !parentOf.has(n)).sort((a, b) => a.localeCompare(b));
  const colOf = new Map<string, number>();
  let leaf = 0;
  const assignCols = (n: string, guard: Set<string>): number => {
    const cached = colOf.get(n);
    if (cached !== undefined || guard.has(n)) return cached ?? 0;
    guard.add(n);
    const kids = childrenOf.get(n) ?? [];
    let c: number;
    if (!kids.length) {
      c = leaf++;
    } else {
      const cs = kids.map((k) => assignCols(k, guard));
      c = (cs[0] + cs[cs.length - 1]) / 2;
    }
    colOf.set(n, c);
    return c;
  };
  for (const r of roots) assignCols(r, new Set());
  for (const n of names) if (!colOf.has(n)) colOf.set(n, leaf++); // unreachable nodes
  const totalCols = Math.max(1, leaf);

  // A card grows by a footer strip when it has subagents (and the toggle is on),
  // one row taller per subagent, and by one chip row when it has connectors (and
  // that toggle is on); each depth row is as tall as its tallest card.
  const cardH = (n: string) =>
    NODE_H +
    (showChannels && (chanCountByName.get(n) || 0) > 0 ? CHIP_ROW_H : 0) +
    (showSubs ? subsFooterH((subsByName.get(n) || []).length) : 0);

  // Vertical bands: one row per depth.
  const layers: string[][] = [];
  for (const n of names) (layers[depthOf(n)] ||= []).push(n);
  const depthY: number[] = [];
  const humanBandH = humans.length ? Math.max(...humans.map(humanCardH)) : 0;
  let y = PAD_TOP + (humans.length ? humanBandH + HUMAN_BAND_GAP : 0);
  layers.forEach((layer, d) => {
    if (!layer) return;
    depthY[d] = y;
    y += Math.max(NODE_H, ...layer.map(cardH)) + LAYER_GAP;
  });
  const canvasH = y - LAYER_GAP + PAD_BOTTOM;

  // Width: the tree's own span, widened to fit the human band when there are more
  // people than columns; the tree is then centred under the people.
  const treeW = totalCols * COL_W - COL_GAP;
  const humanW = humans.length ? humans.length * (H_NODE_W + COL_GAP) - COL_GAP : 0;
  const contentW = Math.max(treeW, humanW, NODE_W);
  const treeX = PAD_X + Math.max(0, (contentW - treeW) / 2);

  const positions = new Map<string, Pos>();

  if (humans.length) {
    const slot = contentW / humans.length;
    humans.forEach((h, i) => {
      positions.set(`h:${h.id}`, {
        x: PAD_X + i * slot + slot / 2 - H_NODE_W / 2,
        y: PAD_TOP,
        w: H_NODE_W,
        h: humanCardH(h),
      });
    });
  }

  for (const n of names) {
    positions.set(`a:${n}`, {
      x: treeX + (colOf.get(n) ?? 0) * COL_W,
      y: depthY[depthOf(n)] ?? PAD_TOP,
      w: NODE_W,
      h: cardH(n),
    });
  }

  return { positions, canvasW: contentW + PAD_X * 2, canvasH };
}

// ── geometry helpers ────────────────────────────────────────────────────────
const center = (p: Pos): Pt => ({ x: p.x + p.w / 2, y: p.y + p.h / 2 });

/** Deterministic small lane offset so parallel edges between layers don't stack. */
function laneOffset(key: string): number {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0;
  return ((((h % 7) + 7) % 7) - 3) * 7; // -21..21 in steps of 7
}

function roleColor(role: string): string {
  if (role === "owner") return "var(--accent)";
  if (role === "admin") return "var(--warn)";
  return "var(--text-4)";
}

function initials(name: string): string {
  const parts = (name || "?").split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
}

// ── edge routing ────────────────────────────────────────────────────────────
// Connectors are orthogonal polylines (0/90/180/270° segments only). Each edge:
//   1. picks an exit face on its source and an entry face on its target from
//      the boxes' relative geometry (manager edges prefer the classic
//      bottom-of-manager → top-of-report tree look);
//   2. gets a *port* on that face — edges sharing a face are distributed along
//      it (ordered by where the far end sits) instead of all stabbing the
//      center, and a manager's report-edges share one trunk port so the
//      hierarchy renders as a single bus;
//   3. auto-routes: the free middle segment scans a set of candidate positions
//      and keeps the one crossing the fewest node boxes (then the least
//      detour). Horizontally-routed pairs that can't dodge (e.g. same-row
//      neighbours between them) fall back to a U-route under/over the row;
//   4. can be adjusted by hand: dragging the middle segment slides it along
//      its free axis; the offset persists per user (prefs.orgchart_edges) and
//      double-clicking the connector snaps it back to the auto route.
type Face = "N" | "S" | "E" | "W";
type EdgeKind = "manager" | "talk" | "coworker" | "access";
// Shape families: 'v' vertical Z, 'h' horizontal Z, 'u' under-U, 'n' over-U,
// 'l' single-corner L (perpendicular faces; not hand-adjustable).
type EdgeShape = "v" | "h" | "u" | "n" | "l";

interface EdgeSpec {
  key: string;
  kind: EdgeKind;
  a: string; // tail node id ("a:name" | "h:id")
  b: string; // head node id
  role?: string; // access edges: owner | admin | member
}
interface EdgeAdjust {
  shape: string;
  d: number; // offset of the middle segment from its auto position
}
interface RoutedEdge extends EdgeSpec {
  d: string; // svg path
  shape: EdgeShape;
  /** Drag axis of the middle segment (null → not adjustable, e.g. L-shapes). */
  axis: "x" | "y" | null;
  mid: number;
  defaultMid: number;
  lo: number;
  hi: number;
  grip: Pt | null; // midpoint of the adjustable segment (hover affordance)
}

const STUB = 12; // minimum straight run out of a port
const EDGE_MARGIN = 10; // obstacle inflation for crossing tests
const CORNER_R = 7; // rounded corner radius
const U_CLEAR = 28; // default corridor clearance for U-routes
const PORT_INSET = 12; // ports never sit closer than this to a box corner

function sanitizeEdgeAdjust(raw: unknown): Record<string, EdgeAdjust> {
  const out: Record<string, EdgeAdjust> = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const a = v as Partial<EdgeAdjust> | null;
    if (a && typeof a.shape === "string" && "vhun".includes(a.shape) && Number.isFinite(a.d)) {
      out[k] = { shape: a.shape, d: a.d as number };
    }
  }
  return out;
}

/** Exit face on `a` and entry face on `b`, from box geometry. Manager edges
 *  keep the classic tree look (out the bottom, into the top) whenever the
 *  report actually sits below its manager. */
function pickFaces(a: Pos, b: Pos, kind: EdgeKind): [Face, Face] {
  const ac = center(a);
  const bc = center(b);
  const dx = bc.x - ac.x;
  const dy = bc.y - ac.y;
  if (kind === "manager" && dy !== 0 && Math.abs(dy) >= Math.abs(dx) / 4) {
    return dy > 0 ? ["S", "N"] : ["N", "S"];
  }
  if (Math.abs(dy) >= Math.abs(dx)) return dy >= 0 ? ["S", "N"] : ["N", "S"];
  return dx >= 0 ? ["E", "W"] : ["W", "E"];
}

const faceAxis = (f: Face): "x" | "y" => (f === "N" || f === "S" ? "y" : "x");

/** Point on a box face at fraction t (0..1) of its length, inset from corners. */
function portPoint(p: Pos, face: Face, t: number): Pt {
  if (face === "N" || face === "S") {
    const x = p.x + clamp(t * p.w, PORT_INSET, Math.max(PORT_INSET, p.w - PORT_INSET));
    return { x, y: face === "N" ? p.y : p.y + p.h };
  }
  const y = p.y + clamp(t * p.h, PORT_INSET, Math.max(PORT_INSET, p.h - PORT_INSET));
  return { x: face === "W" ? p.x : p.x + p.w, y };
}

/** True when the axis-aligned segment (p→q) passes through the rect interior. */
function segCrossesRect(p: Pt, q: Pt, r: Pos): boolean {
  if (p.x === q.x) {
    // vertical
    if (p.x <= r.x || p.x >= r.x + r.w) return false;
    const lo = Math.min(p.y, q.y);
    const hi = Math.max(p.y, q.y);
    return hi > r.y && lo < r.y + r.h;
  }
  if (p.y === q.y) {
    // horizontal
    if (p.y <= r.y || p.y >= r.y + r.h) return false;
    const lo = Math.min(p.x, q.x);
    const hi = Math.max(p.x, q.x);
    return hi > r.x && lo < r.x + r.w;
  }
  return false;
}

type Obstacle = Pos & { id: string };

function polylineCrossings(pts: Pt[], obstacles: Obstacle[], skipA: string, skipB: string): number {
  let n = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    for (const r of obstacles) {
      if (r.id === skipA || r.id === skipB) continue;
      if (segCrossesRect(pts[i], pts[i + 1], r)) n++;
    }
  }
  return n;
}

/** SVG path for an orthogonal polyline with rounded corners. */
function roundedPath(pts: Pt[]): string {
  // Drop zero-length + collinear intermediate points first.
  const p: Pt[] = [];
  for (const pt of pts) {
    const last = p[p.length - 1];
    if (last && last.x === pt.x && last.y === pt.y) continue;
    const prev = p[p.length - 2];
    if (last && prev) {
      const collinear = (prev.x === last.x && last.x === pt.x) || (prev.y === last.y && last.y === pt.y);
      if (collinear) {
        p[p.length - 1] = pt;
        continue;
      }
    }
    p.push(pt);
  }
  if (p.length < 2) return "";
  let d = `M${p[0].x},${p[0].y}`;
  for (let i = 1; i < p.length - 1; i++) {
    const a = p[i - 1];
    const v = p[i];
    const b = p[i + 1];
    const r = Math.min(CORNER_R, Math.hypot(v.x - a.x, v.y - a.y) / 2, Math.hypot(b.x - v.x, b.y - v.y) / 2);
    const inX = Math.sign(v.x - a.x);
    const inY = Math.sign(v.y - a.y);
    const outX = Math.sign(b.x - v.x);
    const outY = Math.sign(b.y - v.y);
    d += ` L${v.x - inX * r},${v.y - inY * r} Q${v.x},${v.y} ${v.x + outX * r},${v.y + outY * r}`;
  }
  const end = p[p.length - 1];
  d += ` L${end.x},${end.y}`;
  return d;
}

/** Polyline for a shape family given its ports and middle-segment position. */
function shapePoints(shape: EdgeShape, pa: Pt, pb: Pt, mid: number): Pt[] {
  if (shape === "h") return [pa, { x: mid, y: pa.y }, { x: mid, y: pb.y }, pb];
  // 'v', 'u' and 'n' all share the vertical-out/vertical-in structure; only
  // where `mid` is allowed to sit differs.
  return [pa, { x: pa.x, y: mid }, { x: pb.x, y: mid }, pb];
}

interface ShapeParams {
  defaultMid: number;
  lo: number;
  hi: number;
}

/** Legal range + auto position of the middle segment for one shape family. */
function shapeParams(shape: EdgeShape, pa: Pt, pb: Pt, lane: number): ShapeParams {
  if (shape === "v") {
    const lo = Math.min(pa.y, pb.y) + STUB;
    const hi = Math.max(pa.y, pb.y) - STUB;
    const m = (pa.y + pb.y) / 2 + lane;
    if (lo <= hi) return { defaultMid: clamp(m, lo, hi), lo, hi };
    const mm = (pa.y + pb.y) / 2; // boxes too close — pin the segment
    return { defaultMid: mm, lo: mm, hi: mm };
  }
  if (shape === "h") {
    const lo = Math.min(pa.x, pb.x) + STUB;
    const hi = Math.max(pa.x, pb.x) - STUB;
    const m = (pa.x + pb.x) / 2 + lane;
    if (lo <= hi) return { defaultMid: clamp(m, lo, hi), lo, hi };
    const mm = (pa.x + pb.x) / 2;
    return { defaultMid: mm, lo: mm, hi: mm };
  }
  if (shape === "u") {
    const lo = Math.max(pa.y, pb.y) + STUB;
    return { defaultMid: lo + U_CLEAR - STUB, lo, hi: lo + 640 };
  }
  // 'n'
  const hi = Math.min(pa.y, pb.y) - STUB;
  return { defaultMid: hi - U_CLEAR + STUB, lo: hi - 640, hi };
}

/** Candidate positions for the middle segment: the auto midpoint plus lanes
 *  hugging each obstacle's inflated boundary (the dodge positions). */
function midCandidates(
  shape: EdgeShape,
  params: ShapeParams,
  obstacles: Obstacle[],
  pa: Pt,
  pb: Pt,
  skipA: string,
  skipB: string,
): number[] {
  const out = [params.defaultMid];
  const axis: "x" | "y" = shape === "h" ? "x" : "y";
  const spanLo = Math.min(axis === "x" ? pa.y : pa.x, axis === "x" ? pb.y : pb.x);
  const spanHi = Math.max(axis === "x" ? pa.y : pa.x, axis === "x" ? pb.y : pb.x);
  for (const r of obstacles) {
    if (r.id === skipA || r.id === skipB) continue;
    // Only obstacles the middle segment could actually meet.
    const rLo = axis === "x" ? r.y : r.x;
    const rHi = axis === "x" ? r.y + r.h : r.x + r.w;
    if (rHi < spanLo || rLo > spanHi) continue;
    const before = (axis === "x" ? r.x : r.y) - 1;
    const after = (axis === "x" ? r.x + r.w : r.y + r.h) + 1;
    if (before >= params.lo && before <= params.hi) out.push(before);
    if (after >= params.lo && after <= params.hi) out.push(after);
    if (out.length > 14) break;
  }
  return [...new Set(out)];
}

/** Per-edge route memo: reused while an edge's ports, adjustment, and the
 *  whole obstacle field are unchanged (e.g. dragging ONE connector leaves
 *  every other edge's route identical — no reason to re-score them). */
interface RouteCache {
  obstacleSig: string;
  routes: Map<string, { sig: string; route: RoutedEdge }>;
}

/** Route every visible edge. Deterministic: same inputs → same paths. */
function routeEdges(
  specs: EdgeSpec[],
  nodeIds: string[],
  posOf: (id: string) => Pos | undefined,
  adjust: Record<string, EdgeAdjust>,
  cache?: RouteCache,
): RoutedEdge[] {
  // ── ports: group endpoints per (node, face), distribute along the face ────
  interface EndRef {
    spec: EdgeSpec;
    end: "a" | "b";
    slot: string; // shared slot id → shared port (manager trunk)
    sort: number; // far endpoint coordinate along the face
  }
  const groups = new Map<string, EndRef[]>();
  const faces = new Map<string, [Face, Face]>();
  const live: EdgeSpec[] = [];
  // Single definition of the slot policy: all of a manager's report-edges
  // leave through one trunk port; every other endpoint gets its own slot.
  // (portOf below must resolve the exact same id, or ports silently collapse
  // to face-center.)
  const slotIdFor = (s: EdgeSpec, end: "a" | "b", gk: string): string =>
    s.kind === "manager" && end === "a" ? `trunk:${gk}` : `${s.key}:${end}`;
  for (const s of specs) {
    const a = posOf(s.a);
    const b = posOf(s.b);
    if (!a || !b) continue;
    live.push(s);
    const [fa, fb] = pickFaces(a, b, s.kind);
    faces.set(s.key, [fa, fb]);
    const add = (node: string, face: Face, end: "a" | "b", other: Pos) => {
      const gk = `${node}|${face}`;
      const slot = slotIdFor(s, end, gk);
      const sort = faceAxis(face) === "y" ? center(other).x : center(other).y;
      const arr = groups.get(gk);
      const ref: EndRef = { spec: s, end, slot, sort };
      if (arr) arr.push(ref);
      else groups.set(gk, [ref]);
    };
    add(s.a, fa, "a", b);
    add(s.b, fb, "b", a);
  }

  // slot id → fraction along the face
  const slotT = new Map<string, number>();
  for (const [, refs] of groups) {
    const bySlot = new Map<string, number[]>();
    for (const r of refs) {
      const arr = bySlot.get(r.slot);
      if (arr) arr.push(r.sort);
      else bySlot.set(r.slot, [r.sort]);
    }
    const slots = [...bySlot.entries()].map(([slot, sorts]) => ({
      slot,
      sort: sorts.reduce((x, y) => x + y, 0) / sorts.length,
    }));
    slots.sort((x, y) => x.sort - y.sort || (x.slot < y.slot ? -1 : 1));
    slots.forEach(({ slot }, i) => slotT.set(slot, (i + 1) / (slots.length + 1)));
  }

  const portOf = (s: EdgeSpec, end: "a" | "b"): Pt & { face: Face } => {
    const node = end === "a" ? s.a : s.b;
    const face = faces.get(s.key)![end === "a" ? 0 : 1];
    const t = slotT.get(slotIdFor(s, end, `${node}|${face}`)) ?? 0.5;
    return { ...portPoint(posOf(node)!, face, t), face };
  };

  // ── route each edge ───────────────────────────────────────────────────────
  // Inflate every box once (each edge skips its own two endpoints by id), and
  // gate the per-edge route cache on the whole obstacle field being unchanged.
  const obstacles: Obstacle[] = [];
  for (const id of nodeIds) {
    const p = posOf(id);
    if (!p) continue;
    obstacles.push({
      id,
      x: p.x - EDGE_MARGIN,
      y: p.y - EDGE_MARGIN,
      w: p.w + EDGE_MARGIN * 2,
      h: p.h + EDGE_MARGIN * 2,
    });
  }
  if (cache) {
    const sig = obstacles.map((b) => `${b.id}:${b.x},${b.y},${b.w},${b.h}`).join("|");
    if (cache.obstacleSig !== sig) {
      cache.obstacleSig = sig;
      cache.routes.clear();
    }
  }

  const out: RoutedEdge[] = [];
  for (const s of live) {
    const pa = portOf(s, "a");
    const pb = portOf(s, "b");
    const stored = adjust[s.key];
    const edgeSig = `${pa.x},${pa.y},${pa.face}|${pb.x},${pb.y},${pb.face}|${stored ? `${stored.shape}:${stored.d}` : ""}`;
    const hit = cache?.routes.get(s.key);
    if (hit && hit.sig === edgeSig) {
      out.push(hit.route);
      continue;
    }
    const remember = (route: RoutedEdge): RoutedEdge => {
      cache?.routes.set(s.key, { sig: edgeSig, route });
      return route;
    };

    const family: EdgeShape =
      faceAxis(pa.face) === faceAxis(pb.face) ? (faceAxis(pa.face) === "y" ? "v" : "h") : "l";
    // Manager buses stay merged (no lane scatter); other kinds spread a little
    // so parallel connectors between the same rows don't stack.
    const lane = s.kind === "manager" ? 0 : laneOffset(s.key);

    if (family === "l") {
      // Single corner: vertical exit meets horizontal entry (or vice versa).
      const cornerFirst = faceAxis(pa.face) === "y";
      const corner = cornerFirst ? { x: pa.x, y: pb.y } : { x: pb.x, y: pa.y };
      out.push(
        remember({
          ...s,
          d: roundedPath([pa, corner, pb]),
          shape: "l",
          axis: null,
          mid: 0,
          defaultMid: 0,
          lo: 0,
          hi: 0,
          grip: null,
        }),
      );
      continue;
    }

    const compatible =
      stored && (stored.shape === family || (family === "h" && (stored.shape === "u" || stored.shape === "n")));

    if (compatible) {
      // The human placed this connector: honour their shape + offset verbatim.
      const shape = stored.shape as EdgeShape;
      // U-routes flip the ports onto the top/bottom faces.
      const [qa, qb] = uPorts(shape, s, posOf, pa, pb);
      const params = shapeParams(shape, qa, qb, lane);
      const mid = clamp(params.defaultMid + stored.d, params.lo, params.hi);
      const pts = shapePoints(shape, qa, qb, mid);
      out.push(remember(finishRoute(s, shape, qa, qb, pts, mid, params)));
      continue;
    }

    // Auto: score candidate middle segments; fewest box crossings wins, then
    // the smallest detour from the natural midpoint.
    const score = (pts: Pt[], m: number, base: number, bias: number) =>
      polylineCrossings(pts, obstacles, s.a, s.b) * 1000 + Math.abs(m - base) * 0.25 + bias;

    let best: { shape: EdgeShape; pa2: Pt & { face: Face }; pb2: Pt & { face: Face }; pts: Pt[]; mid: number; params: ShapeParams; cost: number } | null = null;
    const consider = (
      sh: EdgeShape,
      qa: Pt & { face: Face },
      qb: Pt & { face: Face },
      bias: number,
    ) => {
      const pr = shapeParams(sh, qa, qb, lane);
      for (const m of midCandidates(sh, pr, obstacles, qa, qb, s.a, s.b)) {
        const pts = shapePoints(sh, qa, qb, m);
        const cost = score(pts, m, pr.defaultMid, bias);
        if (!best || cost < best.cost) best = { shape: sh, pa2: qa, pb2: qb, pts, mid: m, params: pr, cost };
      }
    };

    consider(family, pa, pb, 0);
    if (family === "h") {
      // Same-row connectors often can't dodge sideways — offer the U-routes
      // (under/over the row) at a small penalty so the Z wins when it's clean.
      const [ua, ub] = uPorts("u", s, posOf, pa, pb);
      consider("u", ua, ub, 60);
      const [na, nb] = uPorts("n", s, posOf, pa, pb);
      consider("n", na, nb, 60);
    }

    const b = best!;
    out.push(remember(finishRoute(s, b.shape, b.pa2, b.pb2, b.pts, b.mid, b.params)));
  }
  return out;
}

/** Ports for U-routes: both ends leave through the bottom ('u') or top ('n')
 *  face, offset a touch per-edge so parallel U-routes don't overlap. */
function uPorts(
  shape: EdgeShape,
  s: EdgeSpec,
  posOf: (id: string) => Pos | undefined,
  pa: Pt & { face: Face },
  pb: Pt & { face: Face },
): [Pt & { face: Face }, Pt & { face: Face }] {
  if (shape !== "u" && shape !== "n") return [pa, pb];
  const face: Face = shape === "u" ? "S" : "N";
  const off = laneOffset(s.key) / 2;
  const mk = (id: string): Pt & { face: Face } => {
    const p = posOf(id)!;
    const t = clamp(0.5 + off / Math.max(1, p.w), 0.15, 0.85);
    return { ...portPoint(p, face, t), face };
  };
  return [mk(s.a), mk(s.b)];
}

function finishRoute(
  s: EdgeSpec,
  shape: EdgeShape,
  pa: Pt,
  pb: Pt,
  pts: Pt[],
  mid: number,
  params: ShapeParams,
): RoutedEdge {
  // A pinned segment (boxes too close — lo/hi collapsed) has no travel range:
  // don't offer the drag, or a grab would just record a junk zero adjustment.
  const locked = params.lo >= params.hi;
  const axis: "x" | "y" | null = locked ? null : shape === "h" ? "x" : "y";
  const grip: Pt | null = locked
    ? null
    : axis === "x"
      ? { x: mid, y: (pa.y + pb.y) / 2 }
      : { x: (pa.x + pb.x) / 2, y: mid };
  return {
    ...s,
    d: roundedPath(pts),
    shape,
    axis,
    mid,
    defaultMid: params.defaultMid,
    lo: params.lo,
    hi: params.hi,
    grip,
  };
}

const EDGE_LEGEND_KEY: Record<EdgeKind, string> = {
  manager: "legend.manager",
  talk: "legend.talk",
  coworker: "legend.coworker",
  access: "legend.access",
};

const SCOPED_CSS = `
.org-root { display: flex; flex-direction: column; height: 100%; min-height: 0; }
/* Live/Offline pill — these rules otherwise live only in ActivityView's scoped
   <style>, which is unmounted on this route, so the pill needs its own copy. */
.org-root .a2a-live { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600; padding: 3px 10px 3px 8px; border-radius: var(--r-pill); border: 1px solid var(--border); background: var(--surface-2); color: var(--text-3); }
.org-root .a2a-live.is-live { color: var(--success); border-color: color-mix(in srgb, var(--success) 35%, transparent); background: var(--success-soft); }
.org-root .a2a-live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-4); flex-shrink: 0; }
.org-root .a2a-live.is-live .dot { background: var(--success); box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 70%, transparent); animation: org-live-pulse 1.8s ease-out infinite; }
@keyframes org-live-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 55%, transparent); }
  70% { box-shadow: 0 0 0 6px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
.org-canvas-wrap { flex: 1; overflow: hidden; padding: 0; min-height: 0; position: relative; touch-action: none; cursor: grab; }
.org-canvas-wrap.panning { cursor: grabbing; }
.org-canvas-wrap.dragging, .org-canvas-wrap.dragging .org-node { cursor: grabbing; }
.org-stage { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
/* Snap grid — shown only mid drag/resize. 16px == GRID in this file. */
.org-stage.is-gridding {
  background-image: radial-gradient(color-mix(in srgb, var(--border-strong) 50%, transparent) 1px, transparent 1.2px);
  background-size: 16px 16px;
}
/* Glide: brief transform ease for fit-to-view / jump-to-node (never pans/drags). */
.org-stage.is-gliding { transition: transform 0.26s var(--ease); }
/* Semantic zoom: below ~55% the cards drop detail and grow their names, so a
   30–50 node overview stays legible. */
.org-stage.is-far .org-sub, .org-stage.is-far .org-subs, .org-stage.is-far .org-badge { display: none; }
.org-stage.is-far .org-name { font-size: 16px; }
.org-edges { position: absolute; inset: 0; overflow: visible; pointer-events: none; }
.org-edge { fill: none; stroke-linecap: round; stroke-linejoin: round; transition: opacity var(--t-fast) var(--ease), stroke-width var(--t-fast) var(--ease); }
.org-edge.manager { stroke: color-mix(in srgb, var(--text-3) 55%, var(--border-strong)); stroke-width: 1.75; }
.org-edge.access { stroke-width: 1.4; stroke-dasharray: 4 4; opacity: 0.7; }
.org-edge.coworker { stroke: var(--text-4); stroke-width: 1.3; stroke-dasharray: 2 5; opacity: 0.55; }
.org-edge.talk { stroke: color-mix(in srgb, var(--accent) 55%, transparent); stroke-width: 1.5; opacity: 0.65; }
.org-edge.talk.flow {
  stroke: var(--accent); stroke-width: 2.4; opacity: 1;
  stroke-dasharray: 7 6; animation: org-dash 0.7s linear infinite;
  filter: drop-shadow(0 0 4px color-mix(in srgb, var(--accent) 60%, transparent));
}
/* Ego-network focus: hovering a card or connector dims everything else. */
.org-edges path.org-edge.is-dim { opacity: 0.07; }
.org-edge.manager.is-hot { stroke: var(--text-3); stroke-width: 2.4; }
.org-edge.access.is-hot { opacity: 1; stroke-width: 2; }
.org-edge.coworker.is-hot { opacity: 1; stroke-width: 2; }
.org-edge.talk.is-hot { opacity: 1; stroke-width: 2.2; stroke: var(--accent); }
/* Invisible fat twin of each connector: hover + drag hit area. */
.org-edge-hit { fill: none; stroke: transparent; stroke-width: 13; pointer-events: stroke; }
.org-grip { fill: var(--surface); stroke: var(--accent); stroke-width: 1.6; pointer-events: none; }
@keyframes org-dash { to { stroke-dashoffset: -26; } }
@keyframes org-subs-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
@keyframes org-subs-glow {
  0%, 100% { text-shadow: 0 0 3px color-mix(in srgb, var(--accent) 35%, transparent); }
  50% { text-shadow: 0 0 8px color-mix(in srgb, var(--accent) 85%, transparent); }
}
.org-node {
  position: absolute; box-sizing: border-box; border-radius: var(--r-sm);
  background: var(--surface); border: 1px solid var(--border);
  box-shadow: var(--shadow-sm); padding: 8px 10px; cursor: grab;
  transition: border-color var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease), opacity var(--t-fast) var(--ease);
  display: flex; flex-direction: column; gap: 2px; overflow: hidden;
  touch-action: none; user-select: none;
}
.org-node:hover { border-color: var(--border-strong); box-shadow: var(--shadow); }
.org-node.is-dim { opacity: 0.3; }
.org-node.is-match { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 0 12px color-mix(in srgb, var(--accent) 30%, transparent); }
/* Bottom-right resize grip — appears on hover/selection, drags w + h. */
.org-resize {
  position: absolute; right: 2px; bottom: 2px; width: 13px; height: 13px;
  cursor: nwse-resize; opacity: 0; touch-action: none;
  background:
    linear-gradient(135deg, transparent 0 44%, var(--text-4) 44% 56%, transparent 56% 100%),
    linear-gradient(135deg, transparent 0 70%, var(--text-4) 70% 82%, transparent 82% 100%);
  transition: opacity var(--t-fast) var(--ease);
}
.org-node:hover .org-resize, .org-node.is-selected .org-resize { opacity: 0.65; }
.org-resize:hover { opacity: 1; }
.org-node.is-selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.org-node.is-active { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.org-node.human {
  background: color-mix(in srgb, ${HUMAN_COLOR} 9%, var(--surface-2));
  border-color: color-mix(in srgb, ${HUMAN_COLOR} 42%, var(--border));
}
.org-node .org-name { font-weight: 600; font-size: 12.5px; font-family: var(--font-mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.org-node .org-sub { font-size: 11px; color: var(--text-3); white-space: normal; overflow: hidden; overflow-wrap: anywhere; line-height: 1.35; }
.org-node .org-sub.org-title-line { white-space: nowrap; text-overflow: ellipsis; flex-shrink: 0; }
.org-pip { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 0 1px var(--border); }
.org-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.org-dot.on { box-shadow: 0 0 0 3px var(--success-soft); }
.org-avatar { width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; font-size: 10.5px; font-weight: 700; color: #fff; }
.org-badge { font-size: 9.5px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; padding: 1px 5px; border-radius: var(--r-pill); border: 1px solid var(--border); color: var(--text-3); white-space: nowrap; overflow-wrap: normal; }
.org-chip { font-size: 11px; font-family: var(--font-mono); padding: 2px 7px; border-radius: var(--r-pill); background: var(--surface-2); border: 1px solid var(--border); color: var(--text-2); }
/* Search: the shared .input class does the field chrome; this wrapper only
   places the icon and the match counter. */
.org-search { position: relative; display: inline-flex; align-items: center; }
.org-search svg { position: absolute; left: 9px; color: var(--text-4); pointer-events: none; }
.org-search input.input { width: 200px; font-size: 12.5px; padding-left: 30px; padding-right: 56px; }
.org-search .org-search-count {
  position: absolute; right: 10px; font-size: 11px; color: var(--text-4);
  font-family: var(--font-mono); pointer-events: none;
}
/* Subagents: a labeled footer strip pinned to the bottom of the parent card,
   one subagent per row. The icon sits at the top, aligned to the first row. */
.org-subs {
  margin-top: auto; display: flex; align-items: flex-start; gap: 5px; min-width: 0;
  padding: 4px 12px 0 0; border-top: 1px solid var(--border);
  font-size: 10.5px; color: var(--text-3);
}
.org-subs-ico { flex-shrink: 0; color: var(--text-4); margin-top: 2px; }
.org-subs-list {
  min-width: 0; display: flex; flex-direction: column; gap: 1px;
  font-family: var(--font-mono);
}
.org-subs-item {
  min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.4;
  transition: color var(--t-fast) var(--ease);
}
/* The icon pulses while any subagent of this card works; the specific subagent
   row that's working lights up in the accent colour and glows. */
.org-subs.is-working .org-subs-ico { color: var(--accent); animation: org-subs-pulse 0.9s ease-in-out infinite; }
.org-subs-item.is-working { color: var(--accent); animation: org-subs-glow 1.1s ease-in-out infinite; }
/* Connectors: one chip per configured messaging channel, between the title line
   and the subagents footer. 18px chips, 4px gap, 5px top margin: the same
   numbers as channelBrands.ts (CHIP_SIZE / CHIP_GAP / CHIP_ROW_H), so the "+N"
   overflow maths and what the browser draws never disagree. A healthy chip has
   no badge; the corner dot only appears when something needs a look. */
.org-chips { display: flex; gap: 4px; margin-top: 5px; min-width: 0; flex-shrink: 0; }
/* The card's height budgets one text line above the chips; a long description
   wrapping to two would push the chip row out of the card. */
.org-node .org-sub.is-one-line { white-space: nowrap; text-overflow: ellipsis; flex-shrink: 0; }
.org-ch {
  position: relative; flex-shrink: 0; box-sizing: border-box;
  display: inline-flex; align-items: center; justify-content: center; line-height: 1;
  transition: opacity var(--t-fast) var(--ease), filter var(--t-fast) var(--ease);
}
.org-ch.is-neutral { background: var(--surface-3); color: var(--text-2); }
.org-ch.is-more {
  background: var(--surface-2); border: 1px solid var(--border); color: var(--text-3);
  font-size: 9px; font-weight: 700; font-family: var(--font-mono); letter-spacing: -0.02em;
}
.org-ch-dot { position: absolute; top: -3px; right: -3px; width: 7px; height: 7px; border-radius: 50%; box-shadow: 0 0 0 2px var(--surface); }
.org-drawer .org-ch-dot, .org-legend-row .org-ch-dot { box-shadow: 0 0 0 2px var(--bg-elev); }
.org-ch.is-connecting .org-ch-dot { background: var(--warn); }
.org-ch.is-error { box-shadow: 0 0 0 1.5px var(--danger); }
.org-ch.is-error .org-ch-dot { background: var(--danger); }
.org-ch.is-stopped { filter: grayscale(1); opacity: 0.45; }
/* A message arriving on this connector right now (thread_activity "thinking"). */
.org-ch.is-active { animation: org-ch-pulse 1.2s ease-out infinite; }
@keyframes org-ch-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 75%, transparent); }
  70% { box-shadow: 0 0 0 6px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
.org-conn-list { display: flex; flex-direction: column; gap: 9px; }
.org-conn-row { display: flex; align-items: center; gap: 9px; font-size: 12.5px; min-width: 0; }
.org-conn-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.org-conn-detail { font-size: 11px; color: var(--text-3); line-height: 1.4; overflow-wrap: anywhere; }
.org-conn-status { font-size: 11px; font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.org-conn-status.is-connected { color: var(--success); }
.org-conn-status.is-connecting { color: var(--warn); }
.org-conn-status.is-error { color: var(--danger); }
.org-conn-status.is-stopped { color: var(--text-4); }
.org-legend-sep { width: 1px; height: 14px; background: var(--border-strong); }
.org-zoom { position: absolute; right: 14px; bottom: 14px; display: flex; flex-direction: column; gap: 6px; z-index: 10; }
.org-zoom button {
  width: 32px; height: 32px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--border); background: var(--surface); border-radius: var(--r-sm);
  color: var(--text-2); cursor: pointer; box-shadow: var(--shadow-sm);
}
.org-zoom button:hover { border-color: var(--border-strong); color: var(--text); }
.org-drawer { width: 320px; flex-shrink: 0; border-left: 1px solid var(--border); background: var(--bg-elev); overflow: auto; padding: 16px; }
.org-drawer h4 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-3); margin: 14px 0 6px; }
.org-legend-row { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--text-3); }
.org-legend-swatch { width: 18px; height: 0; border-top-width: 2px; border-top-style: solid; }
.org-node:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
/* On narrow screens the drawer becomes a bottom sheet instead of vanishing. */
@media (max-width: 920px) {
  .org-drawer {
    position: fixed; left: 0; right: 0; bottom: 0; width: auto; max-height: 55vh;
    border-left: none; border-top: 1px solid var(--border);
    border-radius: 12px 12px 0 0; z-index: 40;
    box-shadow: 0 -12px 32px rgba(0, 0, 0, 0.28);
  }
  /* The wrapped legend would strand the divider at a line end. */
  .org-legend-sep { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .org-edge.talk.flow { animation: none; }
  .org-subs.is-working .org-subs-ico, .org-subs-item.is-working { animation: none; }
  .org-ch.is-active { animation: none; box-shadow: 0 0 0 2px var(--accent); }
  .org-stage.is-gliding { transition: none; }
}
`;

// ── component ─────────────────────────────────────────────────────────────
export function OrgChartView() {
  const t = useT(STR);
  const toast = useToast();
  const nav = useNavigate();
  const [data, setData] = useState<OrgChartData | null>(null);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  // Persisted so the open detail node survives a left-nav round-trip, matching
  // the toggles and pan/zoom around it. A node that no longer exists resolves to
  // null (selectedAgent lookup), which just leaves the drawer closed.
  // The node whose drawer is open is the URL's `?node=<agent>`: a reload or a
  // copied link reopens it, opening one is a Back step and Back closes it.
  const [selectedParam, setSelectedParam] = useRememberedUrlParam("node", "orgchart.selected", { history: "push" });
  const selected = selectedParam;
  const setSelected = useCallback((name: string | null) => setSelectedParam(name), [setSelectedParam]);
  const [showPeople, setShowPeople] = usePersistentState("orgchart.people", true);
  // Admins implicitly reach every agent, so their access edges are pure noise by
  // default — opt in to draw them. Explicit owner/member grants always show.
  const [showAdminAccess, setShowAdminAccess] = usePersistentState("orgchart.adminAccess", false);
  const [showTalk, setShowTalk] = usePersistentState("orgchart.talk", true);
  const [showSubs, setShowSubs] = usePersistentState("orgchart.subs", true);
  const [showChannels, setShowChannels] = usePersistentState("orgchart.channels", true);
  const [showEmoji, setShowEmoji] = usePersistentState("orgchart.emoji", true);
  const [showOnlyOnline, setShowOnlyOnline] = usePersistentState("orgchart.online", true);
  const [view, setView] = usePersistentState<View>("orgchart.view", { scale: 1, tx: 0, ty: 0 });
  const [panning, setPanning] = useState(false);
  const [query, setQuery] = usePersistentState("orgchart.query", "");
  const [matchIdx, setMatchIdx] = useState(-1);
  // Hovered node id ("a:name"/"h:id") or edge key — drives the ego-network
  // focus (hot elements stay, the rest dim).
  const [hover, setHover] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // Manual placement overrides (node id → {x,y,w,h}). Seeded once from per-user
  // prefs (so a curated arrangement survives reloads + follows the human across
  // devices) and committed back, debounced, on each drag/resize gesture end.
  // Nodes the human never touched aren't here — they use the computed layout.
  const { prefs, setPref, loaded: prefsLoaded } = usePrefs();
  const [overrides, setOverrides] = useState<Record<string, Pos>>({});
  const overridesRef = useRef(overrides);
  overridesRef.current = overrides;
  // Manual connector adjustments (edge key → {shape, d}) — same lifecycle as
  // the node overrides, committed to prefs.orgchart_edges.
  const [edgeAdjust, setEdgeAdjust] = useState<Record<string, EdgeAdjust>>({});
  const edgeAdjustRef = useRef(edgeAdjust);
  edgeAdjustRef.current = edgeAdjust;
  // Full unfiltered edge-key universe (assigned after the memo below) — read
  // by commitEdgeAdjust to prune adjustments whose edge no longer exists.
  const allEdgeKeysRef = useRef<Set<string>>(new Set());
  // Per-edge route memo across renders — see RouteCache.
  const routeCacheRef = useRef<RouteCache>({ obstacleSig: "", routes: new Map() });
  const seededRef = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [edgeDragKey, setEdgeDragKey] = useState<string | null>(null);
  // Latest view, readable from the always-mounted drag listeners so pointer
  // deltas convert by the current zoom without re-subscribing each frame.
  const viewRef = useRef(view);
  viewRef.current = view;
  // Active drag/resize gesture (null when idle). `tap` fires if the press never
  // crossed DRAG_THRESHOLD, preserving click-to-select / open behaviour.
  const dragRef = useRef<{
    id: string;
    mode: "move" | "resize";
    sx: number;
    sy: number;
    base: Pos;
    moved: boolean;
    tap?: () => void;
  } | null>(null);
  // Active connector drag (null when idle). `prev` restores on Escape.
  const edgeDragRef = useRef<{
    key: string;
    shape: EdgeShape;
    axis: "x" | "y";
    sx: number;
    sy: number;
    base: number; // starting offset (mid - defaultMid)
    defaultMid: number;
    lo: number;
    hi: number;
    moved: boolean;
    liveD: number; // offset as of the latest pointermove (state can lag a frame)
    prev: EdgeAdjust | undefined;
  } | null>(null);

  useEffect(() => {
    if (!prefsLoaded || seededRef.current) return;
    seededRef.current = true;
    const saved = prefs.orgchart_layout;
    if (saved && typeof saved === "object") setOverrides(sanitizeOverrides(saved));
    const savedEdges = prefs.orgchart_edges;
    if (savedEdges && typeof savedEdges === "object") setEdgeAdjust(sanitizeEdgeAdjust(savedEdges));
  }, [prefsLoaded, prefs.orgchart_layout, prefs.orgchart_edges]);

  // Every node id the current graph knows (ignoring view toggles), so commits
  // can prune overrides for agents/humans that no longer exist — a stale entry
  // would otherwise keep inflating the canvas bounds forever.
  const allNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const h of data?.humans ?? []) ids.add(`h:${h.id}`);
    for (const a of data?.agents ?? []) ids.add(`a:${a.name}`);
    return ids;
  }, [data]);

  const commitOverrides = useCallback(
    (next: Record<string, Pos>) => {
      let out = next;
      if (allNodeIds.size) {
        out = { ...next };
        for (const k of Object.keys(out)) if (!allNodeIds.has(k)) delete out[k];
      }
      setPref("orgchart_layout", out);
    },
    [setPref, allNodeIds],
  );
  // Mirrors commitOverrides: pruning is a property of the commit path (any
  // caller), not of one gesture handler. Only prunes keys absent from the full
  // unfiltered edge universe — toggled-off edges keep their adjustments.
  const commitEdgeAdjust = useCallback(
    (next: Record<string, EdgeAdjust>) => {
      let out = next;
      if (allEdgeKeysRef.current.size) {
        out = { ...next };
        for (const k of Object.keys(out)) if (!allEdgeKeysRef.current.has(k)) delete out[k];
      }
      setPref("orgchart_edges", out);
    },
    [setPref],
  );

  const resetLayout = useCallback(() => {
    setOverrides({});
    commitOverrides({});
    setEdgeAdjust({});
    commitEdgeAdjust({});
    toast.push(t("toast.layoutReset"), "info");
  }, [commitOverrides, commitEdgeAdjust, toast, t]);

  // Live flow state. Peer talk edges key `${from}->${to}`; subagent work keys
  // `${parent}::${sublower}`. Values are expiry timestamps.
  const flowsRef = useRef<Map<string, number>>(new Map());
  const subFlowsRef = useRef<Map<string, number>>(new Map());
  // `agent::channel::thread` → expiry, for turns in flight that arrived on a connector.
  const chanFlowsRef = useRef<Map<string, number>>(new Map());
  const [flowVersion, setFlowVersion] = useState(0);

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const panRef = useRef({ active: false, sx: 0, sy: 0, bx: 0, by: 0 });

  // First load toasts on failure. The quiet refreshes after it (a roster change
  // on the live stream, and a slow tick while the tab is visible) keep the last
  // good chart on failure and skip the re-render when nothing changed. They are
  // what moves a connector chip from "connecting" to "connected" once a WhatsApp
  // QR is scanned, or flags one that just lost its session, without a reload.
  const lastChartJsonRef = useRef("");
  useEffect(() => {
    let cancelled = false;
    let debounce: ReturnType<typeof setTimeout> | null = null;
    const fetchChart = (quiet: boolean) =>
      api
        .get<OrgChartData>("/api/org-chart")
        .then((d) => {
          if (cancelled) return;
          const json = JSON.stringify(d);
          if (json === lastChartJsonRef.current) return;
          lastChartJsonRef.current = json;
          setData(d);
        })
        .catch(() => {
          if (!cancelled && !quiet) toast.push(t("toast.loadFail"), "error");
        });
    const visible = () => typeof document === "undefined" || document.visibilityState === "visible";
    fetchChart(false).finally(() => {
      if (!cancelled) setLoading(false);
    });
    const offStream = subscribeStream((frame) => {
      if ((frame as { type?: string }).type !== "agents_update") return;
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        if (visible()) void fetchChart(true);
      }, 600);
    });
    const tick = setInterval(() => {
      if (visible()) void fetchChart(true);
    }, CHART_REFRESH_MS);
    return () => {
      cancelled = true;
      if (debounce) clearTimeout(debounce);
      clearInterval(tick);
      offStream();
    };
  }, [toast, t]);

  // Live stream → animate the matching talk edge (peer) or satellite (subagent)
  // whenever a message flows.
  useEffect(() => {
    setConnected(getStreamState() === "open");
    const offState = subscribeStreamState((s) => setConnected(s === "open"));
    const offStream = subscribeStream((frame) => {
      if ((frame as ThreadActivityEvent).type === "thread_activity") {
        const d = (frame as ThreadActivityEvent).data;
        if (!d?.agent || !d.channel || NON_CONNECTOR_CHANNELS.has(d.channel)) return;
        // Per thread: two customers on one WhatsApp line are two turns, and the
        // first one ending must not stop the chip pulsing for the second.
        const key = `${d.agent}::${d.channel.toLowerCase()}::${d.thread_id || ""}`;
        if (d.status === "idle") {
          if (!chanFlowsRef.current.delete(key)) return;
        } else if (d.status === "thinking" || d.status === "streaming") {
          const known = chanFlowsRef.current.has(key);
          chanFlowsRef.current.set(key, Date.now() + CHANNEL_TURN_MAX_MS);
          // A streaming tick only extends a pulse already drawn: no re-render.
          if (known) return;
        } else {
          return;
        }
        setFlowVersion((v) => v + 1);
        return;
      }
      const ev = frame as AgentMessageEvent;
      if (ev.type !== "agent_message") return;
      const from = ev.data?.from;
      const to = ev.data?.to;
      if (!from || !to) return;
      if (ev.data?.kind === "subagent") {
        subFlowsRef.current.set(`${from}::${to.toLowerCase()}`, Date.now() + FLOW_MS);
      } else {
        flowsRef.current.set(`${from}->${to}`, Date.now() + FLOW_MS);
      }
      setFlowVersion((v) => v + 1);
    });
    return () => {
      offState();
      offStream();
    };
  }, []);

  // Expire stale flows on a light interval so highlights fade out on their own.
  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const map of [flowsRef.current, subFlowsRef.current, chanFlowsRef.current]) {
        for (const [k, exp] of map) {
          if (exp < now) {
            map.delete(k);
            changed = true;
          }
        }
      }
      if (changed) setFlowVersion((v) => v + 1);
    }, 300);
    return () => clearInterval(id);
  }, []);

  const visibleAgents = useMemo(() => {
    const list = data?.agents ?? [];
    return showOnlyOnline ? list.filter((a) => a.running) : list;
  }, [data, showOnlyOnline]);
  const visibleSet = useMemo(() => new Set(visibleAgents.map((a) => a.name)), [visibleAgents]);

  const managerEdges = useMemo(
    () => (data?.edges.manager ?? []).filter((e) => visibleSet.has(e.manager) && visibleSet.has(e.report)),
    [data, visibleSet],
  );

  const layout = useMemo(
    () => (data ? computeLayout(data.humans, visibleAgents, managerEdges, showSubs, showChannels) : null),
    [data, visibleAgents, managerEdges, showSubs, showChannels],
  );
  const agentByName = useMemo(() => {
    const m = new Map<string, OrgAgent>();
    for (const a of data?.agents ?? []) m.set(a.name, a);
    return m;
  }, [data]);

  // Stage/SVG size: the auto-layout bounds grown to enclose any node a human
  // dragged or resized past them, so pan/fit/edges still reach it.
  const canvas = useMemo(() => {
    let w = layout?.canvasW ?? 0;
    let h = layout?.canvasH ?? 0;
    for (const o of Object.values(overrides)) {
      w = Math.max(w, o.x + o.w + PAD_X);
      h = Math.max(h, o.y + o.h + PAD_BOTTOM);
    }
    return { w, h };
  }, [layout, overrides]);

  // Active flow keys + the agent names currently exchanging messages. `subParents`
  // are agents with a subagent (task) running right now (their footer icon
  // pulses); `subFlowKeys` are the exact `parent::subagent` pairs in flight, so a
  // footer can glow only the subagent row that's actually working.
  const { flowKeys, activeNames, subParents, subFlowKeys } = useMemo(() => {
    void flowVersion; // re-derive when flows change
    const fk = new Set(flowsRef.current.keys());
    const sfk = new Set(subFlowsRef.current.keys());
    const names = new Set<string>();
    const subs = new Set<string>();
    for (const k of fk) {
      const [a, b] = k.split("->");
      if (a) names.add(a);
      if (b) names.add(b);
    }
    for (const k of sfk) {
      const a = k.split("::")[0];
      if (a) {
        names.add(a);
        subs.add(a);
      }
    }
    return { flowKeys: fk, activeNames: names, subParents: subs, subFlowKeys: sfk };
  }, [flowVersion]);

  // agent → the channels it is answering a message on right now.
  const busyChannels = useMemo(() => {
    void flowVersion;
    const m = new Map<string, Set<string>>();
    for (const k of chanFlowsRef.current.keys()) {
      const [agent, channel] = k.split("::");
      if (!agent || !channel) continue;
      const set = m.get(agent);
      if (set) set.add(channel);
      else m.set(agent, new Set([channel]));
    }
    return m;
  }, [flowVersion]);
  const channelBusy = useCallback(
    (agent: string, c: OrgChannel): boolean => {
      for (const ch of busyChannels.get(agent) ?? []) if (activityMatchesChannel(ch, c.type)) return true;
      return false;
    },
    [busyChannels],
  );
  // "WhatsApp · connected", or the failure reason when it needs attention.
  const connectorLabel = useCallback(
    (agent: string, c: OrgChannel): string => {
      const name = channelDisplayName(c.type);
      if (channelBusy(agent, c)) return `${name} · ${t("channel.active")}`;
      const st = channelStatus(c);
      const state = t(`channel.${st}`);
      return st === "error" && c.detail ? `${name} · ${state}: ${c.detail}` : `${name} · ${state}`;
    },
    [channelBusy, t],
  );
  const anyChannels = useMemo(() => visibleAgents.some((a) => (a.channels ?? []).length > 0), [visibleAgents]);

  // Cards drawing a connector chip row ("a:<name>").
  const chipCards = useMemo(
    () =>
      new Set(
        showChannels ? visibleAgents.filter((a) => (a.channels ?? []).length > 0).map((a) => `a:${a.name}`) : [],
      ),
    [showChannels, visibleAgents],
  );

  // Effective position: a manual override if the human placed this node,
  // otherwise the computed layout. A hand-placed card keeps the height it was
  // saved with, which for a card placed before it had connectors is one chip
  // row short: a card with chips is never drawn shorter than its content.
  const pos = useCallback(
    (id: string): Pos | undefined => {
      const auto = layout?.positions.get(id);
      const o = overrides[id];
      if (!o) return auto;
      // A person card's auto height is its content's (one more line with a
      // title), so a hand-placed one is never drawn shorter than that either.
      if (auto && o.h < auto.h && (chipCards.has(id) || id.startsWith("h:"))) return { ...o, h: auto.h };
      return o;
    },
    [overrides, layout, chipCards],
  );

  // ── edges: specs from the wire graph, routed orthogonally ─────────────────
  const routed = useMemo<RoutedEdge[]>(() => {
    if (!data || !layout) return [];
    const specs: EdgeSpec[] = [];
    for (const e of managerEdges) {
      specs.push({ key: `m:${e.manager}->${e.report}`, kind: "manager", a: `a:${e.manager}`, b: `a:${e.report}` });
    }
    for (const e of data.edges.coworker ?? []) {
      if (!visibleSet.has(e.a) || !visibleSet.has(e.b)) continue;
      // `<->` separator: agent names may contain plain dashes, so `a-b` would
      // let distinct pairs collide (e.g. "data"+"ops-eu" vs "data-ops"+"eu").
      specs.push({ key: `c:${e.a}<->${e.b}`, kind: "coworker", a: `a:${e.a}`, b: `a:${e.b}` });
    }
    if (showPeople) {
      for (const e of data.edges.access) {
        if (!showAdminAccess && e.role === "admin") continue;
        if (!visibleSet.has(e.agent)) continue;
        specs.push({ key: `x:${e.user_id}->${e.agent}`, kind: "access", a: `h:${e.user_id}`, b: `a:${e.agent}`, role: e.role });
      }
    }
    if (showTalk) {
      for (const e of data.edges.talk) {
        if (!visibleSet.has(e.from) || !visibleSet.has(e.to)) continue;
        specs.push({ key: `t:${e.from}->${e.to}`, kind: "talk", a: `a:${e.from}`, b: `a:${e.to}` });
      }
    }
    const nodeIds = [
      ...(data.humans ?? []).map((h) => `h:${h.id}`),
      ...visibleAgents.map((a) => `a:${a.name}`),
    ];
    return routeEdges(specs, nodeIds, pos, edgeAdjust, routeCacheRef.current);
  }, [data, layout, managerEdges, visibleSet, visibleAgents, showPeople, showAdminAccess, showTalk, pos, edgeAdjust]);

  // Every edge key the current graph could draw (ignoring view toggles), so a
  // commit can prune adjustments for edges that no longer exist without
  // dropping ones that are merely toggled off.
  const allEdgeKeys = useMemo(() => {
    const keys = new Set<string>();
    if (!data) return keys;
    for (const e of data.edges.manager) keys.add(`m:${e.manager}->${e.report}`);
    for (const e of data.edges.coworker ?? []) keys.add(`c:${e.a}<->${e.b}`);
    for (const e of data.edges.access) keys.add(`x:${e.user_id}->${e.agent}`);
    for (const e of data.edges.talk) keys.add(`t:${e.from}->${e.to}`);
    return keys;
  }, [data]);
  allEdgeKeysRef.current = allEdgeKeys;

  // Ego-network focus: the hovered (or edge-dragged) element plus everything
  // it touches stays crisp; the rest of the chart dims. A stale hover (the
  // hovered element unmounted — a toggle flipped, so pointerleave never fired)
  // resolves to null rather than dimming the whole chart.
  const focus = useMemo(() => {
    const src = edgeDragKey ?? hover;
    if (!src) return null;
    const nodes = new Set<string>();
    const edges = new Set<string>();
    if (src.startsWith("a:") || src.startsWith("h:")) {
      const stillRendered =
        src.startsWith("h:") || visibleSet.has(src.slice(2));
      if (!stillRendered) return null;
      nodes.add(src);
      for (const r of routed) {
        if (r.a === src || r.b === src) {
          edges.add(r.key);
          nodes.add(r.a);
          nodes.add(r.b);
        }
      }
    } else {
      const r = routed.find((x) => x.key === src);
      if (!r) return null;
      edges.add(r.key);
      nodes.add(r.a);
      nodes.add(r.b);
    }
    return { nodes, edges };
  }, [hover, edgeDragKey, routed, visibleSet]);

  // ── search ────────────────────────────────────────────────────────────────
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    // Connector names count, so "whatsapp" finds every agent on WhatsApp.
    return visibleAgents.filter((a) =>
      `${a.name} ${a.display_name || ""} ${a.title || ""} ${a.description || ""} ${(a.channels ?? [])
        .map((c) => channelDisplayName(c.type))
        .join(" ")}`
        .toLowerCase()
        .includes(q),
    );
  }, [query, visibleAgents]);
  const matchSet = useMemo(() => new Set(matches.map((a) => `a:${a.name}`)), [matches]);
  // Restart the Enter-cycle whenever the match list changes — either from
  // typing or from a visibility toggle shrinking it (a stale index would show
  // "4/2" and skip entries).
  useEffect(() => setMatchIdx(-1), [query, matches.length]);

  // Brief transform transition so fit / jump-to-node glides instead of teleporting
  // (never during pan/drag — those set the transform every frame).
  const [gliding, setGliding] = useState(false);
  const glideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const glide = useCallback(() => {
    setGliding(true);
    if (glideTimer.current) clearTimeout(glideTimer.current);
    glideTimer.current = setTimeout(() => setGliding(false), 280);
  }, []);
  useEffect(
    () => () => {
      if (glideTimer.current) clearTimeout(glideTimer.current);
    },
    [],
  );

  const focusNode = useCallback(
    (name: string) => {
      // Always open the drawer — even for agents with no canvas position
      // (e.g. a stopped agent while "Show stopped" is off); just skip the pan.
      setSelected(name);
      const p = pos(`a:${name}`);
      const el = wrapRef.current;
      if (!p || !el) return;
      const s = clamp(Math.max(viewRef.current.scale, 0.9), MIN_SCALE, MAX_SCALE);
      glide();
      setView({
        scale: s,
        tx: el.clientWidth / 2 - (p.x + p.w / 2) * s,
        ty: el.clientHeight / 2 - (p.y + p.h / 2) * s,
      });
    },
    [pos, setView, setSelected, glide],
  );

  const jumpToMatch = useCallback(() => {
    if (!matches.length) return;
    const next = (matchIdx + 1) % matches.length;
    setMatchIdx(next);
    focusNode(matches[next].name);
  }, [matches, matchIdx, focusNode]);

  // ── drag / resize (nodes) ─────────────────────────────────────────────────
  // Start a move (press on a card) or resize (press on its corner handle).
  const beginDrag = useCallback(
    (e: React.PointerEvent, id: string, mode: "move" | "resize", tap?: () => void) => {
      if (e.button !== 0) return; // left button only
      const base = pos(id);
      if (!base) return;
      e.stopPropagation(); // never let this also start a canvas pan
      e.preventDefault();
      dragRef.current = { id, mode, sx: e.clientX, sy: e.clientY, base: { ...base }, moved: false, tap };
      setDragging(true);
    },
    [pos],
  );

  // One pair of window listeners for the component's life — they read dragRef so
  // a gesture survives the pointer leaving the node, and viewRef so deltas track
  // the current zoom. Commit (persist) only when the node actually moved.
  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < DRAG_THRESHOLD) return;
      d.moved = true;
      const s = viewRef.current.scale || 1;
      const next = applyDrag(d.base, d.mode, (e.clientX - d.sx) / s, (e.clientY - d.sy) / s);
      // Grid snapping means most pointermove frames land on the same cell —
      // skip the no-op frames so the router doesn't re-run for nothing.
      const cur = overridesRef.current[d.id];
      if (cur && cur.x === next.x && cur.y === next.y && cur.w === next.w && cur.h === next.h) return;
      setOverrides((prev) => ({ ...prev, [d.id]: next }));
    };
    const onUp = () => {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d) return;
      setDragging(false);
      if (d.moved) commitOverrides(overridesRef.current);
      else d.tap?.();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [commitOverrides]);

  // ── drag (connectors) ─────────────────────────────────────────────────────
  // Grab a connector's middle segment and slide it along its free axis. The
  // offset from the auto position persists per user; double-click resets.
  const beginEdgeDrag = useCallback((e: React.PointerEvent, r: RoutedEdge) => {
    if (!r.axis) return;
    if (e.button !== 0) return; // left button only — leave context menus alone
    e.stopPropagation(); // never let this start a canvas pan
    e.preventDefault();
    edgeDragRef.current = {
      key: r.key,
      shape: r.shape,
      axis: r.axis,
      sx: e.clientX,
      sy: e.clientY,
      base: r.mid - r.defaultMid,
      defaultMid: r.defaultMid,
      lo: r.lo,
      hi: r.hi,
      moved: false,
      liveD: r.mid - r.defaultMid,
      prev: edgeAdjustRef.current[r.key],
    };
    setEdgeDragKey(r.key);
  }, []);

  // Idempotent per-connector reset (double-click) — safe to fire from both the
  // native dblclick and the pointer-tap fallback below.
  const resetEdge = useCallback(
    (key: string) => {
      if (!(key in edgeAdjustRef.current)) return;
      const next = { ...edgeAdjustRef.current };
      delete next[key];
      setEdgeAdjust(next);
      commitEdgeAdjust(next);
      toast.push(t("toast.edgeReset"), "info");
    },
    [commitEdgeAdjust, toast, t],
  );

  // Two quick taps on the same connector = reset. Kept alongside the native
  // dblclick handler because preventDefault on pointerdown suppresses the
  // synthesized mouse events (and so dblclick) in some browsers.
  const lastEdgeTapRef = useRef<{ key: string; t: number }>({ key: "", t: 0 });

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const d = edgeDragRef.current;
      if (!d) return;
      if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < DRAG_THRESHOLD) return;
      d.moved = true;
      const s = viewRef.current.scale || 1;
      const delta = (d.axis === "y" ? e.clientY - d.sy : e.clientX - d.sx) / s;
      const mid = clamp(d.defaultMid + d.base + delta, d.lo, d.hi);
      d.liveD = mid - d.defaultMid;
      setEdgeAdjust((prev) => ({ ...prev, [d.key]: { shape: d.shape, d: d.liveD } }));
    };
    const onUp = () => {
      const d = edgeDragRef.current;
      edgeDragRef.current = null;
      if (!d) return;
      setEdgeDragKey(null);
      if (!d.moved) {
        const now = Date.now();
        const lt = lastEdgeTapRef.current;
        if (lt.key === d.key && now - lt.t < 400) {
          lastEdgeTapRef.current = { key: "", t: 0 };
          resetEdge(d.key);
        } else {
          lastEdgeTapRef.current = { key: d.key, t: now };
        }
        return;
      }
      // Snap the segment to the lane lattice, then persist.
      const snapped = clamp(snapLane(d.defaultMid + d.liveD), d.lo, d.hi);
      const next: Record<string, EdgeAdjust> = {
        ...edgeAdjustRef.current,
        [d.key]: { shape: d.shape, d: snapped - d.defaultMid },
      };
      setEdgeAdjust(next);
      commitEdgeAdjust(next);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [commitEdgeAdjust, resetEdge]);

  // ── zoom / pan ──────────────────────────────────────────────────────────
  const fitToView = useCallback(() => {
    const el = wrapRef.current;
    if (!el || !layout) return;
    const pad = 48;
    const s = clamp(
      Math.min((el.clientWidth - pad * 2) / canvas.w, (el.clientHeight - pad * 2) / canvas.h),
      MIN_SCALE,
      MAX_SCALE,
    );
    glide();
    setView({
      scale: s,
      tx: (el.clientWidth - canvas.w * s) / 2,
      ty: Math.max(pad / 2, (el.clientHeight - canvas.h * s) / 2),
    });
  }, [layout, canvas, setView, glide]);

  const zoomBy = useCallback(
    (factor: number) => {
      const el = wrapRef.current;
      if (!el) return;
      const px = el.clientWidth / 2;
      const py = el.clientHeight / 2;
      setView((v) => {
        const ns = clamp(v.scale * factor, MIN_SCALE, MAX_SCALE);
        const k = ns / v.scale;
        return { scale: ns, tx: px - (px - v.tx) * k, ty: py - (py - v.ty) * k };
      });
    },
    [setView],
  );

  // Auto-fit once on first load (unless the user already has a stored view).
  const didFitRef = useRef(peekPersistentState("orgchart.view") !== undefined);
  useEffect(() => {
    if (didFitRef.current || !layout || !wrapRef.current) return;
    fitToView();
    didFitRef.current = true;
  }, [layout, fitToView]);

  // Trackpad-native wheel semantics (native non-passive listener so we can
  // preventDefault the page scroll): plain wheel/two-finger scroll pans;
  // ctrl/⌘-wheel — which is also what a macOS pinch emits — zooms toward the
  // cursor. Mouse users still zoom via the buttons, +/- keys or ctrl+wheel.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) {
        const rect = el.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        setView((v) => {
          const ns = clamp(v.scale * factor, MIN_SCALE, MAX_SCALE);
          const k = ns / v.scale;
          return { scale: ns, tx: px - (px - v.tx) * k, ty: py - (py - v.ty) * k };
        });
      } else {
        setView((v) => ({ ...v, tx: v.tx - e.deltaX, ty: v.ty - e.deltaY }));
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [setView]);

  // Keyboard: +/- zoom, 0 fit, / focuses search, Escape cancels an edge drag /
  // clears the search / closes the drawer (in that order). Skipped while typing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tgt = e.target as HTMLElement | null;
      const typing =
        !!tgt && (tgt.tagName === "INPUT" || tgt.tagName === "TEXTAREA" || tgt.isContentEditable);
      if (e.key === "Escape") {
        const d = edgeDragRef.current;
        if (d) {
          // Cancel the in-flight connector drag, restoring what was there.
          edgeDragRef.current = null;
          setEdgeDragKey(null);
          setEdgeAdjust((prev) => {
            const next = { ...prev };
            if (d.prev) next[d.key] = d.prev;
            else delete next[d.key];
            return next;
          });
          return;
        }
        if (typing) return; // let the input's own handler clear it
        if (selected) setSelected(null);
        return;
      }
      if (typing) return;
      // Never shadow browser shortcuts (ctrl/⌘ +/-/0 is page zoom).
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        zoomBy(1.2);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        zoomBy(1 / 1.2);
      } else if (e.key === "0") {
        e.preventDefault();
        fitToView();
      } else if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (selected && e.key.startsWith("Arrow")) {
        // Nudge the selected card one grid cell — keyboard equivalent of a
        // drag. Only when focus is on the canvas/body, so arrows still scroll
        // the drawer or any focused widget normally.
        const el = e.target as HTMLElement | null;
        if (el && el !== document.body && !el.classList?.contains("org-node")) return;
        const id = `a:${selected}`;
        const base = pos(id);
        if (!base) return;
        e.preventDefault();
        const dx = e.key === "ArrowLeft" ? -GRID : e.key === "ArrowRight" ? GRID : 0;
        const dy = e.key === "ArrowUp" ? -GRID : e.key === "ArrowDown" ? GRID : 0;
        // Same adjust→commit pipeline as a pointer drag: pure state update,
        // commit as a sibling (never inside the updater — it must stay pure).
        const merged = { ...overridesRef.current, [id]: applyDrag(base, "move", dx, dy) };
        setOverrides(merged);
        commitOverrides(merged);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomBy, fitToView, selected, setSelected, pos, commitOverrides]);

  const onPointerDown = (e: React.PointerEvent) => {
    const tgt = e.target as HTMLElement;
    if (tgt.closest(".org-node") || tgt.closest(".org-zoom")) return;
    panRef.current = { active: true, sx: e.clientX, sy: e.clientY, bx: view.tx, by: view.ty };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    setPanning(true);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const p = panRef.current;
    if (!p.active) return;
    setView((v) => ({ ...v, tx: p.bx + (e.clientX - p.sx), ty: p.by + (e.clientY - p.sy) }));
  };
  const onPointerUp = () => {
    if (panRef.current.active) {
      panRef.current.active = false;
      setPanning(false);
    }
  };
  // Connector hit paths stopPropagation on their own dblclick, so anything
  // arriving here is genuinely empty canvas (or a node/zoom control, filtered).
  const onCanvasDoubleClick = (e: React.MouseEvent) => {
    const tgt = e.target as HTMLElement;
    if (tgt.closest(".org-node") || tgt.closest(".org-zoom")) return;
    fitToView();
  };

  const livePill = (
    <span className={`a2a-live ${connected ? "is-live" : ""}`} title={connected ? t("live.title") : t("offline.title")}>
      <span className="dot" />
      {connected ? t("live") : t("offline")}
    </span>
  );

  const selectedAgent = selected ? agentByName.get(selected) : null;
  const stageStyle = { transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})` };
  const hasOverrides = Object.keys(overrides).length > 0 || Object.keys(edgeAdjust).length > 0;
  const gridding = dragging || edgeDragKey !== null;

  const nodeDim = (id: string) => (focus && !focus.nodes.has(id) ? " is-dim" : "");
  // Per-node hover wiring for the ego-network focus (same enter/leave pair on
  // cards and connector hit paths).
  const hoverProps = (id: string) => ({
    onPointerEnter: () => setHover(id),
    onPointerLeave: () => setHover((x) => (x === id ? null : x)),
  });
  const edgeClass = (r: RoutedEdge, flowing = false) => {
    // Live message flow always stays visible — never dimmed by hover focus.
    const dim = focus && !focus.edges.has(r.key) && !flowing ? " is-dim" : "";
    const hot = focus?.edges.has(r.key) ? " is-hot" : "";
    return `org-edge ${r.kind}${flowing ? " flow" : ""}${hot}${dim}`;
  };
  const edgeTitle = (r: RoutedEdge) => {
    const from = r.a.slice(2);
    const to = r.b.slice(2);
    const what = t(EDGE_LEGEND_KEY[r.kind]);
    return r.axis ? `${from} → ${to} · ${what}\n${t("edge.hint")}` : `${from} → ${to} · ${what}`;
  };

  // Draw order (bottom → top): access, coworker, manager, talk (arrows + flow
  // animation on top). Hit paths render after everything so they always win.
  const drawOrder: EdgeKind[] = ["access", "coworker", "manager", "talk"];
  const byKind = (k: EdgeKind) => routed.filter((r) => r.kind === k);

  return (
    <div className="org-root">
      <style>{SCOPED_CSS}</style>

      {/* page head */}
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 16, padding: "20px 24px 12px", flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.025em", lineHeight: 1.15 }}>{t("title")}</div>
            {livePill}
          </div>
          <div className="muted" style={{ fontSize: 13, marginTop: 6, lineHeight: 1.5, maxWidth: 760 }}>{t("desc")}</div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4, lineHeight: 1.5, opacity: 0.8 }}>{t("hint")}</div>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span className="org-search">
            <Search size={13} />
            <input
              ref={searchRef}
              className="input"
              value={query}
              placeholder={t("search.placeholder")}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  jumpToMatch();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  e.stopPropagation();
                  setQuery("");
                  (e.target as HTMLInputElement).blur();
                }
              }}
              aria-label={t("search.placeholder")}
            />
            {query.trim() && (
              <span className="org-search-count">
                {`${matchIdx >= 0 ? matchIdx + 1 : "–"}/${matches.length}`}
              </span>
            )}
          </span>
          <ChipToggle icon={<Users size={12} />} label={t("toggle.people")} on={showPeople} onClick={() => setShowPeople(!showPeople)} />
          {showPeople && (
            <ChipToggle icon={<KeyRound size={12} />} label={t("toggle.adminAccess")} on={showAdminAccess} onClick={() => setShowAdminAccess(!showAdminAccess)} />
          )}
          <ChipToggle icon={<ArrowLeftRight size={12} />} label={t("toggle.talk")} on={showTalk} onClick={() => setShowTalk(!showTalk)} />
          <ChipToggle icon={<Workflow size={12} />} label={t("toggle.subagents")} on={showSubs} onClick={() => setShowSubs(!showSubs)} />
          <ChipToggle icon={<Plug size={12} />} label={t("toggle.connectors")} on={showChannels} onClick={() => setShowChannels(!showChannels)} />
          <ChipToggle icon={<Sparkles size={12} />} label={t("toggle.emoji")} on={showEmoji} onClick={() => setShowEmoji(!showEmoji)} />
          <ChipToggle icon={<EyeOff size={12} />} label={t("toggle.showStopped")} on={!showOnlyOnline} onClick={() => setShowOnlyOnline(!showOnlyOnline)} />
          {hasOverrides && (
            <button
              className="btn ghost"
              style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, padding: "3px 9px" }}
              onClick={resetLayout}
              title={t("reset.title")}
            >
              <RotateCcw size={13} />
              {t("reset.layout")}
            </button>
          )}
        </div>
      </div>

      {/* legend */}
      <div className="row" style={{ gap: 16, padding: "0 24px 10px", flexWrap: "wrap" }}>
        <span className="org-legend-row"><span className="org-legend-swatch" style={{ borderTopColor: "var(--border-strong)" }} />{t("legend.manager")}</span>
        <span className="org-legend-row"><span className="org-legend-swatch" style={{ borderTopColor: "var(--accent)" }} />{t("legend.talk")}</span>
        <span className="org-legend-row"><span className="org-legend-swatch" style={{ borderTopColor: "var(--text-4)", borderTopStyle: "dotted" }} />{t("legend.coworker")}</span>
        <span className="org-legend-row"><span className="org-legend-swatch" style={{ borderTopColor: "var(--text-4)", borderTopStyle: "dashed" }} />{t("legend.access")}</span>
        <span className="org-legend-row"><span className="org-pip" style={{ background: HUMAN_COLOR }} />{t("legend.human")}</span>
        <span className="org-legend-row"><span className="org-pip" style={{ background: "var(--accent)" }} />{t("legend.owner")}</span>
        <span className="org-legend-row"><span className="org-pip" style={{ background: roleColor("admin") }} />{t("legend.admin")}</span>
        <span className="org-legend-row"><span className="org-pip" style={{ background: "var(--text-4)" }} />{t("legend.member")}</span>
        {showChannels && anyChannels && (
          <>
            <span className="org-legend-sep" aria-hidden />
            <span className="org-legend-row">
              <span className="org-ch is-neutral" style={{ width: 13, height: 13, borderRadius: 4, boxShadow: "0 0 0 2px var(--accent)" }} aria-hidden />
              {t("legend.channelActive")}
            </span>
            <span className="org-legend-row">
              <span className="org-ch is-neutral is-connecting" style={{ width: 13, height: 13, borderRadius: 4 }} aria-hidden>
                <span className="org-ch-dot" />
              </span>
              {t("legend.channelConnecting")}
            </span>
            <span className="org-legend-row">
              <span className="org-ch is-neutral is-error" style={{ width: 13, height: 13, borderRadius: 4 }} aria-hidden>
                <span className="org-ch-dot" />
              </span>
              {t("legend.channelError")}
            </span>
          </>
        )}
      </div>

      <div className="row" style={{ flex: 1, minHeight: 0, alignItems: "stretch" }}>
        {/* graph */}
        <div
          className={`org-canvas-wrap${panning ? " panning" : ""}${dragging ? " dragging" : ""}`}
          ref={wrapRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerUp}
          onDoubleClick={onCanvasDoubleClick}
        >
          {loading ? (
            <div className="spinner" style={{ margin: "48px auto" }} />
          ) : !data || visibleAgents.length === 0 || !layout ? (
            data && data.agents.length > 0 ? (
              // Agents exist but every one is stopped and "Show stopped" is
              // off — say so truthfully and offer the fix.
              <EmptyState
                icon={<EyeOff size={20} strokeWidth={1.75} />}
                title={t("empty.stopped.title")}
                desc={t("empty.stopped.desc")}
                cta={t("empty.stopped.cta")}
                onCta={() => setShowOnlyOnline(false)}
              />
            ) : (
              <EmptyState
                icon={<Network size={20} strokeWidth={1.75} />}
                title={t("empty.title")}
                desc={t("empty.desc")}
              />
            )
          ) : (
            <>
              <div
                className={`org-stage${gridding ? " is-gridding" : ""}${gliding ? " is-gliding" : ""}${view.scale < 0.55 ? " is-far" : ""}`}
                style={{ width: canvas.w, height: canvas.h, ...stageStyle }}
              >
                <svg className="org-edges" width={canvas.w} height={canvas.h}>
                  <defs>
                    <marker id="org-arrow" markerWidth="7" markerHeight="7" refX="5.5" refY="3" orient="auto" markerUnits="userSpaceOnUse">
                      <path d="M0,0 L6,3 L0,6 Z" fill="var(--accent)" />
                    </marker>
                  </defs>

                  {drawOrder.map((kind) =>
                    byKind(kind).map((r) => {
                      if (kind === "talk") {
                        const flowing = flowKeys.has(r.key.slice(2));
                        return (
                          <path
                            key={r.key}
                            className={edgeClass(r, flowing)}
                            markerEnd="url(#org-arrow)"
                            d={r.d}
                          />
                        );
                      }
                      if (kind === "access") {
                        return (
                          <path
                            key={r.key}
                            className={edgeClass(r)}
                            style={{ stroke: roleColor(r.role || "member") }}
                            d={r.d}
                          />
                        );
                      }
                      return <path key={r.key} className={edgeClass(r)} d={r.d} />;
                    }),
                  )}

                  {/* connector hover/drag affordance: grip dot on the hot edge */}
                  {routed.map((r) =>
                    r.grip && (focus?.edges.has(r.key) || edgeDragKey === r.key) && r.axis ? (
                      <circle key={`g:${r.key}`} className="org-grip" cx={r.grip.x} cy={r.grip.y} r={3.5} />
                    ) : null,
                  )}

                  {/* hit paths last so they always catch the pointer; the grab
                      band stays ~13 screen px wide at any zoom level */}
                  {routed.map((r) => (
                    <path
                      key={`h:${r.key}`}
                      className="org-edge-hit"
                      d={r.d}
                      style={{
                        strokeWidth: 13 / (view.scale || 1), // ~13 screen px at any zoom
                        cursor: r.axis ? (r.axis === "y" ? "ns-resize" : "ew-resize") : "default",
                      }}
                      onPointerDown={(e) => beginEdgeDrag(e, r)}
                      {...hoverProps(r.key)}
                      onDoubleClick={(e) => {
                        e.stopPropagation(); // a dbl-click on a connector is never a fit-to-view
                        e.preventDefault();
                        resetEdge(r.key);
                      }}
                    >
                      <title>{edgeTitle(r)}</title>
                    </path>
                  ))}
                </svg>

                {/* human nodes */}
                {data.humans.map((h) => {
                  const p = pos(`h:${h.id}`);
                  if (!p) return null;
                  const role = h.is_admin ? "admin" : h.agents[0]?.role || "member";
                  return (
                    <div
                      key={`h:${h.id}`}
                      className={`org-node human${nodeDim(`h:${h.id}`)}`}
                      style={{ left: p.x, top: p.y, width: p.w, height: p.h }}
                      onPointerDown={(e) => beginDrag(e, `h:${h.id}`, "move")}
                      {...hoverProps(`h:${h.id}`)}
                      title={h.email || h.name}
                    >
                      <div className="row" style={{ gap: 8, minWidth: 0 }}>
                        <span className="org-avatar" style={{ background: roleColor(role) }}>{initials(h.name)}</span>
                        <span style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 1, flex: 1 }}>
                          <span className="org-name" style={{ fontFamily: "var(--font-sans)" }}>{h.name}</span>
                          {!!(h.title || "").trim() && (
                            // One line: the card budgets exactly one for it.
                            <span className="org-sub org-title-line" title={h.title || undefined}>
                              {h.title}
                            </span>
                          )}
                          <span className="org-sub row" style={{ gap: 4 }}>
                            <span className="org-badge" style={{ borderColor: HUMAN_COLOR, color: HUMAN_COLOR }}>{t("badge.human")}</span>
                            <span className="org-badge" style={{ borderColor: roleColor(role), color: roleColor(role) }}>
                              {h.is_admin ? t("legend.admin") : role}
                            </span>
                          </span>
                        </span>
                      </div>
                      <span
                        className="org-resize"
                        onPointerDown={(e) => beginDrag(e, `h:${h.id}`, "resize")}
                        title={t("node.resize")}
                        aria-label={t("node.resize")}
                      />
                    </div>
                  );
                })}

                {/* agent nodes */}
                {visibleAgents.map((a) => {
                  const p = pos(`a:${a.name}`);
                  if (!p) return null;
                  const color = a.color || "var(--accent)";
                  const isActive = activeNames.has(a.name);
                  const isMatch = matchSet.has(`a:${a.name}`);
                  const channels = showChannels ? a.channels ?? [] : [];
                  const chips = chipLayout(channels.length, p.w);
                  const hidden = channels.slice(chips.shown);
                  return (
                    <div
                      key={`a:${a.name}`}
                      className={`org-node${selected === a.name ? " is-selected" : ""}${isActive ? " is-active" : ""}${isMatch ? " is-match" : ""}${nodeDim(`a:${a.name}`)}`}
                      style={{ left: p.x, top: p.y, width: p.w, height: p.h }}
                      onPointerDown={(e) => beginDrag(e, `a:${a.name}`, "move", () => setSelected(a.name))}
                      {...hoverProps(`a:${a.name}`)}
                      title={a.description || a.name}
                      role="button"
                      tabIndex={0}
                      aria-label={a.name}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelected(a.name);
                        }
                      }}
                    >
                      <div className="row" style={{ gap: 7, minWidth: 0 }}>
                        <span className="org-pip" style={{ background: color }} />
                        {showEmoji && <span style={{ fontSize: 15, lineHeight: 1 }}>{a.emoji || "🦁"}</span>}
                        <span className="org-name" style={{ flex: 1 }}>{agentLabel(a)}</span>
                        <span className={`org-dot${a.running ? " on" : ""}`} style={{ background: a.running ? "var(--success)" : "var(--text-5, var(--text-4))" }} title={a.running ? t("node.running") : t("node.stopped")} />
                      </div>
                      <div className={`org-sub${channels.length > 0 ? " is-one-line" : ""}`}>
                        {a.title || (a.reports.length ? `${a.reports.length} ${t("drawer.reports").toLowerCase()}` : a.description || t("node.untitled"))}
                      </div>
                      {channels.length > 0 && (
                        <div className="org-chips">
                          {channels.slice(0, chips.shown).map((c) => (
                            <ChannelChip
                              key={c.type}
                              channel={c}
                              label={connectorLabel(a.name, c)}
                              active={channelBusy(a.name, c)}
                            />
                          ))}
                          {chips.overflow > 0 && (
                            <ChannelOverflowChip
                              count={chips.overflow}
                              status={worstStatus(hidden)}
                              label={t("channel.more", {
                                n: String(chips.overflow),
                                names: hidden.map((c) => connectorLabel(a.name, c)).join(", "),
                              })}
                            />
                          )}
                        </div>
                      )}
                      {showSubs && a.subagents.length > 0 && (
                        <div
                          className={`org-subs${subParents.has(a.name) ? " is-working" : ""}`}
                          title={`${t("toggle.subagents")}: ${a.subagents.join(", ")}`}
                        >
                          <Workflow size={11} className="org-subs-ico" aria-hidden />
                          <span className="org-subs-list">
                            {a.subagents.map((s) => {
                              const working = subFlowKeys.has(`${a.name}::${s.toLowerCase()}`);
                              return (
                                <span key={s} className={`org-subs-item${working ? " is-working" : ""}`}>
                                  {s}
                                </span>
                              );
                            })}
                          </span>
                        </div>
                      )}
                      <span
                        className="org-resize"
                        onPointerDown={(e) => beginDrag(e, `a:${a.name}`, "resize")}
                        title={t("node.resize")}
                        aria-label={t("node.resize")}
                      />
                    </div>
                  );
                })}

              </div>

              {/* zoom controls */}
              <div className="org-zoom">
                <button onClick={() => zoomBy(1.2)} aria-label={t("zoom.in")} title={`${t("zoom.in")} (+)`}><ZoomIn size={16} /></button>
                <button onClick={() => zoomBy(1 / 1.2)} aria-label={t("zoom.out")} title={`${t("zoom.out")} (-)`}><ZoomOut size={16} /></button>
                <button onClick={fitToView} aria-label={t("zoom.fit")} title={`${t("zoom.fit")} (0)`}><Maximize2 size={15} /></button>
              </div>
            </>
          )}
        </div>

        {/* detail drawer */}
        {selectedAgent && (
          <aside className="org-drawer">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
              <div className="row" style={{ gap: 8, minWidth: 0 }}>
                {showEmoji && <span style={{ fontSize: 18 }}>{selectedAgent.emoji || "🦁"}</span>}
                <span className="org-name" style={{ fontSize: 15 }}>{selectedAgent.name}</span>
              </div>
              <button className="btn ghost sidebar-icon-btn" onClick={() => setSelected(null)} aria-label={t("drawer.close")} title={t("drawer.close")}>
                <X size={16} />
              </button>
            </div>
            {selectedAgent.description && (
              <div className="muted" style={{ fontSize: 12.5, marginTop: 6, lineHeight: 1.5 }}>{selectedAgent.description}</div>
            )}

            <h4>{t("drawer.title")}</h4>
            <div style={{ fontSize: 12.5 }}>{selectedAgent.title || t("drawer.none")}</div>

            <h4>{t("drawer.status")}</h4>
            <div className="row" style={{ gap: 6, fontSize: 12.5 }}>
              <span className="org-dot" style={{ background: selectedAgent.running ? "var(--success)" : "var(--text-4)" }} />
              {selectedAgent.running ? t("node.running") : t("node.stopped")}
            </div>

            <h4>{t("drawer.connectors")}</h4>
            {(selectedAgent.channels ?? []).length === 0 ? (
              <div className="muted" style={{ fontSize: 12.5 }}>{t("drawer.noConnectors")}</div>
            ) : (
              <div className="org-conn-list">
                {(selectedAgent.channels ?? []).map((c) => {
                  const st = channelStatus(c);
                  const busy = channelBusy(selectedAgent.name, c);
                  return (
                    <div key={c.type} className="org-conn-row">
                      <ChannelChip channel={c} size={22} label={connectorLabel(selectedAgent.name, c)} active={busy} />
                      <div className="org-conn-meta">
                        <span>{channelDisplayName(c.type)}</span>
                        {st === "error" && c.detail && <span className="org-conn-detail">{c.detail}</span>}
                      </div>
                      <span className={`org-conn-status is-${st}`}>{busy ? t("channel.active") : t(`channel.${st}`)}</span>
                    </div>
                  );
                })}
              </div>
            )}
            <button
              type="button"
              className="btn ghost"
              style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, padding: "3px 9px", marginTop: 8 }}
              onClick={() => nav(agentDetailPath(selectedAgent.name, "connectors"))}
            >
              <Plug size={13} />
              {t("drawer.manageConnectors")}
            </button>

            <h4>{t("drawer.manager")}</h4>
            <ChipRow names={selectedAgent.manager ? [selectedAgent.manager] : []} onClick={focusNode} known={agentByName} t={t} />

            <h4>{t("drawer.reports")}</h4>
            <ChipRow names={selectedAgent.reports} onClick={focusNode} known={agentByName} t={t} />

            <h4>{t("drawer.coworkers")}</h4>
            <ChipRow names={selectedAgent.coworkers ?? []} onClick={focusNode} known={agentByName} t={t} />

            <h4>{t("drawer.canMessage")}</h4>
            <Messaging agent={selectedAgent} dir="output" onClick={focusNode} known={agentByName} t={t} />

            <h4>{t("drawer.canBeMessagedBy")}</h4>
            <Messaging agent={selectedAgent} dir="input" onClick={focusNode} known={agentByName} t={t} />

            {selectedAgent.subagents.length > 0 && (
              <>
                <h4>{t("drawer.subagents")}</h4>
                <ChipRow names={selectedAgent.subagents} t={t} />
              </>
            )}

            <h4>{t("drawer.access")}</h4>
            <AccessList agent={selectedAgent.name} humans={data?.humans ?? []} t={t} />

            <button className="btn" style={{ marginTop: 16, width: "100%" }} onClick={() => nav(agentDetailPath(selectedAgent.name))}>
              {t("drawer.open")}
            </button>
          </aside>
        )}
      </div>
    </div>
  );
}

// ── small components ────────────────────────────────────────────────────────
// Pill toggle on the design system's .chat-chip (globals.css) — the same
// control ChatHeader and CalendarView use, so theming stays in one place.
function ChipToggle({
  icon,
  label,
  on,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" className={`chat-chip${on ? " is-on" : ""}`} onClick={onClick} aria-pressed={on}>
      {icon}
      {label}
    </button>
  );
}

function ChipRow({
  names,
  onClick,
  known,
  t,
}: {
  names: string[];
  onClick?: (name: string) => void;
  known?: Map<string, OrgAgent>;
  t: (k: string) => string;
}) {
  if (!names.length) return <div className="muted" style={{ fontSize: 12 }}>{t("drawer.none")}</div>;
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
      {names.map((n) => {
        const isKnown = known?.has(n);
        return (
          <span
            key={n}
            className="org-chip"
            style={{ cursor: onClick && isKnown ? "pointer" : "default", opacity: known && !isKnown ? 0.6 : 1 }}
            onClick={() => onClick && isKnown && onClick(n)}
          >
            {n}
          </span>
        );
      })}
    </div>
  );
}

/** Mode-aware messaging summary for one direction (everyone / no one / list /
 *  autogenerated from the org line). */
function Messaging({
  agent,
  dir,
  onClick,
  known,
  t,
}: {
  agent: OrgAgent;
  dir: "input" | "output";
  onClick?: (name: string) => void;
  known?: Map<string, OrgAgent>;
  t: (k: string) => string;
}) {
  if (agent.autogenerate) {
    const derived = [
      ...(agent.manager ? [agent.manager] : []),
      ...agent.reports,
      ...(agent.coworkers ?? []),
    ];
    return (
      <>
        <div className="muted" style={{ fontSize: 11.5, marginBottom: 4 }}>{t("drawer.autogen")}</div>
        <ChipRow names={derived} onClick={onClick} known={known} t={t} />
      </>
    );
  }
  const agents = dir === "input" ? agent.input_agents : agent.output_agents;
  const mode = (dir === "input" ? agent.input_mode : agent.output_mode) || (agents.length ? "list" : "everyone");
  if (mode === "everyone") return <div className="muted" style={{ fontSize: 12 }}>{t("drawer.everyone")}</div>;
  if (mode === "none") return <div className="muted" style={{ fontSize: 12 }}>{t("drawer.noone")}</div>;
  return <ChipRow names={agents} onClick={onClick} known={known} t={t} />;
}

function AccessList({ agent, humans, t }: { agent: string; humans: OrgHuman[]; t: (k: string) => string }) {
  const rows = humans
    .map((h) => ({ h, grant: h.agents.find((g) => g.agent === agent) }))
    .filter((r) => r.grant);
  if (!rows.length) return <div className="muted" style={{ fontSize: 12 }}>{t("drawer.none")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {rows.map(({ h, grant }) => (
        <div key={h.id} className="row" style={{ gap: 8, fontSize: 12.5, minWidth: 0 }}>
          <span className="org-avatar" style={{ width: 20, height: 20, fontSize: 9, background: roleColor(grant!.role) }}>{initials(h.name)}</span>
          <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.name}</span>
          <span className="org-badge" style={{ borderColor: roleColor(grant!.role), color: roleColor(grant!.role) }}>{grant!.role}</span>
        </div>
      ))}
    </div>
  );
}

function EmptyState({
  icon,
  title,
  desc,
  cta,
  onCta,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  cta?: string;
  onCta?: () => void;
}) {
  return (
    <div style={{ textAlign: "center", color: "var(--text-3)", margin: "56px auto", maxWidth: 440 }}>
      <div style={{ display: "inline-flex", width: 44, height: 44, alignItems: "center", justifyContent: "center", borderRadius: "50%", background: "var(--surface-2)", color: "var(--text-4)", marginBottom: 12 }}>
        {icon}
      </div>
      <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--text)", marginBottom: 4 }}>{title}</div>
      <div style={{ fontSize: 12.5, lineHeight: 1.55, marginBottom: cta ? 12 : 0 }}>{desc}</div>
      {cta && onCta && <button className="btn" onClick={onCta}>{cta}</button>}
    </div>
  );
}
