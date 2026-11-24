"""What this box reports about itself (`deployment/version.py` + reporter).

The property under test: **the runtime never invents a version.** A box that
cannot identify its own build says so, and every surface renders that as
"not reported" rather than a placeholder.

The bug this suite exists to prevent (2026-08-14): nothing on the box reported
a version, so `deployments.runtime_version` stayed NULL and the console rendered
NULL as "latest (as deployed)". A box running week-old code and a box running
this morning's produced identical text, so an Update that pulled a stale image
was indistinguishable from one that worked — which cost a live debugging
session.
"""

from __future__ import annotations

import httpx
import pytest

from olano.deployment import version as ver
from olano.deployment import version_reporter as vr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (ver.IMAGE_REF_ENV, ver.BUILD_SHA_ENV, ver.BUILD_TIME_ENV):
        monkeypatch.delenv(name, raising=False)


# ── resolution order ─────────────────────────────────────────────────────────


def test_image_ref_is_the_most_precise_answer(monkeypatch) -> None:
    monkeypatch.setenv(ver.IMAGE_REF_ENV, "latest@sha256:abc123")
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "deadbeefcafe1234")
    assert ver.runtime_version() == "latest@sha256:abc123"


def test_baked_commit_answers_did_my_merge_reach_the_box(monkeypatch) -> None:
    """The FULL sha: CI tags each image with its commit, so the control plane
    resolves this to a registry digest and can tell whether a newer image
    exists. A 12-char prefix reported for display broke that comparison —
    `updateAvailable` went blank on every box that reported for itself."""
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "46006d69f0d0bfa39df274c4c47991b3fa4ec77c")
    assert ver.runtime_version() == "git-46006d69f0d0bfa39df274c4c47991b3fa4ec77c"


def test_a_source_install_reports_its_package_version() -> None:
    # No image, no baked commit — still a real answer, just a different kind.
    assert str(ver.runtime_version()).startswith("src-")


def test_the_value_fits_the_control_planes_column(monkeypatch) -> None:
    # The API validates max(64); a longer value would be truncated server-side
    # into something that no longer matches what CI published.
    monkeypatch.setenv(ver.IMAGE_REF_ENV, "x" * 200)
    assert len(str(ver.runtime_version())) <= ver.MAX_VERSION_CHARS


# ── never invent a version ───────────────────────────────────────────────────


def test_an_unidentifiable_build_returns_none_not_a_placeholder(monkeypatch) -> None:
    """THE RULE. `None` is a real answer and must survive to the UI. Returning
    a friendly string here is exactly the bug: it makes "I don't know" and
    "you're up to date" render identically."""
    monkeypatch.setattr(ver, "_package_version", lambda: None)
    monkeypatch.setattr("olano.__version__", "", raising=False)  # no package version either
    assert ver.runtime_version() is None


def test_report_marks_an_unknown_build_as_unknown(monkeypatch) -> None:
    monkeypatch.setattr(ver, "runtime_version", lambda: None)
    monkeypatch.setattr(ver, "_package_version", lambda: None)
    report = ver.version_report()
    assert report["known"] is False
    assert report["runtime_version"] is None


def test_report_carries_the_raw_signals_for_diagnosis(monkeypatch) -> None:
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "abc123def456")
    monkeypatch.setenv(ver.BUILD_TIME_ENV, "2026-08-14T18:47:00Z")
    report = ver.version_report()
    assert report["known"] is True
    assert report["build_sha"] == "abc123def456"
    assert report["build_time"] == "2026-08-14T18:47:00Z"


# ── reporting ────────────────────────────────────────────────────────────────


class _Cfg:
    url = "https://cloud.example.test"
    token = "olano_dep_tok"


def test_reports_the_version_to_the_control_plane(monkeypatch) -> None:
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "abc123def4567890")
    monkeypatch.setattr(ver, "_package_version", lambda: "0.10.6")
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: _Cfg())

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append({"url": str(request.url), "body": json.loads(request.content.decode())})
        return httpx.Response(200, json={"ok": True})

    assert vr.report_runtime_version(transport=httpx.MockTransport(handler)) is True
    assert sent[0]["url"] == "https://cloud.example.test/api/v1/health"
    # `build_sha` is the canonical identity the control plane stores as
    # `reported_commit`; `runtime_version` stays for older control planes;
    # `package_version` is the release name it shows beside the commit.
    assert sent[0]["body"] == {
        "runtime_version": "git-abc123def4567890",
        "build_sha": "abc123def4567890",
        "package_version": "0.10.6",
    }


def test_a_source_install_reports_no_build_sha_field(monkeypatch) -> None:
    """No baked sha ⇒ the field is omitted, never sent empty — the same
    never-a-placeholder rule as the version itself."""
    monkeypatch.delenv(ver.BUILD_SHA_ENV, raising=False)
    monkeypatch.delenv(ver.IMAGE_REF_ENV, raising=False)
    monkeypatch.setattr(ver, "_package_version", lambda: "0.9.1")
    monkeypatch.setattr("olano.__version__", "0.9.1", raising=False)
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: _Cfg())

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    assert vr.report_runtime_version(transport=httpx.MockTransport(handler)) is True
    assert sent[0] == {"runtime_version": "src-0.9.1", "package_version": "0.9.1"}


def test_an_unknown_package_version_is_omitted_not_sent_empty(monkeypatch) -> None:
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "abc123def4567890")
    monkeypatch.setattr(ver, "_package_version", lambda: None)
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: _Cfg())

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    assert vr.report_runtime_version(transport=httpx.MockTransport(handler)) is True
    assert "package_version" not in sent[0]


def test_an_unknown_version_reports_nothing_at_all(monkeypatch) -> None:
    """Silence beats a placeholder: the console then shows "not reported",
    which is the truth and sends the operator looking in the right place."""
    monkeypatch.setattr(ver, "runtime_version", lambda: None)
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: _Cfg())

    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not POST an unknown version")

    assert vr.report_runtime_version(transport=httpx.MockTransport(boom)) is False


def test_no_cloud_channel_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "abc123def4567890")
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: None)
    assert vr.report_runtime_version() is False


def test_a_failing_control_plane_never_raises(monkeypatch) -> None:
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "abc123def4567890")
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: _Cfg())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    assert vr.report_runtime_version(transport=httpx.MockTransport(handler)) is False


def test_a_network_error_never_raises(monkeypatch) -> None:
    monkeypatch.setenv(ver.BUILD_SHA_ENV, "abc123def4567890")
    from olano.deployment import sources

    monkeypatch.setattr(sources, "cloud_settings_config", lambda: _Cfg())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert vr.report_runtime_version(transport=httpx.MockTransport(handler)) is False
