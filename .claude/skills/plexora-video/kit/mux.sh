#!/bin/bash
# mux.sh <video.mp4> <soundtrack.wav> <out.mp4> [preview]  -- AAC 192k; "preview" re-encodes 720p ~2.5 Mbit/s
set -e
cd "$(dirname "$0")"
FF=$(ls pylib/imageio_ffmpeg/binaries/ffmpeg* | head -1)
if [ "$4" = "preview" ]; then
  "$FF" -y -loglevel error -i "$1" -i "$2" -map 0:v -map 1:a -vf "scale=1280:720:flags=lanczos,format=yuv420p" \
    -c:v libx264 -preset slow -b:v 2100k -maxrate 3000k -bufsize 5000k -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
    -c:a aac -b:a 160k -shortest -movflags +faststart "$3"
else
  "$FF" -y -loglevel error -i "$1" -i "$2" -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest -movflags +faststart "$3"
fi
ls -la "$3"
