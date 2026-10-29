# Automatic gating: where the time went, and what was cut (2026-10-29)

*Every number here comes from a recorded session or a run of the tools named
beside it, on this machine (Windows, a local 67 GB OME-TIFF), never from
memory. The plan this executes: "Investigate and optimize auto-gating
performance" (the session's plan file); the user's decisions: the live
comparison is driven through the Plexora MCP tools on lsp11385 and
lsp35766_p193, and lanes become the viewer's default once its bars pass.*

## Tools

- `tools/bench_gates/stage_report.py <session>`: a session's time from its own
  log: wall, the bulk pass (calibration, each marker's profile), reader time
  per packet kind, the server's gap between an answer and the next packet,
  the engine's stage timings, the mirror, and (when the harness drove it) the
  trace's model and tool calls.
- `tools/bench_gates/perf_bench.py run|compare`: a propose-mode session on a
  real image with no model (`bench.TruthAgent` reading each marker's Auto gate
  as the truth), on a scratch copy of the project made fresh for every run;
  `--lanes N` readers, `--latency`/`--scale` hold each answer back a model
  call's seconds. `compare` says whether two runs decided the same: every
  marker's state, gate and confidence, and every packet's JSON (floats to nine
  significant digits; see "Determinism") and image bytes.
- The records behind every table below: `docs/internal/bench/runs/perf-2026-10-29/`
  (gitignored; `<run>.json` from `perf_bench.py run`, `cmp_*.txt` its
  comparisons).
- The engine's stage timings (`BaseEngine.timed`): `issued` events carry `ms`
  {choose, build, sample, plan, render, store}, `answered` {apply, regression,
  lineage, write}, `profiled` {profile, settle}, `calibrated` {calibrate}, and
  `mirrored` events the presenter's sends.

## The live run this started from

`gs_20261028T183832_5ce697`, ImageLSP23362 (16 markers, 312k cells, engine
v2, classifier `layer`, mirrored, image on a network share): 946 s.

| component | time | source |
|---|---|---|
| model calls (23 Opus panels, median 11.7 s; 15 Sonnet stain checks, 6.7 s) | 404 s | trace `model_calls` |
| `gating_answer` (apply, next packet, mirror), median 10.0 s x 38 | 392 s | trace `tool_calls` |
| `gating_next` while the bulk pass ran | 109 s | trace `tool_calls` |
| answer -> next packet, server only (median) | 6.7 s | `decisions.jsonl` |
| first packet | 103 s | `decisions.jsonl` |

Packets out at once: 1, in every recorded run.

## Zero-model bench, lsp11385, 16 markers

`perf_bench.py run --project lsp11385 --markers CD45,Ki67,CD3e,CD20L,CD11c,
CD163,CD8a,Ecad,PDL1,PD1,CD4,FOXP3,CD68,CD31,SMA,SOX10` (202k cells, engine
v2, classifier `layer`, serial, no simulated model time). Seconds, summed
over the session (30 packets).

| | baseline (p0) | + VoI cache, parallel crop reads (p2a) | + parallel profiles and calibration (p3) |
|---|---|---|---|
| decisions vs baseline | -- | identical | identical |
| decided in | 171.9 | 151.5 | 144.4 |
| calibration | 23.0 | 10.2 | 10.0 |
| bulk pass, last marker profiled after | 122.8 | 116.7 | 51.4 |
| server gap per packet, median | 2.19 | 1.16 | 1.13 |
| render (collages) | 28.6 | 16.9 | 14.5 |
| VoI planning (`plan`) | 7.8 | 2.4 | 2.1 |
| apply (answer -> transition, incl. VoI) | 15.8 | 11.9 | 11.1 |

Same code twice (p0 against p0b): decisions identical, wall 171.9 / 167.6 s
(+/- 3 %). "Decided in" in this table includes the driver fitting its truth
gates after the start (15-40 s); every run below reads them from a cache.

**Final, the same cached truth** (`p0c` the code before any change, `s1`/`s2`
everything here, serial, classifier `layer`):

| | before (p0c) | after (s1) | after, again (s2) |
|---|---|---|---|
| decided in | 156.0 s | 105.1 s (-33 %) | 101.3 s |
| every marker's state, gate and confidence | -- | the same as before | identical to s1 |
| packets | 31 | 30 | identical to s1 |

The packets themselves differ from before's: a marker is now first asked
about once every marker it reads has been profiled (below), so CD45's first
look is drawn with Ecad's and SOX10's T1 gates among its references rather
than before them, and the two audit strips became one. Every final decision
is the same.

The classifier layer's read-ahead (`classifier: layer`) runs on a thread of
the same process: with it off the session decides in 80.7 s (server gap
median 0.67 s against 1.06 s), its decisions identical. Its pixel pass now
steps aside between reads while a packet is drawn or an answer applied
(`engine.yield_to_foreground`, at most 2 s at a time): 97.2 s, gap 0.83 s.

Render, profiled alone (`collage.rerender` of 12 stored panels, byte-identical
to the stored PNGs): 0.83 s -> 0.31 s a panel. A crop read decodes the whole
pyramid tile under it; the shared reader shelf held one tifffile handle per
image under an exclusive lock, so a panel's ~70-120 tile reads went one after
another.

## The mirror (`plexora.agent.sessions.mirror.Presenter`)

A synthetic project with a fake tab acking at once, default pacing
(600 ms): `gating_answer` 4.4-6.3 s a look with the script sent inside the
call (HEAD), 0.5-0.7 s with the presenter. Of three looks answered within two
seconds, the tab was shown the first, the two after it were dropped as stale,
and the newest (the audit strip) followed once the first had had its time.

## Lanes

The same bench with each answer held back the live run's median model time
(11.7 s a cell panel, 6.7 s a stain check, 8 s otherwise; `--scale 1.0`),
`--lanes N` readers each passing `parallel: N`:

| lanes | decided in | packets out at most | reissued | decisions vs one lane |
|---|---|---|---|---|
| 1 | 440.0 s | 1 | 0 | -- |
| 2 | 278.2 s (-37 %) | 2 | 0 | identical |
| 3 | 257.3 s (-42 %) | 3 | 0 | identical |
| 4 | 249.8 s (-43 %) | 4 | 0 | identical |

The knee is two to three lanes on this panel: most markers read each other's
gates, so a fourth lane finds little to do. "Decided in" counts the bulk pass
too (profiles done at ~52 s).

So a session runs on as many lanes as its panel keeps busy, not a fixed
number (`Engine.useful_lanes`, stored at the start): its markers per step of
its longest chain of markers each waiting for the one before, rounded up, at
most `LANES` (4). This panel: a chain of 7 or 8 depending on where the plan
puts CD163 (16 markers, so 3 or 2 lanes); lsp11385's 33 markers with the
user's gates, 4; a panel whose every marker reads the one before, 1. A
session that names `lanes` gets them.

Before `unprofiled_partner`, four lanes once reissued CD45's first look: it
went out while the bulk pass was still profiling, Ecad's and SOX10's T1 gates
(later in gating order, exclusive partners of CD45) were written beside it,
and its answer was refused as stale. The final gates matched, but the path
did not -- and one at a time the same timing decided what a first look
stood on, unchecked. Now neither order asks about a marker before every
marker it reads is profiled.

## Determinism

Two runs of the same code agree to about 15 significant digits, not to the
bit: a mixture fit's reductions run on threaded BLAS, whose summation order
moves from run to run (SOX10's Auto gate 6.583253850837842 against
...836). No decision moved; `perf_bench.py compare` reads floats to nine
digits.

## What changed, and what each was checked by

| change | where | check |
|---|---|---|
| stage timings in the log | `agent/sessions/engine.py` (`timed`), `autogate/bulk.py`, `hypotheses/__init__.py`, `autogate/engine.py` | stage report reproduces the live run's table |
| the mirror off the driver's path | `agent/sessions/mirror.py` (`Presenter`), gating `capabilities_session.py` (`_mirror`, `_mirror_display`, `finish`), QC through `agent/sessions/tools.py` | `tests/test_session_mirror.py`, `tests/test_agent_viewer_api.py`, `tests/test_qc_mirror.py` |
| Beta draws remembered | `hypotheses/evidence.py` (`_beta_draws`) | 40 random tables equal to the bit against the old code; 5.7x faster |
| several readers per image, a panel's crops read side by side | `server/utils/source_image.py` (`ReaderShelf`), `agent/evidence/crops.py` | re-rendered panels byte-identical; `tests/test_source_image_core.py` |
| calibration channels side by side | `agent/evidence/cell_window.py` | bench: identical |
| an image's markers profiled ahead on threads, settled in order | `autogate/bulk.py` (`_Ahead`) | bench: identical |
| a marker waits for every marker whose gate it reads | `autogate/engine.py` (`_links`, `passed_by`) | `test_every_gate_a_marker_can_read_is_one_it_waits_for` |
| nothing is asked about a marker before every marker it reads is profiled | `autogate/engine.py` (`unprofiled_partner`, in `next_unit` and `ready_units`) | `test_a_marker_waits_for_its_earlier_partners...`; bench s1 = s2, lanes 1-4 identical |
| the classifier's pixel pass steps aside for packet drawing and answers | `agent/sessions/engine.py` (`foreground`, `yield_to_foreground`), `classifier/features.py` | `test_background_reads_step_aside_while_a_packet_is_drawn`; bench identical |
| lanes in the session, the harness and the `delegate` block | `SessionOptions.lanes`, `engine.lanes_for`, `ai/delegation.py`, `ai/harness/decision.py` | `tests/test_ai_models_mcp.py`, `tests/test_ai_parallel_markers.py` |

### The gap lanes had (closed)

`_links` linked a marker only to its stated partners at moderate confidence or
better (not state or signalling readouts) and to derived exclusions. What a
marker reads is wider: its scored start (`scoring.evidence_partners`, every
relation the tree gives it, low-confidence exclusions included), its lineage
check (`lineage_purity`, every exclusion) and its children's consensus. On
lsp11385's 34-marker panel 120 relation pairs were not links, so with lanes a
marker could be scored or checked while a partner it reads was still open
beside it -- a different start from the serial run's. `_links` now holds
every relation, either way round. In the other direction, lanes held a
marker until a T1 partner's audit strip had gone; the serial order passes a
T1 gate (and a parent set aside for its children) by, so lanes do too
(`passed_by`).

The cost: the longest dependency chain on the 16-marker bench panel is 9
markers (8 under the old links), on a 16-marker melanoma panel 12 (9).

## Measured and not done

- Caching `cells.values`: the table is in memory; a column costs ~3 ms.
- Keeping the session record in memory: `engine_for` costs ~21 ms (load
  4 ms, save 8 ms); a few per packet.
- Speculative regression: 1.7 s over a 30-packet session.
- Rendering outside the session lock: ~0.5 s a panel now, beside a 12 s model
  call.

## Live: lanes through MCP, lsp11385

Driven from Claude Code through the Plexora MCP tools, as the `delegate`
block says: `plexora-gating-worker` conversations (Opus 5.5), one after
another for one lane, one per lane launched together otherwise. 16 markers
(the bench's), propose mode, `reuse_answers: false`, no mirror, a scratch
copy of the project (`PLEXORA_DATA_PATH`), the arms interleaved (one lane,
auto, one lane, auto). Auto gave 2 lanes (16 markers, a chain of 8). Truth:
the user's gates of 2026-10-21 (`~/gatevision/lsp11385_2_user_gates_2026-10-21.json`).
Records: `docs/internal/bench/runs/perf-2026-10-29/live/`.

| | one lane (2 runs) | auto, 2 lanes (2 runs) |
|---|---|---|
| decided in (start to last answer) | 690, 692 s | 440, 454 s (-35 %) |
| mean \|gap\| to the user's gates | 0.205, 0.264 | 0.208, 0.251 |
| paired \|gap\| difference, auto - one lane, by marker | | -0.005 (95 % bootstrap -0.033 .. +0.028) |
| markers more than 0.30 worse | | none |
| run-to-run spread of the final gate | 0.193 | 0.127 |
| packets per marker | 2.66 | 2.53 |
| packets out at most / reissued | 1 / 0 | 2 / 0 |

Every bar the plan set holds: the difference's interval spans zero, no
marker is 0.30 worse, the spread is not wider, packets per marker are not
more, nothing was reissued. So `LANES` is 4: a session runs on as many lanes
as its panel keeps busy, at most 4, unless it names them. The two arms
differ marker by marker about as much as two runs of one arm do (the
reader is a model); 6 of 16 markers wrote the same gate in at least three
of the four runs.

## What the live run should now take

From the live run's own numbers (ImageLSP23362, 946 s): the mirror's ~3.3 s
and most of the 6.7 s of server work per packet are gone from the critical
path (the bench's server gap fell 2.2 -> 0.8 s; the image here is local, the
live one on a network share, where parallel crop reads gain more), and the
bulk pass profiles four markers at once. One lane: about 946 - 38 x (3.3 + ~4)
- ~50 s of first-packet wait, roughly 10 minutes. Three lanes take a further
~40 % of what is left on a panel like the bench's. Measured live only once
the comparison below runs.

## Still to do

- The same comparison on a node-held image (lsp35766_p193, which needs the
  transfer node's login) once the user's gates for it are found: the p193
  set named in `GATING_GENERALISE_2026-10-18.md` (`all_gates.csv`) is not in
  `~/gatevision` on the Mac.
- The in-app harness with lanes on a live run (the viewer's Auto Gating):
  the bench and these MCP runs cover the engine and the worker path.
