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

## OpenSlide, as @computationalpathologygroup/openslide-js 1.0.0-beta.5 (website)

`plexora/client/external/openslide-js-1.0.0-beta.5/`, the whole-slide reader
the website's page uses for MRXS, NDPI, SVS and Sakura slides
(`client/src/js/node/slideReader.js`), is openslide-js
(https://github.com/computationalpathologygroup/openslide-js): OpenSlide 4.0.0
(https://openslide.org) and the libraries it links -- GLib, Cairo, gdk-pixbuf,
fontconfig, pixman, FreeType, libtiff, libjpeg-turbo, OpenJPEG, libpng,
libxml2, expat, zlib, zstd, libffi, SQLite -- compiled to WebAssembly, under
the **GNU Lesser General Public License, version 2.1 only**. Its files are as
published (the npm package's ES-module build, worker, glue and `.wasm`); the
`.wasm` is a separate file the page loads at run time, never linked into
Plexora's code, and the desktop app does not load it. The licence (`LICENSE`),
every bundled component's own licence (`NOTICE`), and where the corresponding
source is -- the openslide-js repository at the commit the package was built
from, whose `wasm/` directory holds the build, the pinned sources and every
patch -- are in that directory (`SOURCE`, with each file's sha256). **The
LGPL's terms for this component are to be confirmed by counsel before the
website's release.**

## OpenJPEG, as @cornerstonejs/codec-openjpeg 1.3.6 (website)

`plexora/client/external/openjpeg-1.3.6/`, the JPEG 2000 decoder the website's
Python worker uses for tifffile's JPEG 2000 tiles, is the decoder build of
@cornerstonejs/codec-openjpeg (https://github.com/cornerstonejs/codec-openjpeg),
under the **MIT License**, compiled from OpenJPEG (https://github.com/uclouvain/openjpeg),
under the **2-clause BSD License**. The WebAssembly file is as published; the
JavaScript file has one line appended to make it an ES module. Both licences
and the published files' sha256 are in that directory's `LICENSE`.
