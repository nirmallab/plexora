# autocontrast — learned display windows for multiplex images

A research pipeline. It tests whether a small shared model can propose each channel's
black and white points, in raw units, the way a person sets them in Plexora. It is not wired
into Plexora. Plexora is used here as a library: its readers, its tissue mask, and its two
heuristics, which serve as baselines. Design, decisions and limitations are in
`docs/internal/AUTOCONTRAST.md`.

```bash
PY=<conda root>/envs/plexora/bin/python                    # everything except training
TPY=~/plexora-autocontrast/venv-train/bin/python           # training (torch, onnx)
cd tools/autocontrast
```

Outputs go under `$AUTOCONTRAST_HOME`, which defaults to `~/plexora-autocontrast`. That covers
labels, examples, runs and caches. Keep it out of the repo and out of any synced folder. Source
images and Plexora project databases are only ever read.

## The loop

```bash
# 1. Set windows in Plexora as usual, then pull them (read-only) as labels.
$PY -m biological_autocontrast labels pull lsp11385 --annotator you@lab.org
#    Edit $AUTOCONTRAST_HOME/manifest.csv: give each image its specimen/patient/tissue/batch.
#    Two images of one specimen share a specimen id, so they never straddle a split.
$PY -m biological_autocontrast labels index

# 2. Materialise examples (patches, histogram, domain) -- the only step that reads images.
$PY -m biological_autocontrast examples

# 3. Train. The reported metrics are held out by specimen (>= 5 specimens), otherwise by
#    channel folds, and the run says valid: false.
$TPY -m biological_autocontrast train --out ~/plexora-autocontrast/runs/r1 --device mps

# 4. Score the model against the labels and the baselines (no image reads).
$PY -m biological_autocontrast evaluate --cv ~/plexora-autocontrast/runs/r1 \
    --checkpoint ~/plexora-autocontrast/runs/r1/model.onnx --out ~/plexora-autocontrast/runs/r1/eval

# 5. Look at proposals for any image.
$PY -m biological_autocontrast predict <image> --checkpoint ~/plexora-autocontrast/runs/r1/model.onnx \
    --output ~/plexora-autocontrast/out/<name> --review-html
```

## Commands

| command | does |
|---|---|
| `inspect <image> [--json]` | Dimensions, levels, pixel size, and each channel's intensity domain: effective bits and where they came from, range, saturated/NaN fractions. |
| `propose <image> --output D [--review-html]` | Heuristic windows (Plexora's percentile calibration). Writes `contrast_predictions.heuristic.csv/json`, with `model_available: false` on every row. |
| `predict <image> --checkpoint model.onnx --output D [--review-html]` | Model windows (`contrast_predictions.csv/json`). |
| `labels pull <project> --annotator A` | Windows saved in a Plexora project. Defaults (0..65535/65536, or the whole domain) are skipped. `--data-root` points at another Plexora root, and `--image` covers an image that has moved. The project may also be given as its folder or its `.db`. |
| `labels add <image> --channel C --min a --max b --annotator A [--decision no_signal]` | One label by hand. |
| `labels export --out F --format csv\|json\|parquet`, `labels index` | Export all labels, or join them with the manifest into `labels_index.parquet`. |
| `examples` | Labels to cached npz examples, plus `examples.json` (targets and baseline windows). |
| `train --out D [--device mps] [--no-patches\|--no-hist\|--max-patches K]` | Writes `checkpoint.pt`, `model.onnx`, `metrics.json`, `cv_predictions.json`, `log.jsonl` and `loss_curve.svg`. |
| `evaluate --out D [--cv RUN] [--checkpoint model.onnx]` | Writes `evaluation.json/.md`: error, tolerance rate, wins over each baseline, ECE, by role and bit source, cross-bit-depth, stress inputs. |
| `export --checkpoint checkpoint.pt` | Re-export a checkpoint to ONNX. |
| `synth <folder>` | Synthetic fixtures: 8/16/12/14-bit, float with NaN/inf, a dim channel, mixed ranges, a blank image and an NGFF z-stack. |

Images: OME-TIFF/TIFF, OME-Zarr, DICOM-WSI (a folder or a bucket URL such as `gs://idc-open-data/<id>`, read through Plexora's reader and cache), and `node://<node>/<resource>` for an image held by a connected Plexora data node (raw values over the node's region route; the node must be connected in Plexora). `labels pull` picks the right one from the project's config.

Common options: `--z/--t` (explicit plane; a stack is never projected), `--channels 0,3,CD3`,
`--seed`, `--force` (skip the cache) and `--assume-bits N` (effective bit depth when the
metadata lacks it). `--config file.yaml` goes before the command; `configs/default.yaml` lists
every key.

Python: `from biological_autocontrast import predict_contrast;
predict_contrast(image_path, checkpoint_path=None, channel_indices=None, z=None, t=None, seed=42)`.

## Tests

```bash
$PY  -m pytest -q -p no:randomly            # everything; torch tests skip
$TPY -m pytest -q tests/test_model.py tests/test_nets.py   # with the torch tests
```

This suite sits outside Plexora's `testpaths`. A test that needs an ONNX file exports one
through `$AUTOCONTRAST_TRAIN_PY`, which defaults to the venv above. Without it, that test skips.

## The training environment

The plexora env has no torch, so training uses a venv layered on the `plexora-train` conda
env (torch, onnx, onnxruntime), with pytest added:

```bash
<conda root>/envs/plexora-train/bin/python -m venv --system-site-packages ~/plexora-autocontrast/venv-train
~/plexora-autocontrast/venv-train/bin/pip install pytest
```

Any Python with `requirements-train.txt` works. Training needs neither tifffile nor plexora:
it reads only the examples folder.
