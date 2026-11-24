"""Shared sprite-sheet processing pipeline.

Single source of truth for chroma-key + grid detection + per-frame anchoring.
Imported by:
  - olano/runtime/api.py        — production sprite generation
  - scripts/debug_sprite_gen.py    — standalone debugging script

Everything in here is deterministic CPU work; no async, no I/O outside the
PIL Image objects passed in. Keep it that way so both call-sites have
identical behaviour.

Pipeline overview:
  1. detect_background()  — classifies bg as alpha / solid colour, picks tol
  2. detect_grid()        — gutter detection per axis, even-division fallback
  3. remove_background()  — soft-alpha chroma key + magenta despill
  4. frame_anchor()       — finds (foot-x, foot-y) per cell
  5. align_to_canvas()    — pastes onto fixed canvas, pinning the anchor
  6. crop_sheet()         — orchestrates 1-5 over a layout dict and returns
                            {pose_name: aligned PIL.Image}
"""

from __future__ import annotations

from io import BytesIO
from typing import cast

import numpy as np
from PIL import Image, ImageDraw

# ── Constants ────────────────────────────────────────────────────────────────

MIN_RUN = 20

# Output canvas — every frame ends up at this size with its foot pinned to
# (ANCHOR_CX, ANCHOR_FY). 320 tall (vs the 256 cell) gives extra head-room
# so taller sprites don't clip; 44 px below the foot leaves a small floor gap.
CANVAS_W = 256
CANVAS_H = 320
ANCHOR_CX = CANVAS_W // 2  # 128
ANCHOR_FY = 276


# ── Background detection ─────────────────────────────────────────────────────


def detect_background(img: Image.Image) -> tuple[str, tuple[int, int, int] | None, int]:
    """Classify the sheet's background. Returns (mode, rgb, tolerance).

    mode == "alpha"  → image already has transparency; rgb is None
    mode == "color"  → solid bg; rgb is the median corner colour
    """
    rgba = np.asarray(img.convert("RGBA"))
    h, w = rgba.shape[:2]
    alpha = rgba[..., 3]

    if (alpha < 64).sum() > 0.02 * h * w:
        return ("alpha", None, 64)

    corners = np.array(
        [
            rgba[0, 0, :3],
            rgba[0, w - 1, :3],
            rgba[h - 1, 0, :3],
            rgba[h - 1, w - 1, :3],
        ],
        dtype=np.int16,
    )

    if int(np.std(corners, axis=0).max()) < 30:
        median = np.median(corners, axis=0)
        bg: tuple[int, int, int] = (int(median[0]), int(median[1]), int(median[2]))
        # Magenta needs a wider tol because of high-saturation jpeg/png noise;
        # white (and other near-greyscale bgs) needs a tighter one.
        is_mag = abs(bg[0] - 255) < 40 and bg[1] < 60 and abs(bg[2] - 255) < 40
        return ("color", bg, 150 if is_mag else 60)

    return ("color", (255, 0, 255), 150)  # last-resort: assume magenta


def background_mask(
    img: Image.Image, bg_mode: str, bg_rgb: tuple[int, int, int] | None, tol: int
) -> np.ndarray:
    """Boolean array — True where the pixel is background."""
    if bg_mode == "alpha":
        return np.asarray(img.convert("RGBA"))[..., 3] < tol
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = np.array(bg_rgb, dtype=np.int16)
    return cast(np.ndarray, np.abs(rgb - bg).sum(axis=2) < tol)


def remove_background(
    img: Image.Image, bg_mode: str, bg_rgb: tuple[int, int, int] | None, tol: int
) -> Image.Image:
    """Soft-alpha chroma key + despill — produces clean anti-aliased edges.

    • Soft alpha: distance from bg colour ramps alpha 0→255 across a small
      transition zone, so anti-aliased edge pixels get partial transparency
      instead of a hard binary cut.
    • Despill: pulls magenta cast (R + B above G) out of edge pixels — kills
      the pink halo that survives a binary chroma key.
    """
    rgba = np.asarray(img.convert("RGBA")).copy().astype(np.float32)

    if bg_mode == "alpha":
        rgba[rgba[..., 3] < tol, 3] = 0
        return Image.fromarray(rgba.astype(np.uint8), "RGBA")

    bg = np.array(bg_rgb, dtype=np.float32)
    R, G, B = rgba[..., 0], rgba[..., 1], rgba[..., 2]

    distance = np.sqrt((R - bg[0]) ** 2 + (G - bg[1]) ** 2 + (B - bg[2]) ** 2)
    DIST_LOW = max(15.0, tol * 0.30)
    DIST_HIGH = max(60.0, tol * 0.85)
    alpha = np.clip((distance - DIST_LOW) / (DIST_HIGH - DIST_LOW), 0.0, 1.0) * 255.0

    is_magenta_bg = bg[0] > 200 and bg[1] < 80 and bg[2] > 200
    if is_magenta_bg:
        cast = np.maximum(np.minimum(R - G, B - G), 0)
        R = R - cast
        B = B - cast

    out = np.stack([R, G, B, alpha], axis=-1)
    out = np.clip(out, 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


# ── Grid detection ───────────────────────────────────────────────────────────


def _runs_of_false(mask: np.ndarray, min_run: int = MIN_RUN) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i, N = 0, len(mask)
    while i < N:
        if not mask[i]:
            j = i
            while j < N and not mask[j]:
                j += 1
            if j - i >= min_run:
                out.append((i, j - 1))
            i = j
        else:
            i += 1
    return out


def _even_ranges(total: int, n: int) -> list[tuple[int, int]]:
    return [(round(i * total / n), round((i + 1) * total / n) - 1) for i in range(n)]


def detect_grid(
    img: Image.Image,
    bg_mode: str,
    bg_rgb: tuple[int, int, int] | None,
    tol: int,
    *,
    expected_cols: int,
    expected_rows: int,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], str, str]:
    """Detect cell ranges per axis. Falls back to even division when gutter
    detection finds the wrong number of bands.

    Returns (col_ranges, row_ranges, col_source, row_source).
    *_source ∈ {"detected", "even"} so callers can log which axis used fallback.
    """
    is_bg = background_mask(img, bg_mode, bg_rgb, tol)
    detected_cols = _runs_of_false(cast(np.ndarray, is_bg.all(axis=0)))
    detected_rows = _runs_of_false(cast(np.ndarray, is_bg.all(axis=1)))

    w, h = img.size
    if len(detected_cols) == expected_cols:
        cols, col_src = detected_cols, "detected"
    else:
        cols, col_src = _even_ranges(w, expected_cols), "even"
    if len(detected_rows) == expected_rows:
        rows, row_src = detected_rows, "detected"
    else:
        rows, row_src = _even_ranges(h, expected_rows), "even"

    return cols, rows, col_src, row_src


def annotate_grid(img: Image.Image, cols: list, rows: list, out_path) -> None:
    """Draw detected cell boundaries on a copy of the sheet for debugging."""
    debug = img.convert("RGB").copy()
    draw = ImageDraw.Draw(debug)
    for i, (x0, x1) in enumerate(cols):
        draw.rectangle([x0, 0, x1, debug.height - 1], outline=(0, 255, 0), width=2)
        draw.text((x0 + 4, 4), f"C{i}", fill=(0, 255, 0))
    for i, (y0, y1) in enumerate(rows):
        draw.rectangle([0, y0, debug.width - 1, y1], outline=(255, 255, 0), width=2)
        draw.text((4, y0 + 4), f"R{i}", fill=(255, 255, 0))
    debug.save(out_path)


# ── Per-frame anchoring ──────────────────────────────────────────────────────


def frame_anchor(rgba_arr: np.ndarray, alpha_thresh: int = 16) -> tuple[int, int] | None:
    """Foot anchor for in-place animation:

      x = alpha-weighted x-centre of the BOTTOM HALF of the silhouette
          (lower body / feet — torso-stable across walk frames since arm-swing
          extents only widen the upper half of the bbox).
      y = lowest opaque pixel (foot baseline).

    Returns None if the cell is empty.
    """
    alpha = rgba_arr[..., 3] > alpha_thresh
    if not alpha.any():
        return None
    ys, xs = np.where(alpha)
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    half_y = (y0 + y1) // 2
    bottom = alpha[half_y : y1 + 1, x0 : x1 + 1].astype(np.int64)
    col_w = bottom.sum(axis=0)
    if col_w.sum() == 0:
        cx = (x0 + x1) // 2
    else:
        cols = np.arange(x0, x1 + 1)
        cx = int((cols * col_w).sum() // col_w.sum())
    return (cx, y1)


def align_to_canvas(
    sprite: Image.Image,
    *,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    anchor_x: int = ANCHOR_CX,
    anchor_y: int = ANCHOR_FY,
) -> Image.Image:
    """Place a sprite on a fixed-size canvas, pinning its foot anchor to
    (anchor_x, anchor_y). All frames sharing this canvas line up exactly so
    walk / stance loops play in place rather than drifting."""
    sprite_rgba = sprite.convert("RGBA")
    arr = np.asarray(sprite_rgba)
    anchor = frame_anchor(arr)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    if anchor is None:
        return canvas
    sx, sy = anchor
    canvas.paste(sprite_rgba, (anchor_x - sx, anchor_y - sy), sprite_rgba)
    return canvas


# ── Height normalisation — keep visual character size constant across poses ──


def normalize_heights(
    sprites: dict[str, Image.Image], *, target_char_h: int | None = None
) -> dict[str, Image.Image]:
    """Scale each sprite so the opaque character bbox has the same height.

    Why: a mid-stride walk frame's bbox is naturally shorter than a standing
    stance (back leg bent, body slightly compressed). The renderer sizes each
    pose by its TEXTURE height (`spriteH / tex.height`), so without this pass
    the walk frames render visibly smaller than the standing/coffee/sleep
    poses — the character "shrinks" while walking.

    This pass:
      1. Measures the bbox of opaque content in every sprite
      2. Picks a target height (median of all bboxes, robust to outliers)
      3. Uniformly scales each sprite so its bbox matches the target
      4. Re-aligns the scaled sprite onto the standard canvas with the foot
         pinned at (ANCHOR_CX, ANCHOR_FY) — so animation alignment survives

    Pass the FULL set of an agent's sprites (walks + stances together) so they
    all share the same target — that's what makes the character look the same
    size across walking, thinking, sleeping, etc.
    """
    if not sprites:
        return sprites

    # Measure character bbox for each sprite.
    measurements: dict[str, tuple[int, int, int, int] | None] = {}
    heights: list[int] = []
    for pose, img in sprites.items():
        bbox = img.getbbox()
        measurements[pose] = bbox
        if bbox is not None:
            h = bbox[3] - bbox[1]
            if h > 0:
                heights.append(h)

    if not heights:
        return sprites

    if target_char_h is None:
        # Median is robust to one outlier frame (e.g. an arm sticking up
        # weirdly) without distorting the whole set toward that frame.
        target_char_h = sorted(heights)[len(heights) // 2]

    out: dict[str, Image.Image] = {}
    for pose, img in sprites.items():
        bbox = measurements[pose]
        if bbox is None:
            out[pose] = img
            continue
        cur_h = bbox[3] - bbox[1]
        if cur_h <= 0:
            out[pose] = img
            continue
        scale = target_char_h / cur_h
        if abs(scale - 1.0) < 0.02:
            # Within 2 % — skip the resample, keep image quality.
            out[pose] = img
            continue
        new_w = max(1, int(round(img.width * scale)))
        new_h = max(1, int(round(img.height * scale)))
        scaled = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        # Re-anchor: align_to_canvas re-derives the foot from the scaled
        # silhouette, so foot lands at the same canvas coordinate again.
        out[pose] = align_to_canvas(scaled)

    return out


# ── Tight-crop pass — strip wasted empty space while keeping alignment ──────


def crop_to_common_bbox(
    sprites: dict[str, Image.Image], padding: int = 4
) -> dict[str, Image.Image]:
    """Crop every sprite to the UNION of their content bounding boxes.

    Every input sprite must already be on the same fixed canvas (i.e. they
    share a coordinate system thanks to align_to_canvas). The output dict
    has the same keys, all cropped to one common box just large enough to
    contain every non-transparent pixel across the whole set, plus a small
    padding margin.

    Why a UNION rather than a per-frame tight crop:
      - Per-frame tight crops produce different-sized images, so the frames
        no longer share a coordinate system → animation jitters.
      - A union bbox keeps all frames the same size → animation still plays
        in place, but every PNG is tight to the silhouette across the set.

    Pass the dict for a single agent's complete sprite set so walks and
    stances share the same crop box and the character renders at a
    consistent visual size when transitioning between idle and walking.
    """
    if not sprites:
        return sprites

    x0_all = y0_all = None
    x1_all = y1_all = None
    for img in sprites.values():
        bbox = img.getbbox()  # tightest box containing non-zero pixels
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        x0_all = x0 if x0_all is None else min(x0_all, x0)
        y0_all = y0 if y0_all is None else min(y0_all, y0)
        x1_all = x1 if x1_all is None else max(x1_all, x1)
        y1_all = y1 if y1_all is None else max(y1_all, y1)

    if x0_all is None:
        return sprites  # every sprite was empty
    # All four bounds are set together whenever x0_all is set.
    assert y0_all is not None and x1_all is not None and y1_all is not None

    # Apply padding and clamp to original canvas dimensions.
    sample = next(iter(sprites.values()))
    W, H = sample.size
    x0 = max(0, x0_all - padding)
    y0 = max(0, y0_all - padding)
    x1 = min(W, x1_all + padding)
    y1 = min(H, y1_all + padding)

    return {pose: img.crop((x0, y0, x1, y1)) for pose, img in sprites.items()}


# ── High-level orchestrator ──────────────────────────────────────────────────


def crop_sheet(
    sheet_bytes: bytes,
    layout: dict[str, tuple[int, int]],
    *,
    expected_cols: int = 4,
    expected_rows: int = 4,
    on_log=None,
) -> dict[str, Image.Image]:
    """Slice a sprite sheet into individual aligned frames.

    Args:
      sheet_bytes:    raw bytes of the PNG/JPG sheet
      layout:         {pose_name: (col_idx, row_idx)} — the same cell can
                      map to multiple poses (used for aliases like idle_left
                      = first walk_left frame).
      expected_cols:  expected grid width (default 4)
      expected_rows:  expected grid height (default 4)
      on_log:         optional callable(str) for progress reporting

    Returns: {pose_name: PIL.Image} — every image is CANVAS_W × CANVAS_H
             RGBA with the foot anchored to (ANCHOR_CX, ANCHOR_FY).
    """
    log = on_log or (lambda _: None)

    with Image.open(BytesIO(sheet_bytes)) as _src:
        sheet = _src.convert("RGBA")

    bg_mode, bg_rgb, tol = detect_background(sheet)
    log(f"background: mode={bg_mode} rgb={bg_rgb} tol={tol}")

    cols, rows, col_src, row_src = detect_grid(
        sheet,
        bg_mode,
        bg_rgb,
        tol,
        expected_cols=expected_cols,
        expected_rows=expected_rows,
    )
    log(f"grid: {len(cols)} cols ({col_src}) × {len(rows)} rows ({row_src})")

    out: dict[str, Image.Image] = {}
    for pose, (col, row) in layout.items():
        if col >= len(cols) or row >= len(rows):
            log(f"pose {pose} at ({col},{row}) beyond detected grid — skipping")
            continue
        x0, x1 = cols[col]
        y0, y1 = rows[row]
        cell = sheet.crop((x0, y0, x1 + 1, y1 + 1))
        transparent = remove_background(cell, bg_mode, bg_rgb, tol)
        out[pose] = align_to_canvas(transparent)

    return out
