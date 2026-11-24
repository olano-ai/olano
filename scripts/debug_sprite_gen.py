"""Standalone debug script for sprite generation.

Runs the same pipeline as the dashboard's "Generate cartoon" feature, but as a
plain Python script so we can iterate on prompts and cropping without hitting
the API + UI loop.

Pipeline:
  1. Generate portrait (1024×1024) using the reference image
  2. Generate page 1 walks  (1024×1024 sheet, 4 directions × 4 frames)
  3. Generate page 2 stances(1024×1024 sheet, 4 stances × 4 frames)
  4. Detect cell grid via magenta gutter scanning
  5. Crop each cell, chroma-key magenta → transparent
  6. Build looping GIFs for every animation row

Output structure:
  <out>/raw/portrait.png          — generated portrait
  <out>/raw/page1-walks.png       — generated walk-cycle sheet
  <out>/raw/page2-stances.png     — generated stance sheet
  <out>/raw/page1-walks.grid.png  — debug overlay showing detected cell boundaries
  <out>/raw/page2-stances.grid.png
  <out>/cells/<row>/<col>.png     — each cropped cell as transparent PNG
  <out>/gifs/<row>.gif            — looping animation per row (white background)
  <out>/gifs/<row>.transparent.gif — same but with magenta backdrop for visibility

Usage:
  uv run python scripts/debug_sprite_gen.py --image ~/Downloads/photo.png

  # Iterate on prompts without re-running expensive image gen:
  uv run python scripts/debug_sprite_gen.py \\
      --image ~/Downloads/photo.png \\
      --out ~/Desktop/sprite-debug-1 \\
      --skip-portrait --skip-page1 --skip-page2

  # Fresh run with description and emoji:
  uv run python scripts/debug_sprite_gen.py \\
      --image ~/Downloads/photo.png \\
      --description "young woman with cat ears, glasses, dark hair" \\
      --emoji "🐱"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

# Make `olano.sprites.prompts` importable when the script is run from the
# repo root (default `uv run` invocation).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from olano.sprites.processing import (
    align_to_canvas,
    annotate_grid,
    detect_background,
    detect_grid,
    remove_background,
)
from olano.sprites.processing import (
    frame_anchor as _frame_anchor,
)
from olano.sprites.prompts import (
    PAGE1_PROMPT,
    PAGE2_PROMPT,
    PORTRAIT_PROMPT_BASE,
    build_user_input,
    with_user_input,
)

# Sheet shape — these are the row/col counts our prompts ask the model to
# produce, and what `detect_grid` falls back to via even division if the
# model hands us something off-shape.
EXPECTED_COLS = 4
EXPECTED_ROWS = 4

PAGE1_ROW_LABELS = ("walk_left", "walk_right", "walk_up", "walk_down")
PAGE2_ROW_LABELS = ("sleeping", "coffee", "thinking", "laptop")


def make_gif(
    frames: list[Image.Image],
    out_path: Path,
    *,
    frame_ms: int,
    bg: tuple[int, int, int] = (255, 255, 255),
) -> None:
    if not frames:
        return
    flat = []
    for f in frames:
        canvas = Image.new("RGB", f.size, bg)
        if f.mode == "RGBA":
            canvas.paste(f, (0, 0), f)
        else:
            canvas.paste(f, (0, 0))
        flat.append(canvas)
    flat[0].save(
        out_path,
        save_all=True,
        append_images=flat[1:],
        duration=frame_ms,
        loop=0,
        format="GIF",
        disposal=2,
    )


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI calls
# ─────────────────────────────────────────────────────────────────────────────

# gpt-image-1 token pricing (USD per 1M tokens) — published by OpenAI 2025.
# https://openai.com/api/pricing/
GPT_IMAGE_1_PRICING = {
    "text_input": 5.00,  # $5 / 1M text input tokens
    "image_input": 10.00,  # $10 / 1M image input tokens (refs)
    "image_output": 40.00,  # $40 / 1M image output tokens (generated pixels)
}

# Per-image fallback cost when the API doesn't return usage data.
# These are rounded approximations for 1024×1024 quality=auto/medium.
GPT_IMAGE_1_FALLBACK_COST = {
    "1024x1024": 0.042,  # ~medium quality
    "1024x1536": 0.063,
    "1536x1024": 0.063,
}

# Running tally for the end-of-run total.
_TOTAL_COST_USD = 0.0


def _to_dict(obj):
    """Best-effort: convert pydantic / openai SDK objects to dicts for safe lookups."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    return None


def report_cost(resp, *, label: str, size: str) -> float:
    """Print token usage + computed cost for a single image-gen response.

    NOTE: OpenAI does NOT return cost in dollars. The API returns token counts
    in `usage`; we multiply by the published per-token rates above. So token
    counts are exact, dollar amounts are derived. Verify against your billing
    dashboard. If prices change, edit GPT_IMAGE_1_PRICING at the top of file.
    """
    global _TOTAL_COST_USD
    usage = _to_dict(getattr(resp, "usage", None))

    # Always print the raw usage payload so it can be cross-checked.
    print(f"[{label}] raw usage from API: {usage if usage else '(none returned)'}")

    if usage and "input_tokens" in usage:
        ti = usage.get("input_tokens_details") or {}
        text_in = int((ti or {}).get("text_tokens", 0) or 0)
        image_in = int((ti or {}).get("image_tokens", 0) or 0)
        # Some SDK versions only expose total input_tokens, not the breakdown.
        if not (text_in or image_in):
            text_in = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)

        cost_text = text_in * GPT_IMAGE_1_PRICING["text_input"] / 1_000_000
        cost_image = image_in * GPT_IMAGE_1_PRICING["image_input"] / 1_000_000
        cost_out = out_tok * GPT_IMAGE_1_PRICING["image_output"] / 1_000_000
        cost = cost_text + cost_image + cost_out
        _TOTAL_COST_USD += cost
        print(
            f"[{label}] computed cost: ${cost:.4f}  ="
            f"  text {text_in}×${GPT_IMAGE_1_PRICING['text_input']}/1M (${cost_text:.4f})"
            f" + img_in {image_in}×${GPT_IMAGE_1_PRICING['image_input']}/1M (${cost_image:.4f})"
            f" + img_out {out_tok}×${GPT_IMAGE_1_PRICING['image_output']}/1M (${cost_out:.4f})"
        )
        return cost

    # No usage in response — fall back to a flat per-image estimate.
    cost = GPT_IMAGE_1_FALLBACK_COST.get(size, 0.0)
    _TOTAL_COST_USD += cost
    print(
        f"[{label}] estimated cost: ~${cost:.4f}  (no token usage in response — flat {size} fallback)"
    )
    return cost


def _decode_image_response(resp, label: str) -> bytes:
    item = (resp.data or [None])[0]
    if item is None:
        raise RuntimeError(f"OpenAI returned no {label} data")
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        import httpx

        with httpx.Client(timeout=60.0) as h:
            return h.get(item.url).content
    raise RuntimeError(f"OpenAI returned no {label} bytes")


async def gen_portrait(
    client, model: str, prompt: str, ref_bytes: bytes | None, ref_filename: str
) -> bytes:
    print(f"[portrait]  model={model} ref={'yes' if ref_bytes else 'no'} prompt={len(prompt)}c")
    t = time.time()
    if ref_bytes:
        resp = await client.images.edit(
            model=model,
            image=(ref_filename, ref_bytes, "image/png"),
            prompt=prompt,
            quality="low",
            size="1024x1024",
        )
    else:
        resp = await client.images.generate(
            model=model,
            prompt=prompt,
            quality="low",
            size="1024x1024",
        )
    print(f"[portrait]  done in {time.time() - t:.1f}s")
    report_cost(resp, label="portrait", size="1024x1024")
    return _decode_image_response(resp, "portrait")


async def gen_sheet(
    client, model: str, prompt: str, refs: list[tuple[str, bytes, str]], label: str
) -> bytes:
    """Generate a magenta-background sprite sheet via gpt-image-2.

    The model paints #FF00FF where the prompt asks for transparent — the
    cropper then does soft-alpha chroma key + despill so edges land clean.
    """
    sizes = ", ".join(f"{n}={len(b) // 1024}KB" for (n, b, _) in refs)
    print(f"[{label}] model={model} refs=[{sizes}] prompt={len(prompt)}c")
    t = time.time()
    try:
        resp = await client.images.edit(
            model=model,
            image=refs if len(refs) > 1 else refs[0],
            prompt=prompt,
            quality="low",
            size="1024x1024",
        )
    except Exception as exc:
        # Drain whatever the SDK exposes — bare 4xx errors from gpt-image-2
        # often have empty messages; you need body+request_id to debug.
        bits = []
        for attr in ("status_code", "message", "body", "request_id"):
            v = getattr(exc, attr, None)
            if v:
                bits.append(f"{attr}={str(v)[:300]}")
        resp_obj = getattr(exc, "response", None)
        if resp_obj is not None:
            try:
                bits.append(f"resp.text={resp_obj.text[:300]}")
            except Exception:
                pass
        raise RuntimeError(f"[{label}] OpenAI call failed: {' | '.join(bits) or exc}") from exc
    print(f"[{label}] done in {time.time() - t:.1f}s")
    report_cost(resp, label=label, size="1024x1024")
    return _decode_image_response(resp, label)


# ─────────────────────────────────────────────────────────────────────────────
# Cropping + GIFs
# ─────────────────────────────────────────────────────────────────────────────


def crop_and_gif(
    sheet_path: Path, row_labels: tuple[str, ...], cells_dir: Path, gifs_dir: Path, *, frame_ms: int
) -> tuple[int, int]:
    """Always produces len(row_labels) row directories × EXPECTED_COLS cells.

    1. Auto-detect background (magenta / transparent / white / any solid).
    2. Gutter detection per axis with even-division fallback.
    3. Crop each cell, soft-alpha chroma key + despill.
    4. Per-frame anchoring: pin every frame's foot baseline + lower-body
       x-centre to the same canvas coords so the animation plays in place.
    5. Save tight-cropped (raw) AND aligned versions, plus per-row GIFs.
    """
    with Image.open(sheet_path) as im:
        sheet = im.convert("RGBA")

    bg_mode, bg_rgb, tol = detect_background(sheet)
    print(f"  background: mode={bg_mode}  rgb={bg_rgb}  tol={tol}")

    cols, rows, col_src, row_src = detect_grid(
        sheet,
        bg_mode,
        bg_rgb,
        tol,
        expected_cols=EXPECTED_COLS,
        expected_rows=len(row_labels),
    )
    print(f"  grid: {len(cols)} cols ({col_src}) × {len(rows)} rows ({row_src})")
    annotate_grid(sheet, cols, rows, sheet_path.with_suffix("").with_suffix(".grid.png"))

    cells_dir.mkdir(parents=True, exist_ok=True)
    gifs_dir.mkdir(parents=True, exist_ok=True)

    for r_idx, (y0, y1) in enumerate(rows):
        row_label = row_labels[r_idx] if r_idx < len(row_labels) else f"row{r_idx}"
        row_dir = cells_dir / row_label
        row_dir.mkdir(parents=True, exist_ok=True)
        aligned_frames: list[Image.Image] = []
        anchors: list[tuple[int, int] | None] = []
        for c_idx, (x0, x1) in enumerate(cols):
            cell = sheet.crop((x0, y0, x1 + 1, y1 + 1))
            transparent = remove_background(cell, bg_mode, bg_rgb, tol)
            # Per-frame anchored copy on a fixed canvas — what the animation uses.
            aligned = align_to_canvas(transparent)
            anchor = _frame_anchor(np.asarray(transparent))
            anchors.append(anchor)
            # Save BOTH versions: raw cell (for debugging) and aligned (for animation).
            transparent.save(row_dir / f"{c_idx + 1}.raw.png")
            aligned.save(row_dir / f"{c_idx + 1}.png")
            aligned_frames.append(aligned)
        anchors_str = ", ".join(f"({a[0]},{a[1]})" if a else "—" for a in anchors)
        print(f"    {row_label} per-frame anchors (x,y): {anchors_str}")
        frames = aligned_frames
        if frames:
            make_gif(frames, gifs_dir / f"{row_label}.gif", frame_ms=frame_ms, bg=(255, 255, 255))
            make_gif(frames, gifs_dir / f"{row_label}.dark.gif", frame_ms=frame_ms, bg=(40, 40, 40))
            print(
                f"  → {row_label}: {len(frames)} frames "
                f"({row_dir.name}/{1}.png … {len(frames)}.png)"
            )

    return len(cols), len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


async def amain() -> int:
    ap = argparse.ArgumentParser(
        description="Debug script for sprite-sheet generation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--image", required=True, type=Path, help="Reference photo (PNG/JPG/WebP)")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: ./sprite-debug/<timestamp>)",
    )
    ap.add_argument("--description", default="", help="Character description")
    ap.add_argument("--emoji", default="", help="Emoji inspiration")
    ap.add_argument("--style", default="", help="Additional style guidance")
    ap.add_argument(
        "--model", default="gpt-image-2", help="OpenAI image model (default: gpt-image-2)"
    )
    ap.add_argument(
        "--frame-ms", type=int, default=200, help="GIF frame duration in ms (default: 200)"
    )
    ap.add_argument(
        "--skip-portrait", action="store_true", help="Reuse existing raw/portrait.png in --out"
    )
    ap.add_argument("--skip-page1", action="store_true", help="Reuse existing raw/page1-walks.png")
    ap.add_argument(
        "--skip-page2", action="store_true", help="Reuse existing raw/page2-stances.png"
    )
    args = ap.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set in environment", file=sys.stderr)
        return 1

    image_path = args.image.expanduser()
    if not image_path.is_file():
        print(f"reference image not found: {image_path}", file=sys.stderr)
        return 1

    out = (args.out or (Path.cwd() / "sprite-debug" / time.strftime("%Y%m%d-%H%M%S"))).expanduser()
    raw_dir = out / "raw"
    cells_dir = out / "cells"
    gifs_dir = out / "gifs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cells_dir.mkdir(parents=True, exist_ok=True)
    gifs_dir.mkdir(parents=True, exist_ok=True)

    print(f"output: {out}")
    print(f"image:  {image_path}")
    print(f"model:  {args.model}\n")

    # Build prompts
    user_input = build_user_input(args.description, args.emoji, args.style)
    portrait_prompt = with_user_input(PORTRAIT_PROMPT_BASE, user_input)
    page1_prompt = with_user_input(PAGE1_PROMPT, user_input)
    page2_prompt = with_user_input(PAGE2_PROMPT, user_input)

    # Save the resolved prompts so we can see what was actually sent
    (out / "prompt-portrait.txt").write_text(portrait_prompt)
    (out / "prompt-page1.txt").write_text(page1_prompt)
    (out / "prompt-page2.txt").write_text(page2_prompt)

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)

    ref_bytes = image_path.read_bytes()
    ref_filename = image_path.name

    # ── 1. Portrait ───────────────────────────────────────────────────────
    portrait_path = raw_dir / "portrait.png"
    if args.skip_portrait and portrait_path.is_file():
        print(f"[portrait]  reusing {portrait_path}")
        portrait_bytes = portrait_path.read_bytes()
    else:
        portrait_bytes = await gen_portrait(
            client, args.model, portrait_prompt, ref_bytes, ref_filename
        )
        portrait_path.write_bytes(portrait_bytes)
        print(f"[portrait]  saved → {portrait_path}\n")

    # ── 2. Page 1 walks ───────────────────────────────────────────────────
    page1_path = raw_dir / "page1-walks.png"
    if args.skip_page1 and page1_path.is_file():
        print(f"[page1]     reusing {page1_path}")
        page1_bytes = page1_path.read_bytes()
    else:
        page1_refs = [
            ("portrait.png", portrait_bytes, "image/png"),
            (ref_filename, ref_bytes, "image/png"),
        ]
        page1_bytes = await gen_sheet(client, args.model, page1_prompt, page1_refs, "page1")
        page1_path.write_bytes(page1_bytes)
        print(f"[page1]     saved → {page1_path}\n")

    # ── 3. Page 2 stances ─────────────────────────────────────────────────
    page2_path = raw_dir / "page2-stances.png"
    if args.skip_page2 and page2_path.is_file():
        print(f"[page2]     reusing {page2_path}")
        page2_bytes = page2_path.read_bytes()
    else:
        # Re-encode page 1 to plain RGB before passing back as a ref —
        # gpt-image-2 sometimes 404s on its own outputs (alpha/profile quirks).
        with Image.open(BytesIO(page1_bytes)) as _src:
            buf = BytesIO()
            _src.convert("RGB").save(buf, format="PNG", optimize=True)
            page1_norm = buf.getvalue()
        page2_refs = [
            ("portrait.png", portrait_bytes, "image/png"),
            ("walks.png", page1_norm, "image/png"),
        ]
        page2_bytes = await gen_sheet(client, args.model, page2_prompt, page2_refs, "page2")
        page2_path.write_bytes(page2_bytes)
        print(f"[page2]     saved → {page2_path}\n")

    # ── 4-6. Crop + GIFs ──────────────────────────────────────────────────
    print("[crop+gif] page 1 walks")
    p1_cols, p1_rows = crop_and_gif(
        page1_path, PAGE1_ROW_LABELS, cells_dir / "walks", gifs_dir, frame_ms=args.frame_ms
    )

    print("\n[crop+gif] page 2 stances")
    p2_cols, p2_rows = crop_and_gif(
        page2_path, PAGE2_ROW_LABELS, cells_dir / "stances", gifs_dir, frame_ms=args.frame_ms
    )

    print(f"\n{'─' * 60}")
    print(f"DONE. output: {out}")
    print(f"  page 1: {p1_cols} cols × {p1_rows} rows  (expected 4×4)")
    print(f"  page 2: {p2_cols} cols × {p2_rows} rows  (expected 4×4)")
    if p1_cols != 4 or p1_rows != 4:
        print(f"  ⚠ page 1 grid is wrong — see {raw_dir}/page1-walks.grid.png")
    if p2_cols != 4 or p2_rows != 4:
        print(f"  ⚠ page 2 grid is wrong — see {raw_dir}/page2-stances.grid.png")
    if _TOTAL_COST_USD > 0:
        print(f"  💰 total OpenAI cost this run: ${_TOTAL_COST_USD:.4f}")
    else:
        print("  💰 no API calls billed (all stages skipped)")
    print(f"\nopen the gif folder to see animations:\n  open {gifs_dir}\n")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
