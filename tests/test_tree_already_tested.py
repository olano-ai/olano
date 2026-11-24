"""The post-merge E2E skip, pinned: it skips only on a proven-identical tree.

`.github/scripts/tree-already-tested.sh` lets the push-to-main run of e2e.yml
skip the suite when the merged PR's own E2E run already passed on the same git
tree. Skipping a check is only safe when the proof is exact, so every way the
proof can be missing or wrong (a different tree, no merged PR, an expired
artifact, an API that answers nothing) must come out as "run the suite". The
script is run here against a fake `gh` on PATH that serves saved API shapes,
and the workflow wiring the script depends on is asserted alongside it.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "tree-already-tested.sh"
E2E = ROOT / ".github" / "workflows" / "e2e.yml"
ARTIFACT = "e2e-tested-tree"
REPO = "olano-ai/olano"

pytestmark = pytest.mark.skipif(
    not (shutil.which("jq") and shutil.which("unzip") and shutil.which("git")),
    reason="needs jq, unzip and git (present on every CI runner)",
)

# A stand-in for `gh api <endpoint> [--jq <expr>]`: answers from a JSON map of
# endpoint -> body (or -> {"zip": path}), applies --jq with the real jq, and
# fails like gh does (non-zero, nothing on stdout) for an unknown endpoint.
FAKE_GH = r"""#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
if not args or args[0] != "api":
    sys.exit(2)
endpoint = args[1]
jq = args[args.index("--jq") + 1] if "--jq" in args else None
routes = json.load(open(os.environ["FAKE_GH_ROUTES"]))
if endpoint not in routes:
    sys.stderr.write("HTTP 404: " + endpoint + "\n")
    sys.exit(1)
body = routes[endpoint]
if isinstance(body, dict) and "zip" in body:
    sys.stdout.buffer.write(open(body["zip"], "rb").read())
    sys.exit(0)
raw = json.dumps(body)
if jq is None:
    sys.stdout.write(raw)
    sys.exit(0)
sys.exit(subprocess.run(["jq", "-r", jq], input=raw.encode()).returncode)
"""


def _commit(repo: Path) -> tuple[str, str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True, env=env)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha, tree


def _zip(path: Path, tree: str) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tested-tree.txt", tree + "\n")
    path.write_bytes(buf.getvalue())
    return path


def _routes(sha: str, zip_path: Path, *, merged: bool = True, expired: bool = False) -> dict:
    return {
        f"repos/{REPO}/commits/{sha}/pulls": [
            {"number": 42, "merged_at": "2026-10-20T00:00:00Z" if merged else None}
        ],
        f"repos/{REPO}/pulls/42": {"head": {"ref": "feature/x"}},
        f"repos/{REPO}/actions/workflows/e2e.yml/runs?event=pull_request&status=success&branch=feature/x&per_page=5": {
            "workflow_runs": [{"id": 7}]
        },
        f"repos/{REPO}/actions/runs/7/artifacts": {
            "artifacts": [{"id": 99, "name": ARTIFACT, "expired": expired}]
        },
        f"repos/{REPO}/actions/artifacts/99/zip": {"zip": str(zip_path)},
    }


def _ask(tmp_path: Path, repo: Path, sha: str, routes: dict) -> tuple[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable), encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(routes), encoding="utf-8")
    out_file = tmp_path / "github_output"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_ROUTES": str(routes_file),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": sha,
        "GITHUB_OUTPUT": str(out_file),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), "e2e.yml", ARTIFACT],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == out_file.read_text().strip()
    return out_file.read_text().strip(), proc.stdout


def test_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_identical_tree_skips(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha, tree = _commit(repo)
    verdict, out = _ask(tmp_path, repo, sha, _routes(sha, _zip(tmp_path / "a.zip", tree)))
    assert verdict == "skip=true", out
    assert "PR #42" in out


def test_different_tree_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha, _ = _commit(repo)
    other = "0" * 40
    verdict, _ = _ask(tmp_path, repo, sha, _routes(sha, _zip(tmp_path / "a.zip", other)))
    assert verdict == "skip=false"


def test_unmerged_pull_request_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha, tree = _commit(repo)
    routes = _routes(sha, _zip(tmp_path / "a.zip", tree), merged=False)
    assert _ask(tmp_path, repo, sha, routes)[0] == "skip=false"


def test_expired_artifact_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha, tree = _commit(repo)
    routes = _routes(sha, _zip(tmp_path / "a.zip", tree), expired=True)
    assert _ask(tmp_path, repo, sha, routes)[0] == "skip=false"


def test_api_answering_nothing_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha, _ = _commit(repo)
    assert _ask(tmp_path, repo, sha, {})[0] == "skip=false"


def test_unknown_commit_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _, tree = _commit(repo)
    # The checkout does not hold this commit, so its tree cannot be read.
    missing = "f" * 40
    routes = _routes(missing, _zip(tmp_path / "a.zip", tree))
    assert _ask(tmp_path, repo, missing, routes)[0] == "skip=false"


# ── the workflow wiring the script relies on ─────────────────────────────────


def _e2e() -> dict:
    return yaml.safe_load(E2E.read_text(encoding="utf-8"))


def test_only_a_push_asks() -> None:
    job = _e2e()["jobs"]["already-tested"]
    assert job["if"] == "github.event_name == 'push'"
    step = next(s for s in job["steps"] if s.get("id") == "check")
    assert step["run"].split()[1:] == ["e2e.yml", ARTIFACT]


def test_suite_runs_unless_proven_tested() -> None:
    job = _e2e()["jobs"]["e2e"]
    assert job["needs"] == "already-tested"
    # Without !cancelled() the skipped need on a PR would skip the suite too.
    assert job["if"] == "${{ !cancelled() && needs.already-tested.outputs.skip != 'true' }}"


def test_pull_request_run_records_its_tree_on_success() -> None:
    steps = _e2e()["jobs"]["e2e"]["steps"]
    record = next(s for s in steps if s.get("name") == "Record the tested tree")
    upload = next(s for s in steps if s.get("name") == "Upload the tested tree")
    for step in (record, upload):
        assert step["if"] == "success() && github.event_name == 'pull_request'"
    assert "HEAD^{tree}" in record["run"] and "tested-tree.txt" in record["run"]
    assert upload["with"]["name"] == ARTIFACT
    assert upload["with"]["path"] == "tested-tree.txt"
    # The record must come after the suite, or a failing suite would leave one.
    names = [s.get("name") for s in steps]
    assert names.index("Run E2E tests") < names.index("Record the tested tree")
