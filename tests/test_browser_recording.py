"""Recording an agent's browser (olano/browser/recording.py).

A recording is the live view's screencast fed to ffmpeg at a steady rate:
these run the real thing against Chromium and ffmpeg and read the file back
with ffprobe. Skipped where either is missing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest


@lru_cache(maxsize=1)
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available() or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="playwright/chromium or ffmpeg not installed",
)

_W, _H = 1280, 800
_MOVING = """<!doctype html><title>Clock</title><style>body{margin:0;background:#fff}</style>
<div id=t style="font:bold 120px sans-serif;padding:40px"></div>
<script>setInterval(() => { document.getElementById('t').textContent = Date.now(); }, 50);</script>"""


@pytest.fixture
async def live(monkeypatch):
    from playwright.async_api import async_playwright

    from olano.browser import sessions

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": _W, "height": _H})
        page = await context.new_page()
        sess = sessions.Session(agent="merli", kind="headless", context=context, page=page)
        monkeypatch.setitem(sessions._sessions, "merli", sess)
        await page.set_content(_MOVING)
        try:
            yield sess
        finally:
            if sess.recorder is not None:
                await sess.recorder.stop()
            await browser.close()


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


async def test_a_recording_is_a_playable_mp4_of_the_page(live, tmp_path):
    from olano.browser import recording

    state = await recording.start_recording("merli", tmp_path)
    assert state["name"].startswith("browser-") and state["name"].endswith(".mp4")
    assert live.recorder is not None and live.snapshot()["recording"]["name"] == state["name"]
    # Starting again while one runs is the same recording, not a second one.
    assert (await recording.start_recording("merli", tmp_path))["name"] == state["name"]
    await asyncio.sleep(2.0)
    saved = await recording.stop_recording("merli")
    assert saved is not None
    assert live.recorder is None and live.snapshot()["recording"] is None
    assert saved.path == tmp_path / "recordings" / state["name"]
    assert saved.bytes > 0 and saved.relative == f"recordings/{state['name']}"
    assert saved.seconds >= 1.0
    info = _probe(saved.path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"]) == (_W, _H)
    assert float(info["format"]["duration"]) >= 1.0
    # Stopped twice: the same file, no error.
    assert await recording.stop_recording("merli") is None


async def test_closing_the_session_saves_the_recording(live, tmp_path, monkeypatch):
    from olano.browser import recording, sessions

    state = await recording.start_recording("merli", tmp_path)
    await asyncio.sleep(1.0)
    recorder = live.recorder
    assert await sessions.close("merli", save=False)
    saved = await recorder.stop()  # already finished by close(): the same result
    assert saved.path.name == state["name"] and saved.bytes > 0


async def test_the_cap_ends_a_recording_on_its_own(live, tmp_path, monkeypatch):
    from olano.browser import recording

    monkeypatch.setattr(recording, "MAX_RECORDING_S", 1.0)
    state = await recording.start_recording("merli", tmp_path)
    for _ in range(100):
        if live.recorder is None:
            break
        await asyncio.sleep(0.1)
    assert live.recorder is None, "the cap should have ended it"
    path = tmp_path / "recordings" / state["name"]
    assert path.exists() and path.stat().st_size > 0


async def test_a_picture_is_a_png_in_the_screenshots_folder(live, tmp_path):
    from olano.browser import recording

    saved = await recording.take_picture("merli", tmp_path)
    assert saved.path.parent == tmp_path / "screenshots"
    assert saved.path.name.startswith("live-") and saved.path.suffix == ".png"
    assert saved.path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert saved.relative.startswith("screenshots/")


async def test_without_a_browser_recording_is_refused(tmp_path):
    from olano.browser import recording

    with pytest.raises(recording.RecordingUnavailable):
        await recording.start_recording("nobody", tmp_path)
    assert await recording.stop_recording("nobody") is None


def test_the_tool_is_a_browser_tool_everywhere():
    from olano.browser.policy import BROWSER_TOOL_NAMES
    from olano.tools._name_index import NAME_INDEX

    assert "browser_record" in BROWSER_TOOL_NAMES
    assert NAME_INDEX["browser_record"] == "olano.tools.browser"
