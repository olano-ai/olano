#!/usr/bin/env bash
# Records and builds a demo's videos: the take, the voice (if not made yet), the music (fitted to the
# take's end card), then the MP4. A watchdog stops the run if free memory falls below 1.2 GB.
#   [DEMO=tour] [SLOW=3] demo/make.sh [landscape] [portrait]
set -uo pipefail
cd "$(dirname "$0")"
export DEMO=${DEMO:-tour}
WORK=${DEMO_WORK:-$PWD/.work}
LAYOUTS=${*:-landscape portrait}
record() {   # one take; a take that gets stuck before recording (exit 3) is tried again
  for try in 1 2 3 4; do
    LAYOUT=$1 DSF=${DSF:-1.25} node record.mjs &
    local pid=$!
    while kill -0 $pid 2>/dev/null; do
      avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
      if [ "$avail" -lt 1200 ]; then echo "low memory (${avail} MB): stopping"; pkill -P $pid; kill $pid; exit 1; fi
      sleep 2
    done
    wait $pid; local rc=$?
    [ $rc = 0 ] && return 0
    [ $rc = 3 ] || return $rc
    echo "retrying $1 ($try)"
  done
  return 1
}
for L in $LAYOUTS; do
  record "$L" || exit 1
  if [ ! -f "audio/$DEMO/vo0.mp3" ] && [ -n "${OPENAI_API_KEY:-}" ]; then node voiceover.mjs "$DEMO" || exit 1; fi
  if [ -f "audio/$DEMO/vo0.mp3" ] && { [ ! -f "audio/$DEMO/music.mp3" ] || [ -n "${MUSIC:-}" ]; }; then
    END=$(node -e "console.log(require('$WORK/frames-$DEMO-$L/marks.json').end.toFixed(1))")
    (cd .. && uv run --quiet python demo/music.py "$DEMO" "$END") || exit 1
  fi
  ./build.sh "$L" || exit 1
done
