"""E2E: opening a skill from the Skills tab actually shows what it says.

The tab used to be a dead list — a name and one line of description per skill,
with no way to read the SKILL.md behind it or see the scripts and forms bundled
with it. Sending people to the Files view could not fix it either: the common
pack lives in ``<home>/skills``, outside every agent workspace, where
``/api/files`` refuses to look.

So these tests pin the two halves of the fix at both viewports: every row says
where its skill lives and whether it can be edited, and clicking one opens a
viewer with the rendered SKILL.md, the bundled files, and — only for a skill
inside the workspace — a way through to the Files view.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"
# Body text unique to each skill, so a passing assertion proves the *file* was
# read rather than the list row echoed back.
MANAGED_BODY = "Runs the shared quarterly close."
OWN_BODY = "Files this agent's expense reports."
FORM_BODY = "field: reimbursement_total"
# Filler skills, so the list crosses the threshold where the search box appears
# (SKILLS_SEARCH_THRESHOLD = 8). Without them the search + scoped-bulk tests
# would silently exercise nothing.
FILLERS = [f"filler-{i}" for i in range(1, 9)]
ALL_SKILLS = ["quarterly-close", "expense-filing", *FILLERS]
# `filler-1` gets a real-world-length description. Skill descriptions are
# written for the model, and the shipped `xlsx` one runs to ~90 words — the row
# has to survive that without taking over the list.
LONG_DESCRIPTION = (
    "Use this skill any time a spreadsheet file is the primary input or output. This "
    "means any task where the user wants to open, read, edit, or fix an existing .xlsx, "
    ".xlsm, .csv, or .tsv file (adding columns, computing formulas, formatting, charting, "
    "cleaning messy data); create a new spreadsheet from scratch or from other data "
    "sources; or convert between tabular file formats. Trigger especially when the user "
    "references a spreadsheet file by name or path, even casually, and wants something "
    "done to it or produced from it."
)


@pytest.fixture(scope="module")
def skills_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _skills_on_disk(lionclaw_home):
    """One managed skill and one workspace skill, each bundling a second file.

    The managed one goes in ``<home>/skills`` — the pack every agent shares, and
    the folder the Files view cannot reach, which is the whole reason the viewer
    has its own read path.
    """
    workspace = lionclaw_home / "workspace"

    managed = lionclaw_home / "skills" / "quarterly-close"
    managed.mkdir(parents=True, exist_ok=True)
    (managed / "SKILL.md").write_text(
        f"---\nname: quarterly-close\ndescription: Shared close checklist.\n---\n\n{MANAGED_BODY}\n",
        encoding="utf-8",
    )
    (managed / "checklist.md").write_text("# Checklist\n", encoding="utf-8")

    own = workspace / "skills" / "expense-filing"
    (own / "forms").mkdir(parents=True, exist_ok=True)
    (own / "SKILL.md").write_text(
        f"---\nname: expense-filing\ndescription: Expense reports.\n---\n\n{OWN_BODY}\n",
        encoding="utf-8",
    )
    (own / "forms" / "claim.yaml").write_text(f"{FORM_BODY}\n", encoding="utf-8")

    for name in FILLERS:
        d = workspace / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        description = LONG_DESCRIPTION if name == "filler-1" else f"Placeholder {name}."
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
            encoding="utf-8",
        )

    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "workspace": str(workspace),
                "backend": {"type": "filesystem", "root_dir": str(workspace)},
            }
        ),
        encoding="utf-8",
    )
    return lionclaw_home


@pytest.fixture(params=["desktop", "mobile"])
def viewport(request):
    return request.param


@pytest.fixture
def skills_page(skills_dashboard, _skills_on_disk, viewport):
    """The agent detail page on its Skills tab, sized for ``viewport``."""
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        page = ctx.new_page()
        try:
            page.goto(
                f"{skills_dashboard.base_url}/agents/{AGENT}?tab=skills",
                wait_until="domcontentloaded",
            )
            page.locator(".skill-row").first.wait_for(state="visible", timeout=30_000)
            yield page, viewport
        finally:
            ctx.close()
            browser.close()


def _row(page, name: str):
    return page.locator(".skill-row").filter(has_text=name).first


def _open(page, name: str):
    """Click a skill row and block until its viewer has really loaded.

    The dialog frame mounts before ``/api/catalog/skills/detail`` answers, so
    waiting on the dialog alone reads an empty body — assert against the file
    list, which only exists once the response landed.
    """
    _row(page, name).click()
    dialog = page.locator("[role='dialog']")
    dialog.wait_for(state="visible", timeout=10_000)
    dialog.locator("button:has-text('SKILL.md')").first.wait_for(state="visible", timeout=15_000)
    return dialog


def test_every_row_says_where_its_skill_lives(skills_page) -> None:
    """The labels the list was missing: scope, plus editable vs read-only.

    Asserted per row rather than per page — the point is that the *managed* one
    reads read-only while the agent's own reads editable, which a page-wide
    text search would satisfy with either badge appearing anywhere.
    """
    page, _ = skills_page

    managed = _row(page, "quarterly-close")
    assert "Common skill" in managed.inner_text()
    assert "Read-only" in managed.inner_text()

    own = _row(page, "expense-filing")
    assert "Agent skill" in own.inner_text()
    assert "Editable" in own.inner_text()


def test_opening_a_managed_skill_shows_its_body_and_bundled_files(skills_page) -> None:
    """The gap this closes: <home>/skills is unreachable from the Files view."""
    page, _ = skills_page
    dialog = _open(page, "quarterly-close")
    # SKILL.md renders on open — no second click to read the skill itself.
    assert MANAGED_BODY in dialog.inner_text()
    # …and the file beside it is offered.
    assert dialog.locator("button:has-text('checklist.md')").count() == 1
    # A skill the dashboard cannot write says so instead of offering a dead link.
    assert dialog.locator("a:has-text('Open in Files')").count() == 0


def test_opening_a_bundled_file_previews_it(skills_page) -> None:
    """A skill is its scripts and forms too, not just its SKILL.md."""
    page, _ = skills_page
    dialog = _open(page, "expense-filing")
    assert OWN_BODY in dialog.inner_text()

    dialog.locator("button:has-text('claim.yaml')").click()
    page.wait_for_timeout(600)
    assert FORM_BODY in dialog.inner_text()


def test_a_workspace_skill_links_through_to_files(skills_page) -> None:
    """Editing stays with the Files view, which owns the write path."""
    page, _ = skills_page
    dialog = _open(page, "expense-filing")
    link = dialog.locator("a:has-text('Open in Files')")
    assert link.count() == 1
    href = link.first.get_attribute("href") or ""
    assert "path=skills%2Fexpense-filing" in href
    assert f"agent={AGENT}" in href


def test_the_viewer_fits_the_viewport(skills_page) -> None:
    """A two-pane dialog must become one pane on a phone, not pan sideways."""
    page, _ = skills_page
    dialog = _open(page, "expense-filing")
    width = page.evaluate("() => window.innerWidth")
    box = dialog.first.bounding_box()
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= width + 1


def test_the_document_gets_room_to_be_read_in(skills_page) -> None:
    """A SKILL.md is a document, so the viewer is sized to read one.

    It first shipped as a 900px dialog around a fixed 420px pane, which is a
    peephole for the thing the whole feature exists to show. Assert the
    rendered geometry rather than the CSS that produces it: a later layout may
    reach these another way, but it must still reach them.
    """
    page, viewport = skills_page
    dialog = _open(page, "quarterly-close")
    doc = page.locator("[data-testid='skill-doc']").first
    doc.wait_for(state="visible", timeout=10_000)

    # Reading type, not UI type — at both viewports.
    font = page.evaluate("el => parseFloat(getComputedStyle(el).fontSize)", doc.element_handle())
    assert font >= 14, f"the document renders at {font}px"

    if viewport == "desktop":
        box = dialog.first.bounding_box()
        assert box is not None
        vh = page.evaluate("() => window.innerHeight")
        vw = page.evaluate("() => window.innerWidth")
        # The dialog takes the screen it can have instead of shrink-wrapping a
        # short file, so a long SKILL.md scrolls in a tall pane.
        assert box["height"] > vh * 0.75, f"dialog is only {box['height']}px of {vh}px"
        assert box["width"] > vw * 0.6, f"dialog is only {box['width']}px of {vw}px"


def test_the_switch_turns_a_skill_off_for_this_agent_without_deleting_it(skills_page) -> None:
    """The per-skill on/off switch, end to end through a real save.

    Worth an e2e rather than an API test: the switch is a second control inside
    a row that is itself a button, so a regression here is not a wrong value —
    it is a click that opens the viewer instead of flipping the switch, which no
    in-process test can see.
    """
    from playwright.sync_api import expect

    page, _ = skills_page

    # Scoped to THIS row's switch throughout: the list is not sorted, so a
    # page-wide `button[role='switch']` would assert against whichever skill
    # happened to render first.
    row = page.locator(".skill-card").filter(has_text="quarterly-close").first
    switch = row.locator("button[role='switch']").first
    expect(switch).to_have_attribute("aria-checked", "true")

    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false", timeout=10_000)
    # The row must not have opened the viewer: the switch is a sibling of the
    # open button, not nested inside it.
    assert page.locator("[role='dialog']").count() == 0
    # Off is a visible state, not a silent one — otherwise "why is it ignoring
    # that skill" has no answer on this page. And it is still listed: a
    # disabled skill that vanished could never be switched back on.
    expect(row).to_contain_text("Off")
    expect(row).to_contain_text("quarterly-close")

    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true", timeout=10_000)
    expect(row).not_to_contain_text("Off")


def _capture_saves(page):
    """Collect every agent-config PUT the page makes, newest last.

    Asserting on the request payload rather than on a reloaded page is
    deliberate: a reload races the debounce window (a hard reload inside it
    drops the pending write, exactly as it does for the MCP tool switches), so a
    reload-based assertion would be testing the harness's timing, not the
    coalescing this exists to pin.
    """
    saves = []

    def on_request(request):
        if request.method == "PUT" and "/api/agents/" in request.url:
            saves.append(request)

    page.on("request", on_request)
    return saves


def _settle(page):
    """Wait past the write debounce and its round trip."""
    page.wait_for_timeout(1500)


def test_flipping_several_switches_fast_keeps_every_one(skills_page) -> None:
    """The race the debounce exists to prevent.

    Writes used to fire per click, each building its denylist from the value
    rendered at click time — so two quick clicks both started from the pre-burst
    list and the second silently dropped the first. Clicking both switches
    inside the debounce window is exactly that case: it must produce ONE write,
    and that write must carry BOTH names.
    """
    from playwright.sync_api import expect

    page, _ = skills_page
    saves = _capture_saves(page)

    first = page.locator(".skill-card").filter(has_text="quarterly-close").first
    second = page.locator(".skill-card").filter(has_text="expense-filing").first
    first.locator("button[role='switch']").first.click()
    second.locator("button[role='switch']").first.click()

    expect(first.locator("button[role='switch']").first).to_have_attribute(
        "aria-checked", "false", timeout=10_000
    )
    expect(second.locator("button[role='switch']").first).to_have_attribute(
        "aria-checked", "false", timeout=10_000
    )
    _settle(page)

    assert len(saves) == 1, f"a burst must coalesce into one write, got {len(saves)}"
    assert set(saves[0].post_data_json["disabled_skills"]) == {
        "quarterly-close",
        "expense-filing",
    }, "the second click built on the first, not on the pre-burst list"


def test_bulk_switches_act_on_every_listed_row(skills_page) -> None:
    """ "Turn all off" / "Turn all on" — one write covering rows that may be
    scrolled out of view, which is why the action announces itself."""
    from playwright.sync_api import expect

    page, _ = skills_page
    saves = _capture_saves(page)

    page.get_by_role("button", name="Turn all off").click()
    for name in ("quarterly-close", "expense-filing"):
        expect(
            page.locator(".skill-card").filter(has_text=name).locator("button[role='switch']").first
        ).to_have_attribute("aria-checked", "false", timeout=10_000)
    _settle(page)
    assert len(saves) == 1
    # Every listed skill, in one write — including the rows below the fold.
    assert set(saves[0].post_data_json["disabled_skills"]) == set(ALL_SKILLS)

    page.get_by_role("button", name="Turn all on").click()
    for name in ("quarterly-close", "expense-filing"):
        expect(
            page.locator(".skill-card").filter(has_text=name).locator("button[role='switch']").first
        ).to_have_attribute("aria-checked", "true", timeout=10_000)
    _settle(page)
    assert len(saves) == 2
    assert saves[1].post_data_json["disabled_skills"] == []


def test_a_switch_survives_a_reload(skills_page) -> None:
    """The write really reaches agent.json5, not just the optimistic overlay."""
    from playwright.sync_api import expect

    page, _ = skills_page
    row = page.locator(".skill-card").filter(has_text="quarterly-close").first
    row.locator("button[role='switch']").first.click()
    expect(row.locator("button[role='switch']").first).to_have_attribute(
        "aria-checked", "false", timeout=10_000
    )
    # Settle before reloading: a hard reload inside the debounce window drops
    # the pending write by design (same as the MCP tool switches), so reloading
    # immediately would be asserting on a race rather than on persistence.
    _settle(page)

    page.reload(wait_until="domcontentloaded")
    page.locator(".skill-row").first.wait_for(state="visible", timeout=30_000)
    reloaded = page.locator(".skill-card").filter(has_text="quarterly-close").first
    expect(reloaded.locator("button[role='switch']").first).to_have_attribute(
        "aria-checked", "false", timeout=10_000
    )
    expect(reloaded).to_contain_text("Off")


def test_search_filters_the_list_and_scopes_the_bulk_buttons(skills_page) -> None:
    """With a search active, a bulk button that reached rows the filter hides
    would be the worst kind of surprise — so it acts on the results only."""
    from playwright.sync_api import expect

    page, _ = skills_page
    saves = _capture_saves(page)

    # The box exists because the fixture seeds past the threshold; a listing
    # that fell back under it would fail here rather than skip silently.
    expect(page.locator(".skill-card")).to_have_count(len(ALL_SKILLS))
    search = page.get_by_role("searchbox")

    search.fill("expense")
    expect(page.locator(".skill-card")).to_have_count(1)

    page.get_by_role("button", name="Turn all off").click()
    _settle(page)
    assert saves, "the scoped bulk action wrote nothing"
    # Only the filtered row — the eight it is hiding are untouched.
    assert saves[-1].post_data_json["disabled_skills"] == ["expense-filing"]

    search.fill("")
    expect(page.locator(".skill-card")).to_have_count(len(ALL_SKILLS))


def test_a_long_description_does_not_take_over_the_row(skills_page) -> None:
    """Descriptions are written for the model and run to a paragraph.

    Unclamped, one row filled a phone screen and the list stopped being
    scannable. Asserted against the row carrying a real-length description, and
    on the RENDERED height rather than the CSS that produces it — a later layout
    may clamp another way, but it must still clamp.
    """
    page, viewport = skills_page
    row = page.locator(".skill-card").filter(has_text="filler-1").first
    row.scroll_into_view_if_needed()
    box = row.bounding_box()
    assert box is not None
    # Two clamped lines plus the title row and padding; a phone wraps wider.
    cap = 200 if viewport == "mobile" else 160
    assert box["height"] < cap, (
        f"a {len(LONG_DESCRIPTION)}-char description renders {box['height']}px tall"
    )
    # And it is genuinely clipped, not merely narrow. `line-clamp` truncates
    # visually and leaves the full text in the DOM — which is what lets the row
    # stay searchable — so the signal is the overflow, not the text content.
    desc = row.locator(".skill-desc").first
    clipped = desc.evaluate("el => el.scrollHeight > el.clientHeight + 1")
    assert clipped, "the long description is not being clamped"
    # The card never pushes the page sideways at either viewport.
    width = page.evaluate("() => window.innerWidth")
    assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
