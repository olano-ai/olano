#!/usr/bin/env bash
# Are every one of this commit's workflow runs green?
#
# The image pipelines ask this before they publish: an edge image is built only
# from a commit whose CI passed, and a release tag is promoted only when the
# tagged commit's checks are green. Both used to publish from `main` on a timer
# with no gate at all, so a merge that broke the test suite reached `:latest`,
# and from there every managed box whose owner pressed Update, before anyone
# read the red check.
#
# Usage:  checks-green.sh <sha>
# Prints one line per run, then a verdict line, and writes `verdict=<v>` to
# $GITHUB_OUTPUT when that is set. Always exits 0: the verdict is an answer,
# not a failure, and the caller decides what to do with it.
#
#   green    every run finished with success, skipped or neutral (a commit
#            with NO runs is green too: ci.yml ignores docs-only commits, and
#            a change that triggers no workflow cannot have failed one)
#   pending  at least one run is still queued or in progress
#   red      at least one run failed, was cancelled or timed out
#
# Left out, because they are not checks of the commit: runs of the publishing
# workflows themselves (build-images.yml, release.yml: the askers, not the
# evidence), Dependabot's own update jobs (event `dynamic`, which GitHub files
# against main's tip and which fail whenever an update cannot be resolved),
# and anything scheduled.
#
# Waiting. CHECKS_WAIT_SECONDS=<n> keeps asking while the verdict is `pending`,
# for up to n seconds, and answers with the first verdict that is not pending
# (`pending` again when the deadline passes: still an answer, and the caller
# still decides). Unset or 0 asks once. CHECKS_POLL_SECONDS (default 30) is the
# pause between two asks. The callers that are a person pressing a button set
# the wait: a dispatched edge build or release lands within the 45 minutes the
# commit's own checks can take, and failing at once only sends the person to
# `force=true`, which publishes an image nothing has tested. A red verdict ends
# the wait at once; a verdict that cannot be read is pending and is asked again.
#
# Reads the runs through `gh api`. CHECKS_RUNS_FILE=<path> substitutes a saved
# response (re-read on every ask), which is how tests/test_release_pipeline.py
# exercises every verdict without a network.

set -uo pipefail

sha="${1:-${GITHUB_SHA:-}}"
if [ -z "$sha" ]; then
  echo "usage: checks-green.sh <sha>" >&2
  exit 2
fi
repo="${GITHUB_REPOSITORY:-olano-ai/olano}"

# Only whole seconds; anything else is "do not wait", said once, rather than a
# bash arithmetic error that reads as the gate itself being broken.
seconds() {
  local name=$1 fallback=$2 value="${!1:-}"
  if [ -z "$value" ]; then
    echo "$fallback"
  elif [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$value"
  else
    echo "${name}=${value} is not a number of seconds; using ${fallback}" >&2
    echo "$fallback"
  fi
}
wait_seconds=$(seconds CHECKS_WAIT_SECONDS 0)
poll_seconds=$(seconds CHECKS_POLL_SECONDS 30)
[ "$poll_seconds" -gt 0 ] || poll_seconds=1

# One ask: lists the runs, prints the table, sets `verdict`.
ask() {
  local runs table pending red
  if [ -n "${CHECKS_RUNS_FILE:-}" ]; then
    runs=$(cat "$CHECKS_RUNS_FILE")
  else
    runs=$(gh api "repos/${repo}/actions/runs?head_sha=${sha}&per_page=100" 2>&1) || {
      echo "could not list workflow runs for ${sha:0:12}: ${runs}"
      verdict=pending
      return
    }
  fi

  # One line per run that counts, in the order the API gave them.
  table=$(jq -r '
    [ .workflow_runs[]
      | select((.path | test("/(build-images|release)\\.yml$")) | not)
      | select(.event != "dynamic" and .event != "schedule")
    ]
    | .[]
    | "\(.name)\t\(.event)\t\(.status)\t\(.conclusion // "-")"
  ' <<<"$runs")

  if [ -z "$table" ]; then
    echo "no workflow runs for ${sha:0:12} (nothing was triggered, nothing can have failed)"
    verdict=green
    return
  fi
  printf '%s\n' "$table" | awk -F'\t' '{ printf "  %-32s %-18s %-12s %s\n", $1, $2, $3, $4 }'
  pending=$(printf '%s\n' "$table" | awk -F'\t' '$3 != "completed"' | wc -l)
  red=$(printf '%s\n' "$table" | awk -F'\t' '$3 == "completed" && $4 != "success" && $4 != "skipped" && $4 != "neutral"' | wc -l)
  if [ "$red" -gt 0 ]; then
    verdict=red
  elif [ "$pending" -gt 0 ]; then
    verdict=pending
  else
    verdict=green
  fi
}

verdict=pending
started=$(date +%s)
# `date +%s` truncates, so `started` can be up to a second before the real
# start; a deadline of started + wait could give up after wait - 1 seconds.
# The extra second makes the wait at least what was asked (it is only read
# inside the loop, which wait 0 never enters).
deadline=$((started + wait_seconds + 1))
ask
while [ "$verdict" = "pending" ] && [ "$wait_seconds" -gt 0 ]; do
  now=$(date +%s)
  if [ "$now" -ge "$deadline" ]; then
    echo "checks for ${sha:0:12} are still running after waiting $((now - started))s; giving up"
    break
  fi
  remaining=$((deadline - now))
  pause=$((poll_seconds < remaining ? poll_seconds : remaining))
  echo "checks for ${sha:0:12} are still running; asking again in ${pause}s ($((now - started))s of ${wait_seconds}s waited)"
  sleep "$pause"
  echo "$(date -u +%H:%M:%S) asking again"
  ask
done

echo "verdict=${verdict}"
[ -n "${GITHUB_OUTPUT:-}" ] && echo "verdict=${verdict}" >> "$GITHUB_OUTPUT"
exit 0
