"""The release scheme in docs/RELEASING.md, pinned.

`:latest` is a release and never `main`; `:edge` is `main` after green checks;
a release promotes the `:<sha>` image rather than rebuilding it; a dispatched
release creates its tag only after the gate; PyPI is opt-in.
Each of those is one line in a workflow that a well-meaning edit could undo
without any run going red, so the shape of the workflows is asserted here, and
the CI gate script is exercised against saved API shapes for every verdict.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
GATE = ROOT / ".github" / "scripts" / "checks-green.sh"
BUILD_ACTION = ROOT / ".github" / "actions" / "build-images" / "action.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # PyYAML reads an unquoted `on:` key as the boolean True.
    return doc.get("on") or doc.get(True) or {}


def _steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def _step(doc: dict, job: str, name: str) -> dict:
    for step in _steps(doc, job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{job!r} has no step named {name!r}")


# ── build-images.yml: edge only ──────────────────────────────────────────────


def test_edge_pipeline_never_runs_on_a_tag_and_never_writes_latest() -> None:
    doc = _load(WORKFLOWS / "build-images.yml")
    triggers = _triggers(doc)
    assert set(triggers) == {"schedule", "workflow_dispatch"}, triggers
    tags_step = _step(doc, "build", "Tags for this build")
    assert "latest" not in tags_step["run"], "build-images.yml must never push :latest"
    assert 'suffixes+=("edge")' in tags_step["run"]
    # :edge follows main only; a dispatch on another ref publishes :<sha> alone.
    assert 'if [ "$PUSH_EDGE" = "true" ]' in tags_step["run"]


def test_edge_pipeline_is_gated_on_green_checks() -> None:
    doc = _load(WORKFLOWS / "build-images.yml")
    gate = _step(doc, "check", "Are HEAD's checks green?")
    assert gate["run"].strip().startswith(".github/scripts/checks-green.sh")
    decide = _step(doc, "check", "Decide")["run"]
    assert '"$VERDICT" = "green"' in decide
    assert "actions" in doc["permissions"] and doc["permissions"]["actions"] == "read"


def _e2e_timeout_minutes() -> int:
    # The gates wait for the longest check, which is e2e.yml's job; read its
    # timeout rather than repeating the number here, so raising one without
    # the other fails this file instead of quietly shortening the wait.
    return int(_load(WORKFLOWS / "e2e.yml")["jobs"]["e2e"]["timeout-minutes"])


E2E_TIMEOUT_MINUTES = _e2e_timeout_minutes()


def _wait_seconds_in(expression: str) -> int:
    # `${{ cond && '2700' || '0' }}`: the wait the expression grants when cond holds.
    quoted = [int(v) for v in re.findall(r"'(\d+)'", expression)]
    assert quoted, expression
    return max(quoted)


def test_edge_dispatch_waits_for_running_checks_and_the_schedule_does_not() -> None:
    # Pressed within the ~45 minutes a commit's checks take, a dispatch used to
    # fail at once with "dispatch again with force=true", i.e. publish untested.
    doc = _load(WORKFLOWS / "build-images.yml")
    check = doc["jobs"]["check"]
    wait = check["env"]["CHECKS_WAIT_SECONDS"]
    assert "github.event_name == 'workflow_dispatch'" in wait
    assert "inputs.force != true" in wait, "a forced dispatch must not wait"
    assert wait.rstrip("} ").endswith("|| '0'"), "the schedule never waits"
    seconds = _wait_seconds_in(wait)
    assert seconds >= E2E_TIMEOUT_MINUTES * 60, "shorter than e2e.yml's job timeout"
    assert check["timeout-minutes"] * 60 > seconds, "the job would time out mid-wait"
    decide = _step(doc, "check", "Decide")["run"]
    dispatch, _, schedule = decide.partition('if [ "$PUBLISHED" = "true" ]')
    assert '"$VERDICT" = "pending"' in dispatch and "still running after" in dispatch
    assert "the next scheduled window will pick it up" in schedule
    assert "should_build=false" in schedule


def test_release_gate_waits_for_running_checks() -> None:
    doc = _load(WORKFLOWS / "release.yml")
    gate = _step(doc, "verify", "Checks are green for the tagged commit")
    seconds = int(gate["env"]["CHECKS_WAIT_SECONDS"])
    assert seconds >= E2E_TIMEOUT_MINUTES * 60
    assert doc["jobs"]["verify"]["timeout-minutes"] * 60 > seconds
    assert '"$verdict" = "pending"' in gate["run"]


def test_edge_pipeline_and_release_share_one_build_definition() -> None:
    assert BUILD_ACTION.exists()
    build = _load(WORKFLOWS / "build-images.yml")
    release = _load(WORKFLOWS / "release.yml")
    assert any(s.get("uses") == "./.github/actions/build-images" for s in _steps(build, "build"))
    assert any(s.get("uses") == "./.github/actions/build-images" for s in _steps(release, "images"))
    action = _load(BUILD_ACTION)
    for key in ("backend_tags", "dashboard_tags", "sandbox_tags", "browser_tags", "sha"):
        assert action["inputs"][key]["required"] is True


# ── release.yml: promote, then publish ───────────────────────────────────────


def test_release_runs_on_a_version_tag_or_a_dispatch_that_creates_one() -> None:
    doc = _load(WORKFLOWS / "release.yml")
    triggers = _triggers(doc)
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"]}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"version"}
    assert inputs["version"]["required"] is True
    # The dispatch path mints the tag itself, only after the gate has passed,
    # and only from main or a release branch.
    names = [s.get("name") for s in _steps(doc, "verify")]
    gate = names.index("Checks are green for the tagged commit")
    mint = names.index("Create the release tag (dispatch only)")
    assert gate < mint
    assert _steps(doc, "verify")[mint]["if"] == "github.event_name == 'workflow_dispatch'"
    resolve = _step(doc, "verify", "Resolve the version and the commit")["run"]
    assert "refs/heads/main|refs/heads/release/*" in resolve


def test_release_jobs_build_the_verified_commit_not_the_branch_tip() -> None:
    # A dispatch runs on a branch, and main can move between jobs: every job
    # after verify checks out the commit verify resolved, and the GitHub
    # Release names the tag explicitly, since github.ref is the branch there.
    doc = _load(WORKFLOWS / "release.yml")
    for job in ("package", "images", "github-release"):
        checkout = next(
            s for s in _steps(doc, job) if str(s.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ needs.verify.outputs.sha }}", job
    gh_release = _step(doc, "github-release", "Create GitHub Release")["with"]
    assert gh_release["tag_name"] == "${{ needs.verify.outputs.tag }}"


def test_release_promotes_the_sha_image_instead_of_rebuilding() -> None:
    doc = _load(WORKFLOWS / "release.yml")
    promote = _step(doc, "images", "Promote :<sha> to the release tags")["run"]
    assert "docker buildx imagetools create" in promote
    assert '"${image}:${SHA}"' in promote
    # Build only when :<sha> is missing, never unconditionally.
    fallback = next(
        s for s in _steps(doc, "images") if s.get("uses") == "./.github/actions/build-images"
    )
    assert fallback["if"] == "steps.have.outputs.need_build == 'true'"
    # A pre-release moves neither :latest nor :X.Y.
    assert '"$PRERELEASE" != "true"' in promote
    assert "latest" in promote


def test_release_is_gated_and_checks_the_declared_version() -> None:
    doc = _load(WORKFLOWS / "release.yml")
    gate = _step(doc, "verify", "Checks are green for the tagged commit")["run"]
    assert "checks-green.sh" in gate and '"$verdict" != "green"' in gate
    version = _step(doc, "verify", "pyproject version matches the tag")["run"]
    assert "tomllib" in version and "declared != tagged" in version


def test_the_package_reports_the_version_pyproject_declares() -> None:
    """``olano.__version__`` is what a box reports as its package version
    (``deployment/version.py``); the release gate checks only
    ``pyproject.toml``, so a bump that misses the constant ships a release
    whose diagnostics name the previous one."""
    import tomllib

    import olano

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert olano.__version__ == declared


def test_pypi_publish_is_opt_in_and_never_for_prereleases() -> None:
    doc = _load(WORKFLOWS / "release.yml")
    cond = doc["jobs"]["publish-pypi"]["if"]
    assert "vars.PUBLISH_TO_PYPI == 'true'" in cond
    assert "prerelease != 'true'" in cond
    gh_release = _step(doc, "github-release", "Create GitHub Release")["with"]
    assert "prerelease" in gh_release


def test_release_publishes_the_docs_for_the_verified_commit_after_the_images() -> None:
    # docs.olano.ai/X.Y comes from the release's commit (a dispatch runs on a
    # branch that can move), only once the images exist, never for a
    # pre-release; and the Docs workflow itself never runs on a tag push, so a
    # dispatched release (whose tag starts no run) and a pushed tag publish
    # the docs exactly once each, through this job.
    release = _load(WORKFLOWS / "release.yml")
    docs = release["jobs"]["docs"]
    assert docs["uses"] == "./.github/workflows/docs.yml"
    assert set(docs["needs"]) == {"verify", "images"}
    assert docs["with"]["version"] == "${{ needs.verify.outputs.minor }}"
    assert docs["with"]["ref"] == "${{ needs.verify.outputs.sha }}"
    assert "prerelease != 'true'" in docs["if"]
    triggers = _triggers(_load(WORKFLOWS / "docs.yml"))
    assert set(triggers) == {"push", "workflow_dispatch", "workflow_call"}
    assert "tags" not in triggers["push"]
    assert triggers["push"]["branches"] == ["main"]


def test_ci_covers_release_branches() -> None:
    for name in ("ci.yml", "e2e.yml"):
        branches = _triggers(_load(WORKFLOWS / name))["push"]["branches"]
        assert "release/**" in branches, f"{name}: hotfix branches would skip the gate"


# ── checks-green.sh: every verdict, offline ──────────────────────────────────


def _run(
    name: str,
    status: str,
    conclusion: str | None,
    path: str = ".github/workflows/ci.yml",
    event: str = "push",
) -> dict:
    return {"name": name, "event": event, "status": status, "conclusion": conclusion, "path": path}


def _verdict(tmp_path: Path, runs: list[dict]) -> tuple[str, str]:
    fixture = tmp_path / "runs.json"
    fixture.write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")
    env = {
        **os.environ,
        "CHECKS_RUNS_FILE": str(fixture),
        "GITHUB_REPOSITORY": "olano-ai/olano",
    }
    env.pop("GITHUB_OUTPUT", None)
    proc = subprocess.run(
        ["bash", str(GATE), "0123456789abcdef0123456789abcdef01234567"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert last.startswith("verdict=")
    return last.split("=", 1)[1], proc.stdout


def test_gate_script_is_executable() -> None:
    assert GATE.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([_run("CI", "completed", "success"), _run("E2E", "completed", "success")], "green"),
        ([_run("CI", "completed", "success"), _run("E2E", "completed", "skipped")], "green"),
        ([], "green"),
        ([_run("CI", "in_progress", None)], "pending"),
        ([_run("CI", "completed", "success"), _run("E2E", "queued", None)], "pending"),
        ([_run("CI", "completed", "failure")], "red"),
        ([_run("CI", "completed", "cancelled"), _run("E2E", "in_progress", None)], "red"),
        ([_run("CI", "completed", "timed_out")], "red"),
    ],
)
def test_gate_verdicts(tmp_path: Path, runs: list[dict], expected: str) -> None:
    verdict, _ = _verdict(tmp_path, runs)
    assert verdict == expected


def test_gate_ignores_the_publishing_workflows_themselves(tmp_path: Path) -> None:
    runs = [
        _run("CI", "completed", "success"),
        _run(
            "Build runtime images (GHCR)", "in_progress", None, ".github/workflows/build-images.yml"
        ),
        _run("Release", "completed", "failure", ".github/workflows/release.yml"),
        # Dependabot's updater runs are filed against main's tip and fail whenever
        # an update cannot be resolved (seen live on 792cbc1b); not a check.
        _run(
            "uv in / for click, fastapi - Update #1608125355",
            "completed",
            "failure",
            "dynamic/dependabot/dependabot-updates",
            event="dynamic",
        ),
        _run(
            "Nightly sweep", "completed", "failure", ".github/workflows/sweep.yml", event="schedule"
        ),
    ]
    verdict, out = _verdict(tmp_path, runs)
    assert verdict == "green", out
    assert "Release" not in out.replace("verdict=green", "")


def _run_gate(env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["bash", str(GATE), "0123456789abcdef0123456789abcdef01234567"],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _last_verdict(proc: subprocess.CompletedProcess[str]) -> str:
    last = proc.stdout.strip().splitlines()[-1]
    assert last.startswith("verdict="), proc.stdout
    return last.split("=", 1)[1]


def _gh_env(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    # The script finds `gh` on PATH; the test puts its own first.
    env = {k: v for k, v in os.environ.items() if k not in {"GITHUB_OUTPUT", "CHECKS_RUNS_FILE"}}
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["GITHUB_REPOSITORY"] = "olano-ai/olano"
    env.update(extra)
    return env


def test_gate_treats_an_unreadable_api_as_pending(tmp_path: Path) -> None:
    # No fixture and a `gh` that cannot answer: the script must not pretend to know.
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n", encoding="utf-8"
    )
    fake_gh.chmod(0o755)
    proc = _run_gate(_gh_env(tmp_path, {}), timeout=30)
    assert _last_verdict(proc) == "pending"


# ── checks-green.sh: waiting for a pending verdict ───────────────────────────
#
# A dispatched edge build or release pressed while the commit's checks are
# still running used to fail at once; with CHECKS_WAIT_SECONDS the gate keeps
# asking and answers with the first settled verdict.


def _fixture(tmp_path: Path, name: str, runs: list[dict]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")
    return path


def test_gate_waits_for_a_pending_verdict_to_settle(tmp_path: Path) -> None:
    # Ask 1: the API cannot be read (pending, asked again). Ask 2: a run is
    # still in progress. Ask 3: everything finished green.
    pending = _fixture(tmp_path, "pending", [_run("CI", "in_progress", None)])
    green = _fixture(
        tmp_path, "green", [_run("CI", "completed", "success"), _run("E2E", "completed", "success")]
    )
    counter = tmp_path / "asks"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f"n=$(cat '{counter}' 2>/dev/null || echo 0); n=$((n + 1)); echo $n > '{counter}'\n"
        "case $n in\n"
        "  1) echo 'HTTP 502: Bad Gateway' >&2; exit 1 ;;\n"
        f"  2) cat '{pending}' ;;\n"
        f"  *) cat '{green}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = _gh_env(tmp_path, {"CHECKS_WAIT_SECONDS": "30", "CHECKS_POLL_SECONDS": "1"})
    proc = _run_gate(env)
    assert _last_verdict(proc) == "green"
    assert counter.read_text().strip() == "3", proc.stdout
    assert proc.stdout.count("asking again in") == 2, proc.stdout


def test_gate_gives_up_waiting_at_the_deadline_and_still_answers(tmp_path: Path) -> None:
    pending = _fixture(tmp_path, "pending", [_run("E2E", "queued", None)])
    env = {
        **os.environ,
        "CHECKS_RUNS_FILE": str(pending),
        "GITHUB_REPOSITORY": "olano-ai/olano",
        "CHECKS_WAIT_SECONDS": "2",
        "CHECKS_POLL_SECONDS": "1",
    }
    env.pop("GITHUB_OUTPUT", None)
    started = time.monotonic()
    proc = _run_gate(env)
    assert time.monotonic() - started >= 2
    assert _last_verdict(proc) == "pending"
    assert "giving up" in proc.stdout


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([_run("CI", "completed", "failure"), _run("E2E", "in_progress", None)], "red"),
        ([_run("CI", "completed", "success")], "green"),
        ([], "green"),
    ],
)
def test_gate_never_waits_on_a_settled_verdict(
    tmp_path: Path, runs: list[dict], expected: str
) -> None:
    # Red is red even while another run is still going: waiting for it would
    # only delay the refusal, and a settled verdict costs no runner minutes.
    env = {
        **os.environ,
        "CHECKS_RUNS_FILE": str(_fixture(tmp_path, "runs", runs)),
        "GITHUB_REPOSITORY": "olano-ai/olano",
        "CHECKS_WAIT_SECONDS": "600",
        "CHECKS_POLL_SECONDS": "300",
    }
    env.pop("GITHUB_OUTPUT", None)
    started = time.monotonic()
    proc = _run_gate(env, timeout=30)
    assert time.monotonic() - started < 10
    assert _last_verdict(proc) == expected
    assert "asking again" not in proc.stdout


def test_gate_reads_a_bad_wait_as_no_wait(tmp_path: Path) -> None:
    # A typo in the workflow must degrade to the old one-ask gate, not to a
    # bash arithmetic error that reads as the gate itself being broken.
    pending = _fixture(tmp_path, "pending", [_run("CI", "in_progress", None)])
    env = {
        **os.environ,
        "CHECKS_RUNS_FILE": str(pending),
        "GITHUB_REPOSITORY": "olano-ai/olano",
        "CHECKS_WAIT_SECONDS": "soon",
        "CHECKS_POLL_SECONDS": "1",
    }
    env.pop("GITHUB_OUTPUT", None)
    proc = _run_gate(env, timeout=30)
    assert _last_verdict(proc) == "pending"
    assert "asking again" not in proc.stdout
    assert "CHECKS_WAIT_SECONDS=soon is not a number" in proc.stderr
