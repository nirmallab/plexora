"""The soundtrack: narration lines over synthesised, quiet interface sounds.

    python mix.py audio.json vo/ soundtrack.wav [--no-sfx] [--no-voice]

Every sound is made here from sines and filtered noise (nothing to license),
placed at the storyboard's own times (audio_events.mjs). Levels: narration
near -16 LUFS-ish (peaks -3 dBFS); effects 14-26 dB under it, and ducked a
further 5 dB while a line is being spoken. 48 kHz stereo, a slight width on
the effects only, the voice dead centre.
"""
import json
import os
import subprocess
import sys

import numpy as np
from scipy import signal
from scipy.io import wavfile

SR = 48000
RNG = np.random.default_rng(7)


def db(x):
    return 10 ** (x / 20)


def env(n, attack, release, shape=3.0):
    """An attack ramp then an exponential-ish release, n samples."""
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    r = np.exp(-shape * np.maximum(t - attack, 0) / max(release, 1e-4))
    return a * r


def bandpass(x, lo, hi, order=2):
    sos = signal.butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def highpass(x, f, order=2):
    return signal.sosfilt(signal.butter(order, f, btype="high", fs=SR, output="sos"), x)


# -- the sounds --------------------------------------------------------------------------

def click():
    """A trackpad-ish click: a short bright tick, then a softer release tick."""
    def tick(gain, f):
        n = int(0.03 * SR)
        noise = highpass(RNG.standard_normal(n), 2500) * env(n, 0.0005, 0.006, 4)
        tone = np.sin(2 * np.pi * f * np.arange(n) / SR) * env(n, 0.0005, 0.01, 4)
        return gain * (0.6 * noise / (np.abs(noise).max() + 1e-9) + 0.4 * tone)
    out = np.zeros(int(0.12 * SR))
    a = tick(1.0, 2100)
    out[:a.size] += a
    b = tick(0.45, 1700)
    out[int(0.075 * SR):int(0.075 * SR) + b.size] += b
    return out * db(-14)


def whoosh(dur, gain=-27):
    """Air past the lens: band noise swept up then down, a sin^2 swell."""
    n = int(max(dur, 0.6) * SR)
    t = np.linspace(0, 1, n)
    noise = RNG.standard_normal(n)
    # sweep the band by filtering in short blocks
    out = np.zeros(n)
    block = 2048
    for i in range(0, n, block):
        c = 500 + 1700 * np.sin(np.pi * t[min(i, n - 1)]) ** 1.5
        seg = noise[i:i + block]
        out[i:i + block] = bandpass(seg, c * 0.6, c * 1.6)
    swell = np.sin(np.pi * t) ** 2
    out = out * swell
    return out / (np.abs(out).max() + 1e-9) * db(gain)


def swish(dur):
    return whoosh(min(max(dur, 0.6), 1.2), gain=-33)


def chime(freqs, decay=0.9, gain=-20, thump=False):
    n = int((decay * 2.2) * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k, f in enumerate(freqs):
        out += (0.7 ** k) * np.sin(2 * np.pi * f * t) * env(n, 0.004, decay * (1 - 0.15 * k), 4)
        out += 0.18 * (0.7 ** k) * np.sin(2 * np.pi * 2.76 * f * t) * env(n, 0.002, decay * 0.35, 4)  # bell partial
    if thump:
        out += 0.5 * np.sin(2 * np.pi * 110 * t) * env(n, 0.003, 0.18, 4)
    return out / (np.abs(out).max() + 1e-9) * db(gain)


def card():
    """The agent card updating: a soft rising blip."""
    n = int(0.22 * SR)
    t = np.arange(n) / SR
    f = 880 + 440 * np.clip(t / 0.06, 0, 1)
    phase = 2 * np.pi * np.cumsum(f) / SR
    out = np.sin(phase) * env(n, 0.003, 0.07, 4)
    return out / (np.abs(out).max() + 1e-9) * db(-30)


def arpeggio(freqs, step=0.09, gain=-22):
    pieces = [chime([f], decay=0.8, gain=0) for f in freqs]
    n = int(step * SR) * len(freqs) + max(p.size for p in pieces)
    out = np.zeros(n)
    for i, p in enumerate(pieces):
        out[int(i * step * SR):int(i * step * SR) + p.size] += p * (0.85 ** i)
    return out / (np.abs(out).max() + 1e-9) * db(gain)


def swell(dur=4.5, gain=-21):
    """The end card: a warm low chord that blooms and fades."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for f, a in ((110, 1.0), (164.8, 0.7), (220, 0.6), (329.6, 0.35), (440, 0.2)):
        out += a * np.sin(2 * np.pi * f * t + RNG.uniform(0, 6.28))
    shape = np.clip(t / 1.2, 0, 1) ** 2 * np.exp(-np.maximum(t - 1.2, 0) / 1.6)
    out = out * shape
    return out / (np.abs(out).max() + 1e-9) * db(gain)


SOUNDS = {
    "click": lambda e: click(),
    "whoosh": lambda e: whoosh(e.get("dur", 1.5)),
    "swish": lambda e: swish(e.get("dur", 1.0)),
    "card": lambda e: card(),
    "reveal_exclude": lambda e: chime([523.25, 784.0], decay=0.9, gain=-21, thump=True),
    "reveal_warn": lambda e: chime([659.25], decay=0.7, gain=-25),
    "reveal_all": lambda e: arpeggio([523.25, 659.25, 784.0, 1046.5]),
    "done": lambda e: arpeggio([659.25, 987.77], step=0.14, gain=-21),
    "end": lambda e: swell(),
}


# -- voice ---------------------------------------------------------------------------------

def load_voice(path, ffmpeg):
    """A narration line at 48 kHz mono float, peak -3 dBFS."""
    tmp = path + ".48k.wav"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", path, "-ar", str(SR), "-ac", "1",
                    "-c:a", "pcm_f32le", tmp], check=True)
    sr, x = wavfile.read(tmp)
    os.remove(tmp)
    x = x.astype(np.float64)
    x = highpass(x, 70)
    return x / (np.abs(x).max() + 1e-9) * db(-3)


def main():
    events_path, vo_dir, out_path = sys.argv[1:4]
    flags = set(sys.argv[4:])
    spec = json.load(open(events_path))
    ffmpeg = next(os.path.join("pylib/imageio_ffmpeg/binaries", f)
                  for f in os.listdir("pylib/imageio_ffmpeg/binaries") if f.startswith("ffmpeg"))
    n = int((spec["duration"] + 1.0) * SR)
    voice = np.zeros(n)
    fx = np.zeros((n, 2))
    spoken = np.zeros(n, dtype=bool)
    for e in spec["events"]:
        i = int(e["t"] * SR)
        if e["kind"] == "voice":
            if "--no-voice" in flags:
                continue
            x = load_voice(os.path.join(vo_dir, e["id"] + ".wav"), ffmpeg)
            j = min(n, i + x.size)
            voice[i:j] += x[:j - i]
            spoken[max(0, i - int(0.15 * SR)):min(n, j + int(0.25 * SR))] = True
            continue
        if "--no-sfx" in flags or e["kind"] not in SOUNDS:
            continue
        x = SOUNDS[e["kind"]](e)
        j = min(n, i + x.size)
        pan = {"click": 0.15, "card": -0.25, "whoosh": 0.0, "swish": 0.0}.get(e["kind"], 0.0)
        fx[i:j, 0] += x[:j - i] * np.sqrt(0.5 * (1 - pan))
        fx[i:j, 1] += x[:j - i] * np.sqrt(0.5 * (1 + pan))
    # duck the effects under speech (smoothed so it breathes, not pumps)
    duck = np.where(spoken, db(-5), 1.0)
    k = int(0.12 * SR)
    duck = np.convolve(duck, np.ones(k) / k, mode="same")
    fx *= duck[:, None]
    mix = fx + voice[:, None] * np.sqrt(0.5) * 1.41
    peak = np.abs(mix).max()
    if peak > db(-1):
        mix *= db(-1) / peak
    wavfile.write(out_path, SR, mix.astype(np.float32))
    print(f"wrote {out_path}: {n / SR:.1f} s, peak {20 * np.log10(np.abs(mix).max() + 1e-12):.1f} dBFS")


if __name__ == "__main__":
    main()
