"""Deterministic HTML rendering — the seam that makes social graphics reproducible.

A generated image guesses at a headline; these tools do not. The properties that
matter are therefore *exactness* (the canvas is the pixel size asked for, and
`scale` multiplies it) and *asset resolution* (a relative `<img>` or an
`@font-face` pointing at a bundled `.ttf` must load, or the card silently
renders in fallback sans with a broken hero).

The rendering tests drive a real Chromium and skip when one isn't installed —
the pure-unit tests below them cover input classification and the range guards
without a browser, so a machine with no browsers still catches a regression in
the parts that don't need one.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from olano.tools.render_tools import (
    _looks_like_html,
    _resolve_source,
    _resolve_target,
    _validate_canvas,
    render_html_image,
    render_html_pdf,
    render_html_svg,
)


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            browser.close()
        return True
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="no Chromium available for Playwright"
)


def _png_size(path: Path) -> tuple[int, int]:
    """Width/height straight out of the PNG IHDR — no image library needed."""
    header = path.read_bytes()[16:24]
    width, height = struct.unpack(">II", header)
    return width, height


CARD = """<!doctype html><html><head><style>
  html,body{margin:0;padding:0}
  .card{width:1080px;height:1350px;background:#0A2540;color:#fff;
        font-family:Georgia,serif;font-size:80px;padding:64px;box-sizing:border-box}
</style></head><body><div class="card">Exactly this wide.</div></body></html>"""


@pytest.fixture
def ws(tmp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    return tmp_workspace


# ── Rendering (needs a browser) ────────────────────────────────────────────


@needs_chromium
def test_inline_html_renders_at_the_exact_canvas_size(ws: Path):
    result = render_html_image.func(
        html=CARD, width=1080, height=1350, scale=1, save_path="renders/card.png"
    )
    assert "Saved:" in result, result
    out = ws / "renders" / "card.png"
    assert out.is_file()
    assert _png_size(out) == (1080, 1350)


@needs_chromium
def test_scale_multiplies_the_pixel_dimensions(ws: Path):
    # The whole point of `scale`: author the CSS at the logical size, export at
    # retina. A 2x that silently returned 1080x1350 would ship soft graphics.
    render_html_image.func(
        html=CARD, width=1080, height=1350, scale=2, save_path="renders/card@2x.png"
    )
    assert _png_size(ws / "renders" / "card@2x.png") == (2160, 2700)


@needs_chromium
def test_html_file_path_renders_the_same_as_inline(ws: Path):
    (ws / "card.html").write_text(CARD, encoding="utf-8")
    render_html_image.func(
        html="card.html", width=1080, height=1350, scale=1, save_path="renders/from-file.png"
    )
    render_html_image.func(
        html=CARD, width=1080, height=1350, scale=1, save_path="renders/from-inline.png"
    )
    assert (ws / "renders" / "from-file.png").read_bytes() == (
        ws / "renders" / "from-inline.png"
    ).read_bytes()


@needs_chromium
def test_relative_asset_references_resolve_against_the_workspace(ws: Path):
    """An inline document must resolve `<img src="...">` like a file one does.

    This is why inline HTML is written into the workspace instead of the system
    temp dir: `/tmp` is not on the sandbox's shared mount, so a hero image the
    agent just generated would 404 and the card would render with a blank hole.
    """
    # 2x2 red PNG.
    red = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000200000002080200000057dd52f80000"
        "001849444154789c636460f8cfc0c00cc4c0c0c0c000009e0103fd4b8d4e4c00000000"
        "49454e44ae426082"
    )
    (ws / "hero.png").write_bytes(red)
    html = (
        '<!doctype html><html><body style="margin:0">'
        '<img src="hero.png" style="width:200px;height:200px">'
        "</body></html>"
    )
    render_html_image.func(
        html=html, width=200, height=200, scale=1, save_path="renders/with-asset.png"
    )
    out = ws / "renders" / "with-asset.png"
    blank = ws / "renders" / "blank.png"
    render_html_image.func(
        html='<!doctype html><html><body style="margin:0"></body></html>',
        width=200,
        height=200,
        scale=1,
        save_path=str(blank),
    )
    # If the image had failed to load the page would be the same empty white.
    assert out.read_bytes() != blank.read_bytes()


@needs_chromium
def test_pdf_gets_one_page_per_page_break(ws: Path):
    """`page-break-after` is how a carousel becomes a LinkedIn document post."""
    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    slides = "".join(
        f'<div style="width:1080px;height:1350px;background:#111;'
        f'page-break-after:always">Slide {i}</div>'
        for i in range(1, 4)
    )
    result = render_html_pdf.func(
        html=f'<!doctype html><html><body style="margin:0">{slides}</body></html>',
        width=1080,
        height=1350,
        save_path="renders/carousel.pdf",
    )
    assert "Saved:" in result, result
    assert len(PdfReader(str(ws / "renders" / "carousel.pdf")).pages) == 3


@needs_chromium
def test_default_save_path_lands_in_the_workspace_renders_dir(ws: Path):
    result = render_html_image.func(html=CARD, width=400, height=400, scale=1)
    assert str(ws / "renders") in result
    assert list((ws / "renders").glob("render-*.png"))


@needs_chromium
def test_inline_render_leaves_no_temp_html_behind(ws: Path):
    render_html_image.func(html=CARD, width=400, height=400, scale=1, save_path="renders/x.png")
    assert not list((ws / "renders").glob(".inline-*.html"))


@needs_chromium
def test_missing_html_file_is_an_actionable_error(ws: Path):
    result = render_html_image.func(html="nope/missing.html", width=400, height=400)
    assert result.startswith("[tool-error] render_html_image:")
    assert "not found" in result


# ── Guards and classification (no browser) ─────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["<!doctype html><html></html>", "<html>", "  <div>hi</div>", "<style>a{}</style>", "<svg/>"],
)
def test_documents_are_classified_as_inline_html(value: str):
    assert _looks_like_html(value)


@pytest.mark.parametrize("value", ["card.html", "/abs/path/card.html", "renders/slide-1.html"])
def test_paths_are_not_classified_as_html(value: str):
    assert not _looks_like_html(value)


def test_empty_html_names_what_to_pass(tmp_path: Path):
    with pytest.raises(ValueError, match="html is empty"):
        _resolve_source("   ", tmp_path)


def test_inline_source_is_written_inside_the_workspace(tmp_path: Path):
    source, is_temp = _resolve_source("<div>x</div>", tmp_path)
    assert is_temp
    assert source.is_relative_to(tmp_path)
    assert source.read_text() == "<div>x</div>"


def test_existing_file_is_used_in_place(tmp_path: Path):
    (tmp_path / "a.html").write_text("<div>a</div>", encoding="utf-8")
    source, is_temp = _resolve_source("a.html", tmp_path)
    assert not is_temp
    assert source == tmp_path / "a.html"


def test_relative_save_path_resolves_against_the_workspace(tmp_path: Path):
    target, created = _resolve_target("out/card.png", tmp_path, ".png")
    assert target == tmp_path / "out" / "card.png"
    # The directories this call had to make, collected before the mkdir so the
    # render can hand them to the sandbox user along with the file.
    assert created == [tmp_path / "out"]


def test_resolve_target_reports_no_created_dirs_when_the_parent_exists(tmp_path: Path):
    (tmp_path / "out").mkdir()
    _, created = _resolve_target("out/card.png", tmp_path, ".png")
    assert created == []


@pytest.mark.parametrize(
    ("width", "height", "scale"),
    [
        (0, 400, 1.0),
        (400, 0, 1.0),
        (99_999, 400, 1.0),
        (400, 99_999, 1.0),
        (400, 400, 0),
        (400, 400, 9),
    ],
)
def test_out_of_range_canvases_are_refused(width: int, height: int, scale: float):
    # A stray extra digit would otherwise ask Chromium for a surface big enough
    # to take the box down, and a 0 would write an empty file that looks fine
    # until someone opens it.
    assert _validate_canvas(width, height, scale).startswith("[tool-error]")


def test_a_normal_social_canvas_passes():
    assert _validate_canvas(1080, 1350, 2.0) == ""


@pytest.mark.parametrize("render", [render_html_image, render_html_pdf])
def test_unknown_wait_until_is_rejected_before_launching_a_browser(render, ws: Path):
    result = render.func(html=CARD, width=400, height=400, wait_until="whenever")
    assert result.startswith("[tool-error]")
    assert "wait_until" in result


# ── wait_for + SVG extraction (needs a browser) ────────────────────────────

# A page that draws AFTER load — the shape of the charts skill's Vega frame:
# the <svg> appears 300 ms later, then a hidden `.render-done` marker.
LATE_SVG = """<!doctype html><html><body style="margin:0"><script>
setTimeout(() => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("width", "300"); s.setAttribute("height", "120"); s.id = "late";
  const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  r.setAttribute("width", "300"); r.setAttribute("height", "120"); r.setAttribute("fill", "#2a78d6");
  s.appendChild(r); document.body.appendChild(s);
  const done = document.createElement("div"); done.className = "render-done"; done.hidden = true;
  document.body.appendChild(done);
}, 300);
</script></body></html>"""


@needs_chromium
def test_wait_for_captures_content_drawn_after_load(ws: Path):
    result = render_html_svg.func(
        html=LATE_SVG,
        width=400,
        height=200,
        save_path="renders/late.svg",
        wait_for=".render-done",
        selector="#late",
    )
    assert "Saved:" in result, result
    # The markdown image line rides along for .svg too (the dashboard renders it).
    assert "![late]" in result
    root = ET.fromstring((ws / "renders" / "late.svg").read_text(encoding="utf-8"))
    assert root.tag.endswith("svg")
    assert root.get("width") == "300"


@needs_chromium
def test_without_wait_for_the_late_element_is_not_there_yet(ws: Path):
    # The contract cuts both ways: no `wait_for` means "capture at load", and a
    # page that only draws later legitimately has nothing to extract yet.
    result = render_html_svg.func(
        html=LATE_SVG, width=400, height=200, save_path="renders/early.svg", selector="#late"
    )
    assert "[tool-error]" in result and "matched nothing" in result


@needs_chromium
def test_wait_for_that_never_appears_is_an_actionable_error(ws: Path, monkeypatch):
    from olano.tools import render_tools

    monkeypatch.setattr(render_tools, "_RENDER_TIMEOUT_MS", 1000)
    result = render_html_image.func(
        html=CARD, width=200, height=200, scale=1, save_path="renders/never.png", wait_for=".never"
    )
    assert "[tool-error]" in result
    assert "wait_for" in result and ".never" in result


@needs_chromium
def test_svg_extraction_keeps_text_and_namespace(ws: Path):
    html = (
        "<!doctype html><html><body>"
        '<svg width="300" height="100"><text x="10" y="50">Hi &amp; bye</text></svg>'
        "</body></html>"
    )
    result = render_html_svg.func(html=html, save_path="renders/static.svg")
    assert "Saved:" in result, result
    raw = (ws / "renders" / "static.svg").read_text(encoding="utf-8")
    assert raw.startswith("<?xml")
    root = ET.fromstring(raw)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert "Hi & bye" in "".join(root.itertext())


def test_svg_selector_must_not_be_empty(ws: Path):
    # Refused before any browser launches.
    result = render_html_svg.func(html=CARD, selector="   ")
    assert result.startswith("[tool-error] render_html_svg: selector")


# ── What a render may load ─────────────────────────────────────────────────
#
# The HTML is the agent's and Chromium runs in the runtime process, so a
# file:// reference reads the runtime host's disk and a remote one is an
# outbound request. Both go through the browser's request guard.


@pytest.fixture
def outside_secret(tmp_path_factory: pytest.TempPathFactory, ws: Path) -> Path:
    secret = tmp_path_factory.mktemp("outside") / "secret.txt"
    secret.write_text("RUNTIME-HOST-SECRET", encoding="utf-8")
    assert ws not in secret.parents
    return secret


@needs_chromium
def test_a_host_file_outside_the_workspace_is_not_rendered(ws: Path, outside_secret: Path):
    html = (
        "<!doctype html><html><body>"
        f'<iframe src="{outside_secret.as_uri()}"></iframe>'
        f'<img src="{outside_secret.as_uri()}">'
        "</body></html>"
    )
    result = render_html_image.func(html=html, width=400, height=300, scale=1)
    assert "Saved:" in result, result
    assert "[egress] Blocked 2 request(s)" in result
    assert f"local file {outside_secret}" in result


@needs_chromium
def test_a_workspace_symlink_to_a_host_file_is_not_followed(ws: Path, outside_secret: Path):
    """The agent can plant a symlink from its sandbox; Chromium on the host
    would follow it to the host's file. The check runs on the resolved path."""
    link = ws / "innocent.txt"
    link.symlink_to(outside_secret)
    html = f'<!doctype html><html><body><iframe src="{link.as_uri()}"></iframe></body></html>'
    result = render_html_image.func(html=html, width=400, height=300, scale=1)
    assert "[egress] Blocked 1 request(s)" in result, result
    assert "innocent.txt" in result


@needs_chromium
def test_workspace_files_still_load(ws: Path):
    (ws / "note.txt").write_text("hello", encoding="utf-8")
    # Inline HTML is written into renders/, so a workspace file is one level up.
    html = '<!doctype html><html><body><iframe src="../note.txt"></iframe></body></html>'
    result = render_html_image.func(html=html, width=400, height=300, scale=1)
    assert "Saved:" in result and "[egress]" not in result, result


def test_an_html_source_outside_the_workspace_is_refused(ws: Path, outside_secret: Path):
    page = outside_secret.parent / "page.html"
    page.write_text("<!doctype html><html><body>x</body></html>", encoding="utf-8")
    for render in (render_html_image, render_html_pdf, render_html_svg):
        result = render.func(html=str(page), width=400, height=300)
        assert result.startswith(f"[tool-error] {render.name}: Local file"), result
        assert "outside this agent's workspace" in result


def test_a_symlinked_source_is_checked_where_it_points(ws: Path, outside_secret: Path):
    (ws / "page.html").symlink_to(outside_secret)
    result = render_html_image.func(html="page.html", width=400, height=300)
    assert "outside this agent's workspace" in result, result


@needs_chromium
def test_remote_assets_follow_the_agents_egress_lists(ws: Path):
    from olano.security.request_context import push_request_context

    html = (
        "<!doctype html><html><body>"
        '<img src="http://127.0.0.1:9/hero.png">'
        '<link rel="stylesheet" href="https://fonts.example.net/x.css">'
        "</body></html>"
    )
    policy = {"security": {"ssrf_allowlist": ["example.com"], "ssrf_blocklist": []}}
    with push_request_context(metadata=policy):
        result = render_html_image.func(html=html, width=400, height=300, scale=1)
    assert "Saved:" in result, result
    assert "[egress] Blocked 2 request(s)" in result
    assert "127.0.0.1" in result and "fonts.example.net" in result


def test_a_save_path_outside_the_workspace_is_refused(ws: Path, tmp_path_factory):
    """`save_path` used to be taken as given: the runtime (root) would create
    the directories and write there, then hand the file to the agent."""
    outside = tmp_path_factory.mktemp("runtime-host") / "etc" / "config.yaml"
    for render in (render_html_image, render_html_pdf, render_html_svg):
        result = render.func(html=CARD, width=400, height=300, save_path=str(outside))
        assert result.startswith(f"[tool-error] {render.name}: '"), result
        assert "cannot be saved there" in result
    assert not outside.parent.exists()
    # A refused target leaves no inline temp file behind either.
    assert not list((ws / "renders").glob(".inline-*.html")) if (ws / "renders").exists() else True


def test_a_save_path_under_a_symlinked_directory_is_refused(ws: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("runtime-host")
    (ws / "exports").symlink_to(outside)
    result = render_html_image.func(html=CARD, width=400, height=300, save_path="exports/x.png")
    assert "cannot be saved there" in result, result
    assert not (outside / "x.png").exists()
