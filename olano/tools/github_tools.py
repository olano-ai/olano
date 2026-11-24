"""GitHub tools for the GitHubAgent subagent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_GH_CREDS_MSG = (
    "[missing-credentials] GITHUB_TOKEN or GITHUB_PAT not set in vault. "
    "Connect GitHub in the dashboard Connections tab, or store a personal "
    "access token in the vault."
)


def _gh_client() -> tuple[Any, str | None]:
    """Return (Github, error_string). One is always None."""
    gh_mod, err = optional_import("github", purpose="GitHub API access")
    if err:
        return None, err
    token = get_credential("GITHUB_TOKEN") or get_credential("GITHUB_PAT")
    if not token:
        return None, _GH_CREDS_MSG
    assert gh_mod is not None  # narrowed: err is None ⇒ import succeeded
    try:
        return gh_mod.Github(token), None
    except Exception as exc:
        return None, f"[tool-error] GitHub auth: {exc}"


@tool
def github_list_repos(owner: str = "") -> str:
    """List GitHub repositories for the authenticated user or a given owner."""
    g, err = _gh_client()
    if err:
        return err
    try:
        if owner:
            user = g.get_user(owner)
            repos = user.get_repos()
        else:
            repos = g.get_user().get_repos()
        lines = [
            f"{getattr(r, 'full_name', '?')} — {getattr(r, 'description', None) or '(no description)'}"
            for r in list(repos)[:20]
        ]
        return "\n".join(lines) or "No repositories found."
    except Exception as exc:
        return f"[tool-error] github_list_repos: {exc}"


@tool
def github_create_issue(repo: str, title: str, body: str = "", labels: str = "") -> str:
    """Create a GitHub issue. repo='owner/name', labels='bug,enhancement' (comma-separated)."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        label_list = [lbl.strip() for lbl in labels.split(",") if lbl.strip()]
        issue = r.create_issue(title=title, body=body, labels=label_list)
        return f"Created issue #{getattr(issue, 'number', '?')}: {getattr(issue, 'html_url', '?')}"
    except Exception as exc:
        return f"[tool-error] github_create_issue: {exc}"


@tool
def github_list_issues(repo: str, state: str = "open", limit: int = 10) -> str:
    """List issues in a repository. state='open'|'closed'|'all'."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        issues = list(r.get_issues(state=state))[:limit]
        lines = [
            f"#{getattr(i, 'number', '?')} [{getattr(i, 'state', '?')}] "
            f"{getattr(i, 'title', '?')} — {getattr(i, 'html_url', '?')}"
            for i in issues
        ]
        return "\n".join(lines) or "No issues found."
    except Exception as exc:
        return f"[tool-error] github_list_issues: {exc}"


@tool
def github_create_pr(repo: str, title: str, body: str, head: str, base: str = "main") -> str:
    """Create a pull request. head='feature-branch', base='main'."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        pr = r.create_pull(title=title, body=body, head=head, base=base)
        return f"Created PR #{getattr(pr, 'number', '?')}: {getattr(pr, 'html_url', '?')}"
    except Exception as exc:
        return f"[tool-error] github_create_pr: {exc}"


@tool
def github_get_file(repo: str, path: str, ref: str = "main") -> str:
    """Get the content of a file from a GitHub repo."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        content = r.get_contents(path, ref=ref)
        if hasattr(content, "decoded_content"):
            return str(content.decoded_content.decode("utf-8"))
        return str(content)
    except Exception as exc:
        return f"[tool-error] github_get_file: {exc}"


@tool
def github_list_prs(repo: str, state: str = "open", limit: int = 10) -> str:
    """List pull requests in a repository."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        prs = list(r.get_pulls(state=state))[:limit]
        lines = []
        for p in prs:
            head_ref = getattr(getattr(p, "head", None), "ref", "?")
            base_ref = getattr(getattr(p, "base", None), "ref", "?")
            lines.append(
                f"#{getattr(p, 'number', '?')} [{getattr(p, 'state', '?')}] "
                f"{getattr(p, 'title', '?')} ({head_ref} → {base_ref})"
            )
        return "\n".join(lines) or "No pull requests found."
    except Exception as exc:
        return f"[tool-error] github_list_prs: {exc}"


@tool
def github_search_code(query: str, repo: str = "") -> str:
    """Search code on GitHub. query='function:my_func', repo='owner/name' (optional)."""
    g, err = _gh_client()
    if err:
        return err
    try:
        full_query = f"{query} repo:{repo}" if repo else query
        results = list(g.search_code(full_query))[:10]
        lines = [
            f"{getattr(getattr(r, 'repository', None), 'full_name', '?')}/"
            f"{getattr(r, 'path', '?')} — {getattr(r, 'html_url', '?')}"
            for r in results
        ]
        return "\n".join(lines) or "No code results found."
    except Exception as exc:
        return f"[tool-error] github_search_code: {exc}"


@tool
def github_merge_pr(repo: str, pr_number: int, method: str = "merge") -> str:
    """Merge a GitHub PR. method='merge'|'squash'|'rebase'."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        pr = r.get_pull(pr_number)
        result = pr.merge(merge_method=method)
        return (
            f"PR #{pr_number} merged ({method}). "
            f"merged={getattr(result, 'merged', '?')} sha={getattr(result, 'sha', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] github_merge_pr: {exc}"


@tool
def github_add_pr_comment(repo: str, pr_number: int, body: str) -> str:
    """Add an issue-style comment to a PR (uses the issue comments endpoint)."""
    g, err = _gh_client()
    if err:
        return err
    try:
        r = g.get_repo(repo)
        issue = r.get_issue(pr_number)
        comment = issue.create_comment(body)
        return f"Comment added to PR #{pr_number}: {getattr(comment, 'html_url', '?')}"
    except Exception as exc:
        return f"[tool-error] github_add_pr_comment: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
