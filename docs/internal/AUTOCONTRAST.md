# Learned auto-contrast (research): status and design

Code: `tools/autocontrast/` (README there for commands). Nothing in Plexora imports it.

## Status (2026-10-15)

- **Phase 0 is done.** It includes the readers, the intensity domain, stratified patch
  sampling, features, the baselines and label pulling from Plexora. Heuristic proposals have
  a review page, and the tests pass.
- **Phase 1 is done as a pipeline.** It covers examples, the model, training, the ONNX
  export, model inference and evaluation. Its results are not evidence yet: there is one
  labelled specimen (lsp11385, 36 windows), so every run reports `valid: false`. Its
  held-out numbers are leave-channels-out on that one slide (see Results).
- **Next:** label more slides in Plexora, then `labels pull` each one. At five or more
  specimens, training holds out by specimen and the numbers start to mean something for a
  new slide.

## Decisions (with the user, 2026-10-15)

- **No integration yet.** First prove accuracy. If it holds up, it becomes a licensed
  feature in the channel section that autogating and auto-QC can call. The licence tier is
  decided then.
- **Min/max only; gamma is fixed at 1.0.** Plexora renders linearly everywhere:
  `frag.glsl` uses `range_clamp`, and `source_image.blend_plane` and the volume renderer do
  the same. A predicted gamma would have nothing to apply it. The field stays in every record
  so the contract matches the spec.
- **Labels come from Plexora.** The user sets windows in the viewer as usual, and
  `labels pull` reads the project's `channelList` read-only. There is no separate labelling
  app.

## The intensity domain

    x_norm = (x - offset) / (full_scale - offset)        x_raw = offset + x_norm * (full_scale - offset)

`full_scale` is 2^bits − 1, never the observed maximum. Dividing by the maximum is what
makes a dim channel look bright, and it makes windows incomparable across images. The bits
come from the first of these that applies:

1. `--assume-bits` (`user`)
2. OME `SignificantBits`
3. TIFF `BitsPerSample` when it is below the dtype
4. the dtype (`dtype_nominal`, with a warning)

Pixels above a declared depth fall back to the dtype range. Floats have no nominal range:
the offset is min(0, p0.01) and full_scale is p99.99 of the overview. That is recorded as
`sampled_range` with confidence 0.4, so float windows compare only roughly.

lsp11385 carries no `SignificantBits`, like most CyCIF OME-XML. So it is `dtype_nominal`,
0..65535.

## Sampling and features

Each channel gets 24 patches of 128 px at the level nearest 1 µm/px (level 2 on lsp11385,
1.3 µm). They are chosen on the overview from footprint-sized candidates (two grids, half a
footprint apart), with these quotas: 6 high, 6 intermediate, 4 background, 4 texture,
4 random. Patches stay at least one footprint apart. A shortfall is filled from the tissue
pool and recorded in `quota_fallbacks`. Selection is deterministic per
`(seed, crc32(channel name))`.

Tissue is `image_qc.tissue_mask` on the nuclear channel's overview. The nuclear channel is
found by name; without one, channel 0 is used, and that is recorded.

The histogram has 256 bins in canonical space: a zero bin, log-spaced bins from 10^-5.5 to 1,
and an over-range bin. Alongside it go 10 quantiles and a 16-value domain vector with a
missing-mask. Patches are never rescaled one by one, because the absolute level is what the
model has to read.

## Model

A dual-branch network with 141k parameters (the limit is 500k):

- **Patch branch.** A depthwise-separable CNN runs on log-scaled patches, plus a category
  embedding. It is pooled by masked attention plus a masked mean.
- **Histogram branch.** An MLP over the histogram, the quantiles and the domain vector.

The heads are `min = sigmoid`, `span = softplus + eps` and `max = min(min + span, 1)`, plus
`confidence` (the probability of being within tolerance) and `abstain`. The loss is Smooth L1
on log(min), log(max) and log(span), with a floor of 0.001, plus BCE terms for abstain and
confidence.

Augmentations are applied jointly to patches, the pixel sample and the targets:

- gain ×0.7–1.4 (the targets scale with it);
- an offset up to 2%;
- Poisson-Gaussian noise;
- quantisation to 8/12/14 bits, with the domain vector re-derived;
- flips and rotations.

The model is exported to ONNX opset 17 with dynamic batch and patch count. The export is
checked against torch to 1e-4 and run on onnxruntime CPU in the plexora env.

Abstain rules run before the model:

| rule | condition |
|---|---|
| `dark_channel` | sampled max below 1% of full scale |
| `no_tissue` | tissue below 1% |
| `no_finite_pixels` | no finite pixels |

Saturation above 5% and finite pixels below 99% are flagged, not abstained on.

## Results

Tolerance: the white point within ×1.25 and the black point within ×1.5 of the label, both
above a floor of 0.001 of full scale.

Run r0 (2026-10-15) used 36 lsp11385 windows: 40 epochs × 6 augmented draws, 6 channel
folds, MPS, about 10 minutes. In the channel folds, every copy of a marker name sits in one
fold, so the second Hoechst round never leaks across.

| method | within tol. | white median err | black median err | beats percentile | beats GMM |
|---|---|---|---|---|---|
| percentile (Plexora calibration) | 6% | ×3.00 | ×1.44 | – | – |
| GMM (viewer Auto) | 3% | ×2.18 | ×1.70 | – | – |
| fixed p1..p99.9 | 6% | ×1.97 | ×1.88 | – | – |
| **model, held-out channels** | **22%** | **×1.46** | ×1.48 | 78% | 78% |
| model, in-sample (not evidence) | 39% | ×1.18 | ×1.25 | 94% | 97% |

How to read this:

- The heuristics set white points two to three times lower than this annotator does.
- The model learns the annotator's level from the other channels of the same slide. It does
  not show that it transfers to another slide, so the run reports `valid: false`.
- **Cross-bit-depth.** The same examples were re-quantised to 8/12/14 bits. The canonical
  white point moves ×1.07 at the median, and 83% / 100% / 100% stay within tolerance. The
  8-bit drop is the 1/255 step at these dim windows: the label's white point is around
  0.01–0.1 of full scale.
- **Stress inputs** (all-zero, saturated, dim, no patches) give finite, ordered windows.
- **Confidence** is poorly calibrated so far (ECE 0.23).
**r2 (2026-10-15).** 98 windows from three slides, each slide held out in turn: lsp11385;
LSP11337, which has artefacts and high antibody concentrations and is read through the O2 node;
and IDC fc078201, a CRC DICOM from a different panel. Still `valid: false`, with 3 of 5
specimens.

| held-out slide | model | percentile | GMM | fixed |
|---|---|---|---|---|
| lsp11385 | **28%**, white ×1.50 | 6%, ×3.00 | 3%, ×2.18 | 6%, ×1.97 |
| LSP11337 | 11%, ×1.77 | 3%, ×1.72 | **22%, ×1.29** | 16%, ×1.33 |
| IDC fc078201 | **16%**, ×1.60 | 0%, ×1.86 | 4%, ×2.15 | 4%, ×1.36 |
| all 98 | **18%**, ×1.61 | 3%, ×2.23 | 10%, ×1.80 | 9%, ×1.53 |

Each cell is the share of channels within tolerance, then the median white-point error.

- On a slide it has never seen, the model beats the percentile calibration on 69% of
  channels and the GMM on 66%. Against the fixed p1–p99.9 window it wins only 54%.
- On LSP11337 the model loses to the simple heuristics. On that slide your white points sit
  close to the data's own upper tail, and three slides are not enough for the model to learn
  when that applies.
- Reproduce with the README loop. The outputs are in `~/plexora-autocontrast/runs/r0/`.

## Baselines

All baselines run on the overview, in raw units.

- **percentile** is Plexora's display calibration (`calibration.channel_window`):
  p50..p99.5, high ≥ 3× low, and p30..p99 for a nuclear channel.
- **gmm** is the viewer's Auto button (`channel_gmm_of`). It is transcribed with a fixed
  random state and fitted on a 200k-pixel sample. A parity test pins it to Plexora's function
  under the same seed. The fit is unstable on unimodal channels: different seeds or
  subsamples give windows ×2 apart. That is the button's behaviour, not a bug here.
- **fixed** is p1..p99.9 of non-zero pixels.

## Limitations

- **One specimen.** Nothing yet says the model generalises to another slide, tissue,
  instrument or panel.
- **Labels carry their viewing context.** That includes the colour, the co-displayed
  channels, the monitor, and whether the window was set for gating or for a figure. There is
  no inter-annotator agreement yet.
- **Effective bit depth is usually missing.** Most CyCIF images fall back to their dtype. A
  12-bit camera in a uint16 file then has its windows near the bottom of the canonical range;
  the model must learn that from the data.
- **No gamma.** It would need the renderer first.
- **Not modelled:** autofluorescence and bleed-through, which would need co-channel inputs
  (a Phase 2 hook). Uneven illumination is not handled either, because one global window
  cannot fix it.
- **Tissue mask.** It assumes a nuclear channel identifiable by name.
- **Abstain is untested on real data.** The thresholds are hand-set until there are
  `no_signal` labels. Plexora has no way to record "this channel shows nothing"; use
  `labels add --decision no_signal`.
- **Not handled:** z/t stacks are read per plane, but channelList windows are per project, so
  pulled labels are z = 0. Brightfield/RGB is refused.
- **The legacy exporter.** The ONNX export uses torch's TorchScript-based exporter, which is
  deprecated from 2.9 but still present in 2.14.

## Image sources

OME-TIFF, OME-Zarr, DICOM-WSI (local or bucket; Plexora's `dicom_wsi` reader), and data-node images
(`node://<node>/<resource>`: the node's raw `/region` route, token from Plexora's `nodes.json`; only while the
node is connected). A node image's identity is its resource id plus its geometry; a bucket DICOM's is its URL plus
geometry -- neither has an mtime.

## Integration seams (not built)

- The channel section's Auto button would call `predict_contrast(image, checkpoint,
  channel_indices, z, t)`.
- Autogating and QC would use it as a `calibrate_display` consumer.
- Weights would be fetched through `plexora/vision/model_files.py`, as the removed cell model
  did (`git show 194fb93^1:plexora/plugins/gating/server/autogate/cellcall_weights.py`).
- The cache key already has the spec's shape: file fingerprint, channel, z/t, checkpoint id,
  config digest and the versions.
- Phase 2 hooks exist as schema only: the pairs table (`labels.PAIR_COLUMNS`) and
  `ContrastNet.embedding` for a ranking head. Co-channel or mask inputs would be extra patch
  channels behind a `MODEL_LAYOUT` bump.
