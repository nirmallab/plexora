#!/bin/bash
# audio_setup.sh -- the narration toolchain, inside the work dir only:
# a venv with kokoro-onnx (+ phonemizer-fork, espeakng-loader) and the
# Kokoro v1.0 model files (~350 MB). Mixing uses the plexora env (numpy, scipy).
set -e
cd "$(dirname "$0")"
PY=${PY:-python3}
"$PY" -m venv tts-venv
tts-venv/bin/pip install -q kokoro-onnx soundfile
mkdir -p tts
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [ -s "tts/$f" ] || curl -sSL -o "tts/$f" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
done
ls -la tts
