// Mixes a demo's voiceover and music (audio/<DEMO>/, from voiceover.mjs and music.py) for one recording: each line starts just
// after its step (marks.json from record.mjs) and never overlaps the one before; the music ducks under
// the voice, fades out with the end card and the whole mix is normalised to -14 LUFS (YouTube/Reels/TikTok).
//   node mix.mjs <frames dir> <ffmpeg>   ->  <frames dir>/mix.wav; prints the seconds to hold the last frame
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';

const [D, FF] = process.argv.slice(2);
const A = process.env.DEMO_AUDIO || path.join(path.dirname(new URL(import.meta.url).pathname), 'audio', process.env.DEMO || 'tour');
const marks = JSON.parse(fs.readFileSync(path.join(D, 'marks.json')));
const STEPS = Object.keys(marks).filter((k) => k !== 'stop');     // one voice line per step, in order
const LEAD = STEPS.map((_, i) => (i ? 0.35 : 0.5));              // pause between a step starting and its line
const ix = JSON.parse(fs.readFileSync(path.join(D, 'index.json')));
const videoEnd = ix.at(-1).ts - ix[0].ts + 0.5;          // build.sh shows the last frame for 0.5 s

// speech span of a clip (trims the silence the TTS leaves at either end)
function span(f) {
  const r = spawnSync(FF, ['-hide_banner', '-i', f, '-af', 'silencedetect=n=-50dB:d=0.05', '-f', 'null', '-'], { encoding: 'utf8' }).stderr;
  const [h, m, s] = r.match(/Duration: (\d+):(\d+):([\d.]+)/).slice(1).map(Number), dur = h * 3600 + m * 60 + s;
  const st = [...r.matchAll(/silence_start: ([\d.]+)/g)].map((x) => +x[1]), en = [...r.matchAll(/silence_end: ([\d.]+)/g)].map((x) => +x[1]);
  const a = st.length && st[0] < 0.01 && en.length ? en[0] : 0;
  const b = st.length && st.at(-1) > dur - 0.6 && (en.length < st.length || en.at(-1) >= dur - 0.01) ? st.at(-1) : dur;
  return { a: Math.max(0, a - 0.03), b: Math.min(dur, b + 0.08) };
}
let t = 0;
const place = STEPS.map((s, i) => {
  const { a, b } = span(path.join(A, `vo${i}.mp3`));
  const at = Math.max(marks[s] + LEAD[i], t + 0.3);
  t = at + (b - a);
  return { i, a, b, at };
});
const total = Math.max(videoEnd, t + 1.6);              // the end card holds until the last line has settled
const hold = total - videoEnd;

const inputs = place.flatMap(({ i }) => ['-i', path.join(A, `vo${i}.mp3`)]).concat(['-i', path.join(A, 'music.mp3')]);
const M = place.length;
let fc = place.map(({ i, a, b, at }) =>
  `[${i}:a]atrim=${a.toFixed(3)}:${b.toFixed(3)},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,adelay=${Math.round(at * 1000)}:all=1[v${i}]`).join(';');
fc += `;${place.map(({ i }) => `[v${i}]`).join('')}amix=inputs=${M}:normalize=0,highpass=f=70,`
  + `acompressor=threshold=0.1:ratio=2.5:attack=8:release=120:makeup=1.5,apad,atrim=0:${total.toFixed(3)},asplit[vo][sc]`;
fc += `;[${M}:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:${total.toFixed(3)},volume=-15dB,`
  + `afade=t=in:d=1.2,afade=t=out:st=${(total - 2.8).toFixed(3)}:d=2.8[m0]`;
fc += `;[m0][sc]sidechaincompress=threshold=0.02:ratio=5:attack=60:release=700:knee=4[m]`;
fc += `;[vo][m]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000[out]`;
execFileSync(FF, ['-v', 'error', '-y', ...inputs, '-filter_complex', fc, '-map', '[out]', '-ar', '48000', '-c:a', 'pcm_s16le', path.join(D, 'mix.wav')], { stdio: ['ignore', 'ignore', 'inherit'] });
for (const p of place) console.error(`  vo${p.i} ${p.at.toFixed(2)}–${(p.at + p.b - p.a).toFixed(2)} s`);
console.error(`  video ${videoEnd.toFixed(2)} s, end card held +${hold.toFixed(2)} s`);
console.log(hold.toFixed(3));
