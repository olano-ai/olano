"""The dashboard shell contract — one source of truth, two consumers.

Two things live here: the selectors and helpers for driving the app shell
itself (the sidebar, the mobile topbar and its off-canvas nav drawer), and the
route checklist below.

Each checklist entry is ``(url_suffix, selector_proving_the_view_rendered)``.
Two things run this list:

* ``tests/e2e/test_route_checks.py`` — the pytest that CI runs, so a UI change
  that strands a selector fails a check at the moment it lands;
* ``.claude/skills/olano-tester/verify_dashboard.py`` — the hand-run skill,
  which imports from here rather than keeping its own copy.

It lived only in the skill until a UI move went unnoticed: #228 relocated the
sign-in-links section from Connections to Config, and because nothing
automated read the list, four checks stayed broken across both viewports until
someone happened to run the skill. Hence this module and the test beside it.

**Write selectors that would fail if the feature disappeared.** A check that
only proves the page shell mounted is nearly free to satisfy and catches
nothing — prefer a selector keyed on the section's own content. And prefer
*body text unique to one card* over a heading two cards can share: Playwright
runs in strict mode, so an ambiguous selector fails as loudly as a missing one
(that is exactly what the duplicated "Sign-in links" heading did).
"""

from __future__ import annotations

# ── the app shell ────────────────────────────────────────────────────────────
# Shared with the checklist below for the same reason it exists: three suites
# drive these three controls, and a markup move should be a one-line fix here
# rather than a hunt through copies.
#
# These describe the ADVANCED shell — the 25-route operator dashboard. A user
# whose ui_mode is "simple" gets a different tree with no `aside.sidebar` and
# no route table at all, so these selectors do not resolve there. That is not a
# gap: every check below signs in as an admin, and ui_mode defaults to
# "advanced" for every account, so the existing suite is unaffected by the
# simple shell existing. Simple-mode coverage lives in its own module with the
# SIMPLE_* selectors below, rather than by branching every helper here.
MOBILE_TOPBAR = "header.mobile-topbar"
HAMBURGER = f"{MOBILE_TOPBAR} button.mobile-nav-btn"
SIDEBAR = "aside.sidebar[aria-label='Primary']"
# The shell's own box — the grid that fills the window. Not an "is the app up"
# signal (see APP_SHELL_SELECTOR in conftest, which is what waiting uses); this
# is for the checks that measure the window the shell was given.
APP_SHELL = "div.app-shell"

# "SHELL" resolves per viewport: on desktop the sidebar is always present once
# authenticated; below the breakpoint the sidebar is an off-canvas drawer, so
# the mobile checks assert the hamburger trigger instead.
SHELL_SELECTOR: dict[str, str] = {"desktop": SIDEBAR, "mobile": HAMBURGER}

# ── the simple shell ─────────────────────────────────────────────────────────
# The consumer bot-chat surface (ui_mode="simple"). Kept here beside its
# advanced counterpart so there is still one place to fix when markup moves.
SIMPLE_ROOT = "[data-simple-shell]"
SIMPLE_BOT_RAIL = f"{SIMPLE_ROOT} [data-simple-bot-rail]"
SIMPLE_COMPOSER = f"{SIMPLE_ROOT} [data-simple-composer]"


def is_mobile_viewport(page) -> bool:
    """True where the sidebar is an off-canvas drawer rather than the nav.

    Asks the DOM rather than the caller's viewport name: the breakpoint is a
    CSS media query, so "the mobile topbar is on screen" *is* the definition.
    """
    return page.locator(MOBILE_TOPBAR).is_visible()


def open_mobile_nav(page, timeout: int = 5_000) -> None:
    """Tap the hamburger and block until the drawer is really open.

    Two waits, not one, on purpose. ``aria-expanded`` proves the tap reached
    React; the shell class proves the shell reflected it. A single wait on the
    class can report nothing better than "the drawer never opened" — which is
    the half of the answer that narrows nothing down, and is exactly the report
    CI produced for the landing-redirect race fixed in ``app.tsx``.
    """
    page.locator(HAMBURGER).click()
    page.wait_for_selector(f"{HAMBURGER}[aria-expanded='true']", timeout=timeout)
    page.wait_for_selector(f".app-shell.mobile-nav-open {SIDEBAR}", timeout=timeout)
    page.wait_for_timeout(400)  # let the slide-in settle


def open_simple_rail(page, timeout: int = 10_000) -> None:
    """Make the simple shell's bot rail CLICKABLE, at whichever viewport.

    Below the breakpoint the rail is an off-canvas drawer parked with
    ``transform: translateX(-100%)`` — which Playwright still reports as
    *visible*, because it has a box and is not hidden. So "is the rail visible"
    is the wrong question and produces the confusing failure it produced here:
    a row that "resolved" and was "visible, enabled and stable" and yet stayed
    "outside of the viewport" through thirty seconds of retried clicks.

    Ask the shell instead. The rail's own open button exists only in drawer
    mode, so its presence *is* the viewport test, and ``.simple-shell.rail-open``
    is the state to wait on.
    """
    opener = page.get_by_role("button", name="Show my agents")
    if opener.count() == 0:
        return  # wide viewport: the rail is a column and is already reachable
    if page.locator(f"{SIMPLE_ROOT}.rail-open").count() == 0:
        opener.first.click()
    page.wait_for_selector(f"{SIMPLE_ROOT}.rail-open", timeout=timeout)
    page.wait_for_timeout(400)  # let the slide-in settle


MORE_HEADER = f"{SIDEBAR} button.nav-group-header:not(.bot-section-header)"


def click_nav_link(page, href: str) -> None:
    """Click the sidebar link to *href*, expanding the More group if it is there.

    Every tab sits under **More** now (the agent list is the top of the nav),
    and More ships collapsed and renders its links only while open — so a test
    that navigates "through the SPA's own navigation" cannot assume
    `aside.sidebar a[href=…]` exists. The bot list's own section headers share
    the `nav-group-header` class, hence the `:not`.
    """
    link = page.locator(f"{SIDEBAR} a[href='{href}']")
    if link.count() == 0:
        header = page.locator(f"{MORE_HEADER}.collapsed")
        if header.count() > 0:
            header.first.click()
        link.first.wait_for(state="visible", timeout=10_000)
    link.first.click()


def close_mobile_nav(page, timeout: int = 5_000) -> None:
    """Tap the hamburger again and block until the drawer is fully gone.

    The hamburger stays on screen while the drawer is open — that is the point
    of starting the drawer below the topbar — so closing it is the same tap.
    """
    page.locator(HAMBURGER).click()
    page.wait_for_selector(f"{HAMBURGER}[aria-expanded='false']", timeout=timeout)
    page.wait_for_selector(".app-shell.mobile-nav-open", state="detached", timeout=timeout)
    page.wait_for_timeout(400)  # the hide is delayed by one transition


# ── the Files tree ───────────────────────────────────────────────────────────
# One tree for every agent the signed-in person can reach, every agent row
# collapsed on load (zero listing requests until someone opens one). Rows are
# WAI-ARIA tree items, not buttons, and carry their kind and agent as data
# attributes so a test can address "merli's workspace row" without matching on
# text a rename could change. Five suites drive this tree; the selectors and
# the open-it-up helper live here for the same reason the shell ones do.
TREE = "[role='tree']"
TREE_ROW = "[role='treeitem']"


def tree_row(page, name: str):
    """The tree row whose label contains *name* (a file, folder or agent)."""
    return page.locator(f"{TREE_ROW}:has-text('{name}')")


def agent_row(page, agent: str):
    return page.locator(f"{TREE_ROW}[data-kind='agent'][data-agent='{agent}']")


def workspace_row(page, agent: str):
    return page.locator(f"{TREE_ROW}[data-kind='workspace'][data-agent='{agent}']")


def open_agent_workspace(page, agent: str = "merli", timeout: int = 15_000) -> None:
    """Expand *agent* → workspace and block until its root listing has landed.

    Idempotent: a row already open is left alone, so this is safe after a
    navigation back to /files where the in-session expansion persisted. The
    final wait is on a row of the listing itself (an entry, or the "empty"
    marker) rather than on the caret flipping — the caret flips before the
    request is even sent.
    """
    a = agent_row(page, agent).first
    a.wait_for(state="visible", timeout=timeout)
    if a.get_attribute("aria-expanded") != "true":
        a.click()
    w = workspace_row(page, agent).first
    w.wait_for(state="visible", timeout=timeout)
    if w.get_attribute("aria-expanded") != "true":
        w.click()
    page.locator(
        f"{TREE_ROW}[data-kind='entry'][data-agent='{agent}'], "
        f"{TREE} [role='presentation']:not(:has(.spinner))"
    ).first.wait_for(state="visible", timeout=timeout)


ROUTE_CHECKS: list[tuple[str, str]] = [
    ("/overview", "SHELL"),
    ("/issues", "button:has-text('New issue')"),
    ("/revisions", "button:has-text('Snapshot now')"),
    # Notifications section — everything the deployment needs a human for. Both
    # breakpoints mount their own bell (CSS hides one), so the selector has to
    # be the visible one, not the first in the DOM.
    ("/overview", "section:has-text('Notifications')"),
    ("/overview", "button.notification-bell:visible"),
    # Insights toolbar — the selector proves the window picker rendered with the
    # rolling last-24h "1 day" option (hourly resolution), not just the shell.
    ("/insights", "button:has-text('1 day')"),
    # The sidebar's bot list (views/nav/BotNav.tsx) is NOT listed here: it lives
    # inside the sidebar, which is a closed drawer on mobile, and these checks
    # never open it. tests/e2e/test_bot_nav.py covers it at both viewports,
    # opening the drawer first where it has to.
    ("/chat", "SHELL"),
    # The header's history button — present at both viewports, and the ONLY way
    # to reach the thread list anywhere: the rail is an off-canvas drawer at
    # every viewport now, closed on arrival. A UI move that strands this button
    # strands the conversation history entirely, so it gets its own check.
    ("/chat", "[data-testid='chat-history-toggle']"),
    # Agents gallery — the selector proves the toolbar rendered (create button),
    # not just the shell. Scoped to the content area: the sidebar's bot list
    # carries its own "New agent" button on every route, and an unscoped
    # selector resolved to both — a strict-mode failure, not a rendered view.
    ("/agents", "main button:has-text('New agent')"),
    # Agent detail, Overview tab: the single largest editable view in the
    # dashboard, and until this entry the only one of the standing checks never
    # opened. The two selectors are per-option explanation items (`OptionNotes`
    # in views/agents/Toggle.tsx) under the Oversight card's Mode select and the
    # Environment card's Access select: they exist only once the agent's config
    # has loaded and the i18n strings resolved, so they prove the config surface
    # rendered, not that the detail shell mounted. `li:has-text` is unambiguous
    # because each notes list is flat. "merli" is the agent the harness seeds
    # into every test home (harness.seed_agent_configs).
    ("/agents/merli?tab=overview", "li:has-text('Asks only before moving money')"),
    ("/agents/merli?tab=overview", "li:has-text('No real disk at all')"),
    # Agent detail, Browser tab: the "browser is off" paragraph. A test home
    # leaves config.yaml → browser at its default (off), and that paragraph
    # renders only once GET /api/agents/{name}/browser answered with the
    # effective availability, so it proves the tab talked to the backend.
    ("/agents/merli?tab=browser", "[data-testid='browser-off']"),
    # Cortex: the settings card's live summary line, not the page shell. The
    # two-tier table it used to key on is behind that card, which is collapsed
    # by default — and the summary is better evidence anyway: it renders only
    # once GET /api/cognition/status has landed and resolved this agent's
    # engine state, so it proves the view mounted AND talked to the backend.
    ("/cortex", ".cortex-settings .cortex-settings-summary"),
    ("/scheduler", "SHELL"),
    ("/calendar", "SHELL"),
    # Files — the tree's agent row for the harness's one agent, collapsed: it
    # proves the roster reached the tree, not just that the shell mounted.
    ("/files", f"{TREE_ROW}[data-kind='agent'][data-agent='merli']"),
    # Notes wiki — the selector proves the view's own toolbar rendered (its
    # search box), not just the shell; present with or without agents.
    ("/notes", "input[placeholder='Search notes…']"),
    # Knowledge base — keyed on the search card's own subtitle rather than the
    # heading, which "Search" shares with the tab button and the card title.
    # This route had no check at all, so the view could be restructured (or
    # throw on mount) without anything noticing.
    ("/knowledge", "section.card:has-text('Semantic search across a store')"),
    # Admin Config view — the selector proves the Message rate limits section
    # (added with the per-agent/global rate-limit UI) mounted, not just the shell.
    ("/config", "section.card:has-text('Message rate limits')"),
    # The platform sign-in toggle's label. "Team accounts" is the deliberate
    # rename of the old "Multi-user mode" label (which collided with the
    # per-agent Multi-user tab); this check pins the new name so a regression
    # that reverts the label — resurrecting the collision — fails loudly.
    ("/config", "section.card:has-text('Team accounts')"),
    # The Playground opt-in. It sits in the same card as the sign-in toggle and
    # is the only control that decides whether the Playground tab and its route
    # exist on this box, so a UI move that strands it must fail here rather than
    # leave a default-off feature with no way to turn it on.
    ("/config", "section.card:has-text('Playground')"),
    # Default models section — the `default:*` alias targets (Turbo/Pro/Max).
    # Keyed on the Turbo target field label, unique to this card; a UI move
    # that strands it would leave the alias layer with no retarget control.
    ("/config", "section.card:has-text('Turbo target')"),
    # Speech (STT/TTS) settings section — provider dropdowns for voice-note
    # transcription / voice replies. Keyed on the card's own setting id: the
    # word "Speech" also appears in the Mobile app card's body, so a text
    # match resolves to two cards in strict mode.
    ("/config", "section.card[data-setting-id='config.speech']"),
    # Outbound access — the deployment-wide egress_allow / egress_deny lists.
    # Keyed on the section title; the two textareas inside are the only place
    # a box can set the deployment half of the lists without editing the file.
    ("/config", "section.card:has-text('Outbound access')"),
    # Media & search section — per-feature source pickers (vision / image /
    # web search) choosing the Olano managed service vs a BYOK provider model.
    # Keyed on the vision field label (unique to this card).
    ("/config", "section.card:has-text('Image understanding (vision)')"),
    # (No cost-display check: the unit money renders in is deployment policy,
    # so Config deliberately has no "Cost display" card to assert on.)
    # Disk monitor & cleanup section — thresholds + the allow-listed cleanup
    # action checkboxes (docker_prune shown with its "aggressive" badge).
    ("/config", "section.card:has-text('Disk monitor & cleanup')"),
    # Usage budgets — the token/cost ceilings a turn is refused against. Keyed
    # on the help text's own wording rather than the heading: the card is the
    # only place on the box these numbers can be seen or moved, and this line
    # renders only once the six rows resolved, so a card that mounted with
    # nothing to show fails here rather than looking fine.
    ("/config", "section.card:has-text('runaway-loop guards')"),
    # MCP connection health — the token-renewal / auto-reconnect knobs. Keyed on
    # a field label rather than the heading: "MCP connection health" would also
    # match a future card that merely mentions it, and this proves the controls
    # themselves rendered.
    ("/config", "section.card:has-text('Renew when under')"),
    # Observability (Langfuse): the key-source line renders only once
    # GET /api/observability/langfuse/status answered, so a section that
    # mounted without its status fails here instead of looking fine.
    ("/config", "[data-testid='observability-key-source']"),
    # The agent's Observability card on its Overview tab: the "In effect now"
    # line renders only once the links endpoint resolved the agent's policy.
    ("/agents/merli?tab=overview", "[data-testid='observability-agent-effective']"),
    # Sandbox resources — the per-agent CPU/RAM ceilings for the Docker sandbox
    # agents' shell commands run in. Keyed on the summary line's own wording
    # rather than the heading or a field label: it is unique to this card, and
    # it only renders once the limits actually resolved, so a card that mounted
    # with no answer to show fails here instead of looking fine.
    ("/config", 'section.card:has-text("each agent\'s sandbox is capped at")'),
    # Cloud catalogs — per-catalog sync status chips + the "Sync now" button
    # (the only user-facing way to refresh catalogs without SSH; the official
    # connector buttons on Connections depend on this catalog being current).
    ("/config", "section.card:has-text('Cloud catalogs')"),
    # Magic links. TWO cards on Config share the heading "Magic links" — the
    # mint/list/revoke section (moved here from Connections in #228, "its
    # natural admin home") sits directly above the enable/TTL/PIN settings —
    # so each is keyed on body text unique to it rather than on the heading,
    # which matches both and trips Playwright's strict mode.
    #
    # The mint UI. Admin-only, so it is present exactly when the checks run as
    # one (the default). Its "Send a magic link" button is conditional on
    # minting being available, so anchor on the always-rendered hint instead.
    ("/config", "section.card:has-text('they open on their own phone')"),
    # TTL / PIN policy for those links (olano/magic_links).
    ("/config", "section.card:has-text('Link lifetime')"),
    # Connections tab — proves the generalized connector catalog rendered
    # (a generic-provider card, not just the legacy Google one). The magic-link
    # section is deliberately NOT checked here any more; see above.
    ("/connections", ".card:has-text('Microsoft 365')"),
    # Providers: on a self-hosted (open) box the Olano card carries the paste
    # form for an Olano account API key (/api/olano/credits-key). The test
    # runtime is open and the checks run as an admin, so the form must mount;
    # a managed deployment never renders it, which e2e/test_providers_model_chips
    # pins separately.
    ("/providers", "[data-testid='olano-credits-key']"),
    # GitHub is one connector with two sign-in methods (an OAuth app, or a
    # one-time code typed on GitHub's device page) since the separate
    # "GitHub CLI" card was retired. Keyed on the name, which the card shows
    # in the default system scope whatever the runtime's connection state.
    ("/connections", ".card:has-text('GitHub')"),
    # Agent to Agent — the selector proves the conversation rail mounted (one
    # row per agent pair), not just the page shell.
    ("/activity", "[data-testid='a2a-rail']"),
    # Admin Config view — the External access section (inbound MCP / A2A / OAuth
    # switches + token list) mounted, not just the shell.
    ("/config", "section.card:has-text('External access')"),
    # Users & access — the team roster plus the agent-major access table (one
    # row per agent, owners/users columns). The roster selector keys on the
    # invite form rather than the word "Users", which since the access table
    # gained a USERS column is no longer unique to that section. The table
    # selector proves the access section mounted its data, not just a heading.
    ("/admin", "section.card:has-text('Invite a teammate')"),
    ("/admin", "table.access-table"),
    # Deployment entitlements — posture, limits vs usage, feature/tab toggles.
    # Unmounted for a while on the reasoning that plan limits were practically
    # unlimited; one then bit, invisibly. Keyed on the posture badge, which is
    # the card's own data rather than a heading a future card could share.
    ("/admin", "[data-testid='deployment-posture']"),
]


def check_id(suffix: str, selector: str) -> str:
    """A short, stable pytest id — several checks share one route."""
    return f"{suffix.lstrip('/')}:{selector[:40]}"
