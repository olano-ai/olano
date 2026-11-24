#!/usr/bin/env bash
# Turns the recorded frames into an MP4 (H.264, 60 fps, AAC voiceover + music or a silent track, faststart).
#   [DEMO=tour] demo/build.sh landscape|portrait  ->  demo/videos/olano-<demo>-<layout>.mp4
set -euo pipefail
L=${1:-landscape}
export DEMO=${DEMO:-tour}
WORK=${DEMO_WORK:-$(cd "$(dirname "$0")" && pwd)/.work}
FFMPEG=${FFMPEG:-ffmpeg}
D=$WORK/frames-$DEMO-$L
mkdir -p "$(dirname "$0")/videos"
OUT=$(cd "$(dirname "$0")" && pwd)/videos/olano-$DEMO-$L.mp4
node -e "
const fs = require('fs'), ix = JSON.parse(fs.readFileSync('$D/index.json'));
let out = '';
ix.forEach((f, i) => { out += \"file '\" + f.file + \"'\nduration \" + (i < ix.length - 1 ? ix[i + 1].ts - f.ts : 0.5).toFixed(5) + '\n'; });
fs.writeFileSync('$D/list.txt', out + \"file '\" + ix.at(-1).file + \"'\n\");"
if [ "$L" = portrait ]; then S=1080:1920; else S=1920:1080; fi
# Voiceover and music (audio/<demo>/) when present; otherwise a silent track
if [ -f "$(dirname "$0")/audio/$DEMO/music.mp3" ]; then
  HOLD=$(node "$(dirname "$0")/mix.mjs" "$D" "$FFMPEG")
  AUDIO=(-i "$D/mix.wav"); TPAD=",tpad=stop_mode=clone:stop_duration=$HOLD"
else
  AUDIO=(-f lavfi -i anullsrc=r=48000:cl=stereo); TPAD=""
fi
nice -n 19 "$FFMPEG" -v error -y -f concat -safe 0 -i "$D/list.txt" "${AUDIO[@]}" \
  -vf "scale=$S:flags=lanczos:out_range=tv:out_color_matrix=bt709,fps=60,format=yuv420p$TPAD" -c:v libx264 -preset slow -crf 14 -profile:v high -tune animation -x264-params colorprim=bt709:transfer=bt709:colormatrix=bt709 \
  -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a aac -b:a 192k -shortest -movflags +faststart -threads 4 "$OUT"
echo "$OUT"
