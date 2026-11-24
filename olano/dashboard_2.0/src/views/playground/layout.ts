// ── Playground office layout + image-space ↔ canvas-space (cover) math ──────
//
// Ported 1:1 from the original dashboard's `_PG_THEME1_LAYOUT` and the
// `_pg2ImageToScreen` / `_pg2ScreenToImage` helpers. The background office
// PNG is drawn with `object-fit: cover`; decor sprites and walk anchors are
// declared in *image space* (the PNG's native 1575×999 pixels) and projected
// into canvas pixels with the exact same scale + centre-offset the browser
// uses for `cover`, so everything stays glued to the painted floor and carpets
// at any viewport size.

export const IMG_W = 1575;
export const IMG_H = 999;

// `?v=` cache-buster: an earlier deploy served this stable-named public asset
// with `Cache-Control: immutable`, so browsers froze the old background. Bump
// this version whenever office-theme1.png changes to force a refetch.
export const BG_URL = "/assets/theme1/office-theme1.png?v=2";

// One piece of office furniture drawn on top of the background. Positioned by
// its bottom-centre anchor (ix, iy) — the contact point with the floor — at
// the given image-space width × height (iw × ih).
export interface DecorEntry {
  url: string;
  ix: number;
  iy: number;
  iw: number;
  ih: number;
  role: "sit" | "table";
}

// A walk destination declared in image space. `occupiedBy` is filled in at
// runtime when an agent claims the slot.
export interface AnchorSpec {
  ix: number;
  iy: number;
}

export type WalkableZone =
  | { kind: "rect"; x: number; y: number; w: number; h: number }
  | { kind: "ellipse"; cx: number; cy: number; rx: number; ry: number };

export interface ImagePoint {
  ix: number;
  iy: number;
}

export const DECOR: DecorEntry[] = [
  // top_window_green_rect_carpet — slim runner, two small floor cushions.
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion1-green.png", ix: 420, iy: 390, iw: 86, ih: 70, role: "sit" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion2-green.png", ix: 680, iy: 390, iw: 84, ih: 70, role: "sit" },
  // left_middle_beige_rect_carpet — reading nook, small table + two cushions.
  { url: "/assets/theme1/office-assets-theme-1-coffee-table2.png", ix: 284, iy: 548, iw: 105, ih: 110, role: "table" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion4-yellow.png", ix: 175, iy: 610, iw: 108, ih: 102, role: "sit" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion5-yellow.png", ix: 395, iy: 610, iw: 108, ih: 97, role: "sit" },
  // right_green_oval_carpet — lounge, round table + flanking cushions.
  { url: "/assets/theme1/office-assets-theme-1-coffee-table3.png", ix: 1179, iy: 482, iw: 175, ih: 136, role: "table" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion3-green.png", ix: 1050, iy: 525, iw: 132, ih: 110, role: "sit" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion5-yellow.png", ix: 1310, iy: 525, iw: 130, ih: 117, role: "sit" },
  // bottom_center_beige_rect_carpet — big lounge, largest table + 2 cushions.
  { url: "/assets/theme1/office-assets-theme-1-coffee-table1.png", ix: 781, iy: 798, iw: 260, ih: 194, role: "table" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion6-green.png", ix: 630, iy: 900, iw: 160, ih: 126, role: "sit" },
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion1-green.png", ix: 940, iy: 900, iw: 140, ih: 114, role: "sit" },
  // bottom_right_blue_oval_carpet — accent rug, single green cushion.
  { url: "/assets/theme1/office-assets-theme-1-seating-cushion3-green.png", ix: 1170, iy: 870, iw: 115, ih: 96, role: "sit" },
];

// "Desk" anchors — where WORKING agents stand (typing on their own laptop).
// Hand-picked spots on the carpets that avoid cushions/tables and are spaced.
export const WORK_ANCHORS: AnchorSpec[] = [
  { ix: 331, iy: 341 },
  { ix: 543, iy: 341 },
  { ix: 165, iy: 485 },
  { ix: 400, iy: 485 },
  { ix: 1075, iy: 560 },
  { ix: 1285, iy: 560 },
  { ix: 595, iy: 720 },
  { ix: 965, iy: 720 },
  { ix: 790, iy: 870 },
];

// "Mat" anchors — where agents curl up to sleep (the cushions).
export const REST_ANCHORS: AnchorSpec[] = [
  { ix: 630, iy: 840 },
  { ix: 940, iy: 840 },
  { ix: 1170, iy: 822 },
  { ix: 1050, iy: 468 },
  { ix: 1310, iy: 468 },
  { ix: 175, iy: 560 },
  { ix: 395, iy: 560 },
  { ix: 420, iy: 355 },
  { ix: 680, iy: 355 },
];

// Pantry / coffee-bar stations along the top-right counter.
export const COFFEE_STATIONS: AnchorSpec[] = [
  { ix: 870, iy: 290 },
  { ix: 1055, iy: 290 },
  { ix: 1230, iy: 290 },
];

// Walkable union — places an agent is allowed to stand or wander to.
export const WALKABLE: WalkableZone[] = [
  { kind: "rect", x: 299, y: 286, w: 497, h: 111 },
  { kind: "rect", x: 101, y: 461, w: 365, h: 154 },
  { kind: "rect", x: 548, y: 657, w: 466, h: 295 },
  { kind: "ellipse", cx: 1179, cy: 477, rx: 220, ry: 127 },
  { kind: "ellipse", cx: 1191, cy: 832, rx: 131, ry: 101 },
  { kind: "rect", x: 493, y: 404, w: 455, h: 246 },
  { kind: "rect", x: 85, y: 620, w: 445, h: 120 },
  { kind: "rect", x: 815, y: 268, w: 425, h: 145 },
  { kind: "rect", x: 990, y: 700, w: 200, h: 100 },
];

// Highway waypoints used to re-route around walls/planters when a straight
// segment leaves the walkable union.
export const WAYPOINTS: ImagePoint[] = [
  { ix: 721, iy: 527 },
  { ix: 540, iy: 470 },
  { ix: 900, iy: 480 },
  { ix: 540, iy: 640 },
  { ix: 300, iy: 680 },
  { ix: 875, iy: 410 },
  { ix: 1040, iy: 410 },
  { ix: 1080, iy: 750 },
];

// ── cover-mode projection ───────────────────────────────────────────────────

export interface CoverParams {
  scale: number;
  offsetX: number;
  offsetY: number;
  cw: number;
  ch: number;
}

/** Replicates CSS `background-size: cover`: pick the LARGER per-axis scale so
 *  the image fully covers the canvas, then centre it (offsets go negative on
 *  the overflowing axis). */
export function coverParams(cw: number, ch: number): CoverParams {
  const scale = Math.max(cw / IMG_W, ch / IMG_H);
  const drawnW = IMG_W * scale;
  const drawnH = IMG_H * scale;
  return { scale, offsetX: (cw - drawnW) / 2, offsetY: (ch - drawnH) / 2, cw, ch };
}

export function imageToScreen(ix: number, iy: number, p: CoverParams): { x: number; y: number } {
  return { x: ix * p.scale + p.offsetX, y: iy * p.scale + p.offsetY };
}

export function screenToImage(sx: number, sy: number, p: CoverParams): ImagePoint {
  return { ix: (sx - p.offsetX) / p.scale, iy: (sy - p.offsetY) / p.scale };
}

// ── navmesh ────────────────────────────────────────────────────────────────

/** True if image-space (ix, iy) is inside any walkable zone, optionally
 *  inflated by `tolerance` image-px so neighbouring rects read as connected. */
export function isImagePointWalkable(ix: number, iy: number, tolerance = 0): boolean {
  for (const z of WALKABLE) {
    if (z.kind === "rect") {
      if (ix >= z.x - tolerance && ix <= z.x + z.w + tolerance && iy >= z.y - tolerance && iy <= z.y + z.h + tolerance) {
        return true;
      }
    } else {
      const dx = (ix - z.cx) / (z.rx + tolerance);
      const dy = (iy - z.cy) / (z.ry + tolerance);
      if (dx * dx + dy * dy <= 1) return true;
    }
  }
  return false;
}

/** Sample 20 points along segment a→b; true iff all fall in the walkable
 *  union (18px tolerance bridges small gaps between adjacent rectangles). */
export function segmentIsWalkable(a: ImagePoint, b: ImagePoint): boolean {
  const steps = 20;
  const tolerance = 18;
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const ix = a.ix + (b.ix - a.ix) * t;
    const iy = a.iy + (b.iy - a.iy) * t;
    if (!isImagePointWalkable(ix, iy, tolerance)) return false;
  }
  return true;
}

/** Returns the waypoint list (excluding start) the agent should follow.
 *  Direct → one-hop → two-hop → last-resort direct. */
export function routeImagePath(start: ImagePoint, end: ImagePoint): ImagePoint[] {
  if (segmentIsWalkable(start, end)) return [end];
  for (const wp of WAYPOINTS) {
    if (segmentIsWalkable(start, wp) && segmentIsWalkable(wp, end)) return [wp, end];
  }
  for (const w1 of WAYPOINTS) {
    if (!segmentIsWalkable(start, w1)) continue;
    for (const w2 of WAYPOINTS) {
      if (w2 === w1) continue;
      if (segmentIsWalkable(w1, w2) && segmentIsWalkable(w2, end)) return [w1, w2, end];
    }
  }
  return [end];
}

/** Random image-space point inside any walkable zone. */
export function randomWalkableImagePoint(): ImagePoint {
  const z = WALKABLE[Math.floor(Math.random() * WALKABLE.length)];
  if (z.kind === "rect") {
    return { ix: z.x + Math.random() * z.w, iy: z.y + Math.random() * z.h };
  }
  const t = Math.random() * Math.PI * 2;
  const r = Math.sqrt(Math.random());
  return { ix: z.cx + Math.cos(t) * z.rx * r, iy: z.cy + Math.sin(t) * z.ry * r };
}

/** True if (ix, iy) falls inside any decor footprint (+padding). Decor is
 *  anchored bottom-centre, so a sprite at (cx, cy) covers
 *  x∈[cx-iw/2, cx+iw/2], y∈[cy-ih, cy]. */
export function isImagePointOnDecor(ix: number, iy: number, padding = 28): boolean {
  for (const e of DECOR) {
    const halfW = e.iw / 2 + padding;
    const top = e.iy - e.ih - padding;
    const bot = e.iy + padding;
    if (ix >= e.ix - halfW && ix <= e.ix + halfW && iy >= top && iy <= bot) return true;
  }
  return false;
}

/** Random walkable point that is NOT on any decor (a standing spot). */
export function randomFloorPoint(): ImagePoint {
  for (let i = 0; i < 20; i++) {
    const p = randomWalkableImagePoint();
    if (!isImagePointOnDecor(p.ix, p.iy)) return p;
  }
  return randomWalkableImagePoint();
}
