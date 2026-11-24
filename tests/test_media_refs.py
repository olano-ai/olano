"""Tests for workspace file detection in chat text (inline media + file cards)."""

from __future__ import annotations

from pathlib import Path

from olano.runtime.media_refs import detect_media_refs, file_raw_url


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "agents" / "sinclair" / "workspace"
    (ws / "screenshots").mkdir(parents=True)
    return ws


def test_detects_screenshot_from_tool_result(tmp_path):
    ws = _make_ws(tmp_path)
    shot = ws / "screenshots" / "www-sicop-go-cr-app-1783253897145.png"
    shot.write_bytes(b"\x89PNG")

    content = f"Screenshot saved: {shot} (1420.4 KB)\nPage: https://x\nTitle: SICOP"
    refs = detect_media_refs(content, ws, "sinclair")

    assert len(refs) == 1
    ref = refs[0]
    assert ref["kind"] == "image"
    assert ref["mime"] == "image/png"
    assert ref["path"] == "screenshots/www-sicop-go-cr-app-1783253897145.png"
    assert ref["url"] == (
        "/api/files-raw/screenshots/www-sicop-go-cr-app-1783253897145.png?agent=sinclair"
    )


def test_detects_audio_and_video(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "clip.mp3").write_bytes(b"ID3")
    (ws / "reel.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")

    content = "Saved audio to clip.mp3 and rendered reel.mp4 in the workspace."
    refs = detect_media_refs(content, ws, "sinclair")
    kinds = {r["kind"] for r in refs}
    assert kinds == {"audio", "video"}


def test_ignores_paths_outside_workspace(tmp_path):
    ws = _make_ws(tmp_path)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"\x89PNG")

    content = f"Wrote {outside} and /etc/passwd.png somewhere"
    refs = detect_media_refs(content, ws, "sinclair")
    assert refs == []


def test_ignores_nonexistent_files(tmp_path):
    ws = _make_ws(tmp_path)
    content = "The report is at screenshots/does-not-exist.png"
    refs = detect_media_refs(content, ws, "sinclair")
    assert refs == []


def test_dedupes_repeated_paths(tmp_path):
    ws = _make_ws(tmp_path)
    shot = ws / "screenshots" / "a.png"
    shot.write_bytes(b"\x89PNG")
    content = f"saved {shot} ... and again {shot}"
    refs = detect_media_refs(content, ws, "sinclair")
    assert len(refs) == 1


def test_relative_path_resolves_under_workspace(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "screenshots" / "b.png").write_bytes(b"\x89PNG")
    refs = detect_media_refs("see screenshots/b.png", ws, "sinclair")
    assert len(refs) == 1
    assert refs[0]["path"] == "screenshots/b.png"


def test_empty_and_no_match(tmp_path):
    ws = _make_ws(tmp_path)
    assert detect_media_refs("", ws, "sinclair") == []
    assert detect_media_refs("no media here, just text.", ws, "sinclair") == []


def test_bare_non_media_names_are_not_references(tmp_path):
    """A file name with no directory component is prose, not a hand-off — a
    reply that mentions `notes.txt` in passing must not sprout a card."""
    ws = _make_ws(tmp_path)
    (ws / "notes.txt").write_text("hi")
    (ws / "data.json").write_text("{}")
    refs = detect_media_refs("wrote notes.txt and data.json", ws, "sinclair")
    assert refs == []


def test_documents_with_a_directory_component_become_cards(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "reports").mkdir()
    (ws / "reports" / "accounts.csv").write_text("a,b\n1,2\n")
    (ws / "reports" / "deck.pdf").write_bytes(b"%PDF-1.4")
    (ws / "reports" / "build.py").write_text("print(1)\n")

    content = (
        "Saved: reports/accounts.csv — also reports/deck.pdf, and the script is reports/build.py."
    )
    refs = detect_media_refs(content, ws, "sinclair")
    by_path = {r["path"]: r for r in refs}
    assert by_path["reports/accounts.csv"]["kind"] == "document"
    assert by_path["reports/accounts.csv"]["mime"] == "text/csv"
    assert by_path["reports/deck.pdf"]["kind"] == "document"
    assert by_path["reports/build.py"]["kind"] == "file"
    # Trailing sentence punctuation stays out of the token.
    assert by_path["reports/build.py"]["mentions"] == ["reports/build.py"]


def test_descriptor_carries_links_size_and_every_mention(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "reports").mkdir()
    chart = ws / "reports" / "accounts-by-country.png"
    chart.write_bytes(b"\x89PNG" + b"0" * 96)

    content = (
        f"Saved: {chart}\n"
        "It is also at `reports/accounts-by-country.png` (reports/accounts-by-country.png)."
    )
    refs = detect_media_refs(content, ws, "sinclair")
    assert len(refs) == 1
    ref = refs[0]
    assert ref["kind"] == "image"
    assert ref["size"] == 100
    assert ref["url"] == "/api/files-raw/reports/accounts-by-country.png?agent=sinclair"
    assert ref["download_url"] == ref["url"] + "&download=1"
    assert ref["files_url"] == "/files?path=reports/accounts-by-country.png&agent=sinclair"
    # Absolute, backticked and bare relative forms all resolved to one file;
    # the renderer links each exact token.
    assert ref["mentions"] == [str(chart), "reports/accounts-by-country.png"]


def test_one_card_per_file_across_every_spelling_in_one_reply(tmp_path):
    """A final message may name the same file several ways — bare, backticked,
    absolute, `/workspace/…`, through a symlink, with `..` — and must still
    attach it exactly once. Dedupe is keyed on the RESOLVED workspace-relative
    path, so every form collapses and `mentions` collects the tokens."""
    ws = _make_ws(tmp_path)
    (ws / "reports").mkdir()
    chart = ws / "reports" / "chart.png"
    chart.write_bytes(b"\x89PNG")
    (ws / "latest").symlink_to(ws / "reports", target_is_directory=True)

    content = (
        f"Saved: reports/chart.png\n"
        f"It is at `reports/chart.png`, at {chart}, at /workspace/reports/chart.png, "
        f"at latest/chart.png and at scratch/../reports/chart.png."
    )
    refs = detect_media_refs(content, ws, "sinclair")

    assert len(refs) == 1
    assert refs[0]["path"] == "reports/chart.png"
    assert len(refs[0]["mentions"]) >= 4


def test_file_uri_and_container_paths_resolve(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "reports").mkdir()
    (ws / "reports" / "a.png").write_bytes(b"\x89PNG")
    content = "file:///workspace/reports/a.png and /workspace/reports/a.png"
    refs = detect_media_refs(content, ws, "sinclair")
    assert [r["path"] for r in refs] == ["reports/a.png"]
    assert len(refs[0]["mentions"]) == 2


def test_hidden_credential_and_bootstrap_files_never_become_cards(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / ".olano").mkdir()
    (ws / ".olano" / "state.json").write_text("{}")
    (ws / "config").mkdir()
    (ws / "config" / "service.env").write_text("K=v")
    (ws / "config" / "api-token.txt").write_text("t")
    (ws / "MEMORY.md").write_text("# memory")
    content = (
        "see .olano/state.json, config/service.env, config/api-token.txt and "
        "I updated MEMORY.md and ./MEMORY.md"
    )
    assert detect_media_refs(content, ws, "sinclair") == []


def test_urls_that_end_in_an_extension_are_ignored(tmp_path):
    ws = _make_ws(tmp_path)
    (ws / "screenshots" / "a.png").write_bytes(b"\x89PNG")
    refs = detect_media_refs("https://example.com/screenshots/a.png", ws, "sinclair")
    assert refs == []


def test_caps_number_of_items(tmp_path):
    ws = _make_ws(tmp_path)
    paths = []
    for i in range(12):
        p = ws / "screenshots" / f"shot-{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    content = "saved: " + " ".join(paths)
    refs = detect_media_refs(content, ws, "sinclair")
    assert len(refs) == 8  # _MAX_ITEMS


def test_tilde_tokens_that_are_not_paths_never_raise(tmp_path):
    """Money and version strings match the path token regex, and a leading ``~``
    used to be handed to ``Path.expanduser()``.

    ``~$2.115M`` reads as user ``$2.115M``, which resolves to no home, and
    ``expanduser`` answers that by RAISING ``RuntimeError: Could not determine
    home directory.`` — a message about an env var, for a reply that merely
    quoted a pipeline total. The scan runs on the agent's finished answer, so
    the turn's last act was to report a broken box; nothing was broken, and the
    agent never saw the error it was blamed for.
    """
    ws = _make_ws(tmp_path)
    content = (
        "31 opportunities, $5.76M combined (18 closed won at $3.645M, 13 open at "
        "~$2.115M). United Oil is ~$2.03M of it."
    )
    assert detect_media_refs(content, ws, "sinclair") == []


def test_real_home_paths_still_expand(tmp_path, monkeypatch):
    """The guard rejects an unresolvable ``~user``, not tilde expansion itself."""
    ws = _make_ws(tmp_path)
    (ws / "screenshots" / "a.png").write_bytes(b"\x89PNG")
    monkeypatch.setenv("HOME", str(ws))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ws))

    refs = detect_media_refs("saved to ~/screenshots/a.png", ws, "sinclair")
    assert [r["path"] for r in refs] == ["screenshots/a.png"]


def test_file_raw_url_encodes_segments():
    url = file_raw_url("screenshots/my shot (1).png", "agent name")
    assert url == "/api/files-raw/screenshots/my%20shot%20%281%29.png?agent=agent%20name"
    # Slashes between segments are preserved for FastAPI's {path:path}.
    assert "/api/files-raw/screenshots/" in url
