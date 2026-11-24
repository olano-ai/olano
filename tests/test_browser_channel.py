"""Browser channel selection — the seam that lets the image ship one binary.

Playwright ships two Chromium builds and `playwright install chromium` fetches
BOTH (597 MB browser + 323 MB headless shell, measured). Every launch in olano
is headless, so the runtime image installs the shell alone and points at it
with LIONCLAW_BROWSER_CHANNEL.

The env gate is the whole point: hardcoding the channel would break every
developer who ran `playwright install chromium` (no shell binary present), and
leaving it out would make the slimmed image unable to launch at all.
"""

from __future__ import annotations

import pytest

from olano.tools.browser import _launch_kwargs


def test_unset_keeps_playwrights_default(monkeypatch: pytest.MonkeyPatch):
    # Local dev and the E2E runner install the full browser; passing a channel
    # they do not have would fail the launch.
    monkeypatch.delenv("LIONCLAW_BROWSER_CHANNEL", raising=False)
    assert _launch_kwargs() == {}


def test_set_selects_that_channel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_BROWSER_CHANNEL", "chromium-headless-shell")
    assert _launch_kwargs() == {"channel": "chromium-headless-shell"}


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch, value: str):
    # An empty value must not reach Playwright as channel="" — that is an
    # invalid channel and would fail every launch.
    monkeypatch.setenv("LIONCLAW_BROWSER_CHANNEL", value)
    assert _launch_kwargs() == {}


def test_value_is_stripped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_BROWSER_CHANNEL", " chromium-headless-shell \n")
    assert _launch_kwargs() == {"channel": "chromium-headless-shell"}


def test_image_sets_the_channel_it_installs():
    """The Dockerfile's ENV and its `playwright install` target must agree.

    Installing the shell but launching the default build (or vice versa) fails
    only at runtime, on a customer's box, the first time an agent browses.
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "LIONCLAW_BROWSER_CHANNEL=chromium-headless-shell" in dockerfile
    assert "playwright install --with-deps chromium-headless-shell" in dockerfile
