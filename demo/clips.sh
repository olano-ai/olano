#!/usr/bin/env bash
# Cuts a demo video into one short clip per chapter for social posts: the chapter (its caption, its
# voice line) followed by the end card, with a short crossfade. The chapters are the steps in
# marks.json (record.mjs); steps listed in JOIN run on into the next one.
#   [DEMO=tour] demo/clips.sh [landscape] [portrait]  ->  demo/videos/clips/<demo>-<n>-<step>-<layout>.mp4
set -euo pipefail
cd "$(dirname "$0")"
DEMO=${DEMO:-tour}
WORK=${DEMO_WORK:-$(cd "$(dirname "$0")" && pwd)/.work}
FFMPEG=${FFMPEG:-ffmpeg}
FF=$FFMPEG
JOIN=${JOIN:-}                   # steps that run on into the next one, e.g. JOIN="ask work"
mkdir -p videos/clips
for L in ${*:-portrait landscape}; do
  SRC=videos/olano-$DEMO-$L.mp4
  TOTAL=$({ "$FF" -i "$SRC" 2>&1 || true; } | sed -n 's/.*Duration: \([0-9:.]*\).*/\1/p' | awk -F: '{print $1*3600+$2*60+$3}')
  node -e "
    const m = require('$WORK/frames-$DEMO-$L/marks.json'), join = '$JOIN'.split(' ');
    const steps = Object.keys(m).filter((k) => k !== 'stop' && k !== 'end');
    let n = 0;
    steps.forEach((k, i) => {
      if (i > 0 && join.includes(steps[i - 1])) return;            // already inside the step before
      let j = i + 1; while (join.includes(steps[j - 1]) && j < steps.length) j++;
      const to = j < steps.length ? m[steps[j]] : m.end;
      console.log(++n, k, (m[k] + 0.25).toFixed(2), (to - 0.1).toFixed(2), m.end.toFixed(2));
    });" | while read -r N STEP A B END; do
    OUT=videos/clips/$DEMO-$N-$STEP-$L.mp4
    LEN=$(awk "BEGIN{print $B-$A}"); X=0.5
    # the end card once it has settled (video), with the closing line from its start (audio)
    nice -n 19 "$FF" -v error -y -ss "$A" -t "$LEN" -i "$SRC" -ss "$(awk "BEGIN{print $END+1.3}")" -i "$SRC" -ss "$END" -i "$SRC" -filter_complex "
      [0:v]setpts=PTS-STARTPTS,fade=t=in:d=0.3:color=white[a];[1:v]setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=1.3[b];[a][b]xfade=transition=fade:duration=$X:offset=$(awk "BEGIN{print $LEN-$X}"),format=yuv420p[v];
      [0:a]asetpts=PTS-STARTPTS,afade=t=in:d=0.3[aa];[2:a]asetpts=PTS-STARTPTS[ab];[aa][ab]acrossfade=d=$X[au]" \
      -map '[v]' -map '[au]' -c:v libx264 -preset slow -crf 16 -profile:v high -r 60 -c:a aac -b:a 192k \
      -movflags +faststart -threads 4 "$OUT" </dev/null
    echo "$OUT  $(awk "BEGIN{printf \"%.1f\", $LEN+$TOTAL-$END-$X}") s"
  done
done
