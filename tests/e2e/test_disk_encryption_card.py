"""E2E: the Config page's Disk encryption card on a sealed box.

The card exists only on a managed box provisioned with a sealed data volume
(``/api/status`` custody mode other than ``none``), which is why it has no
entry in ``ROUTE_CHECKS``: that list runs in the default unsealed state, where
the card must be absent (the second test pins exactly that). This module makes
the box sealed for its own duration by pointing the runtime's seal state at a
file shaped like the sealer writes it (sealer 12 and later). The E2E runtime
runs in this process and resolves that path from the
environment on every read, so a module-scoped ``monkeypatch.setenv`` reaches
it; the boot guard that would refuse a home off the sealed volume runs only in
``RuntimeServer.start``, never in the harness's ``create_api`` path.

There is no sealer behind the harness, so ``GET /api/seal/state`` (which Caddy
would proxy to the sealer's socket on a real box) is a runtime 404 here and
the card must render its "unavailable" state for the encryption service while
still showing everything ``/api/status`` knows. That is also the state a real
box shows while its serve unit is down, so it is worth a check of its own.

The ceremony itself is driven against a **fake sealer installed in the browser**
(Playwright's request interception on ``/api/seal/*``): the same JSON the real
``olano-sealer serve`` answers, with a one-key state machine. Nothing reaches
the runtime, which is exactly the real topology (the runtime never sees the
ceremony); what the test proves is the dialog, the card and the API client's
handling of the sealer's own error shape, end to end in a real browser.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from olano.runtime.seal_guard import SEAL_STATE_ENV
from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

LUKS_UUID = "7750f6c5-46d0-4dc4-a681-a31e944e421c"
CARD = "section.card:has-text('Disk encryption')"
# `/api/status` caches the custody half for 5 s; a card that appears within
# that window plus a page load is on time, so the waits below allow for it.
CUSTODY_CACHE_S = 5.0


def _custody_mode(dashboard: harness.RunningDashboard) -> str:
    import urllib.request

    with urllib.request.urlopen(dashboard.runtime_url + "/api/status", timeout=5) as resp:
        return str(json.load(resp).get("custody", {}).get("mode"))


def _wait_for_custody(dashboard: harness.RunningDashboard, mode: str) -> None:
    deadline = time.monotonic() + CUSTODY_CACHE_S + 5
    while time.monotonic() < deadline:
        if _custody_mode(dashboard) == mode:
            return
        time.sleep(0.25)
    raise AssertionError(f"/api/status custody never read {mode!r}")


@pytest.fixture
def sealed_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dashboard: harness.RunningDashboard
) -> Iterator[Path]:
    """Make the running box read as sealed (sealer-12-shaped seal.json), then
    put it back and wait until /api/status agrees, so no later test sees the card."""
    state = tmp_path / "seal.json"
    state.write_text(
        json.dumps(
            {
                "mode": "standard",
                "luks_uuid": LUKS_UUID,
                "backing": "image",
                "provisioned_at": "2026-09-24T06:10:00Z",
                "sealer_version": "12",
                "recovery": {"status": "none", "at": None, "slot": None},
                "header_generation": 1,
                "serve_socket": "/run/olano-sealer/serve.sock",
            }
        ),
        encoding="utf-8",
    )
    with monkeypatch.context() as mp:
        mp.setenv(SEAL_STATE_ENV, str(state))
        _wait_for_custody(dashboard, "standard")
        yield state
    _wait_for_custody(dashboard, "none")


def _open_config(page, dashboard: harness.RunningDashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/config", wait_until="domcontentloaded")
    wait_for_app_shell(page)


def test_sealed_box_shows_the_card_with_its_rows_and_the_service_unavailable(
    page, dashboard: harness.RunningDashboard, sealed_box: Path
) -> None:
    _open_config(page, dashboard)
    card = page.locator(CARD)
    card.wait_for(state="visible", timeout=15_000)

    # What /api/status knows: the custody claim, the volume, the backup.
    card.get_by_text("Standard custody", exact=True).wait_for(state="visible", timeout=10_000)
    assert card.get_by_text(LUKS_UUID).count() == 1
    assert card.get_by_text("Generation 1").count() == 1
    # The one action the card asks of the owner exists and is the create one:
    # no key yet, so no "Replace" and no "Cancel pending".
    create = card.get_by_role("button", name="Create recovery key")
    create.wait_for(state="visible", timeout=10_000)
    assert card.get_by_role("button", name="Replace recovery key").count() == 0
    assert card.get_by_role("button", name="Cancel pending key").count() == 0
    # No sealer answers /api/seal/state in the harness: the service row must
    # say so rather than hide the card or leave a spinner.
    card.get_by_text("Unavailable", exact=True).wait_for(state="visible", timeout=10_000)


def test_the_card_is_absent_on_an_unsealed_box(page, dashboard: harness.RunningDashboard) -> None:
    """Every legacy and self-hosted install, and the default E2E state."""
    assert _custody_mode(dashboard) == "none"
    _open_config(page, dashboard)
    # Prove the page rendered its cards before asserting an absence.
    page.locator("section.card:has-text('Cloud catalogs')").wait_for(
        state="visible", timeout=15_000
    )
    assert page.locator(CARD).count() == 0


# ── the ceremony against a fake sealer ───────────────────────────────────────

# 32 base32 characters, the sealer's own alphabet (A-Z, 2-7), shown dashed.
FAKE_KEY = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U"  # gitleaks:allow
FAKE_KEY_DASHED = "-".join(FAKE_KEY[i : i + 4] for i in range(0, 32, 4))


def install_fake_sealer(page) -> dict[str, Any]:
    """Answer ``/api/seal/*`` in the browser the way ``olano-sealer serve`` does.

    Returns the mutable state the handler reads, so a test can assert what the
    dialog sent (``calls``) and what the sealer now holds (``recovery``).
    """
    state: dict[str, Any] = {
        "recovery": {"status": "none"},
        "generation": 1,
        "calls": [],
    }

    def refuse(route, status: int, code: str, message: str) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps({"error": code, "message": message}),
        )

    def ok(route, doc: dict[str, Any]) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(doc))

    def handle(route) -> None:
        req = route.request
        path = re.sub(r"^https?://[^/]+", "", req.url).split("?", 1)[0]
        body = req.post_data_json if req.method == "POST" else None
        state["calls"].append((req.method, path, body))
        rec = state["recovery"]
        if req.method == "GET" and path == "/api/seal/state":
            return ok(
                route,
                {
                    "mode": "standard",
                    "luks_uuid": LUKS_UUID,
                    "sealer_version": "12",
                    "recovery": rec,
                    "header": {
                        "generation": state["generation"],
                        "uploaded_at": "2026-09-24T06:13:00Z",
                        "pending_upload": False,
                    },
                },
            )
        if path == "/api/seal/recovery/start":
            if rec["status"] == "pending":
                return refuse(route, 409, "pending", "a recovery key is already shown")
            replaces = rec.get("slot") if rec["status"] == "acknowledged" else None
            pending = {
                "status": "pending",
                "slot": 2 if replaces == 1 else 1,
                "at": "2026-09-24T10:00:00Z",
                "expires_at": "2026-09-24T10:15:00Z",
                "op_id": "0123456789abcdef",
                "replaces_slot": replaces,
            }
            state["recovery"] = pending
            state["generation"] += 1
            return ok(
                route,
                {
                    "op_id": pending["op_id"],
                    "key": FAKE_KEY_DASHED,
                    "slot": pending["slot"],
                    "expires_at": pending["expires_at"],
                    "expires_in": 900,
                    "replaces_slot": replaces,
                },
            )
        if path == "/api/seal/recovery/confirm":
            if rec["status"] != "pending":
                return refuse(route, 409, "no_pending", "no recovery key is waiting")
            if (body or {}).get("op_id") != rec["op_id"]:
                return refuse(route, 409, "op_mismatch", "not the operation in progress")
            typed = re.sub(r"[-\s]", "", str((body or {}).get("key") or "")).upper()
            if not re.fullmatch(r"[A-Z2-7]{32}", typed):
                return refuse(route, 400, "invalid_key", "32 letters and digits")
            if typed != FAKE_KEY:
                return refuse(route, 400, "mismatch", "that is not the key that was shown")
            state["recovery"] = {
                "status": "acknowledged",
                "slot": rec["slot"],
                "at": "2026-09-24T10:03:00Z",
            }
            return ok(
                route,
                {
                    "status": "acknowledged",
                    "slot": rec["slot"],
                    "at": "2026-09-24T10:03:00Z",
                    "replaced_slot": rec.get("replaces_slot"),
                },
            )
        if path == "/api/seal/recovery/cancel":
            if rec["status"] != "pending":
                return refuse(route, 409, "no_pending", "no recovery key is waiting")
            state["recovery"] = {"status": "none"}
            return ok(route, {"status": "cancelled", "slot": rec["slot"]})
        return refuse(route, 404, "not_found", "no such route")

    page.route("**/api/seal/**", handle)
    return state


def test_the_ceremony_end_to_end_against_a_fake_sealer(
    page, dashboard: harness.RunningDashboard, sealed_box: Path
) -> None:
    """Create a key, mistype it once, confirm it, and see the card follow.

    The key is asserted to appear in exactly one place on screen (the show
    step) and to be sent back normalised: what the owner typed carries dashes,
    spaces and lower case, what the sealer receives is the 32-character form.
    """
    sealer = install_fake_sealer(page)
    _open_config(page, dashboard)
    card = page.locator(CARD)
    card.wait_for(state="visible", timeout=15_000)
    # The fake sealer answers, so the service row is healthy and the action live.
    card.get_by_text("Sealer 12", exact=True).wait_for(state="visible", timeout=10_000)
    card.get_by_text("No recovery key yet", exact=True).wait_for(state="visible", timeout=10_000)

    card.get_by_role("button", name="Create recovery key").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible", timeout=10_000)
    dialog.get_by_role("button", name="Generate the key").click()

    shown = page.get_by_test_id("recovery-key-shown")
    shown.wait_for(state="visible", timeout=10_000)
    assert shown.inner_text().strip() == FAKE_KEY_DASHED
    # Nowhere else: the card behind the dialog never learns the key.
    assert page.get_by_text(FAKE_KEY_DASHED).count() == 1
    next_button = dialog.get_by_role("button", name="Next: confirm it")
    assert next_button.is_disabled()
    page.get_by_test_id("recovery-key-stored").check()
    next_button.click()

    field = page.get_by_test_id("recovery-key-input")
    field.wait_for(state="visible", timeout=10_000)
    # A typo first: the sealer refuses, the step stays, the expiry is named.
    field.fill("mfrg gzdf mztw q2lk nnwg 23tp obyx e43a")
    dialog.get_by_role("button", name="Confirm").click()
    dialog.get_by_text("That is not the key that was shown").wait_for(
        state="visible", timeout=10_000
    )
    assert sealer["recovery"]["status"] == "pending"
    # Then the right one, typed carelessly.
    field.fill(" mfrg-gzdf mztw q2lk\tnnwg 23tp obyx e43u ")
    dialog.get_by_role("button", name="Confirm").click()
    dialog.get_by_text("Recovery key active", exact=True).wait_for(state="visible", timeout=10_000)
    assert sealer["recovery"]["status"] == "acknowledged"
    confirms = [c for c in sealer["calls"] if c[1] == "/api/seal/recovery/confirm"]
    assert [c[2]["key"] for c in confirms] == [FAKE_KEY[:-1] + "A", FAKE_KEY]
    assert all(c[2]["op_id"] == "0123456789abcdef" for c in confirms)

    # The footer's Close, not the header's X (both are named "Close").
    dialog.locator(".modal-foot").get_by_role("button", name="Close").click()
    dialog.wait_for(state="hidden", timeout=10_000)
    # The card re-reads the sealer: active key, replace offered, create gone.
    card.get_by_test_id("disk-encryption-recovery").get_by_text("Active", exact=True).wait_for(
        state="visible", timeout=10_000
    )
    assert card.get_by_text(re.compile(r"^Active since ")).count() == 1
    assert card.get_by_role("button", name="Replace recovery key").count() == 1
    assert card.get_by_role("button", name="Create recovery key").count() == 0
    assert card.get_by_text("Generation 2").count() == 1


def test_closing_the_dialog_with_a_key_shown_cancels_it_after_asking(
    page, dashboard: harness.RunningDashboard, sealed_box: Path
) -> None:
    """A key that was shown and abandoned must not stay pending in a keyslot."""
    sealer = install_fake_sealer(page)
    _open_config(page, dashboard)
    card = page.locator(CARD)
    card.wait_for(state="visible", timeout=15_000)
    card.get_by_role("button", name="Create recovery key").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Generate the key").click()
    page.get_by_test_id("recovery-key-shown").wait_for(state="visible", timeout=10_000)
    assert sealer["recovery"]["status"] == "pending"

    dialog.get_by_role("button", name="Cancel").click()
    # The confirm prompt names the consequence and offers to keep going.
    prompt = page.get_by_role("alertdialog")
    prompt.wait_for(state="visible", timeout=10_000)
    prompt.get_by_role("button", name="Keep going").click()
    page.get_by_test_id("recovery-key-shown").wait_for(state="visible", timeout=10_000)
    assert sealer["recovery"]["status"] == "pending"

    dialog.get_by_role("button", name="Cancel").click()
    prompt = page.get_by_role("alertdialog")
    prompt.wait_for(state="visible", timeout=10_000)
    prompt.get_by_role("button", name="Cancel the key").click()
    page.get_by_test_id("recovery-key-shown").wait_for(state="hidden", timeout=10_000)
    assert sealer["recovery"]["status"] == "none"
    assert any(c[1] == "/api/seal/recovery/cancel" for c in sealer["calls"])
    card.get_by_text("No recovery key yet", exact=True).wait_for(state="visible", timeout=10_000)
