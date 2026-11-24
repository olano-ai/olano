"""The sealed-volume boot guard (runtime/seal_guard.py).

A box provisioned with a LUKS2 data volume leaves ``/etc/olano/seal.json`` on
the root disk. When it names a volume, the runtime must prove that the mount
holding its home directory is sourced from the sealed device-mapper node, read
from the mount table this process sees; otherwise it refuses to boot rather
than seed a fresh home onto the unencrypted disk. The proof is the mount, not a
marker file: inside the runtime container only the home directory's own bind
mount is visible, which is exactly how the first sealed boxes boot-looped on a
marker beside the home that the container could never see. Every existing
deployment has no seal state and must pass untouched, which is also what the
autouse fixture pins for the rest of the suite (``LIONCLAW_SEAL_STATE`` -> a
file that does not exist).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from olano.runtime import seal_guard
from olano.runtime.seal_guard import (
    MOUNTINFO_ENV,
    SEAL_MAPPER,
    SEAL_STATE_ENV,
    SealGuardError,
    assert_home_on_sealed_volume,
    custody_status,
    holding_mount,
)

_UUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
ROOT_DISK = "/dev/vda1"


class Box:
    """A managed box under tmp_path: a home, a seal state and a mount table of its own."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home_name: str = "olano"
    ) -> None:
        self.state = tmp_path / "etc" / "seal.json"
        self.state.parent.mkdir()
        monkeypatch.setenv(SEAL_STATE_ENV, str(self.state))
        self.mount = tmp_path / "home" / home_name
        self.home = self.mount / ".olano"
        self.home.mkdir(parents=True)
        self.mountinfo = tmp_path / "mountinfo"
        monkeypatch.setenv(MOUNTINFO_ENV, str(self.mountinfo))

    def write_state(self, **fields: object) -> None:
        payload: dict[str, object] = {"mode": "standard", "luks_uuid": _UUID, **fields}
        self.state.write_text(json.dumps(payload), encoding="utf-8")

    def mount_table(self, *mounts: tuple[str, str]) -> None:
        """``(mount point, source)`` pairs, ``/`` on the root disk always first.

        Mount points are written the way proc(5) prints them: spaces as ``\\040``.
        Optional fields (``shared:1``) precede the separator on some lines so the
        parser has to honour it rather than count fields.
        """
        lines = [f"22 1 252:1 / / rw,relatime shared:1 - ext4 {ROOT_DISK} rw"]
        for n, (point, source) in enumerate(mounts, start=100):
            escaped = os.path.realpath(point).replace(" ", "\\040")
            lines.append(
                f"{n} 22 253:0 /home/.olano {escaped} rw,noatime - ext4 {source} rw,data=ordered"
            )
        self.mountinfo.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    return Box(tmp_path, monkeypatch)


def _custody(**overrides: object) -> dict[str, object]:
    """What ``custody_status`` reports for a sealed box whose state predates the
    ceremony fields (sealer 11 and earlier): every optional field at its
    "nothing yet" value, so the dashboard card renders rather than vanishes."""
    doc: dict[str, object] = {
        "mode": "standard",
        "sealed": False,
        "luks_uuid": _UUID,
        "sealer_version": None,
        "recovery": {"status": "none", "at": None},
        "header_generation": None,
        "ceremony_api": False,
    }
    doc.update(overrides)
    return doc


def test_no_seal_state_passes_and_reports_no_custody(box: Box) -> None:
    assert not box.state.exists()
    assert_home_on_sealed_volume(
        box.home
    )  # every existing deployment; the mount table is never read
    assert custody_status(box.home) == {"mode": "none"}


def test_home_on_the_sealed_mapper_passes_and_reports_custody(box: Box) -> None:
    """The proof is the home's own mount. No marker file exists anywhere here."""
    box.write_state()
    box.mount_table((str(box.home), SEAL_MAPPER))
    assert_home_on_sealed_volume(box.home)
    assert custody_status(box.home) == _custody(luks_uuid=_UUID)


def test_home_on_the_root_disk_refuses_to_boot(box: Box) -> None:
    """The mount is up but the home sits on the unencrypted disk: the unclean-reboot race."""
    box.write_state()
    box.mount_table()  # only / on the root disk
    with pytest.raises(SealGuardError) as info:
        assert_home_on_sealed_volume(box.home)
    msg = str(info.value)
    assert "not on the sealed volume" in msg
    assert ROOT_DISK in msg and SEAL_MAPPER in msg and str(box.state) in msg and _UUID in msg


def test_the_longest_mount_point_decides(box: Box) -> None:
    """A home under a sealed parent mount passes; a root-disk mount nested inside it does not."""
    box.write_state()
    box.mount_table((str(box.mount), SEAL_MAPPER))  # the bind of /home/olano, the home is a subdir
    assert_home_on_sealed_volume(box.home)
    box.mount_table((str(box.mount), SEAL_MAPPER), (str(box.home), ROOT_DISK))
    with pytest.raises(SealGuardError, match="not on the sealed volume"):
        assert_home_on_sealed_volume(box.home)


def test_mount_points_with_spaces_are_unescaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Box(tmp_path, monkeypatch, home_name="olano home")
    box.write_state()
    box.mount_table((str(box.home), SEAL_MAPPER))
    assert "\\040" in box.mountinfo.read_text(encoding="utf-8")
    assert holding_mount(box.home) == (os.path.realpath(box.home), SEAL_MAPPER)
    assert_home_on_sealed_volume(box.home)


def test_an_unreadable_mount_table_refuses_to_boot(box: Box) -> None:
    """No proof is a refusal, never a pass: the guard fails closed."""
    box.write_state()
    assert not box.mountinfo.exists()
    with pytest.raises(SealGuardError, match="cannot read"):
        assert_home_on_sealed_volume(box.home)


def test_the_mapper_can_be_pointed_elsewhere_for_a_lab(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    box.write_state()
    monkeypatch.setenv(seal_guard.SEAL_MAPPER_ENV, "/dev/mapper/lab")
    box.mount_table((str(box.home), "/dev/mapper/lab"))
    assert_home_on_sealed_volume(box.home)
    box.mount_table((str(box.home), SEAL_MAPPER))
    with pytest.raises(SealGuardError):
        assert_home_on_sealed_volume(box.home)


def test_state_without_a_uuid_has_nothing_to_check(box: Box) -> None:
    box.state.write_text(json.dumps({"mode": "standard"}), encoding="utf-8")
    assert_home_on_sealed_volume(box.home)  # no mount table needed
    assert custody_status(box.home) == _custody(luks_uuid=None)


def test_unparseable_state_is_treated_as_absent_with_a_warning(
    box: Box, caplog: pytest.LogCaptureFixture
) -> None:
    box.state.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=seal_guard.__name__):
        assert seal_guard.read_seal_state() is None
        assert_home_on_sealed_volume(box.home)  # a corrupt file must not boot-loop a box
        assert custody_status(box.home) == {"mode": "none"}
    assert any("seal state" in rec.getMessage() for rec in caplog.records)


def test_the_real_mount_table_of_this_process_parses() -> None:
    """Whatever this test runs on, its own home is on some mount; the parser must find it."""
    found = holding_mount(Path.home())
    assert found is not None
    point, source = found
    assert os.path.realpath(Path.home()).startswith(point.rstrip("/")) and source


def test_custody_carries_the_ceremony_fields_the_sealer_writes(box: Box) -> None:
    """Sealer 12 rewrites seal.json after every ceremony transition; the status
    surfaces the public half of it. The recovery slot number is dropped (an
    implementation detail of the LUKS header, not something the card shows)
    and no fingerprint or key can be here because the file never holds one."""
    box.write_state(
        sealer_version="12",
        recovery={"status": "acknowledged", "at": "2026-09-24T10:00:00Z", "slot": 1},
        header_generation=3,
        serve_socket="/run/olano-sealer/serve.sock",
        sealer_key_fingerprint="0123456789abcdef",  # gitleaks:allow
    )
    assert custody_status(box.home) == _custody(
        sealer_version="12",
        recovery={"status": "acknowledged", "at": "2026-09-24T10:00:00Z"},
        header_generation=3,
        ceremony_api=True,
    )


def test_custody_reads_a_pending_key_and_tolerates_malformed_optional_fields(box: Box) -> None:
    """A pending ceremony shows as pending; a field of the wrong shape reads as
    absent rather than raising (the status must never break /api/status)."""
    box.write_state(recovery={"status": "pending", "at": "2026-09-24T10:05:00Z"})
    assert custody_status(box.home)["recovery"] == {
        "status": "pending",
        "at": "2026-09-24T10:05:00Z",
    }

    box.write_state(sealer_version=12, recovery="garbage", header_generation="x", serve_socket="")
    assert custody_status(box.home) == _custody(sealer_version="12")

    box.write_state(header_generation="7", recovery={"status": None, "at": 5})
    assert custody_status(box.home) == _custody(header_generation=7)


# -- inside RuntimeServer.start -----------------------------------------------


def _patched_server(monkeypatch: pytest.MonkeyPatch, calls: list[str]):
    """A RuntimeServer whose start() records provisioning and stops at the vault."""
    from olano.runtime.server import RuntimeServer

    class _StopBoot(Exception):
        pass

    server = RuntimeServer(agent_names=["merli"])
    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server.webhook_dispatcher, "start", lambda: None)

    async def fake_provision() -> None:
        calls.append("provision")

    def stop_at_vault() -> None:
        calls.append("unlock")
        raise _StopBoot()

    monkeypatch.setattr(server, "_provision_on_first_boot", fake_provision)
    monkeypatch.setattr(server, "_unlock_vault_for_boot", stop_at_vault)
    return server, _StopBoot


async def test_runtime_start_exits_3_before_provisioning_when_the_volume_is_missing(
    box: Box, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The guard runs BEFORE first-boot provisioning and exits non-zero.

    Exit code 3 is what `restart: unless-stopped` retries; provisioning must not
    have run, or the fresh admin/agent would already be on the wrong disk. Once
    the home's mount is the sealed volume the same box boots straight through.
    """
    box.write_state()  # names a uuid; the home is still on the root disk
    box.mount_table()
    monkeypatch.setenv("LIONCLAW_HOME", str(box.home))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")

    calls: list[str] = []
    server, _stop = _patched_server(monkeypatch, calls)
    with caplog.at_level(logging.CRITICAL, logger="olano.runtime.server"):
        with pytest.raises(SystemExit) as info:
            await server.start()
    assert info.value.code == 3
    assert calls == []  # nothing touched the home
    assert any(
        rec.levelno == logging.CRITICAL and "not on the sealed volume" in rec.getMessage()
        for rec in caplog.records
    )

    box.mount_table((str(box.home), SEAL_MAPPER))
    server, stop = _patched_server(monkeypatch, calls)
    with pytest.raises(stop):
        await server.start()
    assert calls == ["provision", "unlock"]
