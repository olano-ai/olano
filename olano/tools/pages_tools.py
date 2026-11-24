"""Agent tools for publishing password-protected pages from the workspace.

``pages_start`` publishes a directory as it is. ``pages_deploy`` publishes a
page that runs commands: a build (``npm run build`` for a Vite/React/Svelte
app, served from its output as an immutable release), a long-running server
(``node server.js``, ``python3 app.py``; the runtime keeps it alive and proxies
the page URL to it), or both. ``pages_status`` / ``pages_logs`` / ``pages_stop`` /
``pages_restart`` / ``pages_delete`` manage them. Entries live in the host-wide
page registry (``olano/pages/registry.py``); the runtime serves each at
``/pages/<slug>/`` behind HTTP Basic Auth and the page supervisor
(``olano/pages/services.py``) builds and runs the deployed ones.

Security model: the viewer password is set ONLY by the human owner
(``olano pages set-password`` CLI or the admin dashboard endpoint). These
tools can neither read nor write it, so the plaintext never enters an agent's
context, transcript, or shell history. Until the owner sets it, published
pages answer 503: content is never served unauthenticated. ``pages_deploy``
is command execution: it sits in the ``shell`` approval category, the security
middleware gates it exactly like ``execute`` (shell access, the shell guard, a
managed box's sandbox requirement), and its commands run only where the
agent's own shell runs (``olano/pages/policy.py``).

Collision model: slugs are claimed host-wide under an interprocess lock, so
two agents on the same host can never publish over each other; a slug owned
by another agent is reported as taken instead of being overwritten.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.pages import registry as pages_registry
from olano.pages.registry import PagesError
from olano.pages.spec import KIND_SERVICE, SpecError, build_spec, record_spec
from olano.security.tool_metadata import tool_metadata

logger = logging.getLogger(__name__)


def _current_agent_name() -> str:
    """The agent this tool call runs for: the bound runtime context, else the
    home-config default agent."""
    # The credential scope, not the bare bound name: a Cortex worker runs under
    # a throwaway `<parent>__cog__…` name and must publish as its parent, whose
    # viewer password the owner set.
    from olano.tools._credentials import credential_agent_name

    name = credential_agent_name() or ""
    if name:
        return name
    from olano.config.home import load_home_config

    return load_home_config().default_agent


def _workspace_root() -> Path:
    """Resolve the *running* agent's workspace root from the request context.

    Mirrors ``monitor_tools._workspace_root``: tools execute inside a request
    context that carries the agent's real workspace root, so a bare
    ``get_workspace()`` would resolve the legacy/global workspace instead.
    """
    from olano.config.workspace import get_workspace
    from olano.security.request_context import get_request_context

    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _page_url(slug: str) -> str:
    """Best-effort external URL for a page (path-only when no origin is known)."""
    try:
        from olano.core.mcp_setup import _public_origin

        origin = _public_origin()
    except Exception:  # pragma: no cover - defensive
        origin = None
    return f"{origin}/pages/{slug}/" if origin else f"/pages/{slug}/"


def _auth_note(agent: str) -> str:
    if pages_registry.page_auth_configured(agent):
        return "Viewer access: HTTP Basic Auth (password set by the owner for this agent)."
    return (
        "NOT SERVING YET: no viewer password is set for this agent, so the page "
        "answers 503. Ask the human owner to set one in the dashboard (Agent → "
        f"Pages) or run `olano pages set-password --agent {agent}` — agents "
        "cannot set or see that password."
    )


def _push_pages_event(action: str, slug: str, agent: str) -> None:
    try:
        from olano.runtime.api import _push_event

        _push_event("pages_changed", {"action": action, "slug": slug, "agent": agent})
    except Exception:  # pragma: no cover - no live runtime (one-shot CLI)
        pass


def _notify_supervisor(slug: str) -> None:
    try:
        from olano.pages.services import notify_page_changed

        notify_page_changed(slug)
    except Exception:  # pragma: no cover - the next reconcile tick picks it up
        logger.debug("pages: could not notify the supervisor about %s", slug, exc_info=True)


def _ago(stamp: float | int | None) -> str:
    if not stamp:
        return ""
    seconds = max(0, int(time.time() - float(stamp)))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _runtime_line(record: dict[str, Any]) -> str:
    """One line on what a deployed page is doing right now, from the supervisor."""
    spec = record_spec(record)
    if spec is None:
        return ""
    try:
        from olano.pages.services import page_status

        state = page_status(record["slug"]) or {}
    except Exception:  # pragma: no cover - defensive
        state = {}
    current = state.get("state") or "pending"
    parts = [f"{spec.kind} page: {current}"]
    if state.get("port"):
        parts.append(f"port {state['port']}")
    if state.get("ready_at") and current == "running":
        parts.append(f"up since {_ago(state['ready_at'])}")
    if state.get("restarts"):
        parts.append(f"{state['restarts']} restart(s)")
    if state.get("release"):
        parts.append(f"release {state['release']}")
    build = state.get("build") or {}
    if build.get("state") in ("succeeded", "failed"):
        parts.append(f"last build {build['state']} {_ago(build.get('finished_at'))}".rstrip())
    line = "  " + ", ".join(parts)
    if state.get("detail"):
        line += f"\n  note: {state['detail']}"
    if state.get("last_error") and current in ("failed", "backoff", "waiting", "live", "running"):
        error = str(state["last_error"]).strip().splitlines()
        if error:
            line += "\n  last error: " + "\n    ".join(error[-8:])
    return line


def _describe(record: dict[str, Any]) -> str:
    state = "serving" if record.get("enabled") else "stopped"
    head = (
        f"- {record['slug']}: {state}, directory '{record.get('directory')}', "
        f"updated {record.get('updated_at')} → {_page_url(record['slug'])}"
    )
    runtime = _runtime_line(record) if record.get("enabled") else ""
    return f"{head}\n{runtime}" if runtime else head


def _is_service(record: dict[str, Any]) -> bool:
    spec = record_spec(record)
    return spec is not None and spec.kind == KIND_SERVICE


_ROOT_ABSOLUTE = re.compile(r"""(?:src|href|action)\s*=\s*["']/(?!/|pages/)""", re.IGNORECASE)


def _root_path_note(directory: Path) -> str:
    """Warn when index.html links root-absolute paths that escape /pages/<slug>/."""
    index = directory / "index.html"
    try:
        text = index.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return ""
    if _ROOT_ABSOLUTE.search(text):
        return (
            '\nWarning: index.html links root-absolute paths (src="/…" or href="/…"). '
            "The page lives under /pages/<slug>/, so those miss it: use relative paths "
            "(./app.js), or set the framework's base path to /pages/<slug>/."
        )
    return ""


@tool
def pages_start(
    slug: Annotated[
        str,
        "URL slug for the page — it becomes /pages/<slug>/. Lowercase letters, "
        "digits and hyphens, 2-63 chars. Must not be claimed by another agent.",
    ],
    directory: Annotated[
        str,
        "Workspace-relative directory to serve. Must already exist; give it an "
        "index.html as the entry point.",
    ] = "site",
    spa: Annotated[
        bool,
        "Single-page app: serve index.html for any path that is not a file, so "
        "client-side routes like /pages/<slug>/settings load the app. Missing "
        "assets (.js, .css, images) still answer 404.",
    ] = False,
) -> str:
    """Publish a workspace directory as a password-protected page at /pages/<slug>/.

    Serves the files as they are, with no build and no server; use pages_deploy
    for an app that needs `npm run build` or a running backend. Claims the slug
    host-wide (collision-safe across agents): if another agent already owns it,
    pick a different slug (pages_status reports availability). The page is
    protected by HTTP Basic Auth with a password only the human owner sets and
    knows; publishing never makes content publicly readable. Re-running on your
    own slug updates the directory and re-enables serving (and stops any server
    a previous pages_deploy started for it).
    """
    agent = _current_agent_name()
    try:
        record = pages_registry.start_page(agent, slug, directory, _workspace_root(), spa=spa)
    except PagesError as e:
        return f"Error: {e}"
    _push_pages_event("start", record["slug"], agent)
    _notify_supervisor(record["slug"])
    page_dir = _workspace_root() / record["directory"]
    index_note = ""
    if not (page_dir / "index.html").is_file():
        index_note = (
            "\nNote: no index.html in that directory yet — visitors to the "
            "bare URL will get a 404 until you add one."
        )
    else:
        index_note = _root_path_note(page_dir)
    return (
        f"Page '{record['slug']}' is published from '{record['directory']}' at "
        f"{_page_url(record['slug'])}\n{_auth_note(agent)}{index_note}"
    )


@tool_metadata(capability="mutating", risk="high", requires_hitl=True)
@tool
def pages_deploy(
    slug: Annotated[
        str,
        "URL slug for the page — it becomes /pages/<slug>/. Lowercase letters, "
        "digits and hyphens, 2-63 chars. Must not be claimed by another agent.",
    ],
    directory: Annotated[
        str,
        "Workspace-relative project directory (where package.json or the server "
        "code lives). Build and start commands run here.",
    ],
    build: Annotated[
        str,
        "Command run before serving, once per deploy and on every pages_restart, "
        "e.g. 'npm ci && npm run build'. Empty: no build. One line; join steps with &&.",
    ] = "",
    start: Annotated[
        str,
        "Command that runs the HTTP server, e.g. 'node server.js' or 'python3 app.py'. "
        "It must listen on 127.0.0.1 and the port in the PORT environment variable. "
        "Empty: a static page served from `output`.",
    ] = "",
    output: Annotated[
        str,
        "Static builds only: the directory the build writes, relative to `directory`, "
        "e.g. 'dist' (Vite) or 'build' (Create React App).",
    ] = "",
    spa: Annotated[
        bool,
        "Static builds only: serve index.html for paths that are not files, for "
        "client-side routers (React Router, Vue Router).",
    ] = False,
    port: Annotated[
        int,
        "Services only: the port your server listens on if it cannot read $PORT. "
        "0 (default): the runtime picks a free port and passes it as $PORT.",
    ] = 0,
    healthcheck: Annotated[
        str,
        "Services only: path probed until the server is up, then every 30s; any answer "
        "below 500 counts as healthy. Default '/'.",
    ] = "/",
    strip_prefix: Annotated[
        bool,
        "Services only: true (default) forwards /pages/<slug>/x to your server as /x. "
        "Set false when the framework is configured with base path /pages/<slug> "
        "(Next.js basePath) and expects the full path.",
    ] = True,
    build_timeout: Annotated[int, "Seconds the build may run (30-1800). 0: 600."] = 0,
    startup_timeout: Annotated[
        int, "Services only: seconds the server has to answer its healthcheck (5-600). 0: 60."
    ] = 0,
    env: Annotated[
        dict[str, str] | None,
        "Extra non-secret environment for build and start, e.g. {'NODE_ENV': 'production'}. "
        "Never put secrets here: values are stored in plain text and shown in pages_status.",
    ] = None,
) -> str:
    """Deploy a page that runs commands: an npm-built app, a running server, or both.

    Three shapes:
    - Static build (React/Vite/Vue/Svelte/Astro): build='npm ci && npm run build',
      output='dist', optionally spa=true. Each successful build becomes an
      immutable release; a failed build leaves the previous one live.
    - Service (Express, Fastify, Flask, FastAPI, any HTTP server): start='node server.js'.
      The runtime keeps it running, restarts it if it crashes (with backoff), and
      forwards every request for /pages/<slug>/ to it. It must listen on
      127.0.0.1:$PORT. Use relative URLs in its pages (fetch('api/items'), not
      fetch('/api/items')), or read the X-Forwarded-Prefix header.
    - Both: build then start.
    Commands run in your sandbox, like your own shell commands. This call returns
    as soon as the deploy is recorded; the build and start happen in the
    background; check progress with pages_status and pages_logs. The page is
    protected by the owner's viewer password, like every page.
    """
    agent = _current_agent_name()
    try:
        spec = build_spec(
            build=build,
            start=start,
            output=output,
            port=port,
            healthcheck=healthcheck,
            strip_prefix=strip_prefix,
            build_timeout=build_timeout,
            startup_timeout=startup_timeout,
            env=env,
        )
    except SpecError as e:
        return f"Error: {e}"

    from olano.pages import policy
    from olano.security.shell_guard import validate_command

    denial = policy.command_denial(agent)
    if denial:
        return f"Error: {denial}"
    for label, command in (("build", spec.build), ("start", spec.start)):
        if command:
            ok, reason = validate_command(command)
            if not ok:
                return f"Error: {label} command refused. {reason}"

    if spec.kind == KIND_SERVICE:
        try:
            from olano.config.system_settings import load_pages_settings

            limit = load_pages_settings().max_services_per_agent
        except Exception:  # pragma: no cover - never raises
            limit = 5
        try:
            this_slug = pages_registry.normalize_slug(slug)
        except PagesError as e:
            return f"Error: {e}"
        running = [
            p
            for p in pages_registry.list_pages(agent)
            if p.get("enabled") and p["slug"] != this_slug and _is_service(p)
        ]
        if len(running) >= limit:
            names = ", ".join(p["slug"] for p in running)
            return (
                f"Error: this agent already serves {len(running)} page server(s) ({names}), "
                f"the limit on this deployment. Stop one with pages_stop first."
            )

    try:
        record = pages_registry.deploy_page(
            agent, slug, directory, _workspace_root(), spec, spa=spa
        )
    except PagesError as e:
        return f"Error: {e}"
    _push_pages_event("deploy", record["slug"], agent)
    _notify_supervisor(record["slug"])

    steps = []
    if spec.build:
        steps.append(f"build: {spec.build}")
    if spec.start:
        steps.append(f"start: {spec.start}")
    what = (
        "It is building and starting in the background."
        if spec.build and spec.start
        else "It is building in the background."
        if spec.build
        else "Its server is starting in the background."
    )
    page_dir = _workspace_root() / record["directory"]
    note = ""
    if not spec.start:
        note = _root_path_note(page_dir / spec.output)
    return (
        f"Page '{record['slug']}' is deployed from '{record['directory']}' at "
        f"{_page_url(record['slug'])} ({'; '.join(steps)}).\n{what} Check it with "
        f"pages_status('{record['slug']}'); read the output with pages_logs.\n"
        f"{_auth_note(agent)}{note}"
    )


@tool
def pages_status(
    slug: Annotated[
        str,
        "Slug to check — any slug, not just your own. Empty: list all of "
        "your published pages instead.",
    ] = "",
) -> str:
    """Check whether a page slug is available or already taken, and report page state.

    Call with a slug BEFORE pages_start/pages_deploy to find out if it is free
    to claim, taken by another agent on this host, or already one of yours (with
    its URL and serving state). For a deployed page it also reports what the
    build and server are doing right now (building, starting, running, restarting,
    failed) and the last error. Call with no slug to list all of your published
    pages. Read-only: it never claims a slug or changes anything.
    """
    agent = _current_agent_name()
    if slug:
        try:
            normalized = pages_registry.normalize_slug(slug)
        except PagesError as e:
            return f"Error: {e}"
        record = pages_registry.get_page(normalized)
        if record is None:
            return f"Slug '{normalized}' is available."
        if record.get("agent") != agent:
            return f"Slug '{normalized}' is taken by agent '{record.get('agent')}'."
        return f"Your page:\n{_describe(record)}\n{_auth_note(agent)}"
    pages = pages_registry.list_pages(agent)
    if not pages:
        return f"Agent '{agent}' has no published pages. Use pages_start to publish one."
    lines = "\n".join(_describe(r) for r in pages)
    return f"Pages published by '{agent}':\n{lines}\n{_auth_note(agent)}"


@tool
def pages_logs(
    slug: Annotated[str, "Slug of your deployed page."],
    which: Annotated[
        str, "'service' for the running server's output (default), 'build' for the last build."
    ] = "service",
    lines: Annotated[int, "How many lines from the end (1-500)."] = 80,
) -> str:
    """Read the end of a deployed page's server log or last build log.

    Use it when pages_status says a build failed or a server is crashing: the
    output is what the command printed (errors, stack traces). The same files
    are in your workspace under .olano/pages/<slug>/ (service.log, build.log).
    """
    agent = _current_agent_name()
    try:
        normalized = pages_registry.normalize_slug(slug)
    except PagesError as e:
        return f"Error: {e}"
    record = pages_registry.get_page(normalized)
    if record is None:
        return f"Error: no page registered under slug '{normalized}'"
    if record.get("agent") != agent:
        return f"Error: slug '{normalized}' belongs to agent '{record.get('agent')}'"
    if record_spec(record) is None:
        return f"Page '{normalized}' serves files only; it has no build or server logs."
    from olano.pages.paths import build_log_path, service_log_path
    from olano.pages.services import tail_text

    kind = (which or "service").strip().lower()
    if kind not in ("service", "build"):
        return "Error: which must be 'service' or 'build'"
    root = _workspace_root()
    path = (
        build_log_path(root, normalized) if kind == "build" else service_log_path(root, normalized)
    )
    count = max(1, min(500, int(lines or 80)))
    text = tail_text(path, lines=count, max_bytes=512 * 1024)
    if not text.strip():
        return f"The {kind} log for '{normalized}' is empty (or not written yet)."
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    return f"Last {count} lines of {rel}:\n{text}"


@tool
def pages_stop(
    slug: Annotated[str, "Slug of your page to stop serving."],
) -> str:
    """Stop serving one of your pages (visitors get 404) and stop its server if
    it has one. The slug stays reserved for you; use pages_restart to resume or
    pages_delete to release it."""
    agent = _current_agent_name()
    try:
        record = pages_registry.stop_page(agent, slug)
    except PagesError as e:
        return f"Error: {e}"
    _push_pages_event("stop", record["slug"], agent)
    _notify_supervisor(record["slug"])
    extra = " Its server is being stopped." if record_spec(record) is not None else ""
    return f"Page '{record['slug']}' stopped. The slug remains reserved for you.{extra}"


@tool
def pages_restart(
    slug: Annotated[str, "Slug of your page to (re-)enable."],
) -> str:
    """Re-validate the page directory and resume serving a stopped page.

    For a deployed page this also re-runs its build and restarts its server,
    which is how to pick up code changes, and clears a 'failed' state after you
    fixed the cause."""
    agent = _current_agent_name()
    try:
        record = pages_registry.restart_page(agent, slug, _workspace_root())
    except PagesError as e:
        return f"Error: {e}"
    _push_pages_event("restart", record["slug"], agent)
    _notify_supervisor(record["slug"])
    spec = record_spec(record)
    extra = ""
    if spec is not None:
        extra = (
            "\nIts build is re-running and its server restarting in the background; "
            "check pages_status."
            if spec.build
            else "\nIts server is restarting in the background; check pages_status."
        )
    return (
        f"Page '{record['slug']}' is serving again at "
        f"{_page_url(record['slug'])}\n{_auth_note(agent)}{extra}"
    )


@tool
def pages_delete(
    slug: Annotated[str, "Slug of your page to remove entirely."],
) -> str:
    """Unpublish one of your pages, stop its server if it has one, and release
    its slug for anyone to claim. Workspace files are not touched; only the
    registry entry and the built releases kept for serving are removed."""
    agent = _current_agent_name()
    try:
        pages_registry.delete_page(agent, slug)
    except PagesError as e:
        return f"Error: {e}"
    normalized = slug.strip().lower()
    try:
        from olano.pages.releases import delete_releases

        delete_releases(normalized)
    except Exception:  # pragma: no cover - the supervisor sweeps orphans
        logger.debug("pages: could not delete releases for %s", normalized, exc_info=True)
    _push_pages_event("delete", normalized, agent)
    _notify_supervisor(normalized)
    return f"Page '{normalized}' removed and its slug released."


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
