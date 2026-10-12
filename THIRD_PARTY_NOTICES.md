# Third-party software distributed with Plexora

Plexora's own licence is in `LICENSE`. The components below are distributed
with it under their own terms.

## FFmpeg (desktop app, and `pip install "plexora[video]"`)

Video Studio encodes MP4, WebM, ProRes and GIF on the server with an FFmpeg
binary, run as a separate process; Plexora never links or imports it. The
binary comes from the `imageio-ffmpeg` package, which the optional extra
`plexora[video]` installs and the desktop app bundles
(`scripts/release.py RUNTIME_EXTRAS`).

That build of FFmpeg is configured with `--enable-gpl` and includes x264 and
x265, so the binary is licensed under the **GNU General Public License,
version 2 or later**. Its source code is available from the FFmpeg project
(https://ffmpeg.org) and the build recipe from imageio-ffmpeg
(https://github.com/imageio/imageio-ffmpeg). **Before distributing the
desktop app, the distributor must meet the GPL's terms for this binary,
including the offer of its corresponding source.**

## mediabunny 1.60.0 (browser)

`plexora/plugins/video_studio/static/vendor/mediabunny-1.60.0.mjs`, the MP4 and
WebM muxer Video Studio's browser encoder uses, is mediabunny
(https://github.com/Vanilagy/mediabunny), unmodified, under the **Mozilla
Public License 2.0** (`plexora/plugins/video_studio/static/vendor/
LICENSE-mediabunny`). Its source is the published npm package of that version;
how the bundled file is built is in that directory's `README.md`.
