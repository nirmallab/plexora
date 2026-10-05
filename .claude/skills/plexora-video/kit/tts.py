"""Synthesise each narration line with Kokoro (local, no account, nothing to license).

    tts-venv/bin/python tts.py [voice] [speed]      # default af_heart 0.95

Reads vo_script.json ([[id, text], ...]); writes vo/<id>.wav (24 kHz, silence
trimmed) and vo/durations.json -- time the storyboard's `voice` cues from it.
US voices start "a" (af_heart, am_michael), UK voices "b" (bf_emma, bm_george).
Setup: audio_setup.sh.
"""
import json, os, shutil, sys, tempfile
import numpy as np, soundfile as sf
from kokoro_onnx import Kokoro, EspeakConfig


def espeak_data():
    """espeak-ng copies its data path into a short fixed buffer: a long venv path
    (a scratchpad) fails with "phontab not found". Use a short copy."""
    if os.environ.get("PV_ESPEAK_DATA"):
        return os.environ["PV_ESPEAK_DATA"]
    import espeakng_loader
    short = os.path.join(tempfile.gettempdir(), "pv-espeak-data")
    if not os.path.isdir(short):
        shutil.copytree(espeakng_loader.get_data_path(), short)
    return short


voice = sys.argv[1] if len(sys.argv) > 1 else "af_heart"
speed = float(sys.argv[2]) if len(sys.argv) > 2 else 0.95
here = os.path.dirname(os.path.abspath(__file__))
k = Kokoro(f"{here}/tts/kokoro-v1.0.onnx", f"{here}/tts/voices-v1.0.bin",
           espeak_config=EspeakConfig(data_path=espeak_data()))
lang = "en-us" if voice[0] == "a" else "en-gb"
os.makedirs("vo", exist_ok=True)
out = {}
for key, text in json.load(open("vo_script.json")):
    a, sr = k.create(text, voice=voice, speed=speed, lang=lang)
    # trim the model's leading / trailing silence to 40 / 80 ms
    idx = np.nonzero(np.abs(a) > 0.01)[0]
    a = a[max(0, idx[0] - int(0.04 * sr)): idx[-1] + int(0.08 * sr)]
    sf.write(f"vo/{key}.wav", a.astype(np.float32), sr)
    out[key] = round(len(a) / sr, 2)
json.dump({"voice": voice, "speed": speed, "sr": sr, "durations": out}, open("vo/durations.json", "w"), indent=1)
print(json.dumps(out), "total", round(sum(out.values()), 1))
