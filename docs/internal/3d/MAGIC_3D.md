# Magic select in 2-D and through a z-stack: what was measured, and what was chosen

Date: 2026-10-10. Branch `feature/volumetric-3d`. Question from the user: should magic select move
from MobileSAM (SAM 1 family) to SAM 2, given that MIB offers SAM 2 and its 3-D mode is SAM 2 only?
The rule agreed: if SAM 2 does not improve 2-D meaningfully, keep MobileSAM for 2-D and use SAM 2
for 3-D, with PyTorch only in the optional 3-D extra.

All numbers were measured on this machine (Apple silicon laptop), in a scratch virtualenv layered on
the plexora conda env with torch 2.14 and `sam2` (facebookresearch, git), SAM 2.1 checkpoints from
Meta (Apache-2.0). Scripts: the session scratchpad's `sam2/bench2d.py`, `bench3d.py`,
`bench3d_dense.py`, `diag3d.py`.

## 2-D: Xenium nuclei, MobileSAM (Plexora's own code path) vs SAM 2.1

Ground truth: 10x's nucleus boundaries over the DAPI focus image of the ovarian run in
`~/Downloads` (247,636 nuclei). Twelve random 512-px tiles per zoom, every nucleus wholly inside
prompted three ways; IoU against its rasterised outline (mean / 10th percentile).

Full resolution (1,345 nuclei):

| Model | Tight box | Loose box (+25 %) | Centre click |
|---|---|---|---|
| MobileSAM (Plexora) | 0.770 / 0.590 | 0.669 / 0.463 | 0.539 / 0.146 |
| SAM 2.1 tiny | 0.793 / 0.652 | 0.663 / 0.503 | 0.652 / 0.258 |
| SAM 2.1 small | 0.795 / 0.647 | 0.645 / 0.474 | 0.654 / 0.243 |
| SAM 2.1 base+ | 0.804 / 0.638 | 0.693 / 0.494 | 0.676 / 0.241 |

Half resolution (4,997 nuclei): MobileSAM 0.696 / 0.650 / 0.501 (tight / loose / click).

Reading: with a box -- magic select's default since box was measured to beat a click -- SAM 2 is
within 0.02-0.03 of MobileSAM; with a single click it is clearly better (+0.11). MobileSAM's
encoder runs in ~0.08 s on the CoreML GPU path and needs no PyTorch. **Kept MobileSAM for 2-D.**

## Through planes: synthetic nuclei with known 3-D extent

A 40 x 320 x 320 stack of textured ellipsoidal nuclei (radius 10-18 px across, 4-8 planes deep),
blurred twice as much along z as across (an axial PSF), shot noise. Each nucleus prompted with a box
on its middle plane and carried both ways; scored by mean IoU over the planes it really occupies (a
missed plane scores 0), voxel IoU of the carried shape, and "spill" -- planes outlined past its poles.

Findings, in the order they were made:

1. **SAM 2 on Apple's GPU (MPS) is wrong.** The prompted plane comes back right (IoU 0.86-0.92) and
   every carried plane wrong: masks over ~85 % of the frame, or in the wrong place (IoU 0.00). The
   same frames on the CPU track at 0.84. The model's memory of earlier frames is what breaks;
   `sam_video.device()` never picks MPS.
2. **Both models follow the blur past an object's pole.** Axial blur leaves a dimmer ghost of a
   nucleus for several planes. MobileSAM, prompted plane by plane, follows it to the end of the stack
   (27 spill planes per object; its own confidence stays above 0.85); SAM 2 for about three planes.
3. **Contrast against a ring around the object ends it cleanly.** Relative to the prompted plane, the
   object's contrast is above ~0.35 inside its true extent and falls below it within a plane of each
   pole. That is the `faded` guard (`propagate.FADE_MIN`).

With every guard (`plexora/vision/propagate.py`), 22 isolated nuclei:

| Engine | Plane IoU | Voxel IoU | Spill planes / object | Time / object |
|---|---|---|---|---|
| MobileSAM, plane by plane (before `faded`) | 0.649 | 0.261 | 27.3 | 6.8 s |
| MobileSAM, plane by plane | 0.649 | 0.654 | 0.41 | 1.3 s |
| SAM 2.1 tiny video, CPU | 0.645 | 0.652 | 0.59 | 13.7 s |

A denser stack (90 nuclei, 16 touching another; 25 scored):

| Engine | Plane IoU | Voxel IoU | Spill planes / object | Time / object |
|---|---|---|---|---|
| MobileSAM, plane by plane | 0.646 | 0.626 | 2.04 | 1.3 s |
| SAM 2.1 tiny video, CPU | 0.648 | 0.633 | 1.68 | 13.7 s |

## Through planes: real nuclei, no ground truth

Twelve nuclei on plane 95 of the Imaris confocal stack in `nirmal lab/temp/3D/F8IIc-smallchunk.ims`
(190 planes, 0.14 / 0.28 um), boxed from a threshold of the plane and carried by both engines:

| Engine | Planes per nucleus (median, range) | Time per nucleus |
|---|---|---|
| MobileSAM, plane by plane | 31 (22-49) | 4.0 s |
| SAM 2.1 tiny video, CPU | 34 (14-46) | 37.3 s |

On the planes both outlined, the two agree at a median IoU of 0.84 (10th percentile 0.51). XZ cuts
through six of them (`magic_3d_xz.png` beside this file) show the two tracing the same edges; where
they differ -- a pair of touching nuclei, the lower edge of one -- neither is plainly right.

## The decision

- **2-D: MobileSAM**, as agreed: SAM 2 is no better with a box, which is how magic select is used.
- **3-D: both engines, MobileSAM by default.** The measured premise for SAM 2 in 3-D -- that it
  carries an outline better -- did not hold once the `faded` guard was in: on synthetic nuclei
  (isolated and dense) and on real ones the two are equal within noise, and SAM 2 is ten times
  slower on a laptop (CPU only; see above). So a carry needs nothing but the core install; with
  the optional `volume3d` extra installed (`pip install "plexora[volume3d]"`), `auto` uses SAM 2
  -- installing it is the choice -- and `engine` says which on every carry. Raised with the user:
  whether to keep the SAM 2 path at all, given these numbers.

## What was built

- `plexora/vision/propagate.py` -- the carry: reads every plane as magic select reads one (the view,
  composited with the viewer's channels and windows), runs an engine, keeps planes outwards from the
  prompt while every guard passes, and says why each direction stopped.
- `plexora/vision/sam_video.py` -- SAM 2.1 tiny's video predictor, planes handed to it from memory;
  optional (`pip install "plexora[volume3d]"`), weights fetched once and checksummed.
- `POST /segment/v1/carry`, `GET /segment/v1/status` (`through_planes`),
  `POST /segment/v1/install3d`.
- The ROI plugin's `solid` (`contours` / `ellipsoid`), `roi.set_solid`, plane-by-plane cell
  mapping, GeoJSON round trip, the magic bar's Through planes button, the row menu's carry /
  ellipsoid / flat items, and regions drawn in the XZ / YZ section panes.
- Agent: `carry_roi_through_planes`; `create_roi` / `update_roi` take `solid` and `flatten`.
