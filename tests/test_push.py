"""Push notifications to the mobile app (olano/push/, routes/push.py).

What is pinned here:

* the device registry keys on the token, moves a phone between people, caps
  the fan-out and is written 0600;
* the dispatcher batches inside the relay's limits, prunes the tokens the relay
  reports dead, and turns every relay failure into a reason instead of raising;
* each producer reaches the right people with no user content unless
  ``push.previews`` is on, and does nothing at all outside the server;
* a chat turn pushes its reply and its approvals only when nobody is watching
  the turn's stream.
"""

from __future__ import annotations

import json
import stat
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from olano.push import EVENT_KINDS
from olano.push import events as push_events
from olano.push.dispatcher import (
    MAX_DEVICES_PER_NOTIFICATION,
    MAX_NOTIFICATIONS,
    MAX_SENDS_PER_REQUEST,
    PushDispatcher,
    PushMessage,
    _batches,
    get_push_dispatcher,
    set_push_dispatcher,
)
from olano.push.store import (
    MAX_DEVICES_PER_USER,
    Device,
    DeviceStore,
    InvalidDevice,
    get_device_store,
)

APNS_TOKEN = "a" * 64
FCM_TOKEN = "fcm-token_abc:DEF-1234567890"


@pytest.fixture(autouse=True)
def _no_dispatcher():
    from olano.core.runs import REGISTRY

    set_push_dispatcher(None)
    REGISTRY.reset()
    yield
    set_push_dispatcher(None)
    REGISTRY.reset()


class RecordingDispatcher:
    """Stands in for the running dispatcher: records what producers submit."""

    def __init__(self) -> None:
        self.messages: list[PushMessage] = []

    def submit(self, message: PushMessage) -> bool:
        self.messages.append(message)
        return True

    def status(self) -> dict[str, Any]:
        return {"running": True, "last_error": None, "last_sent_at": None}

    def stop(self, timeout: float = 0) -> None:
        pass


@pytest.fixture
def recorder() -> RecordingDispatcher:
    rec = RecordingDispatcher()
    set_push_dispatcher(rec)  # type: ignore[arg-type]
    return rec


# ── store ────────────────────────────────────────────────────────────────────


class TestDeviceStore:
    def test_register_is_keyed_on_the_token(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        first = store.register(user_id="u1", token=APNS_TOKEN, platform="ios", name="Phone")
        again = store.register(user_id="u1", token=APNS_TOKEN, platform="ios", app_version="1.1")
        assert again.id == first.id
        assert len(store.all()) == 1
        assert again.name == "Phone", "an empty name on refresh keeps the old one"
        assert again.app_version == "1.1"

    def test_the_same_phone_signing_in_as_someone_else_moves_with_fresh_opt_ins(
        self, tmp_path
    ) -> None:
        store = DeviceStore(tmp_path / "push.json")
        store.register(user_id="u1", token=APNS_TOKEN, platform="ios", events=["alerts"])
        moved = store.register(user_id="u2", token=APNS_TOKEN, platform="ios")
        assert moved.user_id == "u2"
        assert moved.events == list(EVENT_KINDS)
        assert store.for_user("u1") == []

    def test_rejects_what_no_provider_would_issue(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        with pytest.raises(InvalidDevice):
            store.register(user_id="u1", token="short", platform="ios")
        with pytest.raises(InvalidDevice):
            store.register(user_id="u1", token="x" * 20 + " <script>", platform="ios")
        with pytest.raises(InvalidDevice):
            store.register(user_id="u1", token=APNS_TOKEN, platform="windows")
        with pytest.raises(InvalidDevice):
            store.register(user_id="u1", token=APNS_TOKEN, platform="ios", events=["sms"])

    def test_events_are_kept_in_canonical_order(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        dev = store.register(
            user_id="u1", token=APNS_TOKEN, platform="ios", events=["alerts", "replies"]
        )
        assert dev.events == ["replies", "alerts"]

    def test_a_person_keeps_only_their_most_recent_phones(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        for i in range(MAX_DEVICES_PER_USER + 3):
            store.register(user_id="u1", token=f"{i:02d}" + "b" * 30, platform="android")
            time.sleep(0.001)
        mine = store.for_user("u1")
        assert len(mine) == MAX_DEVICES_PER_USER
        assert not any(d.token.startswith("00") for d in mine), "the oldest was dropped"

    def test_file_is_private_and_the_api_never_returns_the_token(self, tmp_path) -> None:
        path = tmp_path / "run" / "push.json"
        store = DeviceStore(path)
        dev = store.register(user_id="u1", token=APNS_TOKEN, platform="ios")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        public = dev.public()
        assert "token" not in public
        assert public["token_suffix"] == APNS_TOKEN[-6:]

    def test_delete_tokens_and_delete_user(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        store.register(user_id="u1", token=APNS_TOKEN, platform="ios")
        store.register(user_id="u1", token=FCM_TOKEN, platform="android")
        store.register(user_id="u2", token="c" * 64, platform="ios")
        assert store.delete_tokens([APNS_TOKEN, "unknown"]) == 1
        assert store.delete_user("u1") == 1
        assert [d.user_id for d in store.all()] == ["u2"]

    def test_an_unreadable_file_reads_as_empty(self, tmp_path) -> None:
        path = tmp_path / "push.json"
        path.write_text("{not json")
        assert DeviceStore(path).all() == []

    def test_the_process_store_follows_the_home(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "a"))
        first = get_device_store()
        monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "b"))
        second = get_device_store()
        assert first.path != second.path
        assert second.path == tmp_path / "b" / "run" / "push_devices.json"


# ── dispatcher ───────────────────────────────────────────────────────────────


def _device(i: int, platform: str = "ios") -> Device:
    return Device(id=f"d{i}", user_id="u1", token=f"{i:04d}" + "t" * 40, platform=platform)


class TestBatching:
    def test_a_message_to_many_phones_splits_per_relay_limit(self) -> None:
        msg = PushMessage(devices=[_device(i) for i in range(23)], title="t")
        entries = msg.entries()
        assert [len(e["devices"]) for e in entries] == [10, 10, 3]
        assert all(len(e["devices"]) <= MAX_DEVICES_PER_NOTIFICATION for e in entries)

    def test_batches_respect_notification_and_send_limits(self) -> None:
        entries = [
            e
            for i in range(30)
            for e in PushMessage(
                devices=[_device(i * 10 + j) for j in range(3)], title="t"
            ).entries()
        ]
        batches = _batches(entries)
        for batch in batches:
            assert len(batch) <= MAX_NOTIFICATIONS
            assert sum(len(e["devices"]) for e in batch) <= MAX_SENDS_PER_REQUEST
        assert sum(len(b) for b in batches) == 30

    def test_entries_carry_only_what_was_set_and_clip_text(self) -> None:
        msg = PushMessage(
            devices=[_device(1)],
            title="x" * 500,
            body="y" * 500,
            data={"kind": "reply"},
            collapse_key="k" * 100,
            channel="replies",
            priority="normal",
        )
        (entry,) = msg.entries()
        assert len(entry["title"]) == 120
        assert len(entry["body"]) == 240
        assert len(entry["collapse_key"]) == 64
        assert entry["priority"] == "normal"
        assert entry["channel"] == "replies"
        assert "thread_id" not in entry
        assert entry["devices"] == [{"token": _device(1).token, "platform": "ios"}]


def _dispatcher(handler, store: DeviceStore, cloud=("https://cloud.test", "olano_dep_x")):
    return PushDispatcher(store=store, cloud=lambda: cloud, transport=httpx.MockTransport(handler))


class TestDispatcherSend:
    def test_success_posts_with_the_deployment_token_and_prunes_dead_tokens(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        live = store.register(user_id="u1", token=APNS_TOKEN, platform="ios")
        dead = store.register(user_id="u1", token=FCM_TOKEN, platform="android")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"sent": 1, "failed": 1, "invalid_tokens": [FCM_TOKEN]})

        disp = _dispatcher(handler, store)
        summary = disp.send([PushMessage(devices=[live, dead], title="hi")])
        assert summary["sent"] == 1 and summary["invalid_tokens"] == [FCM_TOKEN]
        # A dead token is routine, not a delivery fault the app should report.
        assert summary["error"] is None
        assert disp.status()["last_error"] is None
        (req,) = seen
        assert str(req.url) == "https://cloud.test/api/v1/push"
        assert req.headers["authorization"] == "Bearer olano_dep_x"
        body = json.loads(req.content)
        assert body["notifications"][0]["title"] == "hi"
        assert [d.token for d in store.all()] == [APNS_TOKEN]
        assert disp.status()["invalid_pruned"] == 1

    @pytest.mark.parametrize(
        ("response", "reason"),
        [
            (httpx.Response(401, json={"error": "unauthorized"}), "cloud_auth"),
            (httpx.Response(503, json={"error": "push_unconfigured"}), "cloud_unconfigured"),
            (httpx.Response(500, text="boom"), "cloud_error"),
            (httpx.Response(429, json={"retry_after": 30}), "rate_limited"),
        ],
    )
    def test_relay_failures_become_a_reason(self, tmp_path, response, reason) -> None:
        store = DeviceStore(tmp_path / "push.json")
        dev = store.register(user_id="u1", token=APNS_TOKEN, platform="ios")
        disp = _dispatcher(lambda _r: response, store)
        summary = disp.send([PushMessage(devices=[dev], title="hi")])
        assert summary["error"] == reason
        assert summary["failed"] == 1
        assert disp.status()["last_error"] == reason
        assert store.all(), "a failure never deletes a phone"

    def test_rate_limit_pauses_and_skips_the_remaining_batches(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "120"}, json={})

        disp = _dispatcher(handler, store)
        many = [PushMessage(devices=[_device(i)], title="t") for i in range(MAX_NOTIFICATIONS + 5)]
        summary = disp.send(many)
        assert len(calls) == 1, "the second batch would get the same answer"
        assert summary["failed"] == len(many)
        assert disp._paused_until > time.monotonic() + 100

    def test_failures_beyond_dead_tokens_are_a_fault(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        dev = store.register(user_id="u1", token=APNS_TOKEN, platform="ios")
        disp = _dispatcher(
            lambda _r: httpx.Response(200, json={"sent": 0, "failed": 1, "invalid_tokens": []}),
            store,
        )
        assert disp.send([PushMessage(devices=[dev], title="t")])["error"] == "cloud_error"
        assert store.all(), "a transient failure keeps the phone"

    def test_unreachable_relay(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        store = DeviceStore(tmp_path / "push.json")
        disp = _dispatcher(handler, store)
        summary = disp.send([PushMessage(devices=[_device(1)], title="t")])
        assert summary["error"] == "network"

    def test_no_cloud_channel(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        disp = PushDispatcher(store=store, cloud=lambda: None)
        assert disp.send([PushMessage(devices=[_device(1)], title="t")])["error"] == "not_managed"

    def test_throttle_drops_repeats_of_one_key(self, tmp_path) -> None:
        disp = PushDispatcher(store=DeviceStore(tmp_path / "p.json"), cloud=lambda: None)
        msg = PushMessage(devices=[_device(1)], title="t", throttle_key="k", throttle_seconds=60)
        assert disp.submit(msg) is True
        assert disp.submit(msg) is False
        assert disp.submit(PushMessage(devices=[], title="t")) is False

    def test_the_thread_delivers_what_was_submitted(self, tmp_path) -> None:
        store = DeviceStore(tmp_path / "push.json")
        dev = store.register(user_id="u1", token=APNS_TOKEN, platform="ios")
        got: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            got.append(json.loads(request.content))
            return httpx.Response(200, json={"sent": 1, "failed": 0, "invalid_tokens": []})

        disp = _dispatcher(handler, store)
        disp.start()
        try:
            disp.submit(PushMessage(devices=[dev], title="from the queue"))
            deadline = time.monotonic() + 5
            while not got and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            disp.stop()
        assert got and got[0]["notifications"][0]["title"] == "from the queue"
        assert disp.status()["sent"] == 1


class TestDispatcherLifecycle:
    def test_does_not_start_without_a_cloud_channel(self, monkeypatch) -> None:
        monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
        monkeypatch.setattr("olano.push.dispatcher._default_cloud", lambda: None)
        assert get_push_dispatcher(start=True) is None

    def test_does_not_start_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "olano.push.dispatcher._default_cloud", lambda: ("https://c", "olano_dep_x")
        )
        monkeypatch.setattr(
            "olano.config.system_settings.load_push_settings",
            lambda override=None: SimpleNamespace(enabled=False),
        )
        assert get_push_dispatcher(start=True) is None

    def test_starts_on_a_managed_box(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "olano.push.dispatcher._default_cloud", lambda: ("https://c", "olano_dep_x")
        )
        disp = get_push_dispatcher(start=True)
        assert disp is not None and disp.running
        assert get_push_dispatcher(start=False) is disp


# ── producers ────────────────────────────────────────────────────────────────


def _register(user_id: str = "owner", events: list[str] | None = None) -> Device:
    return get_device_store().register(
        user_id=user_id, token=APNS_TOKEN, platform="ios", events=events
    )


def _write_push_config(tmp_path, monkeypatch, block: dict) -> None:
    import yaml

    from olano.config.home import get_olano_paths

    cfg = get_olano_paths().config_path
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump({"push": block}))


class TestProducers:
    def test_every_producer_is_silent_outside_the_server(self) -> None:
        _register()
        assert push_events.reply_ready(user_id="owner", agent="a", thread_id="t") is False
        assert push_events.inbox_approval_needed(request_id="r", agent="a", action="x") is False
        assert push_events.operator_alert(notification=SimpleNamespace(level="error")) is False

    def test_reply_has_no_content_by_default(self, recorder) -> None:
        _register()
        assert push_events.reply_ready(
            user_id="owner", agent="maria", thread_id="t1", text="The secret plan is ready"
        )
        (msg,) = recorder.messages
        assert msg.title == "maria"
        assert "secret" not in msg.body
        assert msg.data["kind"] == "reply"
        assert msg.data["agent"] == "maria" and msg.data["thread_id"] == "t1"
        assert msg.channel == "replies"
        assert msg.collapse_key == "reply:maria:t1"

    def test_reply_preview_when_the_operator_allows_it(
        self, recorder, tmp_path, monkeypatch
    ) -> None:
        _write_push_config(tmp_path, monkeypatch, {"previews": True})
        _register()
        push_events.reply_ready(
            user_id="owner", agent="maria", thread_id="t1", text="Done:\n" + "x" * 400
        )
        (msg,) = recorder.messages
        assert msg.body.startswith("Done: x")
        assert len(msg.body) <= push_events.PREVIEW_CHARS

    def test_a_failed_turn_says_so(self, recorder) -> None:
        _register()
        push_events.reply_ready(user_id="owner", agent="maria", thread_id="t1", failed=True)
        assert "problem" in recorder.messages[0].body

    def test_a_phone_that_muted_replies_hears_nothing(self, recorder) -> None:
        _register(events=["approvals"])
        assert push_events.reply_ready(user_id="owner", agent="maria", thread_id="t1") is False
        assert recorder.messages == []

    def test_only_the_turns_own_person_is_told(self, recorder) -> None:
        _register(user_id="someone-else")
        assert push_events.reply_ready(user_id="owner", agent="maria", thread_id="t1") is False

    def test_chat_approval_names_the_action_not_its_arguments(self, recorder) -> None:
        _register()
        push_events.chat_approval_needed(
            user_id="owner",
            agent="maria",
            thread_id="t1",
            actions=[{"name": "gmail_send", "args": {"to": "boss@example.com"}}, {"name": "x"}],
        )
        (msg,) = recorder.messages
        assert "gmail_send" in msg.body and "1 more" in msg.body
        assert "boss@example.com" not in json.dumps(msg.entries())
        assert msg.channel == "approvals"

    def test_inbox_approval_reaches_the_owner_on_a_single_user_box(self, recorder) -> None:
        _register()
        push_events.inbox_approval_needed(
            request_id="req1", agent="maria", action="wallet_send", risk="high"
        )
        (msg,) = recorder.messages
        assert msg.data == {"kind": "inbox_approval", "agent": "maria", "item_id": "req1"}
        assert "high risk" in msg.body
        # High risk: the notification opens the app instead of offering buttons.
        assert msg.category == ""
        assert "category" not in msg.entries()[0]

    def test_approvals_carry_the_buttons_and_what_answering_them_needs(self, recorder) -> None:
        _register()
        push_events.chat_approval_needed(
            user_id="owner",
            agent="maria",
            thread_id="t1",
            actions=[{"name": "file_write", "risk": "low"}, {"name": "x"}],
        )
        push_events.inbox_approval_needed(request_id="req2", agent="maria", action="file_write")
        chat, inbox = recorder.messages
        assert chat.entries()[0]["category"] == "approval"
        assert chat.data["count"] == "2" and chat.data["thread_id"] == "t1"
        assert inbox.entries()[0]["category"] == "approval"
        assert inbox.data["item_id"] == "req2"

    def test_a_reply_can_be_answered_from_the_notification(self, recorder) -> None:
        _register()
        push_events.reply_ready(user_id="owner", agent="maria", thread_id="t1", text="Done.")
        push_events.reply_ready(user_id="owner", agent="maria", thread_id="t2", failed=True)
        ok, failed = recorder.messages
        assert ok.entries()[0]["category"] == "reply"
        assert ok.data["thread_id"] == "t1" and ok.data["agent"] == "maria"
        # A failed turn opens the app, where the error is.
        assert "category" not in failed.entries()[0]

    def test_a_high_risk_chat_action_opens_the_app_instead(self, recorder) -> None:
        _register()
        push_events.chat_approval_needed(
            user_id="owner",
            agent="maria",
            thread_id="t1",
            actions=[{"name": "file_write"}, {"name": "wallet_send", "risk": "high"}],
        )
        assert "category" not in recorder.messages[0].entries()[0]

    def test_public_host_rides_along_so_the_app_opens_the_right_deployment(
        self, recorder, monkeypatch
    ) -> None:
        monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
        _register()
        push_events.reply_ready(user_id="owner", agent="maria", thread_id="t1")
        assert recorder.messages[0].data["host"] == "acme.olano.run"

    @pytest.mark.parametrize(
        ("lane", "priority", "pushes"),
        [
            ("approval", "low", True),
            ("pulse", "urgent", True),
            ("pulse", "normal", False),
            ("ops", "urgent", False),
        ],
    )
    def test_pulse_by_lane_and_priority(self, recorder, lane, priority, pushes) -> None:
        _register()
        item = SimpleNamespace(
            id="p1",
            title="Quarterly numbers look off",
            lane_type=SimpleNamespace(value=lane),
            priority_level=SimpleNamespace(value=priority),
        )
        assert push_events.pulse_item_added(agent="maria", item=item) is pushes
        if pushes:
            (msg,) = recorder.messages
            assert "Quarterly" not in msg.body, "finding titles are content"
            assert msg.channel == "cortex"

    def test_pulse_threshold_is_configurable(self, recorder, tmp_path, monkeypatch) -> None:
        _write_push_config(tmp_path, monkeypatch, {"cortex_min_priority": "normal"})
        _register()
        item = SimpleNamespace(
            id="p1",
            title="t",
            lane_type=SimpleNamespace(value="pulse"),
            priority_level=SimpleNamespace(value="normal"),
        )
        assert push_events.pulse_item_added(agent="maria", item=item) is True

    @pytest.mark.parametrize(
        ("level", "pushes"), [("info", False), ("warn", True), ("error", True)]
    )
    def test_alerts_by_level_with_a_long_throttle(self, recorder, level, pushes) -> None:
        _register()
        note = SimpleNamespace(
            id="n1",
            level=level,
            title="Telegram can't start",
            body="",
            agent="maria",
            dedupe_key="channel:maria:telegram",
        )
        assert push_events.operator_alert(notification=note) is pushes
        if pushes:
            (msg,) = recorder.messages
            assert msg.title == "Telegram can't start"
            assert msg.throttle_key == "alert:channel:maria:telegram"
            assert msg.throttle_seconds >= 3600


class TestProducerWiring:
    def test_a_new_bell_row_pushes_and_a_recurrence_does_not(self, recorder) -> None:
        from olano.runtime import notifications

        notifications.reset_for_tests()
        _register()
        notifications.notify(kind="k", title="Disk almost full", level="warn", dedupe_key="disk")
        notifications.notify(kind="k", title="Disk almost full", level="warn", dedupe_key="disk")
        assert [m.title for m in recorder.messages] == ["Disk almost full"]

    def test_a_new_pulse_card_pushes_and_a_duplicate_does_not(
        self, recorder, base_config, tmp_path
    ) -> None:
        from olano.pulse.engine import PulseEngine

        _register()
        engine = PulseEngine(base_config, tmp_path / ".lionclaw")
        engine.add("Proposal body", lane="approval", title="Rewrite SOUL.md")
        engine.add("Proposal body", lane="approval", title="Rewrite SOUL.md")
        assert len(recorder.messages) == 1
        assert recorder.messages[0].data["agent"] == base_config.name

    def test_deleting_a_person_forgets_their_phones(self, tmp_path) -> None:
        from olano.users.store import get_user_store

        store = get_user_store()
        user = store.db.create_user(email="m@example.com", name="M", password_hash="x")
        get_device_store().register(user_id=user.id, token=APNS_TOKEN, platform="ios")
        store.db.delete_user(user.id)
        assert get_device_store().all() == []


# ── routes ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.runtime.api import create_api

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    runner = MagicMock()
    runner.config = config
    runner.ainvoke = AsyncMock(return_value="hi")
    runtime = MagicMock()
    runtime._runners = {"test_agent": runner}
    runtime._channels = []
    runtime.get_runner.return_value = runner
    with TestClient(create_api(runtime)) as c:
        c.runner = runner  # type: ignore[attr-defined]
        yield c


class TestRoutes:
    def test_status_on_a_box_without_the_relay(self, client) -> None:
        body = client.get("/api/push/status").json()
        assert body["available"] is False
        assert body["reason"] == "not_managed"
        assert body["events"] == list(EVENT_KINDS)
        assert body["devices"] == []

    def test_register_list_patch_delete(self, client) -> None:
        resp = client.post(
            "/api/push/devices",
            json={"token": APNS_TOKEN, "platform": "ios", "name": "iPhone", "app_version": "1.0.0"},
        )
        assert resp.status_code == 200, resp.text
        device = resp.json()["device"]
        assert "token" not in device and device["token_suffix"] == APNS_TOKEN[-6:]
        assert device["user_id"] == "owner"

        listed = client.get("/api/push/status").json()["devices"]
        assert [d["id"] for d in listed] == [device["id"]]

        patched = client.patch(f"/api/push/devices/{device['id']}", json={"events": ["approvals"]})
        assert patched.json()["device"]["events"] == ["approvals"]
        assert (
            client.patch(f"/api/push/devices/{device['id']}", json={"events": ["nope"]}).status_code
            == 400
        )

        assert client.delete(f"/api/push/devices/{device['id']}").json() == {"ok": True}
        assert client.delete(f"/api/push/devices/{device['id']}").status_code == 404

    def test_bad_registration_is_a_400(self, client) -> None:
        resp = client.post("/api/push/devices", json={"token": "nope", "platform": "ios"})
        assert resp.status_code == 400

    def test_someone_elses_phone_is_not_found(self, client) -> None:
        other = get_device_store().register(user_id="u-other", token=FCM_TOKEN, platform="android")
        assert client.patch(f"/api/push/devices/{other.id}", json={"name": "x"}).status_code == 404

    def test_test_send_explains_why_it_cannot(self, client) -> None:
        resp = client.post("/api/push/test")
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "not_managed"

    def test_test_send_goes_through_the_relay(self, client, tmp_path) -> None:
        get_device_store().register(user_id="owner", token=APNS_TOKEN, platform="ios")
        disp = PushDispatcher(
            cloud=lambda: ("https://cloud.test", "olano_dep_x"),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"sent": 1, "failed": 0, "invalid_tokens": []})
            ),
        )
        set_push_dispatcher(disp)
        resp = client.post("/api/push/test")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"sent": 1, "failed": 0, "error": None, "pruned": 0}


# ── chat hooks: push only when nobody is watching ────────────────────────────


async def _no_viewer(run, cursor, request):
    """A stream view that never attaches, like an app that went to the background."""
    if False:  # pragma: no cover - makes this an async generator
        yield ""


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestChatHooks:
    def test_reply_pushes_when_nobody_watches(self, client, recorder, monkeypatch) -> None:
        import olano.runtime.routes.chat as chat_routes

        _register()
        monkeypatch.setattr(chat_routes, "_sse_run_view", _no_viewer)

        def fake_events(payload, **kwargs):
            async def gen():
                yield ("text", "All done.")

            return gen()

        client.runner.astream_events = fake_events
        resp = client.post("/api/chat/stream", json={"message": "hi", "agent": "test_agent"})
        assert resp.status_code == 200
        assert _wait_for(lambda: any(m.data.get("kind") == "reply" for m in recorder.messages))
        msg = next(m for m in recorder.messages if m.data["kind"] == "reply")
        assert msg.data["agent"] == "test_agent"

    def test_reply_does_not_push_to_someone_watching(self, client, recorder) -> None:
        import asyncio

        _register()

        def fake_events(payload, **kwargs):
            async def gen():
                # Long enough for the response's own view to attach, as it
                # always has by the time a real model call returns.
                await asyncio.sleep(0.3)
                yield ("text", "All done.")

            return gen()

        client.runner.astream_events = fake_events
        resp = client.post("/api/chat/stream", json={"message": "hi", "agent": "test_agent"})
        assert '"type": "done"' in resp.text
        time.sleep(0.2)
        assert not any(m.data.get("kind") == "reply" for m in recorder.messages)

    def test_a_stopped_turn_is_never_announced(self, client, recorder, monkeypatch) -> None:
        import asyncio

        import olano.runtime.routes.chat as chat_routes
        from olano.core.runs import REGISTRY

        _register()
        monkeypatch.setattr(chat_routes, "_sse_run_view", _no_viewer)

        def fake_events(payload, **kwargs):
            async def gen():
                await asyncio.sleep(30)
                yield ("text", "never")

            return gen()

        client.runner.astream_events = fake_events
        client.post("/api/chat/stream", json={"message": "hi", "agent": "test_agent"})
        assert _wait_for(lambda: bool(REGISTRY.snapshot()))
        (run_id,) = list(REGISTRY.snapshot())
        thread_id = REGISTRY.get(run_id).thread_id
        assert (
            client.post(
                "/api/chat/stop", json={"agent": "test_agent", "thread_id": thread_id}
            ).json()["stopped"]
            is True
        )
        assert _wait_for(lambda: REGISTRY.get(run_id) is None or REGISTRY.get(run_id).done)
        time.sleep(0.1)
        assert not any(m.data.get("kind") == "reply" for m in recorder.messages)

    def test_an_approval_nobody_is_watching_pushes_once(
        self, client, recorder, monkeypatch
    ) -> None:
        import olano.runtime.routes.chat as chat_routes
        from olano.core.runs import REGISTRY

        _register()
        monkeypatch.setattr(chat_routes, "_sse_run_view", _no_viewer)
        decided: dict = {}

        def fake_events(payload, approval_handler=None, **kwargs):
            async def gen():
                decided["decisions"] = await approval_handler([{"name": "gmail_send"}])
                yield ("text", "sent")

            return gen()

        client.runner.astream_events = fake_events
        client.post(
            "/api/chat/stream",
            json={"message": "email it", "agent": "test_agent", "mode": "manual"},
        )
        assert _wait_for(lambda: any(m.data.get("kind") == "approval" for m in recorder.messages))
        (run_id,) = list(REGISTRY.snapshot())
        thread_id = REGISTRY.get(run_id).thread_id
        resp = client.post(
            "/api/chat/approval",
            json={"thread_id": thread_id, "decisions": [{"type": "approve"}]},
        )
        assert resp.status_code == 200, resp.text
        assert _wait_for(lambda: "decisions" in decided)
        assert decided["decisions"] == [{"type": "approve"}]
        approvals = [m for m in recorder.messages if m.data.get("kind") == "approval"]
        assert len(approvals) == 1, "polling must not push the same approval twice"
        assert "gmail_send" in approvals[0].body
