"""Superseded-image reclaim: keep the last good Olano image, drop the rest.

Every update pulls a new backend + dashboard + sandbox image and `compose up
-d` never removes the set it replaced, so a 25 GB disk fills with its
own history until an update can no longer pull.
Dangling-only pruning misses those copies because they still carry tags, and
`docker system prune -af` overshoots: it deletes olano-sandbox:latest whenever
no sandbox container happens to be running, silently disabling agent code-exec.

These tests pin the rule in between — keep what containers use plus the tags
the runtime resolves by name; drop every other image in repos we publish — and
the safety rails around it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from olano.runtime import disk_monitor
from olano.runtime.disk_monitor import select_stale_olano_images

BACKEND = "ghcr.io/olano-ai/olano-backend"
DASHBOARD = "ghcr.io/olano-ai/olano-dashboard"


def img(image_id: str, repository: str, tag: str) -> dict[str, str]:
    return {"id": image_id, "repository": repository, "tag": tag}


def test_keeps_the_running_image_and_drops_its_predecessors():
    images = [
        img("sha-new", BACKEND, "v2"),
        img("sha-old", BACKEND, "v1"),
        img("sha-older", BACKEND, "v0"),
    ]
    assert select_stale_olano_images(images, {"sha-new"}) == [f"{BACKEND}:v1", f"{BACKEND}:v0"]


def test_never_touches_images_we_do_not_publish():
    images = [
        img("sha-pg", "postgres", "16"),
        img("sha-caddy", "caddy", "2"),
        img("sha-customer", "ghcr.io/someone-else/thing", "v1"),
        img("sha-old", BACKEND, "v1"),
    ]
    assert select_stale_olano_images(images, {"sha-pg", "sha-caddy"}) == [f"{BACKEND}:v1"]


def test_protects_the_on_demand_sandbox_tag_even_with_no_container():
    # The runtime resolves olano-sandbox:latest BY NAME and spawns it per agent,
    # so between sessions nothing references it — "unused" must not mean
    # "deletable" here, or code-exec dies until the next update re-tags it.
    images = [
        img("sha-run", BACKEND, "v2"),
        img("sha-sbx", "olano-sandbox", "latest"),
        img("sha-sbx-old", "ghcr.io/olano-ai/olano-sandbox", "v1"),
        img("sha-legacy", "lionclaw-sandbox", "latest"),
    ]
    stale = select_stale_olano_images(images, {"sha-run"})
    assert stale == ["ghcr.io/olano-ai/olano-sandbox:v1"]


def test_dangling_images_are_left_to_the_prune_step():
    # Removal here is by reference; an untagged image has none to remove.
    images = [img("sha-run", BACKEND, "v2"), img("sha-dangling", "<none>", "<none>")]
    assert select_stale_olano_images(images, {"sha-run"}) == []


def test_a_stopped_container_still_protects_its_image():
    # in_use_image_ids comes from `docker ps -a` — a stopped container must be
    # able to start again.
    images = [img("sha-new", BACKEND, "v2"), img("sha-stopped", DASHBOARD, "v1")]
    assert select_stale_olano_images(images, {"sha-new", "sha-stopped"}) == []


class FakeDocker:
    """Scripted `docker` CLI: images/ps/inspect/rmi/prune."""

    def __init__(self, images: list[tuple[str, str, str]], in_use: dict[str, str]):
        self.images = images  # (id, repository, tag)
        self.in_use = in_use  # container id -> image id
        self.removed: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        out = ""
        if argv[:2] == ["docker", "images"]:
            out = "".join(f"{i}\t{r}\t{t}\n" for i, r, t in self.images)
        elif argv[:3] == ["docker", "ps", "-a"]:
            out = "\n".join(self.in_use) + "\n" if self.in_use else ""
        elif argv[:2] == ["docker", "inspect"]:
            out = "\n".join(self.in_use[c] for c in argv[4:] if c in self.in_use) + "\n"
        elif argv[:2] == ["docker", "rmi"]:
            self.removed.append(argv[2])
            out = f"Untagged: {argv[2]}\n"
        elif argv[:3] == ["docker", "image", "prune"]:
            out = "Total reclaimed space: 1.2GB\n"
        elif argv[:3] == ["docker", "builder", "prune"]:
            out = "Total reclaimed space: 300MB\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def run_action(fake: FakeDocker) -> str:
    return disk_monitor._run_olano_stale_images(
        Path("/home"), run_fn=fake, which_fn=lambda _: "/usr/bin/docker", docker_fn=lambda: True
    )


def test_action_removes_by_reference_without_force_then_prunes():
    fake = FakeDocker(
        images=[
            ("sha-new", BACKEND, "v2"),
            ("sha-old", BACKEND, "v1"),
            ("sha-sbx", "olano-sandbox", "latest"),
            ("sha-pg", "postgres", "16"),
        ],
        in_use={"c1": "sha-new", "c2": "sha-pg"},
    )
    summary = run_action(fake)

    assert fake.removed == [f"{BACKEND}:v1"]
    assert not any(
        "-f" in call or "--force" in call for call in fake.calls if call[:2] == ["docker", "rmi"]
    )
    # Dangling + build cache are collected after the untagging.
    assert ["docker", "image", "prune", "-f"] in fake.calls
    assert ["docker", "builder", "prune", "-f"] in fake.calls
    assert "1 superseded Olano image(s) removed" in summary
    assert "Total reclaimed space" in summary


def test_action_refuses_when_no_olano_container_is_running():
    # Stack down (or docker answering oddly): "unused" cannot distinguish the
    # image the box needs to boot from a superseded one, so do nothing.
    fake = FakeDocker(
        images=[("sha-a", BACKEND, "v2"), ("sha-b", BACKEND, "v1")],
        in_use={"c1": "sha-pg"},
    )
    summary = run_action(fake)
    assert fake.removed == []
    assert "nothing removed" in summary


def test_action_skips_cleanly_without_a_docker_daemon():
    fake = FakeDocker(images=[], in_use={})
    summary = disk_monitor._run_olano_stale_images(
        Path("/home"), run_fn=fake, which_fn=lambda _: "/usr/bin/docker", docker_fn=lambda: False
    )
    assert summary == "docker daemon not reachable"
    assert fake.calls == []


def test_action_is_registered_as_safe_and_mirrored_in_settings():
    from olano.config.system_settings import DISK_CLEANUP_ACTIONS, DISK_CLEANUP_SAFE_ACTIONS

    spec = disk_monitor.ACTION_REGISTRY["olano_stale_images"]
    assert spec.safe is True and spec.kind == "internal"
    assert "olano_stale_images" in DISK_CLEANUP_ACTIONS
    assert "olano_stale_images" in DISK_CLEANUP_SAFE_ACTIONS
