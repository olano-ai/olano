/**
 * How big a frame the picture needs, in device pixels: what the live view
 * tells the deployment (`{t: "view", w, h}`) so it never sends more pixels
 * than the phone draws. Zooming in asks for more, up to the page's own size,
 * which the deployment caps. Pure, so `node --test` covers it.
 */

export interface ViewSize {
  w: number;
  h: number;
}

/** Device pixels for a picture drawn `size` CSS pixels big at `dpr`, seen through `zoom`. */
export function viewSizeFor(size: { w: number; h: number }, dpr: number, zoom = 1): ViewSize | null {
  if (size.w <= 0 || size.h <= 0) return null;
  const scale = Math.max(1, dpr || 1) * Math.max(1, zoom || 1);
  return { w: Math.ceil(size.w * scale), h: Math.ceil(size.h * scale) };
}

/** Whether a new size is worth telling the deployment about (a few pixels are not: every change restarts the stream). */
export function viewSizeChanged(last: ViewSize | null, next: ViewSize | null, tolerance = 0.1): boolean {
  if (!next) return false;
  if (!last) return true;
  return Math.abs(next.w - last.w) > last.w * tolerance || Math.abs(next.h - last.h) > last.h * tolerance;
}
