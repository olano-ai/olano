#!/usr/bin/env bash
# Did a pull-request run of this workflow already pass on this exact tree?
#
# A merged PR used to pay for its slowest suite twice: once on the PR (which
# tests `refs/pull/N/merge`, the PR merged into main as main stood at that
# push) and again on the push to main. When main has not moved since the PR's
# last run, the squash commit's tree IS the tree the PR run tested, so the
# second run re-tests identical bytes. Measured on October 2026: 76 of the 107
# post-merge E2E runs were exactly that, about 2,700 billed minutes.
#
# The proof is the git tree hash, never a branch name or a timestamp: the PR run
# records `git rev-parse HEAD^{tree}` in a small artifact when it passes, and
# this script answers `skip=true` only when that recorded tree equals the tree
# of the commit being checked. Any other answer, including every error on the
# way, is `skip=false`: the suite runs. A tree covers every file in the repo,
# workflow files included, so an equal tree means the same code ran the same
# suite under the same workflow.
#
# Usage:  tree-already-tested.sh <workflow file> <artifact name> [sha]
# Needs GITHUB_REPOSITORY, a checkout holding <sha> (default $GITHUB_SHA), and a
# `gh` that can read actions and pull requests. Prints its reason, then
# `skip=<true|false>`, and writes the same to $GITHUB_OUTPUT when that is set.
# Always exits 0: the answer is a decision for the caller, not a failure.
set -uo pipefail

workflow="${1:?usage: tree-already-tested.sh <workflow file> <artifact name> [sha]}"
artifact="${2:?usage: tree-already-tested.sh <workflow file> <artifact name> [sha]}"
sha="${3:-${GITHUB_SHA:-}}"
repo="${GITHUB_REPOSITORY:-}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

answer() {
  echo "$2"
  echo "skip=$1"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "skip=$1" >> "$GITHUB_OUTPUT"
  fi
  exit 0
}

[ -n "$sha" ] && [ -n "$repo" ] || answer false "no commit or repository given; running the suite"

tree="$(git rev-parse "${sha}^{tree}" 2>/dev/null)" ||
  answer false "cannot read the tree of ${sha:0:12}; running the suite"

pr="$(gh api "repos/${repo}/commits/${sha}/pulls" \
  --jq '[.[] | select(.merged_at != null)] | .[0].number // empty' 2>/dev/null)" || pr=""
[ -n "$pr" ] || answer false "no merged pull request carries ${sha:0:12}; running the suite"

branch="$(gh api "repos/${repo}/pulls/${pr}" --jq '.head.ref' 2>/dev/null)" || branch=""
[ -n "$branch" ] || answer false "cannot read the branch of PR #${pr}; running the suite"

# The newest passing PR runs of this workflow on that branch. A run that tested
# an older push of the PR simply records a different tree and does not match.
runs="$(gh api "repos/${repo}/actions/workflows/${workflow}/runs?event=pull_request&status=success&branch=${branch}&per_page=5" \
  --jq '.workflow_runs[].id' 2>/dev/null)" || runs=""

for run in $runs; do
  ids="$(gh api "repos/${repo}/actions/runs/${run}/artifacts" \
    --jq ".artifacts[] | select(.name == \"${artifact}\" and .expired == false) | .id" 2>/dev/null)" || ids=""
  for id in $ids; do
    gh api "repos/${repo}/actions/artifacts/${id}/zip" > "$work/tree.zip" 2>/dev/null || continue
    tested="$(unzip -p "$work/tree.zip" tested-tree.txt 2>/dev/null | tr -d '[:space:]')"
    if [ "$tested" = "$tree" ]; then
      answer true "PR #${pr} run ${run} passed on tree ${tree:0:12}, the tree of ${sha:0:12}; not running it again"
    fi
  done
done

answer false "no passing PR run of ${workflow} tested tree ${tree:0:12} (main moved, or the PR changed after its last run); running the suite"
