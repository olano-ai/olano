"""`git_*` — clone/fetch/pull/push as a connected job, never in the agent's sandbox.

Every test fakes `run_connected_job` and pins what reaches it: the argv shape
(the `-c` neutralisation set, the host-scoped credential helper *after* the
`credential.helper=` clear, `--` before positionals, never `--force`, never
`--recurse-submodules`), the https-only URL policy, workspace containment, the
`.git/config` scan, and the `[missing-credentials]` message that names the
magic link instead of asking for a token.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.tools.git_tools as gt
from olano.tools._sandbox_exec import ExecResult

TOKEN = "ghp_live_token_value_123"
_HTTPS_ONLY = "Olano authenticates https remotes only"


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(gt, "_workspace_root", lambda: root)
    monkeypatch.setattr(gt, "_agent_name", lambda: "alpha")
    return root


@pytest.fixture
def creds(monkeypatch):
    store: dict[str, str] = {"GITHUB_TOKEN": TOKEN}
    monkeypatch.setattr(gt, "get_credential", lambda name, default="": store.get(name, default))
    return store


@pytest.fixture
def job(monkeypatch):
    """Record `run_connected_job` kwargs; return queued results (default: ok)."""
    calls: list[dict] = []
    queue: list[ExecResult] = []

    def fake(argv, **kw):
        calls.append({"argv": list(argv), **kw})
        return queue.pop(0) if queue else ExecResult("done", "", 0)

    monkeypatch.setattr(gt, "run_connected_job", fake)
    return {"calls": calls, "queue": queue}


def _repo(ws: Path, name: str = "repo", config: str | None = None) -> Path:
    repo = ws / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        config
        if config is not None
        else '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n'
        "\turl = https://github.com/acme/repo.git\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        encoding="utf-8",
    )
    return repo


def _overrides(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]


# ── argv shape ─────────────────────────────────────────────────────────────


def test_clone_argv_neutralises_repo_config_and_scopes_the_helper(ws, creds, job):
    out = gt.git_clone.invoke(
        {"url": "https://github.com/acme/repo.git", "branch": "main", "depth": 1}
    )
    assert out.startswith(f"Cloned https://github.com/acme/repo.git into {ws / 'repo'}")
    (call,) = job["calls"]
    argv = call["argv"]
    assert argv[0] == "git"
    overrides = _overrides(argv)
    # The clear precedes the host-scoped helper: on git 2.43 GIT_CONFIG_* is
    # applied BEFORE -c, so both travel as -c pairs, in this order.
    assert overrides.index("credential.helper=") < overrides.index(
        f"credential.https://github.com.helper={gt._CREDENTIAL_HELPER}"
    )
    for expected in (
        "core.hooksPath=/nonexistent",
        "core.sshCommand=false",
        "core.gitProxy=",
        "core.fsmonitor=false",
        "core.askPass=",
        "http.sslVerify=true",
        "protocol.allow=never",
        "protocol.https.allow=always",
        "submodule.recurse=false",
        "fetch.recurseSubmodules=false",
    ):
        assert expected in overrides
    # Not expressible as an override without breaking legitimate boxes (an
    # empty http.proxy disables the env proxy; an empty sslCAInfo disables the
    # CA bundle) — those are covered by the .git/config scan instead.
    assert not any(o.startswith("http.proxy") or o.startswith("http.sslCAInfo") for o in overrides)
    assert argv[argv.index("clone") :] == [
        "clone",
        "--branch",
        "main",
        "--depth",
        "1",
        "--",
        "https://github.com/acme/repo.git",
        str(ws / "repo"),
    ]
    # The helper text names the env var, never the token; the token rides in
    # secret_env only; GIT_CONFIG_* is not used at all.
    assert TOKEN not in " ".join(argv)
    assert "$GIT_JOB_TOKEN" in " ".join(argv)
    assert call["secret_env"] == {"GIT_JOB_TOKEN": TOKEN}
    assert call["extra_env"] == {"GIT_TERMINAL_PROMPT": "0"}
    assert not any(k.startswith("GIT_CONFIG") for k in call["extra_env"])
    assert call["label"] == "git" and call["agent"] == "alpha"
    assert call["workspace_root"] == ws
    assert "--force" not in argv and "--recurse-submodules" not in argv


def test_clone_default_dest_is_repo_name_inside_workspace(ws, creds, job):
    gt.git_clone.invoke({"url": "https://github.com/acme/Some-Repo"})
    assert job["calls"][0]["argv"][-1] == str(ws / "Some-Repo")
    gt.git_clone.invoke({"url": "https://github.com/acme/other.git", "dest": "vendor/other"})
    assert job["calls"][1]["argv"][-1] == str(ws / "vendor" / "other")
    # An absolute path INSIDE the workspace is what the model sees in its prompt.
    gt.git_clone.invoke({"url": "https://github.com/acme/abs.git", "dest": str(ws / "abs")})
    assert job["calls"][2]["argv"][-1] == str(ws / "abs")


@pytest.mark.parametrize(
    "dest",
    ["../escape", "/tmp/elsewhere", "a/../../escape", ""],
    ids=["dotdot", "absolute-outside", "nested-dotdot", "workspace-itself"],
)
def test_clone_dest_must_stay_inside_the_workspace(ws, creds, job, dest, monkeypatch):
    if dest == "":
        dest = str(ws)  # the workspace root itself is not a clone target
    out = gt.git_clone.invoke({"url": "https://github.com/acme/repo.git", "dest": dest})
    assert out.startswith("[refused]")
    assert job["calls"] == []


def test_clone_refuses_a_non_empty_dest(ws, creds, job):
    (ws / "repo").mkdir()
    (ws / "repo" / "x").write_text("x")
    out = gt.git_clone.invoke({"url": "https://github.com/acme/repo.git"})
    assert "already exists and is not empty" in out
    assert job["calls"] == []


# ── URL policy ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/repo.git",
        "ssh://git@github.com/acme/repo.git",
        "http://github.com/acme/repo.git",
        "git://github.com/acme/repo.git",
        "file:///etc/passwd",
        "acme/repo",
        "https://x-access-token:tok@github.com/acme/repo.git",
        "https://github.com/acme/re po.git",
    ],
)
def test_only_plain_https_urls_are_accepted(ws, creds, job, url):
    out = gt.git_clone.invoke({"url": url})
    assert out.startswith("[refused]")
    if "@" in url and not url.startswith("https://"):
        assert _HTTPS_ONLY in out
    assert job["calls"] == []


def test_ssh_remote_in_git_config_is_refused_with_the_https_message(ws, creds, job):
    _repo(ws, config='[remote "origin"]\n\turl = git@github.com:acme/repo.git\n')
    out = gt.git_fetch.invoke({"repo_path": "repo"})
    assert out.startswith("[refused]") and _HTTPS_ONLY in out
    assert job["calls"] == []


# ── credentials ────────────────────────────────────────────────────────────


def test_missing_github_credential_names_the_magic_link_never_asks_for_a_token(ws, creds, job):
    creds.clear()
    _repo(ws)
    out = gt.git_push.invoke({"repo_path": "repo", "branch": "main"})
    assert out.startswith("[missing-credentials] GitHub is not connected for this agent")
    assert 'magic_link_connect_service("github")' in out
    assert "never ask for a token in chat" in out
    assert job["calls"] == []  # a push always needs auth: nothing runs


def test_public_clone_without_credential_runs_unauthenticated(ws, creds, job):
    creds.clear()
    out = gt.git_clone.invoke({"url": "https://github.com/acme/public.git"})
    assert out.startswith("Cloned")
    (call,) = job["calls"]
    assert call["secret_env"] == {}
    assert not any("credential.https://" in o for o in _overrides(call["argv"]))
    assert "credential.helper=" in _overrides(call["argv"])  # still cleared


def test_auth_failure_without_credential_becomes_the_missing_credentials_message(ws, creds, job):
    creds.clear()
    job["queue"].append(
        ExecResult(
            "",
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            128,
        )
    )
    out = gt.git_clone.invoke({"url": "https://github.com/acme/private.git"})
    assert out.startswith("[missing-credentials]")
    assert 'magic_link_connect_service("github")' in out
    assert "terminal prompts disabled" in out


def test_auth_failure_with_credential_points_at_reauthorizing(ws, creds, job):
    job["queue"].append(ExecResult("", "remote: Repository not found.", 128))
    out = gt.git_clone.invoke({"url": "https://github.com/acme/private.git"})
    assert out.startswith("[tool-error] git: the connected GitHub account was refused")
    assert 'magic_link_connect_service("github")' in out


def test_github_pat_is_the_fallback_credential_name(ws, creds, job):
    creds.clear()
    creds["GITHUB_PAT"] = "pat-value"
    gt.git_clone.invoke({"url": "https://github.com/acme/repo.git"})
    assert job["calls"][0]["secret_env"] == {"GIT_JOB_TOKEN": "pat-value"}


def test_token_is_never_offered_to_another_host(ws, creds, job):
    """A gitlab remote gets no github token — and no helper at all; the job
    runs unauthenticated (public repos still work)."""
    _repo(ws, config='[remote "origin"]\n\turl = https://gitlab.com/acme/repo.git\n')
    out = gt.git_fetch.invoke({"repo_path": "repo"})
    assert out.startswith("Fetched origin")
    (call,) = job["calls"]
    assert call["secret_env"] == {}
    assert not any("credential.https://" in o for o in _overrides(call["argv"]))


def test_push_target_is_pushurl_when_set(ws, creds, job):
    _repo(
        ws,
        config='[remote "origin"]\n\turl = https://github.com/acme/repo.git\n'
        "\tpushurl = https://gitlab.com/acme/mirror.git\n",
    )
    gt.git_push.invoke({"repo_path": "repo"})
    (call,) = job["calls"]
    assert call["secret_env"] == {}  # pushes go to gitlab: the github token stays home


# ── fetch / pull / push argv ──────────────────────────────────────────────


def test_fetch_pull_push_argv(ws, creds, job):
    repo = _repo(ws)
    gt.git_fetch.invoke({"repo_path": "repo"})
    gt.git_pull.invoke({"repo_path": str(repo), "branch": "main"})
    gt.git_push.invoke({"repo_path": "repo", "branch": "feature/x", "set_upstream": True})
    fetch, pull, push = (c["argv"] for c in job["calls"])
    assert fetch[fetch.index("-C") :] == ["-C", str(repo), "fetch", "--", "origin"]
    assert pull[pull.index("-C") :] == [
        "-C",
        str(repo),
        "pull",
        "--ff-only",
        "--",
        "origin",
        "main",
    ]
    assert push[push.index("-C") :] == [
        "-C",
        str(repo),
        "push",
        "--set-upstream",
        "--",
        "origin",
        "feature/x",
    ]
    for argv in (fetch, pull, push):
        assert "--force" not in argv and "-f" not in argv
        assert "--recurse-submodules" not in argv
        assert f"credential.https://github.com.helper={gt._CREDENTIAL_HELPER}" in _overrides(argv)
    assert all(c["secret_env"] == {"GIT_JOB_TOKEN": TOKEN} for c in job["calls"])


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (gt.git_fetch, {"repo_path": "repo", "remote": "--upload-pack=touch /tmp/x"}),
        (gt.git_push, {"repo_path": "repo", "remote": "--receive-pack=sh"}),
        (gt.git_push, {"repo_path": "repo", "branch": "-f"}),
        (gt.git_pull, {"repo_path": "repo", "branch": "--rebase"}),
        (gt.git_clone, {"url": "https://github.com/acme/repo.git", "branch": "--upload-pack=x"}),
    ],
)
def test_option_shaped_names_are_refused(ws, creds, job, tool, args):
    _repo(ws)
    out = tool.invoke(args)
    assert out.startswith("[refused]")
    assert job["calls"] == []


# ── repo containment + .git/config scan ────────────────────────────────────


def test_repo_path_must_be_a_git_repo_inside_the_workspace(ws, creds, job, tmp_path):
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    assert gt.git_fetch.invoke({"repo_path": str(outside)}).startswith("[refused]")
    assert gt.git_fetch.invoke({"repo_path": "../outside"}).startswith("[refused]")
    (ws / "plain").mkdir()
    out = gt.git_fetch.invoke({"repo_path": "plain"})
    assert "not a git repository" in out
    (ws / "linked").mkdir()
    (ws / "linked" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    assert "not a git repository" in gt.git_fetch.invoke({"repo_path": "linked"})
    assert job["calls"] == []


def test_remote_without_url_is_refused(ws, creds, job):
    _repo(ws, config="[core]\n\tbare = false\n")
    out = gt.git_fetch.invoke({"repo_path": "repo", "remote": "upstream"})
    assert "has no url" in out
    assert job["calls"] == []


@pytest.mark.parametrize(
    ("snippet", "shown"),
    [
        (
            '[url "https://evil.example/"]\n\tinsteadOf = https://github.com/\n',
            "url.https://evil.example/.insteadof",
        ),
        ("[http]\n\tproxy = http://evil.example:8080\n", "http.proxy"),
        (
            '[http "https://github.com"]\n\tsslCAInfo = /tmp/ca.pem\n',
            "http.https://github.com.sslcainfo",
        ),
        ("[include]\n\tpath = /tmp/extra.gitconfig\n", "include.path"),
        ('[includeIf "gitdir:/"]\n\tpath = ~/x\n', "includeif.gitdir:/.path"),
        (
            '[credential "https://github.com"]\n\thelper = !curl evil\n',
            "credential.https://github.com.helper",
        ),
        ('[filter "lfs"]\n\tsmudge = git-lfs smudge -- %f\n', "filter.lfs.smudge"),
        ("[core]\n\tworktree = /etc\n", "core.worktree"),
        ("[extensions]\n\tworktreeConfig = true\n", "extensions.worktreeconfig"),
        ('[remote "origin"]\n\tproxy = http://evil.example\n', "remote.origin.proxy"),
        ('[remote "origin"]\n\tvcs = ext\n', "remote.origin.vcs"),
    ],
)
def test_git_config_that_could_redirect_the_credential_is_refused(ws, creds, job, snippet, shown):
    _repo(ws, config='[remote "origin"]\n\turl = https://github.com/acme/repo.git\n' + snippet)
    out = gt.git_pull.invoke({"repo_path": "repo"})
    assert out.startswith("[refused]") and f"`{shown}`" in out
    assert job["calls"] == []


def test_config_keys_neutralised_by_overrides_do_not_refuse(ws, creds, job):
    """husky-style `core.hooksPath` is common and harmless here: the `-c`
    override wins over .git/config, so the job proceeds."""
    _repo(
        ws,
        config='[remote "origin"]\n\turl = https://github.com/acme/repo.git\n'
        "[core]\n\thooksPath = .husky\n\tsshCommand = ssh -i key\n"
        "[user]\n\tname = Agent\n\temail = a@b.c\n[pull]\n\trebase = false\n",
    )
    assert gt.git_fetch.invoke({"repo_path": "repo"}).startswith("Fetched")
    assert len(job["calls"]) == 1


def test_git_config_symlinked_outside_the_workspace_is_refused(ws, creds, job, tmp_path):
    repo = _repo(ws)
    outside = tmp_path / "outside.gitconfig"
    outside.write_text('[remote "origin"]\n\turl = https://github.com/acme/repo.git\n')
    (repo / ".git" / "config").unlink()
    (repo / ".git" / "config").symlink_to(outside)
    out = gt.git_fetch.invoke({"repo_path": "repo"})
    assert out.startswith("[refused]") and "inside the workspace" in out
    assert job["calls"] == []


def test_parse_git_config_handles_quotes_comments_and_dotted_headers():
    entries = gt._parse_git_config(
        "# comment\n; another\n[remote.origin]\n\turl = https://h/x  \n"
        '[branch "main"]\n\tremote = "origin"\n\tmerge = refs/heads/main\n'
        "[core]\n\tbare\n"
    )
    assert ("remote", "origin", "url", "https://h/x") in entries
    assert ("branch", "main", "remote", "origin") in entries
    assert ("core", "", "bare", "") in entries


# ── output handling ────────────────────────────────────────────────────────


def test_url_userinfo_residue_is_scrubbed_from_output(ws, creds, job):
    job["queue"].append(
        ExecResult(
            "",
            f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/a/b/': 403",
            128,
        )
    )
    out = gt.git_clone.invoke({"url": "https://github.com/acme/repo.git"})
    assert TOKEN not in out
    assert "https://[redacted]@github.com/a/b/" in out


def test_blocked_and_timeout_results_pass_through(ws, creds, job):
    job["queue"].append(ExecResult("", "[job blocked] sandboxed execution is not enabled", 126))
    assert (
        gt.git_clone.invoke({"url": "https://github.com/acme/repo.git"})
        == "[job blocked] sandboxed execution is not enabled"
    )
    job["queue"].append(
        ExecResult("partial", "[job timeout] git exceeded 600s and was stopped", 124)
    )
    out = gt.git_clone.invoke({"url": "https://github.com/acme/two.git"})
    assert out.startswith("[tool-error] git timed out after 600s.")
    assert "exceeded 600s" in out


def test_generic_failure_reports_exit_code_and_output(ws, creds, job):
    job["queue"].append(ExecResult("", "error: failed to push some refs", 1))
    _repo(ws)
    out = gt.git_push.invoke({"repo_path": "repo"})
    assert out.startswith("[tool-error] git exited 1:")
    assert "failed to push some refs" in out


# ── registration / metadata / surfaces ─────────────────────────────────────


def test_toolkit_registration_and_metadata():
    from olano.security.hitl_policy import CATEGORY_TOOLS
    from olano.tools.registry import _TOOLKITS
    from olano.tools.requirements import BUNDLE_REQUIREMENTS

    assert _TOOLKITS["git"] == "olano.tools.git_tools"
    assert {t.name for t in gt.get_tools()} == {"git_clone", "git_fetch", "git_pull", "git_push"}
    assert gt.get_tool("git_push") is gt.git_push

    push = gt.git_push.lionclaw_tool_metadata
    assert (push.capability, push.risk, push.requires_hitl) == ("write_external", "high", True)
    for name in ("git_clone", "git_fetch", "git_pull"):
        meta = gt.get_tool(name).lionclaw_tool_metadata
        assert (meta.capability, meta.risk, meta.requires_hitl) == ("mutating", "medium", False)
    assert "git_push" in CATEGORY_TOOLS["write_external"]
    assert "git_clone" not in CATEGORY_TOOLS["write_external"]

    (spec,) = BUNDLE_REQUIREMENTS["git"]["env_vars"]
    assert spec["name"] == "GITHUB_TOKEN" and spec["optional"] is True
    assert "public repos need nothing" in spec["guidance"]
    assert "cli" not in BUNDLE_REQUIREMENTS["git"]  # git runs in the job image, not the host


def test_docstrings_send_local_git_to_the_agents_own_sandbox():
    for t in gt.get_tools():
        assert "execute" in t.description
        assert "sandbox" in t.description


def test_progress_describes_git_verbs():
    from olano.platforms.progress import describe_tool

    d = describe_tool("git_clone", {"url": "https://github.com/acme/repo.git"})
    assert (d.icon, d.verb, d.detail) == ("🐙", "Cloning", "https://github.com/acme/repo.git")
    assert describe_tool("git_push", {"repo_path": "/w/repo"}).verb == "Pushing"
    assert describe_tool("git_pull", {"repo_path": "/w/repo"}).verb == "Pulling"
    f = describe_tool("git_fetch", {"repo_path": "/w/repo", "remote": "origin"})
    assert (f.icon, f.verb, f.detail) == ("🐙", "Fetching", "/w/repo")
