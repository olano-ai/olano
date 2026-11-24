"""Draw the app icons and launch splash images for both platforms, and the
web dashboard's favicons and install icons.

    python3 scripts/make_icons.py        # needs Pillow: pip install pillow

The artwork is the mesh mark from the olano.ai wordmark (src/assets/logo.svg,
the same drawing as ui/Logo.tsx): sixteen nodes on two octagonal rings, joined
by 48 edges, drawn in brand orange on white. The logo draws it with hairline
strokes for a 38 px mark, so this keeps the geometry and thickens the strokes
for a launcher icon. It first checks that its node and edge lists still match
logo.svg, so the icon and the wordmark cannot drift apart unnoticed, then
writes assets/icon.svg (the master artwork, 1024 px) and every raster below.

The glyph is centred on the geometry's own centre (512, 512 in logo units) and
sized by the circle that encloses it, so the clear space is the same on every
side of every icon.

Two styles of the same geometry. FULL is the whole mesh, for anything drawn at
48 px or more. SMALL is for favicons and the dashboard's sidebar badge, where
the 48 crossing edges blur into a smudge: a heavy outer octagon keeps the
silhouette, and a light inner ring with the zig-zag between the rings keeps the
mesh texture, dropping the long outer chords and the node dots. At 16 px it
reads as the octagon; at 32 px the mesh shows.

Writes into the native projects, in the layout the Capacitor templates use:

  Android  res/mipmap-*/ic_launcher.png             legacy icon, 48 dp
           res/mipmap-*/ic_launcher_round.png       legacy round icon, 48 dp
           res/mipmap-*/ic_launcher_foreground.png  adaptive foreground, 108 dp,
                                                    glyph inside the 66 dp safe zone
           res/mipmap-anydpi-v26/ic_launcher*.xml   adaptive icon (white
                                                    background, glyph, monochrome)
           res/drawable*/splash.png                 pre-Android 12 launch splash
                                                    (on dark: orange tile, white mesh)
  iOS      AppIcon.appiconset/AppIcon-512@2x.png    1024 px, opaque
           Splash.imageset/splash-2732x2732*.png    launch splash
  Web      olano/dashboard_2.0/public/              favicon.svg and favicon-16/32.png
                                                    (SMALL), icon-192/512.png,
                                                    icon-maskable(-512).svg/png and
                                                    apple-touch-icon.png (FULL)
  Docs     docs/user-guide/assets/                  favicon.svg (SMALL) and mark.svg, the
                                                    header logo (SMALL, white on orange)

The sidebar badge (dashboard_2.0/src/ui/BrandMark.tsx, BrandIcon) is the SMALL
style by hand, in a 24-unit viewBox.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
LOGO = ROOT / "src/assets/logo.svg"
RES = ROOT / "android/app/src/main/res"
WEB = ROOT.parent / "olano/dashboard_2.0/public"
DOCS = ROOT.parent / "docs/user-guide/assets"
XCASSETS = ROOT / "ios/App/App/Assets.xcassets"

ORANGE = (255, 106, 61, 255)
WHITE = (255, 255, 255, 255)
DARK = (27, 27, 31, 255)  # the dashboard's default background, #1b1b1f

# The wordmark's mark, in its own 1024-unit space (src/assets/logo.svg).
OUTER = [
    (355.9, 135.1),
    (668.1, 135.1),
    (888.9, 355.9),
    (888.9, 668.1),
    (668.1, 888.9),
    (355.9, 888.9),
    (135.1, 668.1),
    (135.1, 355.9),
]
INNER = [
    (437.0, 298.3),
    (587.0, 298.3),
    (693.1, 423.5),
    (693.1, 600.5),
    (587.0, 725.7),
    (437.0, 725.7),
    (330.9, 600.5),
    (330.9, 423.5),
]
NODES = OUTER + INNER


def _edges() -> list[tuple[int, int]]:
    """Both rings are closed; outer node i also joins the outer node two steps
    on, and the inner nodes i - 1, i and i + 1."""
    n = len(OUTER)
    edges: list[tuple[int, int]] = []
    for i in range(n):
        edges.append((i, (i + 1) % n))
        edges.append((i, (i + 2) % n))
        edges.append((n + i, n + (i + 1) % n))
        for k in (-1, 0, 1):
            edges.append((i, n + (i + k) % n))
    return edges


EDGES = _edges()
CENTER = 512

# A style is a list of layers: (edges, stroke width, node radius, nodes), in
# logo units. The wordmark itself uses a 7 stroke and 15 nodes.
Style = list[tuple[list[tuple[int, int]], float, float, list[int]]]
FULL: Style = [(EDGES, 15, 26, list(range(16)))]
_N = len(OUTER)
# No node dots: the node radius is the stroke's half width, so the lines just
# meet in rounded joins. A dot any bigger reads as a blob at favicon sizes and
# is not how the logo looks.
SMALL: Style = [
    ([(i, (i + 1) % _N) for i in range(_N)], 60, 30, list(range(_N))),
    (
        [(_N + i, _N + (i + 1) % _N) for i in range(_N)]
        + [(i, _N + (i + k) % _N) for i in range(_N) for k in (-1, 1)],
        30,
        15,
        list(range(_N, 2 * _N)),
    ),
]


def glyph_radius(style: Style) -> float:
    """Radius of the circle enclosing the whole glyph, nodes included."""
    return max(
        math.hypot(NODES[i][0] - CENTER, NODES[i][1] - CENTER) + max(r, w / 2)
        for _, w, r, nodes in style
        for i in nodes
    )


# The glyph's enclosing circle as a share of a full-bleed icon's side. Leaves
# about a tenth of the side clear around the octagon's flat edges, and keeps
# the diagonal edges well inside iOS's rounded corners.
ICON_FILL = 0.88
# Favicons spend every pixel on the glyph.
FAVICON_FILL = 0.94
# A maskable web icon keeps its content inside the centre circle of 80%; the
# glyph's enclosing circle sits just inside it.
MASKABLE_FILL = 0.78
CORNER_RADIUS = 0.2  # rounded square, as a share of the side
SUPERSAMPLE = 4

DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def check_against_logo() -> None:
    """The node and edge lists must be the ones logo.svg draws."""
    text = LOGO.read_text()
    index = {(round(x, 1), round(y, 1)): i for i, (x, y) in enumerate(NODES)}
    circles = {
        (float(x), float(y)) for x, y in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', text)
    }
    lines = {
        frozenset((index.get((float(a), float(b))), index.get((float(c), float(d)))))
        for a, b, c, d in re.findall(
            r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', text
        )
    }
    if circles != set(index) or lines != {frozenset(e) for e in EDGES}:
        sys.exit(f"the mesh differs from {LOGO.relative_to(ROOT)}; update OUTER, INNER or _edges()")
    print(f"matches {LOGO.relative_to(ROOT)} ({len(NODES)} nodes, {len(EDGES)} edges)")


def draw_glyph(
    image: Image.Image,
    center: tuple[float, float],
    radius: float,
    style: Style = FULL,
    color=ORANGE,
) -> None:
    """Draw the mesh onto `image` with its enclosing circle at `center`, `radius` px."""
    draw = ImageDraw.Draw(image)
    unit = radius / glyph_radius(style)
    points = [(center[0] + (x - CENTER) * unit, center[1] + (y - CENTER) * unit) for x, y in NODES]
    for edges, edge_width, node_radius, nodes in style:
        width = max(1, round(edge_width * unit))
        for a, b in edges:
            draw.line((points[a], points[b]), fill=color, width=width)
        # Round every line end so joins at a node never show a notch.
        r = max(node_radius, edge_width / 2) * unit
        for i in {i for edge in edges for i in edge} | set(nodes):
            x, y = points[i]
            rr = r if i in nodes else width / 2
            draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=color)


def draw_icon(
    size: int,
    *,
    fill: float = ICON_FILL,
    shape: str | None = "square",
    style: Style = FULL,
    on_dark: bool = False,
) -> Image.Image:
    """The icon, `size` px square. `shape`: "square" (full bleed), "rounded",
    "circle", or None (glyph on transparent). `fill`: the glyph's enclosing
    circle as a share of `size`. `on_dark` swaps the colours (white glyph on
    an orange tile) for a mark shown on a dark screen, where a white tile glares."""
    tile, glyph = (ORANGE, WHITE) if on_dark else (WHITE, ORANGE)
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if shape == "square":
        draw.rectangle((0, 0, big, big), fill=tile)
    elif shape == "rounded":
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=CORNER_RADIUS * big, fill=tile)
    elif shape == "circle":
        draw.ellipse((0, 0, big - 1, big - 1), fill=tile)
    draw_glyph(image, (big / 2, big / 2), fill * big / 2, style, glyph)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def icon_svg(
    *, fill: float = ICON_FILL, rounded: bool = True, style: Style = FULL, on_dark: bool = False
) -> str:
    """The icon as an SVG, 1024 units square: a white square (rounded or full
    bleed) with the glyph in brand orange, or the colours swapped (`on_dark`)."""
    tile, glyph = ("#ff6a3d", "#ffffff") if on_dark else ("#ffffff", "#ff6a3d")
    size = 1024
    unit = fill * size / 2 / glyph_radius(style)

    def p(i: int) -> tuple[float, float]:
        x, y = NODES[i]
        return (round(size / 2 + (x - CENTER) * unit, 1), round(size / 2 + (y - CENTER) * unit, 1))

    layers = []
    for edges, edge_width, node_radius, nodes in style:
        lines = "".join(
            f'<line x1="{p(a)[0]}" y1="{p(a)[1]}" x2="{p(b)[0]}" y2="{p(b)[1]}"/>' for a, b in edges
        )
        r = round(max(node_radius, edge_width / 2) * unit, 1)
        # stroke="none": a stroked circle would draw half a stroke width past r.
        circles = "".join(
            f'<circle cx="{p(i)[0]}" cy="{p(i)[1]}" r="{r}" stroke="none"/>' for i in nodes
        )
        layers.append(
            f'  <g fill="{glyph}" stroke="{glyph}" stroke-width="{round(edge_width * unit, 1)}" '
            f'stroke-linecap="round">{lines}{circles}</g>\n'
        )
    corner = round(CORNER_RADIUS * size) if rounded else 0
    return (
        "<!-- Drawn by mobile/scripts/make_icons.py; edit that, not this. -->\n"
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" role="img" aria-label="Olano">\n'
        f'  <rect width="{size}" height="{size}" rx="{corner}" ry="{corner}" fill="{tile}"/>\n'
        + "".join(layers)
        + "</svg>\n"
    )


def splash(width: int, height: int, icon: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), DARK)
    image.alpha_composite(
        draw_icon(icon, shape="rounded", on_dark=True), ((width - icon) // 2, (height - icon) // 2)
    )
    return image.convert("RGB")


def save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    print(path.relative_to(ROOT.parent))


def android() -> None:
    # 66 dp of the 108 dp adaptive canvas survives every launcher mask; keep
    # the glyph's enclosing circle a little inside it.
    adaptive_fill = 66 / 108 * 0.97
    for name, d in DENSITIES.items():
        legacy = round(48 * d)
        inner = round(44 * d)  # Material keyline: 2 dp clear on each side
        pad = (legacy - inner) // 2
        canvas = Image.new("RGBA", (legacy, legacy), (0, 0, 0, 0))
        canvas.alpha_composite(draw_icon(inner, shape="rounded"), (pad, pad))
        save(canvas, RES / f"mipmap-{name}/ic_launcher.png")
        canvas = Image.new("RGBA", (legacy, legacy), (0, 0, 0, 0))
        canvas.alpha_composite(draw_icon(inner, shape="circle", fill=0.86), (pad, pad))
        save(canvas, RES / f"mipmap-{name}/ic_launcher_round.png")
        save(
            draw_icon(round(108 * d), shape=None, fill=adaptive_fill),
            RES / f"mipmap-{name}/ic_launcher_foreground.png",
        )

        port = (round(320 * d), round(480 * d))
        icon = round(96 * d)
        save(splash(*port, icon), RES / f"drawable-port-{name}/splash.png")
        save(splash(port[1], port[0], icon), RES / f"drawable-land-{name}/splash.png")
    save(splash(480, 320, 96), RES / "drawable/splash.png")


def ios() -> None:
    save(draw_icon(1024).convert("RGB"), XCASSETS / "AppIcon.appiconset/AppIcon-512@2x.png")
    # LaunchScreen.storyboard aspect-fills this square, so the icon lands at
    # roughly 120 pt on a phone in portrait.
    image = splash(2732, 2732, 400)
    for name in ("splash-2732x2732.png", "splash-2732x2732-1.png", "splash-2732x2732-2.png"):
        save(image, XCASSETS / "Splash.imageset" / name)


def write_text(text: str, path: Path) -> None:
    path.write_text(text)
    print(path.relative_to(ROOT.parent))


def web() -> None:
    write_text(icon_svg(fill=FAVICON_FILL, style=SMALL), WEB / "favicon.svg")
    for size in (16, 32):
        save(
            draw_icon(size, shape="rounded", fill=FAVICON_FILL, style=SMALL),
            WEB / f"favicon-{size}.png",
        )
    for size in (192, 512):
        save(draw_icon(size, shape="rounded"), WEB / f"icon-{size}.png")
    write_text(icon_svg(fill=MASKABLE_FILL, rounded=False), WEB / "icon-maskable.svg")
    save(draw_icon(512, fill=MASKABLE_FILL).convert("RGB"), WEB / "icon-maskable-512.png")
    # iOS masks a home-screen icon itself, so this one is opaque and full bleed.
    save(draw_icon(180).convert("RGB"), WEB / "apple-touch-icon.png")
    # The docs site (mkdocs.yml): the same favicon, and for the header logo,
    # which sits on the deep-orange header bar, the page colours (white mesh
    # on an orange tile) like the dashboard's sidebar badge.
    write_text(icon_svg(fill=FAVICON_FILL, style=SMALL), DOCS / "favicon.svg")
    write_text(icon_svg(fill=FAVICON_FILL, style=SMALL, on_dark=True), DOCS / "mark.svg")


if __name__ == "__main__":
    check_against_logo()
    write_text(icon_svg(), ASSETS / "icon.svg")
    android()
    ios()
    web()
