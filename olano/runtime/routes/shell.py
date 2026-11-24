"""Shell command streaming and execution.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_shell_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the shell routes to ``app``."""
    # ── Allowlisted shell exec (used by the dashboard's "Run" buttons) ────────

    # We only let the dashboard run package-manager installs from this
    # endpoint. Arbitrary shell would be a privilege escalation: the API
    # already serves any user with access to the dashboard. The allowlist
    # is intentionally narrow and matches typical dependency-install hints.
    _SHELL_EXEC_ALLOWLIST: tuple[str, ...] = (
        # Python — always go through the project's uv venv.
        "uv add ",
        "uv pip install ",
        "uv sync",
        "pip install ",
        "pip3 install ",
        "python -m pip install ",
        "python3 -m pip install ",
        # Post-install setup hooks for installed Python packages.
        "playwright install ",
        "python -m playwright install ",
        # Node ecosystem.
        "npm install ",
        "npm i ",
        "pnpm install ",
        "pnpm add ",
        "yarn add ",
        "npx --yes ",
        # System package managers — only the install verbs. Anything beyond
        # the allowlisted prefix is rejected, so e.g. `brew uninstall` won't
        # match. We intentionally do NOT add `apt-get`/`dnf`/`pacman` here:
        # those typically need sudo and the dashboard daemon shouldn't drive
        # privileged commands. The dashboard surfaces them as Copy-only hints.
        "brew install ",
        # Signal — the guided setup wizard runs read-only / non-sensitive
        # signal-cli probes here (e.g. `signal-cli --version`). The captcha
        # register/verify steps go through the dedicated /api/connectors/signal
        # endpoints instead, so the captcha token is never written to the
        # .olano/shell_runs/ logs that this endpoint persists.
        "signal-cli ",
    )

    def _cross_site_request(request: Request) -> bool:
        """True when a browser on another site initiated this request.

        Browsers send ``Sec-Fetch-Site`` on every request and ``Origin`` on
        cross-origin ones; the dashboard's own ``fetch``/``EventSource`` calls
        are same-origin, and non-browser clients send neither header. Mirrors
        ``_terminal_origin_allowed`` (which guards the WebSocket lane): a
        loopback origin is always fine, ``LIONCLAW_DASHBOARD_ORIGIN`` pins the
        host when set, and otherwise the origin must match the request's host.
        """
        from urllib.parse import urlsplit

        site = request.headers.get("sec-fetch-site", "").strip().lower()
        if site == "cross-site":
            return True
        # A link click, an <iframe> or an <img> from ANY site (a sibling
        # subdomain reads as ``same-site``, not ``cross-site``) arrives as a
        # navigation or a no-cors embed; the dashboard's fetch/EventSource
        # never does. Neither is a legitimate way to run a command.
        mode = request.headers.get("sec-fetch-mode", "").strip().lower()
        if mode in ("navigate", "no-cors"):
            return True

        allowed = os.environ.get("LIONCLAW_DASHBOARD_ORIGIN", "").strip()
        expected_host = (
            (urlsplit(allowed).hostname or "").lower()
            if allowed
            else (request.url.hostname or "").lower()
        )

        def _foreign(url: str) -> bool:
            host = (urlsplit(url).hostname or "").lower()
            if host in ("localhost", "127.0.0.1", "::1"):
                return False
            return host != expected_host

        origin = request.headers.get("origin", "").strip()
        if origin:
            return _foreign(origin)
        # Browsers without Fetch Metadata (Safari before 16.4) send neither
        # header on a GET navigation, so fall back to the Referer: the
        # dashboard's own requests carry its URL (the Referrer-Policy this
        # runtime sets keeps it on same-origin requests), a link on another
        # site carries that site's. A page that suppresses its Referer looks
        # like a browser sending nothing at all, so a browser-shaped
        # User-Agent with no provenance headers is refused too; CLI clients
        # send neither the UA shape nor a Referer and stay unaffected.
        referer = request.headers.get("referer", "").strip()
        if referer:
            return _foreign(referer)
        if not site:
            return request.headers.get("user-agent", "").startswith("Mozilla/")
        return False

    def _validate_shell_command(cmd: str) -> str:
        """Common allowlist + safety checks for shell exec endpoints.

        Returns the trimmed command on success, raises HTTPException on reject.
        """
        if not isinstance(cmd, str) or not cmd.strip():
            raise HTTPException(status_code=400, detail="command is required")
        cmd = cmd.strip()
        # A single ``&`` backgrounds one command and starts another, and a
        # newline separates two, so both are compound commands as much as
        # ``&&`` or ``;`` are.
        if any(ch in cmd for ch in ("&&", "||", ";", "|", ">", "<", "`", "$(", "&", "\n", "\r")):
            raise HTTPException(
                status_code=400, detail="Compound shell commands are not allowed here."
            )
        if not any(cmd.startswith(p) for p in _SHELL_EXEC_ALLOWLIST):
            allowed = ", ".join(p.strip() for p in _SHELL_EXEC_ALLOWLIST)
            raise HTTPException(
                status_code=403,
                detail=f"Command not in allowlist. Allowed prefixes: {allowed}",
            )
        return cmd

    def _host_exec_denied_reason() -> str | None:
        """Denial message when host-side shell / install surfaces must be off.

        Under a managed posture these run in the runtime container (which holds
        the product source + secrets) and the dashboard owner is the customer,
        so a host shell / arbitrary `pip`/`npx` install (which runs code) would
        expose /app and the vault. Code execution belongs in the sandbox.
        """
        try:
            from olano.deployment import enforcement as dep_enforcement

            if dep_enforcement.require_sandboxed_exec():
                return (
                    "Host shell and package-install commands are disabled on this "
                    "managed deployment. Run code through an agent (sandboxed "
                    "execution) instead of the runtime host."
                )
        except Exception:
            _api.logger.exception("managed host-exec gate failed; denying to be safe")
            return "Host shell is unavailable."
        return None

    @app.get("/api/shell/stream")
    async def shell_stream(request: Request, command: str = "", timeout_seconds: int = 600) -> Any:
        """Stream stdout/stderr of an allowlisted shell command via SSE.

        Each SSE event is JSON: ``{"type": "stdout"|"stderr", "data": "<line>"}``.
        Final event is ``{"type": "done", "exit_code": int, "log_path": str}``.
        On reject the connection still opens and emits a single error event
        before closing — that's friendlier for the EventSource consumer than a
        bare HTTP 4xx (which it can't read).
        """
        from starlette.responses import StreamingResponse as SR

        # Same gate as the terminal lanes: package installs run on the host.
        can_exec = auth_layer.can_use_terminal()
        # A GET that runs a command must never be reachable from a link on
        # another site (the session cookie rides along on a top-level
        # navigation). Decided here, reported inside the stream like every
        # other refusal so the EventSource consumer can read it.
        cross_site = _cross_site_request(request)

        async def _gen():
            try:
                if cross_site:
                    raise HTTPException(status_code=403, detail="cross_site_request_refused")
                if not can_exec:
                    raise HTTPException(status_code=403, detail="terminal_access_denied")
                _denied = _host_exec_denied_reason()
                if _denied:
                    raise HTTPException(status_code=403, detail=_denied)
                cmd = _validate_shell_command(command)
            except HTTPException as exc:
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "error", "message": str(exc.detail), "status": exc.status_code}
                    )
                    + "\n\n"
                )
                yield "data: " + json.dumps({"type": "done", "exit_code": -1}) + "\n\n"
                return

            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                yield (
                    "data: "
                    + json.dumps({"type": "error", "message": f"Failed to spawn: {exc}"})
                    + "\n\n"
                )
                yield "data: " + json.dumps({"type": "done", "exit_code": -1}) + "\n\n"
                return

            stdout_buf: list[str] = []
            stderr_buf: list[str] = []

            async def _pump(stream, kind: str, buf: list[str]):
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    buf.append(text)
                    yield "data: " + json.dumps({"type": kind, "data": text}) + "\n\n"

            # Multiplex stdout + stderr into a single SSE stream. Use a queue
            # so we can interleave both sources line-by-line as they arrive.
            queue: asyncio.Queue = asyncio.Queue()

            async def _pump_to_queue(stream, kind: str, buf: list[str]) -> None:
                while True:
                    try:
                        line = await stream.readline()
                    except Exception:
                        break
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    buf.append(text)
                    await queue.put({"type": kind, "data": text})

            stdout_task = asyncio.create_task(_pump_to_queue(proc.stdout, "stdout", stdout_buf))
            stderr_task = asyncio.create_task(_pump_to_queue(proc.stderr, "stderr", stderr_buf))

            async def _wait_done() -> int:
                rc = await proc.wait()
                # Flush both pipes before signalling completion.
                await stdout_task
                await stderr_task
                return rc

            wait_task = asyncio.create_task(_wait_done())

            t0 = datetime.now(timezone.utc)
            yield "data: " + json.dumps({"type": "started", "command": cmd}) + "\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                        yield "data: " + json.dumps(event) + "\n\n"
                    except TimeoutError:
                        # No new line in the last second; check if the process
                        # finished. If yes, drain and exit. Otherwise emit a
                        # keepalive so proxies don't time us out.
                        if wait_task.done():
                            # Drain anything left in the queue.
                            while not queue.empty():
                                yield "data: " + json.dumps(queue.get_nowait()) + "\n\n"
                            break
                        yield ": keepalive\n\n"
                    if (datetime.now(timezone.utc) - t0).total_seconds() > timeout_seconds:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "error", "message": f"Timed out after {timeout_seconds}s"}
                            )
                            + "\n\n"
                        )
                        break
            finally:
                # Make sure the process dies if we exit early.
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                rc = await wait_task if not wait_task.done() else wait_task.result()

                # Persist a log file alongside the existing /api/shell/exec format.
                log_rel_path = None
                try:
                    ws = _api._workspace_root()
                    logs_dir = ws / WORKSPACE_STATE_DIRNAME / "shell_runs"
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                    slug = "".join(
                        ch if ch.isalnum() else "-" for ch in " ".join(cmd.split()[0:3])
                    )[:40]
                    log_path = logs_dir / f"{stamp}_{slug}.log"
                    log_text = (
                        f"# Olano shell run (streamed)\n"
                        f"# command:    {cmd}\n"
                        f"# started:    {stamp}\n"
                        f"# exit_code:  {rc}\n\n"
                        f"=== stdout ===\n" + "\n".join(stdout_buf) + "\n"
                        "=== stderr ===\n" + "\n".join(stderr_buf) + "\n"
                    )
                    log_path.write_text(log_text, encoding="utf-8")
                    log_rel_path = str(log_path.relative_to(ws))
                except Exception as exc:
                    _api.logger.warning("Could not persist shell-run log: %s", exc)

                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "done",
                            "exit_code": rc,
                            "log_path": log_rel_path,
                            "command": cmd,
                        }
                    )
                    + "\n\n"
                )

        return SR(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/shell/exec")
    async def shell_exec(body: dict) -> dict:
        """Run a single shell command from the dashboard's chat code blocks.

        Body: ``{"command": "uv add yfinance", "timeout_seconds": 120}``.
        Returns ``{"exit_code", "stdout", "stderr"}``. Rejects anything not
        in ``_SHELL_EXEC_ALLOWLIST`` with a 403 so the user can't accidentally
        ship arbitrary shell from a chat session.
        """
        # Same gate as the terminal lanes: installs execute on the host.
        if not auth_layer.can_use_terminal():
            raise HTTPException(status_code=403, detail="terminal_access_denied")
        _denied = _host_exec_denied_reason()
        if _denied:
            raise HTTPException(status_code=403, detail=_denied)
        cmd = (body or {}).get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            raise HTTPException(status_code=400, detail="command is required")
        cmd = cmd.strip()
        # Reject obvious shell-metacharacter abuse — even if the prefix is
        # allowed, a chained command like `pip install x; rm -rf` shouldn't
        # slip through.
        if any(ch in cmd for ch in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
            raise HTTPException(
                status_code=400, detail="Compound shell commands are not allowed here."
            )
        if not any(cmd.startswith(p) for p in _SHELL_EXEC_ALLOWLIST):
            allowed = ", ".join(p.strip() for p in _SHELL_EXEC_ALLOWLIST)
            raise HTTPException(
                status_code=403,
                detail=f"Command not in allowlist. Allowed prefixes: {allowed}",
            )
        timeout = float((body or {}).get("timeout_seconds") or 180)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to spawn: {exc}") from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise HTTPException(status_code=504, detail=f"Command timed out after {int(timeout)}s")
        stdout_s = stdout_b.decode("utf-8", errors="replace")
        stderr_s = stderr_b.decode("utf-8", errors="replace")

        # Persist the run to a log file in the workspace so the agent can
        # read it back via its filesystem tools and analyze what happened.
        log_rel_path: str | None = None
        try:
            ws = _api._workspace_root()
            logs_dir = ws / WORKSPACE_STATE_DIRNAME / "shell_runs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            # First few alnum chars of the command go into the filename so the
            # directory listing is browsable at a glance.
            slug = "".join(ch if ch.isalnum() else "-" for ch in " ".join(cmd.split()[0:3]))[:40]
            log_path = logs_dir / f"{stamp}_{slug}.log"
            log_text = (
                f"# Olano shell run\n"
                f"# command:    {cmd}\n"
                f"# started:    {stamp}\n"
                f"# exit_code:  {proc.returncode}\n"
                f"# duration_s: (n/a)\n"
                f"\n"
                f"=== stdout ===\n{stdout_s}\n"
                f"=== stderr ===\n{stderr_s}\n"
            )
            log_path.write_text(log_text, encoding="utf-8")
            # Path relative to the workspace root so the agent can plug it
            # straight into ls / read_file / etc. without absolute-path drama.
            log_rel_path = str(log_path.relative_to(ws))
        except Exception as exc:
            _api.logger.warning("Could not persist shell-run log: %s", exc)

        return {
            "exit_code": proc.returncode,
            "stdout": stdout_s,
            "stderr": stderr_s,
            "command": cmd,
            "log_path": log_rel_path,
        }

    # ── Arbitrary shell run (chat "!" shell mode) ────────────────────────────

    @app.post("/api/shell/run")
    async def shell_run(body: dict) -> dict:
        """Run a single arbitrary shell command from the chat "!" shell mode.

        Unlike /api/shell/exec this is intentionally NOT allowlisted — it permits
        pipes / && / redirects, because it's a user-driven terminal lane. The
        controls are the terminal feature flag, (multi-user) admin, and agent
        access — NOT string filtering. Full combined output is written to a
        unique per-run log under the agent's workspace (so the agent can read it
        with its filesystem tools); only a head/tail preview is returned so chat
        context stays small.

        WHERE it runs mirrors the Terminal tab exactly:

        * open/unmanaged install → the runtime host, in the agent's workspace;
        * managed posture → ``docker exec`` into that agent's live sandbox
          (``OlanoDockerBackend``), which holds neither the product source
          nor the vault. The workspace is the same directory either way (it is
          bind-mounted at ``/workspace``), so the run log lands where the agent
          can read it regardless of lane.

        Fails closed: under a managed posture with no live sandbox for the agent
        there is no safe shell to offer, and the request is refused rather than
        downgraded to a host shell.
        """
        from olano.runtime import terminal as terminal_mod

        if not terminal_mod.terminal_enabled():
            raise HTTPException(status_code=403, detail="terminal_disabled")
        cmd = (body or {}).get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            raise HTTPException(status_code=400, detail="command is required")
        cmd = cmd.strip()
        agent = (body or {}).get("agent") or None
        # Same gate as the terminal WS: admin or an admin-granted member.
        if not auth_layer.can_use_terminal():
            raise HTTPException(status_code=403, detail="terminal_access_denied")
        if agent:
            auth_layer.assert_agent_access(agent)

        sandbox = None
        if terminal_mod.terminal_uses_sandbox():
            sandbox = terminal_mod.sandbox_backend_for(agent or _api._default_agent_name())
            if sandbox is None or not sandbox.container_ref():
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Shell commands run in the agent's sandbox on this managed "
                        "deployment, and this agent has no live sandbox container. "
                        "Enable sandboxed execution for the agent (the Terminal tab "
                        "needs the same thing), then retry."
                    ),
                )
        else:
            # Open/unmanaged box — still honour the kill switches.
            _denied = _host_exec_denied_reason()
            if _denied:
                raise HTTPException(status_code=403, detail=_denied)

        ws = _api._workspace_root(agent)
        timeout = float((body or {}).get("timeout_seconds") or 120)

        if sandbox is not None:
            # The sandbox lane. `execute()` already combines stdout+stderr and
            # never raises on timeout — it comes back as exit 124 with the
            # partial output, which is more useful in chat than a bare 504.
            response = await asyncio.to_thread(sandbox.execute, cmd, timeout=int(timeout))
            output = response.output
            rc = response.exit_code
            out_bytes = len(output.encode("utf-8", errors="replace"))
        else:
            from olano.backends.interpreter_shims import with_interpreter_shims
            from olano.security.shell_guard import sanitized_shell_env

            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=str(ws),
                    # Never the runtime's own env: the vault key and deployment
                    # tokens must not be one `printenv` away from a terminal user.
                    env=with_interpreter_shims(sanitized_shell_env()),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,  # combine like a terminal
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to spawn: {exc}") from exc
            try:
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise HTTPException(
                    status_code=504, detail=f"Command timed out after {int(timeout)}s"
                )
            output = out_b.decode("utf-8", errors="replace")
            rc = proc.returncode
            out_bytes = len(out_b)

        # Head/tail preview, capped by both line count and per-line length so a
        # single huge line (minified JS, base64) can't blow up chat context.
        # The line budgets are overridable (head_lines / tail_lines, clamped
        # 0–1000) for callers that want e.g. a last-N-lines view instead of the
        # default head/tail-25 preview.
        def _clamp_lines(value: object, default: int) -> int:
            if not isinstance(value, int | str):
                return default
            try:
                n = int(value)
            except (TypeError, ValueError):
                return default
            return max(0, min(n, 1000))

        head_n = _clamp_lines((body or {}).get("head_lines"), 25)
        tail_n = _clamp_lines((body or {}).get("tail_lines"), 25)
        max_line = 2000

        def _cap(line: str) -> str:
            return (
                line
                if len(line) <= max_line
                else line[:max_line] + f"… (+{len(line) - max_line} chars)"
            )

        lines = output.splitlines()
        total_lines = len(lines)
        capped = [_cap(line) for line in lines]
        if total_lines <= head_n + tail_n:
            # Everything fits in the preview budget — no truncation. When the
            # caller only wants the tail (head_n == 0) keep the lines in `tail`
            # so "last N lines" semantics hold even for short output.
            if head_n == 0:
                head: list[str] = []
                tail = capped
            else:
                head = capped
                tail = []
            truncated = False
        else:
            head = capped[:head_n] if head_n else []
            tail = capped[total_lines - tail_n :] if tail_n else []
            truncated = True

        # Persist the full output to a uniquely-named log in the workspace.
        log_rel_path: str | None = None
        try:
            logs_dir = ws / WORKSPACE_STATE_DIRNAME / "shell_runs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            first = (cmd.split() or ["cmd"])[0]
            slug = "".join(ch if ch.isalnum() else "-" for ch in first)[:24] or "cmd"
            log_path = logs_dir / f"{stamp}_{slug}_{uuid.uuid4().hex[:8]}.log"
            where = f"sandbox:{sandbox.container_ref()}" if sandbox is not None else "runtime host"
            log_path.write_text(
                f"# Olano shell run (chat '!')\n"
                f"# command:   {cmd}\n"
                f"# ran in:    {where}\n"
                f"# cwd:       {'/workspace' if sandbox is not None else ws}\n"
                f"# exit_code: {rc}\n"
                f"# at:        {stamp}\n\n"
                f"{output}",
                encoding="utf-8",
            )
            try:
                log_path.chmod(0o600)
            except OSError:
                pass
            if sandbox is not None:
                # The runtime writes this as root, 0600. The whole point of
                # putting it in the workspace is that the agent reads it back
                # with its file tools — and a sandboxed agent is uid 1000, so
                # without this hand-off `read_file` on the path we just told it
                # about comes back "permission denied". The dir too, so the
                # agent can list/clean up its own run logs.
                from olano.core.builder import chown_for_sandbox

                chown_for_sandbox(logs_dir)
                chown_for_sandbox(log_path)
            log_rel_path = str(log_path.relative_to(ws))
        except Exception as exc:  # never fail the run on logging
            _api.logger.warning("Could not persist shell-run log: %s", exc)

        return {
            "command": cmd,
            "exit_code": rc,
            "log_path": log_rel_path,
            "head": head,
            "tail": tail,
            "total_lines": total_lines,
            "total_bytes": out_bytes,
            "truncated": truncated,
        }
