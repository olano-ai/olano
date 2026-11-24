"""What is this box actually running?

A managed instance runs a container pulled from
``ghcr.io/olano-ai/olano-backend:latest``, and until this module existed it had
no way to answer that question about itself. Nothing in the runtime read a build
identity, nothing reported one, and ``deployments.runtime_version`` was written
by exactly one path — the update agent, on a successful update — so for most
boxes it stayed NULL forever.

The console then rendered that NULL as ``"latest (as deployed)"``, which is not
a version. It is a **fabricated reassurance**: a box running week-old code and a
box running this morning's produce identical text, and a deploy that silently
pulled a stale image is indistinguishable from one that worked. That cost a live
debugging session (2026-08-14): a fix was merged, an Update was pressed, the card
said "latest", and the box was running the previous image because the hourly
image build had not published yet.

The rule this module exists to enforce: **never invent a version.** When the
build identity is absent, every surface says so plainly and the operator knows to
go looking. ``runtime_version()`` returns ``None`` rather than a placeholder, and
callers are expected to render that as "not reported", never as a value.

Where the identity comes from, in order:

1. ``OLANO_IMAGE_REF`` — the exact image reference the box was started from,
   when the deploy path knows it (``<tag>@sha256:…``). Most precise: it names
   the artifact, not just the source.
2. ``OLANO_BUILD_SHA`` — the git commit baked in at image build time
   (``Dockerfile`` ARG → ENV, passed by ``.github/workflows/build-images.yml``).
   This is what makes "did my merge reach the box" answerable at a glance.
3. The package version — a real answer for a pip/source install, which has no
   image at all.

The string is capped at 64 chars because that is the column width the control
plane accepts (``deployments.runtime_version``); a longer value would be
truncated server-side into something that no longer matches what CI published.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

IMAGE_REF_ENV = "OLANO_IMAGE_REF"
BUILD_SHA_ENV = "OLANO_BUILD_SHA"
BUILD_TIME_ENV = "OLANO_BUILD_TIME"

# `deployments.runtime_version` is text but the API validates max(64) — keep the
# reported value inside it so the control plane stores what CI actually built.
MAX_VERSION_CHARS = 64


def build_sha() -> str | None:
    """The git commit this image was built from, if it was baked in."""
    import os

    return (os.environ.get(BUILD_SHA_ENV) or "").strip() or None


def image_ref() -> str | None:
    """The image reference this container was started from, if known."""
    import os

    return (os.environ.get(IMAGE_REF_ENV) or "").strip() or None


def build_time() -> str | None:
    """When the image was built (ISO-8601), if it was baked in."""
    import os

    return (os.environ.get(BUILD_TIME_ENV) or "").strip() or None


def runtime_version() -> str | None:
    """The reportable identity of this runtime, or ``None`` when unknown.

    ``None`` is a real answer and must be rendered as such — see the module
    docstring. Do not substitute a placeholder here or at any call site.
    """
    ref = image_ref()
    if ref:
        return ref[:MAX_VERSION_CHARS]

    sha = build_sha()
    if sha:
        # The FULL sha, not a short one. CI tags every image with its commit
        # (`build-images.yml`), so the control plane resolves `git-<sha>` to a
        # real registry digest and can answer "is a newer image available" —
        # which a 12-char prefix cannot do, because a registry needs the exact
        # tag. `git-` + 40 = 44 chars, inside MAX_VERSION_CHARS. Surfaces
        # shorten it for display; the wire value stays resolvable.
        return f"git-{sha}"[:MAX_VERSION_CHARS]

    try:
        from olano import __version__

        pkg = str(__version__).strip()
    except Exception:  # pragma: no cover - defensive
        pkg = ""
    return f"src-{pkg}"[:MAX_VERSION_CHARS] if pkg else None


def version_report() -> dict[str, object]:
    """Everything known about this runtime's identity, for diagnostics.

    ``known`` is the field that matters: False means the box genuinely cannot
    tell you what it is running, which is a deployment-pipeline gap to fix, not
    a value to paper over.
    """
    version = runtime_version()
    return {
        "runtime_version": version,
        "known": version is not None,
        "image_ref": image_ref(),
        "build_sha": build_sha(),
        "build_time": build_time(),
        "package_version": _package_version(),
    }


def package_version() -> str | None:
    """The installed package's version (``olano.__version__``), or ``None``.

    This is the release name: the release workflow refuses a tag whose version
    differs from ``pyproject.toml``'s, so on a managed box it is the ``X.Y.Z``
    of the image's release and the console shows it beside the build id. On an
    edge image it is the version of the release the image is ahead of, which
    is why the control plane only displays it and never compares it.
    """
    return _package_version()


def _package_version() -> str | None:
    try:
        from olano import __version__

        return str(__version__).strip() or None
    except Exception:  # pragma: no cover - defensive
        return None
