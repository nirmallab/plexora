# Gating engine v4: a dataset gated by groups of aligned images

Handoff, 2026-11-05. For engineers who will work on automatic gating next.

## Status

- **Branch:** `gating-v4`. Not merged.
- **Names:** the cell-panel engine of [GATING_ENGINE_V2.md](GATING_ENGINE_V2.md)
  is registered as `v3`; `v2` and `candidates` are its earlier names
  (`schemas.ENGINE_ALIASES`). `v4` is new.
- **Defaults:** `schemas.DEFAULT_ENGINE_BY_SCOPE`: `v3` for one image, `v4` for
  a dataset, applied in `SessionOptions._defaults_by_engine` when the caller
  names no engine. A stored session keeps the engine it was started with.
- **On one image v4 is v3.** Nothing in `groups` runs unless the session is a
  dataset of more than one image (`groups.active`);
  `tests/test_gating_groups.py::test_v4_on_one_image_is_v3` pins it gate for
  gate and look for look.
- **Measured on:** the synthetic datasets only (`make_gating_dataset`). Not yet
  run live on a real cohort; the thresholds marked `[cal]` are unmeasured.

## Why

The reference-first path (v1, v3) gates one image in full and carries each of
its gates to the others. Nothing the other images show is ever read; a
reference marker that ends in review sends every image to its own full
gating; the drift classes are computed and never decide anything. Each image
also takes a long time to gate, and the images waited on the reference.

## How it works

1. **Parallel bulk pass** (`bulk.run`, every engine). Images are profiled
   `IMAGE_WORKERS` at a time, the threads (`BULK_THREADS`) split between
   them; each image's markers are still settled in gating order under the
   session lock. The scheduler waits on a unit still `pending`, as before.
   `record["bulk_images"]` says where the pass is with each image.
2. **Waiting for every image.** In v4 a dataset's profiled units go to
   `group_pending` (`bulk.settle_unit`), never `transfer_pending`.
3. **Grouping** (`groups.partition`, pure; `groups.build` freezes the result
   into `record["groups"][marker]`). Each image's background is read in its fit
   space (`reference.background`): the mixture's background component for a
   class that has one, else the median and IQR / 1.349 of all cells (a
   mixture splits one mode in two). `reference.align_bg` maps one background
   onto another (`b` the ratio of sds, `a` the offset) and scores the fit on
   the quantiles below both images' positives (`rms_bg`). Classes
   (`reference.classify_group`): `stable`, `member_shifted` (mappable, within
   one background sd), `no_positives` (one mode on the same background),
   `image_specific_shift`, `distribution_change`, `staining_failure` (a hard
   flag, or a weak two-mode stain where the scale image's is clear).
   Groups form in rounds: the medoid of the images left gathers the ones that
   join it, the rest try again around their own medoid, so two batches far
   apart are two groups. An image left alone under `cell_floor` cells is
   attached to the nearest group (`attached_under_floor`). A group of four or
   more members splits once at the largest gap in its shifts
   (`reference.batch_split`). Each group's scale image S is the user's
   `reference_image` when it is a member, else the medoid; up to
   `anchors_max` anchors are chosen evenly by shift rank, the extremes first.
4. **The group's decision lives on S's own unit.** S is decided by the v3
   loop unchanged (`bulk.decide_first`, then the audit sheet or cell panels),
   so its dependencies, regression checks and writes are v3's. Two things
   differ while its group is deciding (`hypotheses._pool`):
   - the other anchors' own GMM gates, carried onto S's axis, join S's
     candidates as added points (`peer:<image>`), so one leads only once read
     on both sides;
   - its looks rotate over the anchors (`hypotheses._pooled_packet`): the
     plan's intervals and sentinel bars are carried into the anchor image,
     cells drawn and rendered there, and each cell's value carried back onto S's
     axis, kept inside the interval it was drawn from. The controller counts
     by value, so every read is pooled evidence for one decision with no
     other change. The packet's `units[0]` names the anchor image and, under
     `unit`, the deciding unit (`Engine.lean`, `Engine.unit_key_of`); the
     mirror and the reader see the image the cells are from. `looks_max`
     grows by one per anchor past the first, up to `looks_cap`.
5. **Carrying** (`groups.carry`, once S's unit is terminal; called from
   `groups.maintain` before every decision is chosen). Accepted: each member's
   gate is S's carried through its alignment; a member whose positive
   fraction there sits with the group's (median and MAD with five members or
   more, else S's) and inside its own guard band is accepted at T1 for the
   audit sheet (`group_aligned`); the worst few that do not get a
   `transfer_check` (`checks_min`, or `checks_share` of the members, within
   the marker's packet ceiling); the rest are accepted flagged
   `group_check_unverified` (or `packet_ceiling`) at low confidence.
   `no_positives` members are checked by numbers only and carried flagged.
   S closed for review: every member closed for review at the carried gate.
   S with no gate at all: each member decided on its own.
6. **Feedback, bounded** (`groups.after_check`, `groups.after_strip`). A
   check read too low or too high, or an audit-sheet row read suspicious,
   gives the member its own decision while the group's `own_looks` last;
   past that it closes for review at the carried gate. `cannot_tell` keeps
   the carried gate, unverified. A marker never spends more than
   `packets_base` plus one packet per image on checks and own decisions.
7. **Termination.** `Engine.close_ungrouped`: units still `group_pending`
   when nothing else is left are first grouped and carried, and only as a
   last resort closed for review. A session cannot hang on a group.

## The viewer

- `mirror.follow == "tab"` in dataset scope: the Presenter keeps the newest
  look per image and plays only the tab's (`agent/sessions/mirror.py`,
  `mirror_script.run`); it never sends `open_project`. A tab that is
  `navigating` is waited for, not turned off.
- After a switch the tab re-registers under the same view id and posts
  `attach_viewer {view_id, project}`; the newest outstanding look for that
  image is replayed.
- `Engine.dataset_progress` (in `progress`, the `dataset_progress` event,
  status and the harness snapshot): each image `gating` (a look of it is
  out), `checking`, `profiling`, `done`, `review` or `queued`, with marker
  counts. The image switcher badges its tiles and counts "N gating"; the
  agent panel says "Gating A of T images in parallel".

## Where it departs from the plan

The plan (2026-11-05) put the decision on a separate `group:` unit, counted
reads by interval index and weighed them per anchor (stratified Beta), split
a group whose anchors disagreed, and reopened a group once on a consensus of
checks. What was built instead, and why:

- **The decision lives on S's unit**, not a new unit kind: the engine's
  dependency, regression, write and close paths all take a unit with one
  project, and S's unit is that unit. Nothing iterating `record["units"]` had
  to learn about a non-image unit.
- **Reads are pooled by value on S's axis.** Interval indices change when the
  range is split or extended; values carried back (clamped into the interval
  they were drawn from) stay correct through both, and every controller rule
  already counts by value.
- **Not built yet:** per-anchor weighting of the pooled reads, the
  `anchors_disagree` split, the consensus reopen, and attaching an image that
  wakes on a sleeping node to a group. Disagreeing members get their own
  decision instead, bounded by `own_looks` and the packet ceiling.

## Measured

Only the synthetic tests so far (`tests/test_gating_groups.py`):
three images with one shifted by 0.5 log units, every marker settled, the
shifted image carried and checked; a scene whose scale images go to panels
draws looks from both anchors; partition cases (two images aligned, shifted,
unaligned; two batches; a failed stain; a 12-core TMA with an empty and a
small core). On images of a few hundred cells the background quantiles' own
noise exceeds the `drift_rms_bg` bar, so membership uses the mappable bar
(`change_rms_bg`) instead.

To do before trusting it on a cohort: a live run on two or more colorectal
images in one dataset (LSP23398 with LSP23402 or LSP23362) against each
image's v3 gates, the packet count per marker against the ceiling, and
memory with `IMAGE_WORKERS` images of whole slides held at once.
