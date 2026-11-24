"""The ``charts`` skill's engine, exercised the way an agent runs it.

The skill is the chart engine: ``scripts/chart.py`` turns a recipe (or any
Vega-Lite spec) into a themed HTML card that ``render_html_image`` rasterises.
These tests drive the *built-in copy* through ``subprocess`` with the same
interpreter the sandbox would use — standard library only, nothing imported
from the skill directory into the olano package — so a dependency that creeps
into the engine, a recipe that stops applying its design rules, or a theme
whose palette no longer clears the six checks fails here, not on a customer's
first chart. The browser tests prove the whole chain: the page's vendored Vega
loads from ``file://``, the fonts are inlined, the ``.render-done`` sentinel
appears, and the PNG comes out at exactly the preset size × 2.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import olano
from olano.security.request_context import push_request_context
from tests.test_render_tools import _png_size, needs_chromium

SKILL = Path(olano.__file__).resolve().parent / "skills" / "built_in" / "charts"
CHART = SKILL / "scripts" / "chart.py"
EXAMPLES = sorted((SKILL / "assets" / "examples").glob("*.json"))
THEMES = ("clean", "midnight", "editorial")
CLEAN = json.loads((SKILL / "assets" / "themes" / "clean.json").read_text(encoding="utf-8"))[
    "tokens"
]


def run(*args: str, cwd: Path, isolated: bool = False) -> subprocess.CompletedProcess[str]:
    # ``-I -S`` (isolated, no site-packages) is the proof that the engine needs
    # nothing beyond the standard library: the sandbox's python3 has none.
    flags = ["-I", "-S"] if isolated else []
    return subprocess.run(
        [sys.executable, *flags, str(CHART), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def emit_vl(example: Path, cwd: Path, *extra: str) -> dict:
    proc = run("render", str(example), "--emit-vl", *extra, cwd=cwd)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _quantitative_fields(encoding: dict) -> set[str]:
    return {
        enc.get("field", "")
        for enc in encoding.values()
        if isinstance(enc, dict) and enc.get("type") == "quantitative"
    }


# ---------------------------------------------------------------------------
# the CLI and every shipped example
# ---------------------------------------------------------------------------
def test_examples_ship_for_every_recipe() -> None:
    assert len(EXAMPLES) >= 12
    types = {json.loads(p.read_text(encoding="utf-8"))["type"] for p in EXAMPLES}
    assert types >= {
        "bar", "column", "stacked_bar", "grouped_bar", "line", "area", "dumbbell",
        "donut", "stat", "histogram", "heatmap", "scatter",
    }  # fmt: skip


@pytest.mark.parametrize("example", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_every_example_emits_valid_themed_vega_lite(example: Path, tmp_path: Path) -> None:
    vl = emit_vl(example, tmp_path)
    assert vl["$schema"].startswith("https://vega.github.io/schema/vega-lite/")
    # The theme lands as Vega-Lite config, and the chart fits the frame's box.
    assert isinstance(vl.get("config"), dict) and vl["config"]
    assert isinstance(vl.get("width"), int) and isinstance(vl.get("height"), int)
    assert vl["width"] > 0 and vl["height"] > 0


@pytest.mark.parametrize("example", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_every_example_renders_a_page_in_every_theme(example: Path, tmp_path: Path) -> None:
    for theme in THEMES:
        out = tmp_path / f"{example.stem}-{theme}.html"
        proc = run("render", str(example), "--theme", theme, "--out", str(out), cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert out.is_file() and out.stat().st_size > 1000
        assert f'render_html_image(html="{out}"' in proc.stdout
        assert 'wait_for=".render-done"' in proc.stdout


def test_engine_runs_on_the_standard_library_alone(tmp_path: Path) -> None:
    proc = run("render", str(EXAMPLES[0]), "--emit-vl", cwd=tmp_path, isolated=True)
    assert proc.returncode == 0, proc.stderr
    proc = run("themes", cwd=tmp_path, isolated=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# the design rules the recipes apply
# ---------------------------------------------------------------------------
def test_bar_recipe_sorts_emphasises_and_labels(tmp_path: Path) -> None:
    ex = next(p for p in EXAMPLES if p.stem == "bar-emphasis")
    vl = emit_vl(ex, tmp_path)
    rows = vl["data"]["values"]
    values = [r["value"] for r in rows]
    assert values == sorted(values, reverse=True), "bars are sorted by value, largest first"
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["United States"]["color"] == CLEAN["emphasis"]
    assert all(
        r["color"] == CLEAN["deemphasis"] for c, r in by_cat.items() if c != "United States"
    ), "every other bar is the de-emphasis gray"
    layers = vl["layer"]
    assert layers[0]["mark"]["type"] == "bar"
    assert layers[0]["encoding"]["color"]["legend"] is None, "one series never gets a legend"
    text_layers = [
        lay for lay in layers if lay["mark"]["type"] == "text" and lay["encoding"].get("text", {}).get("field") == "label"
    ]  # fmt: skip
    assert len(text_layers) == 1, "values are labelled directly, once"
    assert layers[0]["encoding"]["x"]["axis"] is None, "a labelled bar needs no value axis"
    assert _quantitative_fields(layers[0]["encoding"]) == {"value"}, "one value axis, never two"
    # The annotation rides beside its target as its own text layer.
    ann = [
        lay for lay in layers if lay["encoding"].get("text", {}).get("value") == "35% of accounts"
    ]
    assert len(ann) == 1


def test_stacked_recipe_has_a_legend_and_one_value_scale(tmp_path: Path) -> None:
    ex = next(p for p in EXAMPLES if p.stem == "stacked")
    vl = emit_vl(ex, tmp_path)
    bar = vl["layer"][0]
    color = bar["encoding"]["color"]
    assert color["legend"]["values"] == ["Platform", "Services", "Hardware"]
    assert color["scale"]["domain"] == ["Platform", "Services", "Hardware"]
    assert color["scale"]["range"][0] == CLEAN["emphasis"], "the emphasis series takes the accent"
    assert "x2" in bar["encoding"], "segments are stacked start→end on one scale"
    assert _quantitative_fields(bar["encoding"]) == {"start"}


def test_line_recipe_labels_line_ends_and_keeps_context_lines_gray(tmp_path: Path) -> None:
    ex = next(p for p in EXAMPLES if p.stem == "line")
    vl = emit_vl(ex, tmp_path)
    rows = vl["data"]["values"]
    ends = [r for r in rows if r["is_end"]]
    assert len(ends) == 3 and all(r["show"] for r in ends), "every series ends in a direct label"
    assert any("Web" in r["label"] for r in ends)
    colors = {r["series"]: r["color"] for r in rows}
    assert colors["Web"] == CLEAN["emphasis"]
    assert colors["iOS"] != CLEAN["emphasis"] and colors["Android"] != CLEAN["emphasis"]


def test_recipes_refuse_what_cannot_be_read(tmp_path: Path) -> None:
    nine = {
        "type": "line",
        "title": "Nine",
        "categories": ["a", "b"],
        "series": [{"name": f"s{i}", "values": [i, i + 1]} for i in range(9)],
    }
    spec = tmp_path / "nine.json"
    spec.write_text(json.dumps(nine), encoding="utf-8")
    proc = run("render", str(spec), "--emit-vl", cwd=tmp_path)
    assert proc.returncode == 1
    assert "Other" in proc.stderr and "9 series" in proc.stderr

    seven = {
        "type": "donut",
        "title": "Seven",
        "categories": list("abcdefg"),
        "series": [{"name": "n", "values": [7, 6, 5, 4, 3, 2, 1]}],
    }
    spec.write_text(json.dumps(seven), encoding="utf-8")
    proc = run("render", str(spec), "--emit-vl", cwd=tmp_path)
    assert proc.returncode == 1
    assert "7 slices" in proc.stderr and "bar" in proc.stderr

    bad_size = {**nine, "series": nine["series"][:2]}
    spec.write_text(json.dumps(bad_size), encoding="utf-8")
    proc = run("render", str(spec), "--emit-vl", "--size", "100x100", cwd=tmp_path)
    assert proc.returncode == 1
    assert "320" in proc.stderr and "4000" in proc.stderr


# ---------------------------------------------------------------------------
# themes and the frame
# ---------------------------------------------------------------------------
def test_every_theme_passes_the_six_checks(tmp_path: Path) -> None:
    proc = run("themes", "--check", cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[FAIL]" not in proc.stdout
    for theme in THEMES:
        assert f"== {theme} (" in proc.stdout and "→ PASS" in proc.stdout


def test_frame_carries_the_chrome_the_fonts_and_the_render_hook(tmp_path: Path) -> None:
    ex = next(p for p in EXAMPLES if p.stem == "bar-emphasis")
    out = tmp_path / "card.html"
    proc = run("render", str(ex), "--size", "landscape", "--out", str(out), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    page = out.read_text(encoding="utf-8")
    assert "render-done" in page, "the sentinel render_html_image waits for"
    assert "@font-face" in page and "data:font/ttf;base64," in page, (
        "fonts are inlined, never fetched"
    )
    assert 'font-family:"Instrument Sans"' in page
    for chrome in (
        "CUSTOMER FOOTPRINT",
        "The US is a third of the customer base",
        "63",
        "Source: Salesforce",
    ):
        assert chrome in page.upper() if chrome.isupper() else chrome in page
    assert "3 accounts have no billing country" in page
    assert (
        page.count("vega.min.js") == 1
        and "vega-lite.min.js" in page
        and "vega-embed.min.js" in page
    )
    assert "file://" in page, "the vendored scripts load by absolute file:// path"
    assert "https://" not in page.split("<script")[1], "nothing on the page reaches the network"
    # Flat by construction: no CSS or SVG gradients anywhere on the card
    # (Vega's legend config carries `gradientLength` keys — those are not fills).
    assert "linear-gradient" not in page and "radial-gradient" not in page
    assert "<linearGradient" not in page and "<radialGradient" not in page


def test_raw_vega_lite_and_plain_svg_get_the_same_frame(tmp_path: Path) -> None:
    vl = {
        "data": {"values": [{"a": "x", "b": 3}, {"a": "y", "b": 5}]},
        "mark": "bar",
        "encoding": {
            "x": {"field": "a", "type": "nominal"},
            "y": {"field": "b", "type": "quantitative"},
        },
    }
    spec = tmp_path / "raw.vl.json"
    spec.write_text(json.dumps(vl), encoding="utf-8")
    out = tmp_path / "raw.html"
    proc = run(
        "vl", str(spec), "--title", "Y beats X", "--subtitle", "units", "--source", "Test",
        "--theme", "midnight", "--out", str(out), cwd=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    page = out.read_text(encoding="utf-8")
    assert "Y beats X" in page and "Source: Test" in page and 'data-theme="midnight"' in page

    svg = tmp_path / "fig.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><circle r="50" cx="150" cy="100"/></svg>'
    )
    out2 = tmp_path / "framed.html"
    proc = run("frame", "--body", str(svg), "--title", "A circle", "--out", str(out2), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    page2 = out2.read_text(encoding="utf-8")
    assert "<circle" in page2 and "A circle" in page2 and "vega.min.js" not in page2


# ---------------------------------------------------------------------------
# end to end through Olano's renderer
# ---------------------------------------------------------------------------
@needs_chromium
def test_card_rasterises_at_twice_the_preset_size(tmp_path: Path) -> None:
    from olano.tools.render_tools import render_html_image

    ex = next(p for p in EXAMPLES if p.stem == "bar-emphasis")
    out = tmp_path / "card.html"
    proc = run("render", str(ex), "--size", "landscape", "--out", str(out), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    png = tmp_path / "card.png"
    # A real call carries the agent's workspace; the render may only load
    # files from it (and the skills).
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        result = render_html_image.func(
            html=str(out), width=1200, height=675, save_path=str(png), wait_for=".render-done"
        )
    assert "[tool-error]" not in result, result
    assert _png_size(png) == (2400, 1350)


@needs_chromium
def test_svg_card_exports_as_a_standalone_vector(tmp_path: Path) -> None:
    from olano.tools.render_tools import render_html_svg

    ex = next(p for p in EXAMPLES if p.stem == "stacked")
    out = tmp_path / "card.html"
    proc = run(
        "render", str(ex), "--theme", "midnight", "--size", "square", "--svg-card", "--out", str(out), cwd=tmp_path
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    svg_path = tmp_path / "card.svg"
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        result = render_html_svg.func(
            html=str(out), width=1200, height=1200, save_path=str(svg_path),
            wait_for=".render-done", selector="#card",
        )  # fmt: skip
    assert "[tool-error]" not in result, result
    root = ET.fromstring(svg_path.read_bytes())
    assert root.tag.endswith("svg") and root.get("width") == "1200" and root.get("height") == "1200"
    texts = [(t.text or "") for t in root.iter() if t.tag.endswith("text")]
    assert any("Platform now brings in more than half" in t for t in texts), texts[:10]
    # The Vega plot is nested as real vector marks, not an embedded bitmap.
    assert sum(1 for el in root.iter() if el.tag.endswith("path")) > 5
    assert not any(el.tag.endswith("image") for el in root.iter())


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_charts_skill_is_gated_on_the_renderer_and_named_after_its_folder() -> None:
    """The gate lives under ``metadata:`` (the Agent Skills spec permits no
    custom top-level key, and the catalog copy is checked by the reference
    validator), and the loader reads it from there."""
    from olano.skills.loader import _read_frontmatter

    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    fm = _read_frontmatter(text)
    assert fm["name"] == "charts"
    assert fm["metadata"]["requires_tools"] == ["render_html_image"]
    assert "requires_tools" not in fm, "a top-level key fails the catalog's validator"


def test_charts_engine_is_stdlib_only() -> None:
    """The engine runs in the sandbox's bare Python 3.11: no pip packages."""
    scripts = SKILL / "scripts"
    allowed = set(sys.stdlib_module_names) | {"olano_chart", "validate_palette"}
    for py in sorted(scripts.rglob("*.py")):
        extra = _imports(py) - allowed
        assert not extra, f"{py}: {extra}"


def test_charts_vendors_vega_with_its_licenses() -> None:
    vendor = SKILL / "assets" / "vendor"
    for name in ("vega.min.js", "vega-lite.min.js", "vega-embed.min.js"):
        assert (vendor / name).stat().st_size > 10_000, name
    licenses = [p.name for p in vendor.iterdir() if p.name.upper().startswith("LICENSE")]
    assert len(licenses) >= 3, licenses
