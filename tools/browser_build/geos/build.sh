#!/usr/bin/env bash
# shapely for the website's Pyodide, against GEOS 3.13.1 rather than the 3.12.1
# Pyodide ships (still in 314.0.8). Pyodide's own recipes (packages/, from
# pyodide-recipes), with only libgeos's version moved; numpy is there because
# shapely's recipe builds against it.
#
# Why: the website's GEOS 3.12.1 now and then refused a valid outline
# ("orientationIndex encountered NaN/Inf numbers", in a topology-preserving
# simplify): 47 of 17,322 of the artifact detector's outlines, a different one
# each run. 3.13.1 built here: none, and the same outlines as the desktop's
# GEOS 3.13.1 byte for byte. (mask_polygon.geos_again still retries.)
#
#   tools/browser_build/geos/build.sh [python3.14]
#
# Writes dist/shapely-*.whl and dist/shapely-geos.json, which build.py takes
# (--shapely-wheel, default this dist/). Needs a CPython 3.14 (python.org's;
# not uv's 3.14.0rc2) and network access to fetch the sources, Pyodide's cross-
# build environment and Emscripten.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=${1:-${PLEXORA_PYTHON314:-python3.14}}
# build.py's PYODIDE: the cross-build environment must be the one shipped.
PYODIDE=$(sed -n 's/^PYODIDE = "\(.*\)"$/\1/p' ../build.py)
GEOS=$(sed -n 's/^  version: //p' packages/libgeos/meta.yaml | head -1)
rm -rf venv build-dist
uv venv -q -p "$PYTHON" venv
# pip inside the venv: pyodide-build installs host packages with `pip`, and the
# first one on PATH otherwise may be an older Python's.
uv pip install -q -p venv/bin/python "pyodide-build==0.39.3" "cmake==4.4.5" "pip==26.3"
. venv/bin/activate
pyodide xbuildenv install "$PYODIDE"
pyodide xbuildenv install-emscripten
pyodide build-recipes libgeos shapely --recipe-dir packages --install --install-dir build-dist
mkdir -p dist
rm -f dist/shapely-*.whl
cp build-dist/shapely-*.whl dist/
printf '{"geos": "%s", "pyodide": "%s"}\n' "$GEOS" "$PYODIDE" > dist/shapely-geos.json
ls -la dist
