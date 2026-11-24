"""What an agent does when a site asks a person to prove they are human.

Cloudflare's "Verify you are human" box, a CAPTCHA, a "press and hold": a check
only a person can pass. The browser never passes one itself (it does not click
the box, solve the puzzle or disguise itself to avoid being shown one). It
first gives a check that lets an ordinary browser through on its own the time
that takes (``blockpage.INTERSTITIAL_WAIT_S``), and what happens after that is
the **mode**:

- ``wait`` (default): ask for a person (the chat's browser window and a push
  notification) and wait up to ``human_check_wait`` minutes. When the page
  gets past the check, control returns to the agent and it carries on; when
  the time runs out, it is told nobody came, and the site is remembered like
  a skipped one, so re-reading the page does not start another wait.
- ``hold``: the same, with no time limit. A run nobody started from a chat
  (a schedule, a background run, another agent) waits as ``wait`` instead: a
  run like that holding the browser forever would only ever be noticed by
  accident.
- ``skip``: do not ask; the agent is told at once that the site needs a
  person, and moves on or asks the user.

The mode resolves from the agent's ``agent.json5 → browser.human_check``,
else ``config.yaml → browser.human_check``, and an agent may pick another for
one task (``browser_navigate(human_check=...)``). Only the live browser has a
screen a person can use, so every other kind behaves as ``skip``.

A check that is waiting keeps refreshing itself (Cloudflare reloads its page
every two minutes or so with a new box), so while one is pending the block
memory must not refuse the site: ``sessions`` lets those reloads through.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import quote

from olano.config.system_settings import (
    HUMAN_CHECK_MODES,
    HUMAN_CHECK_WAIT_DEFAULT,
    HUMAN_CHECK_WAIT_MAX,
)

# Marker the agent-facing results start with.
MARKER = "Human check"
# Tests shorten a minute; nothing else changes it.
SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True)
class Policy:
    mode: str  # wait | hold | skip
    wait_minutes: int

    @property
    def wait_s(self) -> float | None:
        """Seconds to wait for a person, ``None`` for no limit, ``0`` for not at all."""
        if self.mode == "skip":
            return 0.0
        if self.mode == "hold":
            return None
        return self.wait_minutes * SECONDS_PER_MINUTE


def normalize_mode(value: object) -> str:
    """A valid mode, or ``""`` (use the configured one)."""
    text = str(value or "").strip().lower()
    return text if text in HUMAN_CHECK_MODES else ""


def resolve(agent: str, *, choice: str = "", interactive: bool = True) -> Policy:
    """The policy for *agent*'s next check: *choice* (the agent's own pick for
    this task), else the agent's setting, else the home-wide one."""
    from olano.config.system_settings import load_browser_settings

    home = load_browser_settings()
    mode, minutes = home.human_check, home.human_check_wait
    if agent:
        try:
            from olano.config.loader import load_agent_config

            own = load_agent_config(agent).browser
            mode = own.human_check or mode
            minutes = own.human_check_wait or minutes
        except Exception:
            pass
    mode = normalize_mode(choice) or mode
    if mode == "hold" and not interactive:
        mode = "wait"
    minutes = max(1, min(HUMAN_CHECK_WAIT_MAX, int(minutes or HUMAN_CHECK_WAIT_DEFAULT)))
    return Policy(mode=mode, wait_minutes=minutes)


def _how_long(policy: Policy) -> str:
    if policy.mode == "hold":
        return "with no time limit"
    n = policy.wait_minutes
    return f"up to {n} minute{'s' if n != 1 else ''}"


def ask_reason(site: str, who: str) -> str:
    """What the chat's browser window shows the person."""
    by = f" ({who})" if who else ""
    return f"{site} wants a person to confirm they are human{by}. Tick the box on the page."


def passed_note(site: str) -> str:
    return f"[{MARKER}] A person completed {site}'s check; the page below is past it."


def skip_message(site: str, signal: str, *, live: bool, by_person: bool = False) -> str:
    """The agent is not to wait: configured ``skip``, a headless browser, or
    the person pressed Skip."""
    if by_person:
        why = "The person you asked chose to skip it."
    elif live:
        why = "This agent is set not to wait for a person on these checks."
    else:
        why = "This browser has no screen a person could use to pass it."
    if live:
        then = (
            'if they want to do it, open it again with browser_navigate(url, human_check="wait").'
        )
    else:
        then = "a person can only do it in the live browser (Config → Browser)."
    return (
        f"{MARKER}: {site} asks for a person to prove they are human "
        f'("{signal}"). {why} Do not retry {site} in this task and do not try to pass '
        "the check yourself. Use another source for what you need if the task allows "
        f"it; otherwise tell the user that {site} needs a person: {then}"
    )


def timeout_message(site: str, policy: Policy) -> str:
    return (
        f"{MARKER}: nobody completed {site}'s check within {policy.wait_minutes} "
        f"minute{'s' if policy.wait_minutes != 1 else ''}. Do not retry {site} and do not try "
        "to pass the check yourself. If the task needs this site, tell the user and, when "
        'they are ready, open it again with browser_navigate(url, human_check="wait") (or '
        '"hold" to wait with no limit); if another source would do, use that.'
    )


def waiting_text(site: str, policy: Policy) -> str:
    """For logs and the dashboard: what the agent is doing."""
    return f"Waiting {_how_long(policy)} for a person to pass {site}'s check."


def parked_page_url(site: str, outcome: str) -> str:
    """A ``data:`` page the browser shows once nobody is going to pass a check."""
    why = {
        "timeout": "nobody completed it in time",
        "skipped": "the person asked chose to skip it",
    }.get(outcome, "this agent is set not to wait for one")
    name = html.escape(site or "This site")
    body = (
        "<!doctype html><meta charset=utf-8><title>Stopped at a human check</title>"
        "<body style='font:15px system-ui,sans-serif;max-width:36em;margin:15vh auto;"
        "padding:0 16px;color:#333'>"
        f"<h1 style='font-size:20px'>Stopped at {name}'s human check</h1>"
        f"<p>{name} asked for a person to confirm they are human, and {html.escape(why)}. "
        "The agent was told and has moved on.</p>"
        "<p>To try again, ask the agent to open the site again and say you will verify "
        "it.</p></body>"
    )
    return "data:text/html;charset=utf-8," + quote(body)
