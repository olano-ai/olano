# Releasing Olano

How a change travels from `main` to a managed box or a self-hosted install,
and how to cut, verify and roll back a release.

## The image tags

Three images are published to GHCR on every build: `olano-backend`,
`olano-dashboard` and `olano-sandbox`, all under `ghcr.io/olano-ai/`. Each
carries these tags:

| Tag | Moves when | Who pulls it |
|---|---|---|
| `:edge` | A commit on `main` has green checks and the 3-hourly build runs (`build-images.yml`) | Our canary boxes, anyone tracking `main` |
| `:latest` | A release tag `vX.Y.Z` is pushed (`release.yml`) | Managed boxes on Update, fresh provisioning, the cloud's golden disk images, the default `deploy/docker-compose.allinone.yml` |
| `:X.Y.Z` | That release only; never moves afterwards | Pinning a box to an exact version |
| `:X.Y` | Every patch release of that minor | Tracking a minor line |
| `:<full sha>` | Every build, edge or release | What a release promotes; what an operator pins to roll back |

Two rules follow from the table. **`:latest` is a release, never `main`**: a
merge cannot reach a customer until someone tags it. And **a release is a
promotion, not a rebuild**: `release.yml` re-tags the `:<sha>` image the edge
pipeline already built and tested for that commit, so the bytes a canary ran
are the bytes the fleet receives.

## The CI gate

`.github/scripts/checks-green.sh <sha>` lists every workflow run for a commit
and answers `green`, `pending` or `red`. The edge build runs it before
building; the release workflow runs it before promoting. A commit with no runs
at all (a docs-only change, which `ci.yml` ignores) counts as green: nothing
was triggered, so nothing can have failed. Dependabot's own update jobs,
scheduled runs and the two publishing workflows are not checks of the commit
and are ignored. A run that a later push to `main` cancelled reads as red;
re-run it before tagging that commit.

A pending verdict is handled differently by the two callers. The scheduled
edge build skips its window and the next one (at most three hours later)
picks the commit up. A dispatched edge build and a release are a person
asking for that commit, so both wait for the running checks, up to 75
minutes (what E2E and the test job can take from a push), and then build or
refuse on the verdict they settled on. Dispatching the edge build with
`force=true` skips the gate, wait included, for a commit you know is good;
a release has no such switch.

## Cutting a release

1. **Choose the version.** Semantic versioning: a patch for fixes, a minor for
   features, a major for a breaking change. Before 1.0 a minor may break.
2. **Bump the version in a PR.** Edit `version` in `pyproject.toml` and
   `__version__` in `olano/__init__.py` (what a box reports as its package
   version; a test keeps the two equal), run `uv lock` (the lock records the
   project version, and CI's `uv lock --check` fails otherwise), merge. The release workflow refuses a tag whose version
   does not match `pyproject.toml`.
3. **Confirm `main` is green** at the commit you are about to tag. If the edge
   build has not reached it yet, the release builds `:<sha>` itself first,
   which adds about ten minutes.
4. **Cut it**, either way:

   - **From the Actions tab**: open "Release", press "Run workflow", leave the
     branch on `main`, enter the version (`1.2.0`, no `v`) and run. The
     workflow checks the version against `pyproject.toml` and the gate
     against that commit, then creates the tag `v1.2.0` itself and carries
     on. This is also how an agent session cuts a release: its credentials
     can dispatch a workflow but cannot push a tag.
   - **From git**:

     ```bash
     git fetch origin main
     git tag -a v1.2.0 -m "Olano 1.2.0" origin/main
     git push origin v1.2.0
     ```

   A dispatched release is tied to the commit it started on: if `main` moves
   while the run is in progress, the release still tags, builds and promotes
   the commit the run verified. Dispatching a version whose tag already exists
   elsewhere fails; a version is released once.

5. **Watch the Release workflow** (Actions, "Release"). It verifies the version
   and the commit, builds the wheel and sdist, promotes the three images, and
   creates a GitHub Release with generated notes and the package attached. The
   job summary lists the digests.
6. **Tell the fleet.** The cloud console shows "Update available" on every box
   whose commit is behind `:latest`; owners press Update when it suits them.
   Nothing is pushed to a box without that press.

A pre-release (`v1.2.0-rc.1`, anything with a hyphen) gets `:1.2.0-rc.1` and a
GitHub pre-release only. `:latest` and `:1.2` do not move.

Publishing to PyPI is opt-in: set the repository variable `PUBLISH_TO_PYPI`
to `true` once the `Private :: Do Not Upload` classifier has been removed
from `pyproject.toml` and a `pypi` environment exists. Pre-releases are never
published there.

## Hotfixes

With a linear `main` and frequent releases, a hotfix is a fix merged to `main`
followed by a patch release. When `main` already carries unreleased work you
do not want to ship, branch `release/X.Y` from the last tag, cherry-pick the
fix, bump the patch version, and tag from that branch (or dispatch the Release
workflow on it). CI runs on `release/**` branches, so the gate still applies;
the release workflow builds the images itself because the edge pipeline never
built that commit.

## Rolling back

**The fleet:** release forward. Revert the offending change on `main`, cut a
patch release, and `:latest` moves to a known-good image that every box picks
up on its next Update. Do not re-point `:latest` at an older image by hand: a
box that already took the newer image would then be told it is behind the
older one, and the registry would no longer say which image a given release
produced.

**One box:** pin it. An org admin can `POST /api/v1/deployments/<id>/update`
with `{"tag": "1.1.3"}` (or a full `<sha>`); the update agent pulls that tag
on its next tick. A pinned box drops out of the "Update available" indicator
until it is pinned back to `{"tag": "latest"}`. On a self-hosted box, set
`OLANO_BACKEND_IMAGE`, `OLANO_DASHBOARD_IMAGE` and `OLANO_SANDBOX_IMAGE` in the
compose project's `.env` to the wanted tag and run `docker compose pull && docker compose up -d`.

## The docs

The user guide (`docs/user-guide/`, rendered by MkDocs Material) is published
by `docs.yml`, which keeps one directory per version on the `gh-pages` branch.
`docs.olano.ai` is a DigitalOcean App Platform static site (the `olano-docs`
app) that redeploys that branch on every push, so a publish is live about a
minute after the workflow finishes; GitHub Pages could serve the same branch
instead. The versions:

| Version | Written when |
|---|---|
| `edge` | A push to `main` touches the pages, or `docs.yml` is dispatched with no version |
| `X.Y` | `release.yml` calls `docs.yml` after the images are promoted, from the tagged commit; `latest` is re-pointed at it |

So a typo fixed on `main` is visible under `edge` at once and under `latest`
with the next patch release. To publish the pages of a commit as a version by
hand (the first release's docs were backfilled this way), dispatch **Docs**
with the version in its input. The same pages are concatenated into the guide
every agent carries (`scripts/gen_user_guide.py`), so there is one text to keep
right.

## What the cloud side does with a release

- The "Update available" indicator compares each box's reported commit with
  the commit baked into `:latest`. After this scheme that means "a newer
  release exists".
- The golden disk images used for new droplets are re-baked when `:latest`
  moves, so a new box starts on the current release.
- The image-freshness alarm in the provisioning worker watches `:edge`
  against `main`, so a stalled edge pipeline still pages while `:latest` sits
  still between releases by design.
- The known-bad-commit list that forces an update on affected boxes only
  helps once a newer release exists; cut the patch release first.

## Repository settings that back this up

- Protect the `v*` tags with a ruleset (Settings, Rules) so only maintainers
  can create them, and put GitHub Actions on that ruleset's bypass list so a
  dispatched release can still create its tag. Without the bypass the dispatch
  fails at the tag step and says so.
- Protect `main`: pull requests only, CI required, no force pushes.
- Keep `PUBLISH_TO_PYPI` unset until a PyPI release is a deliberate decision.
