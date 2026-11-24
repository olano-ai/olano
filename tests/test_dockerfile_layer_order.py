"""Pin the backend Dockerfile's layer order.

The runtime image is ~3 GB and every managed box pulls it on first boot and on
every console update. What a release actually costs the fleet is the set of
layers whose digests CHANGED since the previous build, and that is decided by
instruction order alone. Two orderings used to publish ~2.9 GB of new blobs on
every commit (no build step ever hit the cache):

* the build-identity ``ARG OLANO_BUILD_SHA`` at the top — BuildKit keys every
  ``RUN`` that has an ARG in scope on its value, so a new SHA invalidated apt,
  Node, uv and Playwright alike;
* ``COPY olano ./olano`` before the dependency install, so any source edit
  re-ran the 1.32 GB ``uv pip install``.

These checks fail the moment either creeps back. They read the Dockerfile as
text on purpose: the invariant is about instruction order, and a real build is
both too slow for the unit suite and needs a daemon CI's test job lacks.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _final_stage_instructions() -> list[str]:
    """Instructions of the LAST stage, one entry each, continuations joined."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Join `\`-continued lines, drop comments and blanks.
    joined = re.sub(r"\\\n", " ", text)
    lines = [ln.strip() for ln in joined.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    # The final stage starts at the last FROM.
    last_from = max(i for i, ln in enumerate(lines) if ln.startswith("FROM "))
    return lines[last_from:]


def _index(instructions: list[str], pattern: str) -> int:
    hits = [i for i, ln in enumerate(instructions) if re.search(pattern, ln)]
    assert hits, f"no instruction matches {pattern!r}"
    assert len(hits) == 1, f"{pattern!r} matched more than once: {hits}"
    return hits[0]


def test_build_identity_args_come_after_every_run() -> None:
    """OLANO_BUILD_SHA/TIME must be declared after the last RUN.

    Only the runtime reads them (an ENV in the image config), so their position
    is invisible to the container — but declared any earlier they put the commit
    SHA into the cache key of every RUN that follows.
    """
    ins = _final_stage_instructions()
    last_run = max(i for i, ln in enumerate(ins) if ln.startswith("RUN "))
    sha_arg = _index(ins, r"^ARG OLANO_BUILD_SHA")
    time_arg = _index(ins, r"^ARG OLANO_BUILD_TIME")
    build_env = _index(ins, r"^ENV OLANO_BUILD_SHA=")
    assert sha_arg > last_run, "ARG OLANO_BUILD_SHA must come after the last RUN"
    assert time_arg > last_run, "ARG OLANO_BUILD_TIME must come after the last RUN"
    assert build_env > last_run, "ENV OLANO_BUILD_SHA must come after the last RUN"


def test_dependencies_install_before_any_source_is_copied() -> None:
    """The lockfile-driven dependency layer must not depend on the source tree."""
    ins = _final_stage_instructions()
    deps = _index(ins, r"^RUN uv export --locked .* && uv pip install --system --no-cache -r ")
    playwright = _index(ins, r"^RUN playwright install ")
    copy_src = _index(ins, r"^COPY olano \./olano$")
    copy_all = _index(ins, r"^COPY \. \.$")
    project = _index(ins, r"^RUN uv pip install --system --no-cache --no-deps -e \.$")

    assert deps < copy_src, "dependencies must be installed before COPY olano"
    assert playwright < copy_src, (
        "Playwright install must precede COPY olano (it keys on uv.lock only)"
    )
    assert copy_src < project < copy_all, "source install sits between COPY olano and COPY . ."

    # Nothing per-commit may appear above the dependency layer: the only COPYs
    # allowed before it are the lock inputs and stage/image imports.
    for ln in ins[:deps]:
        if ln.startswith("COPY ") and "--from=" not in ln:
            assert ln == "COPY pyproject.toml uv.lock README.md ./" or ln.startswith(
                "COPY docker/olano-sandbox/profile-interactive.sh"
            ), f"per-commit content copied above the dependency layer: {ln}"


def test_uv_image_is_pinned() -> None:
    """A moving uv tag would re-key every layer below it on uv's release cadence."""
    ins = _final_stage_instructions()
    uv_copy = ins[_index(ins, r"^COPY --from=ghcr\.io/astral-sh/uv:")]
    assert re.search(r"uv:\d+\.\d+\.\d+ ", uv_copy), f"pin uv to a version tag: {uv_copy}"


def test_dependency_layer_is_locked_not_frozen() -> None:
    """--locked fails loudly on lock drift; --frozen would ship stale pins."""
    ins = _final_stage_instructions()
    deps = ins[_index(ins, r"^RUN uv export ")]
    assert "--locked" in deps
    assert "--frozen" not in deps
    assert "--no-dev" in deps, "the image must never ship the dev group"
    assert "--no-emit-project" in deps


def test_runtime_runs_under_an_init_that_reaps_orphans() -> None:
    """The runtime must not be PID 1: orphaned Chromium helpers become zombies.

    Python reaps only the children it started, so every grandchild whose parent
    exits first (the render tools' and the browser's ``headless_shell``) stayed
    a zombie until the container restarted. tini as PID 1 reaps them and passes
    signals and the exit code through.
    """
    ins = _final_stage_instructions()
    install = _index(
        ins, r"^RUN apt-get update && apt-get install -y --no-install-recommends tini "
    )
    entry = ins[_index(ins, r"^ENTRYPOINT ")]
    assert entry == 'ENTRYPOINT ["/usr/bin/tini", "--", "olano"]', entry
    assert install < ins.index(entry)
    assert ins[_index(ins, r"^CMD ")] == 'CMD ["serve"]'
