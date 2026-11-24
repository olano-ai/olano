"""Deterministic HTML rendering — HTML/CSS in, exact-pixel PNG or PDF out.

The companion to :mod:`olano.tools.image_tools`. A generative model paints the
*scene*; this paints the *text* — headline, logo, statistics, CTA, brand colours
— at the exact pixel dimensions a platform expects, with typography that is
reproducible instead of hallucinated. Compose the two and a social graphic stops
being a lottery: the photograph comes from ``image_generate``, everything a
human reads comes from here.

**No new dependency.** Playwright and a pinned Chromium are base installs
(``pyproject.toml``), so :func:`render_html_image`, :func:`render_html_pdf` and
:func:`render_html_svg` run on every box, offline, with no API key and nothing
billed. That is the whole reason this module exists rather than an external
design service — and why the three are universal defaults: a chart, a slide or
a report with text on it is *rendered* here, never improvised with a plotting
library the sandbox does not have.

A page that draws with JavaScript (the ``charts`` skill's Vega frame) is not
finished at ``load``; every tool takes ``wait_for``, a CSS selector the page
adds when it is done (``.render-done``), and captures only once it exists.

Both tools take either an HTML **file path** or an inline HTML **string**.
Inline HTML is written to a file inside the workspace before loading, so
``file://`` resolves relative references the same way for both inputs — a
``<link>`` to a stylesheet, an ``@font-face`` pointing at a bundled ``.ttf``, an
``<img src="images/hero.png">`` naming something ``image_generate`` just saved.
An inline document that references nothing renders identically either way.

Chromium bootstrapping (launch flags, the persistent session loop, the HTTP/2
retry) is imported from :mod:`olano.tools.browser` rather than re-rolled: one
definition of "how this box starts a browser" is what keeps the container-root
``--no-sandbox`` fix and the ``LIONCLAW_BROWSER_CHANNEL`` selection applying
here too.

**What a render may load** comes from there as well, because the HTML is the
agent's and Chromium runs in the runtime process, not the sandbox. Every render
context carries the browser's request guard: ``file://`` loads (the source
document included) are limited to what the agent's sandbox can read, resolved
through symlinks (:mod:`olano.browser.files`), so an ``<iframe>`` or ``<img>``
naming a host file renders nothing instead of putting the runtime's files into
a PNG; and the agent's egress lists and the private-network block
(:mod:`olano.browser.guard`) apply to every remote image, script, stylesheet
and font the document pulls in. A refused request leaves that part
of the render empty and is named in a note on the result.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, cast

from langchain_core.tools import tool

from olano.browser.guard import GuardPolicy, GuardState, install_guards
from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents
from olano.tools._toolkit import make_toolkit_api
from olano.tools.browser import (
    _agent_name,
    _base_launch_args,
    _blocked_note,
    _file_access,
    _FileAccess,
    _is_http2_error,
    _launch_kwargs,
    _run_async,
    _save_check,
    _workspace_root,
)

logger = logging.getLogger(__name__)

# Guard rails on the output canvas. The floor stops a typo'd 0 from producing an
# empty file; the ceiling stops a stray extra digit from asking Chromium for a
# multi-gigabyte surface (dimensions are multiplied by `scale`, so a 4x export
# of the ceiling is already 32000px on a side).
_MIN_DIMENSION = 16
_MAX_DIMENSION = 8000
_MAX_SCALE = 4.0

# Long enough for webfonts and generated PNGs to load on a slow box, short
# enough that a document referencing something unreachable fails rather than
# hanging the agent's turn.
_RENDER_TIMEOUT_MS = 30_000

_WAIT_STATES = ("load", "domcontentloaded", "networkidle")


def _renders_dir(workspace_root: Path | None = None) -> Path:
    root = workspace_root if workspace_root is not None else _workspace_root()
    d = root / "renders"
    existed = d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if not existed:
        # The agent lists and cleans this folder; a root-owned one it cannot
        # write is the same defect as a root-owned file inside it.
        handoff_to_sandbox(d)
    return d


def _looks_like_html(value: str) -> bool:
    """True when *value* is an HTML document rather than a path to one.

    Checked before the filesystem, not after: a path check that runs first would
    treat a one-line inline snippet as a missing file and report the wrong
    error. Anything containing a tag is a document; everything else is a path.
    """
    head = value.lstrip()[:512].lower()
    return head.startswith(("<!doctype", "<html", "<div", "<body", "<style", "<section", "<svg"))


def _resolve_source(
    html: str, workspace_root: Path, files: _FileAccess | None = None
) -> tuple[Path, bool]:
    """Return ``(path_to_load, is_temporary)`` for an HTML path or inline document.

    Inline documents land in ``<workspace>/renders/`` rather than the system temp
    dir so relative asset references resolve against the workspace — the sandbox
    mounts the workspace at its host path, and ``/tmp`` is not shared with it.

    A path is checked against *files* (what the agent may read): rendering a
    host file would put its contents into an image the agent can read.
    """
    raw = html.strip()
    if not raw:
        raise ValueError("html is empty — pass an HTML document or a path to an .html file")

    if _looks_like_html(raw):
        tmp = (
            _renders_dir(workspace_root)
            / f".inline-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.html"
        )
        tmp.write_text(raw, encoding="utf-8")
        return tmp, True

    candidate = Path(raw).expanduser()
    if not candidate.is_file():
        candidate = workspace_root / raw
    if not candidate.is_file():
        raise ValueError(
            f"html file not found: {html.strip()} (pass an absolute path, a path relative "
            "to the workspace, or an inline HTML document)"
        )
    if files is not None:
        reason = files.refusal(candidate.resolve().as_uri())
        if reason:
            raise ValueError(reason)
    return candidate, False


def _resolve_target(
    save_path: str, workspace_root: Path, suffix: str, agent: str = ""
) -> tuple[Path, list[Path]]:
    """``(target, directories this call had to create)``.

    The created list is collected BEFORE the mkdir — afterwards there is no way
    to tell the directories this write made from the ones already there — and is
    handed to :func:`_finish_target` once the file exists. A target outside
    what *agent* may write (``browser._save_check``) raises ``ValueError``
    before anything is created.
    """
    if save_path.strip():
        resolved = Path(save_path.strip()).expanduser()
        if not resolved.is_absolute():
            resolved = workspace_root / resolved
        reason = _save_check(resolved, workspace_root, agent)
        if reason:
            raise ValueError(reason)
    else:
        resolved = _renders_dir(workspace_root) / f"render-{int(time.time() * 1000)}{suffix}"
    created = missing_parents(resolved)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved, created


def _finish_target(target: Path, created: list[Path]) -> Path:
    """Hand a freshly rendered file (and any directory it needed) to the agent.

    Called after the render, never inside ``_resolve_target``: the file does not
    exist yet at that point, and chowning a path that is not there is a no-op
    that looks like it worked.
    """
    handoff_to_sandbox(target, created=created)
    return target


def _validate_canvas(width: int, height: int, scale: float) -> str:
    """Return an error string for an out-of-range canvas, or "" when it is fine."""
    for label, value in (("width", width), ("height", height)):
        if not _MIN_DIMENSION <= value <= _MAX_DIMENSION:
            return (
                f"[tool-error] {label}={value} is out of range — "
                f"use {_MIN_DIMENSION}-{_MAX_DIMENSION} pixels."
            )
    if not 0 < scale <= _MAX_SCALE:
        return f"[tool-error] scale={scale} is out of range — use a value above 0 and up to {_MAX_SCALE}."
    return ""


def _format_render(path: Path, width: int, height: int, extra: str = "") -> str:
    """Result shape shared with ``image_tools._format_result``.

    The bare ``Saved: <abspath>`` line is what ``PlatformAdapter.extract_local_files``
    scans for to deliver the file natively on a chat channel; the markdown line
    renders it inline in the dashboard. Keep both.
    """
    size_kb = path.stat().st_size / 1024
    lines = [
        f"Rendered {width}x{height} to {path.suffix.lstrip('.').upper()} ({size_kb:.1f} KB):",
        f"Saved: {path}",
    ]
    if path.suffix.lower() in (".png", ".svg"):
        lines.append(f"![{path.stem}]({path})")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


async def _with_page(
    source: Path,
    wait_until: str,
    viewport: dict[str, int],
    scale: float,
    work: Callable[[Any], Awaitable[str]],
    wait_for: str = "",
    guard: GuardState | None = None,
) -> str:
    """Open *source* in a throwaway Chromium and hand the page to *work*.

    An isolated browser per call, like the one-shot capture tools in
    ``browser.py``: a render must never inherit cookies, viewport or navigation
    state from whatever the shared session was doing.

    *wait_for* is a CSS selector to wait for after navigation — the contract
    for pages that draw with JavaScript (the charts frame appends a hidden
    ``.render-done`` once Vega has painted). ``attached`` rather than
    ``visible`` on purpose: a marker element is hidden by design.

    *guard* is installed on the context before the page exists (see the module
    docstring); what it refused is appended to *work*'s result as a note.
    """
    from playwright.async_api import async_playwright

    async def _open(extra_args: list[str]) -> tuple[Any, Any, Any]:
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(
                headless=True, args=_base_launch_args() + extra_args, **_launch_kwargs()
            )
            ctx = await browser.new_context(viewport=cast(Any, viewport), device_scale_factor=scale)
            if guard is not None:
                await install_guards(ctx, guard)
            page = await ctx.new_page()
            await page.goto(
                source.as_uri(), wait_until=cast(Any, wait_until), timeout=_RENDER_TIMEOUT_MS
            )
            return pw, browser, page
        except Exception as exc:
            try:
                await pw.stop()
            except Exception:
                pass
            if guard is not None:
                for entry in guard.drain():
                    if entry["navigation"]:
                        raise RuntimeError(entry["reason"]) from exc
            raise

    pw = browser = None
    try:
        try:
            pw, browser, page = await _open([])
        except Exception as first_err:
            if not _is_http2_error(str(first_err)):
                raise
            pw, browser, page = await _open(["--disable-http2"])

        if wait_for:
            try:
                await page.wait_for_selector(wait_for, state="attached", timeout=_RENDER_TIMEOUT_MS)
            except Exception as exc:
                raise RuntimeError(
                    f"wait_for {wait_for!r} did not appear within "
                    f"{_RENDER_TIMEOUT_MS // 1000}s — the page never finished drawing "
                    "(check its script errors), or the selector is wrong"
                ) from exc
        # Webfonts resolve after `load` on some documents; waiting on the font
        # set is what stops a card from rendering in fallback sans-serif.
        try:
            await page.evaluate("document.fonts && document.fonts.ready")
        except Exception:
            pass
        result = await work(page)
        if guard is not None and not result.startswith("[tool-error]"):
            result += _blocked_note(guard.drain())
        return result
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


@tool
def render_html_image(
    html: Annotated[
        str,
        "The HTML to render: an inline HTML document, or a path to an .html file "
        "(absolute or relative to the workspace)",
    ],
    width: Annotated[int, "Canvas width in CSS pixels, e.g. 1080 for a square social post"] = 1080,
    height: Annotated[int, "Canvas height in CSS pixels, e.g. 1350 for a 4:5 portrait post"] = 1080,
    save_path: Annotated[
        str,
        "Where to write the PNG, inside the workspace (relative paths resolve against it). Default: workspace/renders/",
    ] = "",
    scale: Annotated[
        float, "Export multiplier — 2 gives a 2x (retina) PNG at twice the pixel dimensions. 1-4"
    ] = 2.0,
    wait_until: Annotated[
        str,
        "When the page counts as loaded: 'load' (default), 'domcontentloaded', or 'networkidle'",
    ] = "load",
    full_page: Annotated[
        bool, "Capture the whole scrollable document instead of exactly width x height"
    ] = False,
    wait_for: Annotated[
        str,
        "CSS selector to wait for before capturing, for pages that draw with "
        "JavaScript (the charts skill's pages add `.render-done` when finished). "
        "Empty: capture right after `wait_until`.",
    ] = "",
) -> str:
    """Render HTML/CSS to a PNG at exact pixel dimensions.

    This is the deterministic half of image production: text, logos, statistics,
    diagrams, CTAs and brand colours come out precisely as specified, unlike a
    generated image where a headline is a guess. Use it for social graphics,
    carousel slides, quote cards, stat panels and thumbnails — typically layered
    over a background produced by ``image_generate``, which you reference from
    the HTML with a plain ``<img src="...">`` or a CSS ``background-image``.

    Runs on the Chromium that ships with Olano: no API key, no network call, no
    credits. Fonts bundled with the box (or any ``.ttf`` in the workspace) load
    through ``@font-face`` with a ``file://`` or relative URL. Local files load
    only from the workspace, skills and shared folders; remote assets follow the
    agent's outbound-access lists.

    At ``scale=2`` a 1080x1350 canvas produces a 2160x2700 PNG — author the CSS
    at the logical size and let ``scale`` handle resolution.
    """
    if wait_until not in _WAIT_STATES:
        return (
            f"[tool-error] render_html_image: wait_until must be one of {', '.join(_WAIT_STATES)}."
        )
    invalid = _validate_canvas(width, height, scale)
    if invalid:
        return invalid.replace("[tool-error] ", "[tool-error] render_html_image: ", 1)

    ws_root = _workspace_root()
    agent = _agent_name()
    files = _file_access(ws_root, agent)
    guard = GuardState(policy=GuardPolicy.capture(), files=files)
    try:
        # Target first: a refused target must not leave an inline temp file.
        target, created_dirs = _resolve_target(save_path, ws_root, ".png", agent)
        source, is_temp = _resolve_source(html, ws_root, files)
    except ValueError as exc:
        return f"[tool-error] render_html_image: {exc}"

    async def _render() -> str:
        async def _shoot(page: Any) -> str:
            await page.screenshot(path=str(target), full_page=full_page)
            return _format_render(_finish_target(target, created_dirs), width, height)

        try:
            return await _with_page(
                source,
                wait_until,
                {"width": width, "height": height},
                scale,
                _shoot,
                wait_for=wait_for,
                guard=guard,
            )
        except ImportError:
            return (
                "[tool-error] render_html_image: playwright not installed. "
                "Run: uv add playwright && playwright install chromium"
            )
        except asyncio.CancelledError:
            return "[tool-error] render_html_image: render cancelled."
        except Exception as exc:
            return f"[tool-error] render_html_image: {exc}"
        finally:
            if is_temp:
                source.unlink(missing_ok=True)

    try:
        return _run_async(_render(), timeout_s=120, reset_on_timeout=False)
    except Exception as exc:
        return f"[tool-error] render_html_image: {exc}"


@tool
def render_html_pdf(
    html: Annotated[
        str,
        "The HTML to render: an inline HTML document, or a path to an .html file "
        "(absolute or relative to the workspace)",
    ],
    width: Annotated[int, "Page width in CSS pixels, e.g. 1080 for a LinkedIn carousel"] = 1080,
    height: Annotated[int, "Page height in CSS pixels, e.g. 1350 for a LinkedIn carousel"] = 1350,
    save_path: Annotated[
        str,
        "Where to write the PDF, inside the workspace (relative paths resolve against it). Default: workspace/renders/",
    ] = "",
    wait_until: Annotated[
        str,
        "When the page counts as loaded: 'load' (default), 'domcontentloaded', or 'networkidle'",
    ] = "load",
    wait_for: Annotated[
        str,
        "CSS selector to wait for before printing, for pages that draw with "
        "JavaScript (`.render-done` for the charts skill). Empty: print after `wait_until`.",
    ] = "",
) -> str:
    """Render HTML/CSS to a PDF at exact page dimensions — one page per slide.

    The format LinkedIn wants for a native document (carousel) post, and the one
    that keeps text selectable and typography crisp at any zoom. Put each slide
    in its own block and separate them with ``page-break-after: always`` (or
    ``break-after: page``); every block becomes one PDF page at exactly
    ``width x height``.

    Backgrounds and colours print as authored — this passes Chromium's
    ``print_background``, so a dark slide stays dark instead of rendering white.

    Runs on the bundled Chromium: no key, no network, no credits. For a single
    still image use ``render_html_image`` instead.
    """
    if wait_until not in _WAIT_STATES:
        return f"[tool-error] render_html_pdf: wait_until must be one of {', '.join(_WAIT_STATES)}."
    invalid = _validate_canvas(width, height, 1.0)
    if invalid:
        return invalid.replace("[tool-error] ", "[tool-error] render_html_pdf: ", 1)

    ws_root = _workspace_root()
    agent = _agent_name()
    files = _file_access(ws_root, agent)
    guard = GuardState(policy=GuardPolicy.capture(), files=files)
    try:
        # Target first: a refused target must not leave an inline temp file.
        target, created_dirs = _resolve_target(save_path, ws_root, ".pdf", agent)
        source, is_temp = _resolve_source(html, ws_root, files)
    except ValueError as exc:
        return f"[tool-error] render_html_pdf: {exc}"

    async def _render() -> str:
        async def _print(page: Any) -> str:
            # `screen` keeps the on-screen CSS authoritative: without it Chromium
            # applies print styles and a slide designed for the viewport
            # reflows into something nobody laid out.
            await page.emulate_media(media="screen")
            await page.pdf(
                path=str(target),
                width=f"{width}px",
                height=f"{height}px",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
            return _format_render(_finish_target(target, created_dirs), width, height)

        try:
            return await _with_page(
                source,
                wait_until,
                {"width": width, "height": height},
                1.0,
                _print,
                wait_for=wait_for,
                guard=guard,
            )
        except ImportError:
            return (
                "[tool-error] render_html_pdf: playwright not installed. "
                "Run: uv add playwright && playwright install chromium"
            )
        except asyncio.CancelledError:
            return "[tool-error] render_html_pdf: render cancelled."
        except Exception as exc:
            return f"[tool-error] render_html_pdf: {exc}"
        finally:
            if is_temp:
                source.unlink(missing_ok=True)

    try:
        return _run_async(_render(), timeout_s=120, reset_on_timeout=False)
    except Exception as exc:
        return f"[tool-error] render_html_pdf: {exc}"


# The only JavaScript this module ever evaluates in a page: a fixed extraction
# of one element's markup. Nothing the agent passes is executed — `selector`
# is an argument to the function, not code.
_EXTRACT_ELEMENT_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  return new XMLSerializer().serializeToString(el);
}
"""


@tool
def render_html_svg(
    html: Annotated[
        str,
        "The HTML to render: an inline HTML document, or a path to an .html file "
        "(absolute or relative to the workspace)",
    ],
    width: Annotated[int, "Viewport width in CSS pixels the page lays out at"] = 1080,
    height: Annotated[int, "Viewport height in CSS pixels the page lays out at"] = 1080,
    save_path: Annotated[
        str,
        "Where to write the SVG, inside the workspace (relative paths resolve against it). Default: workspace/renders/",
    ] = "",
    wait_until: Annotated[
        str,
        "When the page counts as loaded: 'load' (default), 'domcontentloaded', or 'networkidle'",
    ] = "load",
    wait_for: Annotated[
        str,
        "CSS selector to wait for before extracting, for pages that draw with "
        "JavaScript (`.render-done` for the charts skill). Empty: extract after `wait_until`.",
    ] = "",
    selector: Annotated[
        str,
        "CSS selector of the <svg> element to save; the first match is taken. "
        "Default 'svg'; the charts skill's card is '#card'.",
    ] = "svg",
) -> str:
    """Save one ``<svg>`` element of a rendered page as a standalone .svg file.

    The vector twin of ``render_html_image``: a chart or diagram drawn into the
    page as SVG (by Vega, D3, or by hand) comes out as a real vector file that
    scales in a slide deck, a design tool or print — text stays text. Fonts are
    referenced by family name unless the page embedded them; PNG remains the
    right format for email and social platforms.

    Runs on the bundled Chromium: no key, no network, no credits. Only a fixed
    extraction runs in the page; ``selector`` is data, never code.
    """
    if wait_until not in _WAIT_STATES:
        return f"[tool-error] render_html_svg: wait_until must be one of {', '.join(_WAIT_STATES)}."
    invalid = _validate_canvas(width, height, 1.0)
    if invalid:
        return invalid.replace("[tool-error] ", "[tool-error] render_html_svg: ", 1)
    if not selector.strip():
        return "[tool-error] render_html_svg: selector must not be empty."

    ws_root = _workspace_root()
    agent = _agent_name()
    files = _file_access(ws_root, agent)
    guard = GuardState(policy=GuardPolicy.capture(), files=files)
    try:
        # Target first: a refused target must not leave an inline temp file.
        target, created_dirs = _resolve_target(save_path, ws_root, ".svg", agent)
        source, is_temp = _resolve_source(html, ws_root, files)
    except ValueError as exc:
        return f"[tool-error] render_html_svg: {exc}"

    async def _render() -> str:
        async def _extract(page: Any) -> str:
            markup = await page.evaluate(_EXTRACT_ELEMENT_JS, selector.strip())
            if not markup:
                return (
                    f"[tool-error] render_html_svg: selector {selector!r} matched nothing "
                    "in the rendered page."
                )
            if not markup.lstrip().startswith("<?xml"):
                markup = '<?xml version="1.0" encoding="UTF-8"?>\n' + markup
            target.write_text(markup, encoding="utf-8")
            return _format_render(_finish_target(target, created_dirs), width, height)

        try:
            return await _with_page(
                source,
                wait_until,
                {"width": width, "height": height},
                1.0,
                _extract,
                wait_for=wait_for,
                guard=guard,
            )
        except ImportError:
            return (
                "[tool-error] render_html_svg: playwright not installed. "
                "Run: uv add playwright && playwright install chromium"
            )
        except asyncio.CancelledError:
            return "[tool-error] render_html_svg: render cancelled."
        except Exception as exc:
            return f"[tool-error] render_html_svg: {exc}"
        finally:
            if is_temp:
                source.unlink(missing_ok=True)

    try:
        return _run_async(_render(), timeout_s=120, reset_on_timeout=False)
    except Exception as exc:
        return f"[tool-error] render_html_svg: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
