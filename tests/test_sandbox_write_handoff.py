"""Files the RUNTIME writes into a workspace must end up owned by the agent.

The sibling suite (`test_sandbox_workspace_ownership.py`) covers the *boot*
hand-off — the workspace the runtime seeds before the container starts. This one
covers everything written *afterwards*, mid-turn: a chart `render_html_image`
produced, a screenshot, a downloaded file, a file the dashboard's Files view
saved. Each is written by the runtime process (root) into a directory the
sandboxed agent (uid 1000) has bind-mounted, so without a hand-off the agent that
asked for the chart can read it and cannot overwrite, move or delete it.

Two properties matter and both are easy to lose:

* the gate is **a live sandbox**, not the posture — a sidecar counts, because a
  sidecar agent's shell is also uid 1000 over the same mount;
* the gate reads the turn's contextvar, which is **unset** in a dashboard request
  handler — so those surfaces pass the agent by name, and a test per surface is
  the only thing that catches a silent no-op there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.core.builder as builder
from olano.tools import _sandbox_exec


@pytest.fixture
def chowned(monkeypatch) -> list[tuple[str, int, int, bool]]:
    """Record every chown the code attempts, and let them all succeed."""
    calls: list[tuple[str, int, int, bool]] = []

    def fake_chown(path, uid, gid, *, follow_symlinks=True):
        calls.append((str(path), uid, gid, follow_symlinks))

    monkeypatch.setattr(builder.os, "chown", fake_chown)
    return calls


@pytest.fixture
def sandboxed(monkeypatch):
    """Pretend the named agent has a live sandbox (backend OR sidecar)."""

    def _enable(*, agent: str | None = "merli") -> None:
        monkeypatch.setattr(
            _sandbox_exec, "_live_sandbox_for", lambda name: object() if name == agent else None
        )
        monkeypatch.setattr(
            _sandbox_exec, "_live_sandbox_for_current_agent", lambda: object() if agent else None
        )

    return _enable


def _paths(calls) -> list[str]:
    return [c[0] for c in calls]


# ── the helper itself ────────────────────────────────────────────────────────


def test_no_sandbox_means_no_chown(chowned, tmp_path):
    """On the host lane the runtime and the agent are the same user; re-owning a
    file to a uid that means nothing there is a permission change nobody asked
    for."""
    f = tmp_path / "chart.png"
    f.write_bytes(b"x")
    _sandbox_exec.handoff_to_sandbox(f)
    assert chowned == []


def test_hands_over_the_file_and_every_directory_it_created(chowned, sandboxed, tmp_path):
    sandboxed()
    target = tmp_path / "reports" / "q3" / "chart.png"
    created = _sandbox_exec.missing_parents(target)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    _sandbox_exec.handoff_to_sandbox(target, created=created)

    # A file the agent can write inside a directory it cannot is half a hand-off.
    assert _paths(chowned) == [
        str(tmp_path / "reports"),
        str(tmp_path / "reports" / "q3"),
        str(target),
    ]
    assert all(
        uid == builder.SANDBOX_UID and gid == builder.SANDBOX_GID for _, uid, gid, _ in chowned
    )


def test_missing_parents_is_empty_when_the_directory_already_exists(tmp_path):
    (tmp_path / "reports").mkdir()
    assert _sandbox_exec.missing_parents(tmp_path / "reports" / "chart.png") == []


def test_never_raises_when_the_chown_fails(monkeypatch, sandboxed, tmp_path):
    """A hand-off is a courtesy on top of a write that already succeeded — it
    must never turn a delivered file into a tool error."""
    sandboxed()

    def boom(*a, **k):
        raise PermissionError("not root")

    monkeypatch.setattr(builder.os, "chown", boom)
    f = tmp_path / "chart.png"
    f.write_bytes(b"x")
    _sandbox_exec.handoff_to_sandbox(f)  # no raise


def test_symlinks_are_chowned_as_links(chowned, sandboxed, tmp_path):
    """Workspace `shared/*` entries are symlinks into the home; following one
    would re-own the shared source instead of the link."""
    sandboxed()
    target = tmp_path / "real.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    _sandbox_exec.handoff_to_sandbox(link)

    assert chowned and chowned[-1][3] is False


def test_an_explicit_agent_beats_an_unset_contextvar(chowned, monkeypatch, tmp_path):
    """The half of this that a dashboard request depends on: the handler runs
    outside `agent_runtime_context`, so the contextvar route finds nothing and
    the hand-off would silently do nothing without the name."""
    monkeypatch.setattr(_sandbox_exec, "_live_sandbox_for_current_agent", lambda: None)
    monkeypatch.setattr(
        _sandbox_exec, "_live_sandbox_for", lambda name: object() if name == "merli" else None
    )
    f = tmp_path / "notes.md"
    f.write_text("x")

    _sandbox_exec.handoff_to_sandbox(f)
    assert chowned == []

    _sandbox_exec.handoff_to_sandbox(f, agent="merli")
    assert _paths(chowned) == [str(f)]


def test_a_sidecar_counts_as_a_live_sandbox(monkeypatch):
    """`sandbox_is_backend` is False for a sidecar, and asking it would leave
    every Read/Write agent on a managed box with root-owned output."""
    from olano.security import deepagents_middleware

    backend = object()
    monkeypatch.setattr(builder, "get_sandbox_backend", lambda name: backend)
    monkeypatch.setattr(deepagents_middleware, "_is_live_sandbox_backend", lambda b: b is backend)

    assert _sandbox_exec.sandbox_active_for("merli") is True


# ── the surfaces ─────────────────────────────────────────────────────────────


def test_a_render_hands_over_its_png_and_the_folder_it_made(chowned, sandboxed, tmp_path):
    from olano.tools.render_tools import _finish_target, _resolve_target

    sandboxed()
    target, created = _resolve_target("reports/chart.png", tmp_path, ".png")
    target.write_bytes(b"\x89PNG")
    _finish_target(target, created)

    assert _paths(chowned) == [str(tmp_path / "reports"), str(target)]


def test_a_render_on_the_host_lane_hands_over_nothing(chowned, tmp_path):
    from olano.tools.render_tools import _finish_target, _resolve_target

    target, created = _resolve_target("reports/chart.png", tmp_path, ".png")
    target.write_bytes(b"\x89PNG")
    _finish_target(target, created)

    assert chowned == []


def test_a_saved_upload_is_handed_to_the_named_agent(chowned, sandboxed, tmp_path):
    """`save_media_bytes` runs from a request handler and from a messenger task;
    the dashboard one has no bound contextvar, hence the explicit name."""
    from olano.uploads import save_media_bytes

    sandboxed()
    path, reused = save_media_bytes(
        tmp_path, "a.png", "image/png", "image", b"\x89PNG", agent="merli"
    )

    assert not reused
    assert str(path) in _paths(chowned)
    # The uploads/ directory this call created is handed over too.
    assert str(tmp_path / "uploads") in _paths(chowned)


def test_a_reused_upload_is_still_handed_over(chowned, sandboxed, tmp_path):
    """A byte-identical file written by an earlier run — before this hand-off
    existed — is still root-owned, so dedupe must not skip it."""
    from olano.uploads import save_media_bytes

    sandboxed()
    save_media_bytes(tmp_path, "a.png", "image/png", "image", b"\x89PNG", agent="merli")
    chowned.clear()
    path, reused = save_media_bytes(
        tmp_path, "a.png", "image/png", "image", b"\x89PNG", agent="merli"
    )

    assert reused
    assert str(path) in _paths(chowned)


def test_generated_images_are_handed_over(chowned, sandboxed, monkeypatch, tmp_path):
    from olano.tools import image_tools

    sandboxed()
    monkeypatch.setattr(image_tools, "_workspace_root", lambda: tmp_path)
    paths = image_tools._save_images([b"\x89PNG", b"\x89PNG"], "a chart")

    assert len(paths) == 2
    for p in paths:
        assert str(p) in _paths(chowned)


# ── the deliberate exclusions ────────────────────────────────────────────────


def test_a_host_path_write_never_hands_off_to_the_sandbox():
    """A magic link editing /opt/olano/olano.env writes a file that belongs to
    the RUNTIME. Handing it to uid 1000 is a permission change nobody asked for,
    so that writer stays out of this — pinned here so the exclusion is a
    decision on the record rather than an oversight."""
    src = Path("olano/magic_links/adapters/edit_file.py").read_text(encoding="utf-8")
    assert "handoff_to_sandbox" not in src
    assert "Deliberately no chown_for_sandbox" in src


def test_the_helper_documents_what_it_is_not_for():
    doc = _sandbox_exec.handoff_to_sandbox.__doc__ or ""
    for excluded in ("/tmp", "agents/<name>", "outside a workspace"):
        assert excluded in doc, f"the docstring no longer names {excluded!r} as out of scope"


def test_os_chown_has_exactly_one_caller():
    """Every hand-off goes through `builder.chown_for_sandbox`, which never
    raises and always chowns links as links. A second `os.chown` in the tree
    would be a second set of those decisions."""
    import subprocess

    hits = subprocess.run(
        ["grep", "-rn", r"os\.chown\|shutil\.chown", "olano/", "--include=*.py"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    files = {line.split(":", 1)[0] for line in hits}
    assert files == {"olano/core/builder.py"}, f"unexpected chown callers: {sorted(files)}"


# ── text_to_speech: the gate is the path, not the caller ─────────────────────


def _fake_synthesize(monkeypatch):
    """Stand in for the provider call: write bytes where asked, return the path."""

    def synth(text, output_path, voice="", audio_format=""):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ID3fake")
        return str(out)

    monkeypatch.setattr("olano.speech.synthesize_speech", synth)


def test_a_voice_clip_saved_in_the_workspace_is_handed_over(
    chowned, sandboxed, monkeypatch, tmp_path
):
    from olano.security.request_context import push_request_context
    from olano.tools.media_tools import text_to_speech

    sandboxed()
    _fake_synthesize(monkeypatch)
    target = tmp_path / "reports" / "reply.mp3"
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        out = text_to_speech.func(text="hello", output_path=str(target))

    assert out.startswith("Speech saved to"), out
    assert str(target) in _paths(chowned)


def test_the_default_tmp_clip_is_left_to_the_runtime(chowned, sandboxed, monkeypatch, tmp_path):
    """The tool's default output is /tmp, which the container never sees, and
    `handoff_to_sandbox` names /tmp as out of scope. Re-owning it to a uid that
    cannot reach it is a permission change nobody asked for — the same rule
    that keeps a magic-link edit of the runtime's own files out of this."""
    from olano.security.request_context import push_request_context
    from olano.tools.media_tools import text_to_speech

    sandboxed()
    _fake_synthesize(monkeypatch)
    scratch = tmp_path / "scratch" / "lionclaw_speech.mp3"  # stands in for /tmp
    with push_request_context(metadata={"workspace_root": str(tmp_path / "workspace")}):
        out = text_to_speech.func(text="hello", output_path=str(scratch))

    assert out.startswith("Speech saved to"), out
    assert chowned == []


def test_the_audio_writer_itself_knows_nothing_about_sandboxes():
    """`speech._write_audio` is shared with the messenger voice-reply path, which
    writes into the runtime's own audio cache. Putting the hand-off there once
    chowned every voice reply's cache file; the decision belongs to the one
    caller that can produce a workspace file, gated on the path."""
    src = Path("olano/speech.py").read_text(encoding="utf-8")
    assert "handoff_to_sandbox" not in src
