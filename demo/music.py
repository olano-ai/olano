"""Composes a demo's music bed: a soft, original piano + pad piece (~80 bpm, D major) made from scratch
with numpy, so it has no licence attached. The tempo is fitted so its closing chord lands as the end card
appears. Writes audio/<demo>/music.mp3 (with the ffmpeg in FFMPEG, default ffmpeg); mix.mjs ducks it
under the voiceover.
  python music.py <demo> <end card seconds>   (repo venv: needs numpy; the end time is in marks.json)"""

import os
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
DEMO, END = sys.argv[1], float(sys.argv[2])
BARS = max(4, round(END / 3))  # about 80 bpm, fitted so the closing chord starts at END
BAR = END / BARS
BEAT = BAR / 4
rng = np.random.default_rng(7)


def hz(m):
    return 440 * 2 ** ((m - 69) / 12)


# D major: Dmaj9 Bm7 Gmaj7 A6sus4 over and over, ending on A6sus4, then a held Dmaj9 with the end card
D, Bm, G, A = (
    [50, 57, 62, 64, 66, 69],
    [47, 54, 59, 62, 66, 69],
    [43, 55, 59, 62, 66, 71],
    [45, 52, 57, 62, 64, 69],
)
PROG = ([D, Bm, G, A] * 16)[-BARS:]
TOTAL = len(PROG) * BAR + 6.0  # the last chord rings out
N = int(TOTAL * SR)
L = np.zeros(N)
R = np.zeros(N)


def piano(m, dur, vel):
    """Soft felt-piano tone: inharmonic partials, faster decay up the series, felt-hammer attack."""
    f = hz(m)
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, 9):
        fk = f * k * np.sqrt(1 + 0.0004 * k * k)
        if fk > 9000:
            break
        amp = (1 / k**1.6) * (0.6 if k == 2 else 1)
        dec = np.exp(-t * (0.9 + 0.55 * k) * (f / 260) ** 0.35)
        out += amp * dec * np.sin(2 * np.pi * fk * t + rng.uniform(0, 6.28))
    att = np.minimum(1, t / 0.012)
    rel = np.minimum(1, (dur - t) / 0.25)
    return vel * out * att * np.clip(rel, 0, 1)


def pad(ms, dur):
    """Warm pad: detuned sines with soft 2nd/3rd harmonics, slow swell."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros((2, n))
    for m in ms:
        for side, det in ((0, -0.06), (1, 0.06)):
            f = hz(m) * 2 ** (det / 12)
            out[side] += (
                np.sin(2 * np.pi * f * t)
                + 0.18 * np.sin(4 * np.pi * f * t)
                + 0.05 * np.sin(6 * np.pi * f * t)
            )
    env = np.minimum(1, t / 1.4) * np.minimum(1, (dur - t) / 1.2).clip(0, 1)
    return out * env * (1 + 0.08 * np.sin(2 * np.pi * 0.25 * t))


def add(sig, at, pan=0.0, gain=1.0):
    i = int(at * SR)
    s = sig if sig.ndim == 2 else np.vstack([sig * (1 - pan) ** 0.5, sig * (1 + pan) ** 0.5])
    j = min(N, i + s.shape[1])
    L[i:j] += gain * s[0, : j - i]
    R[i:j] += gain * s[1, : j - i]


# arpeggio pattern over each bar in eighth notes (indices into the chord's upper voices)
PATTERN = [1, 3, 2, 4, 5, 3, 4, 2]
for b, ch in enumerate(PROG):
    t0 = b * BAR
    add(pad(ch[1:], BAR + 1.3), t0, gain=0.05)
    add(piano(ch[0] - 12 if ch[0] > 45 else ch[0], BAR + 0.8, 0.55), t0, pan=-0.2)  # bass note
    add(piano(ch[0], BAR, 0.32), t0 + 0.01, pan=-0.1)
    for e, k in enumerate(PATTERN):
        if b == 0 and e < 2:
            continue  # ease in
        vel = (0.30 if e % 2 == 0 else 0.22) * rng.uniform(0.85, 1.1)
        swing = rng.normal(0, 0.008)
        note = ch[k] + (
            12 if (b % 4 == 3 or b == len(PROG) - 1) and e == 4 else 0
        )  # a lift each phrase
        add(piano(note, 2.4, vel), t0 + e * BEAT / 2 + swing, pan=(k - 3) * 0.15)
# final chord, rolled and held
end = len(PROG) * BAR
add(pad(D[1:], 6.0), end, gain=0.05)
for i, m in enumerate([38, 50, 57, 64, 66, 69, 74]):
    add(piano(m, 6.0, 0.4 if i < 2 else 0.28), end + i * 0.045, pan=(i - 3) * 0.12)


# gentle low-pass (one pole, ~5 kHz) and a soft room reverb (decaying stereo noise impulse)
def lowpass(x, fc=5000):
    a = np.exp(-2 * np.pi * fc / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(0, len(x), 4096):
        seg = x[i : i + 4096]
        out = np.empty_like(seg)
        for j, v in enumerate(seg):
            acc = (1 - a) * v + a * acc
            out[j] = acc
        y[i : i + 4096] = out
    return y


def reverb(x, seed):
    n = int(2.6 * SR)
    t = np.arange(n) / SR
    ir = np.random.default_rng(seed).normal(0, 1, n) * np.exp(-t * 2.6)
    ir[: int(0.012 * SR)] = 0
    ir /= np.sqrt((ir**2).sum())
    m = 1 << int(np.ceil(np.log2(len(x) + n)))
    return np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(ir, m), m)[: len(x)]


L, R = lowpass(L), lowpass(R)
L, R = 0.78 * L + 0.32 * reverb(L, 1), 0.78 * R + 0.32 * reverb(R, 2)
x = np.vstack([L, R])
x *= 0.5 / np.abs(x).max()  # peak -6 dBFS; mix.mjs sets the final level
fade = int(3 * SR)
x[:, -fade:] *= np.linspace(1, 0, fade) ** 2
out = str(Path(__file__).resolve().parent / "audio" / DEMO / "music.wav")
with wave.open(out, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes((x.T * 32767).astype("<i2").tobytes())
import subprocess

subprocess.run(
    [
        os.environ.get("FFMPEG", "ffmpeg"),
        "-v",
        "error",
        "-y",
        "-i",
        out,
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        out[:-3] + "mp3",
    ],
    check=True,
)
Path(out).unlink()
print(out[:-3] + "mp3", f"{TOTAL:.1f} s")
