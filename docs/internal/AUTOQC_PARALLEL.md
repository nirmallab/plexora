# AutoQC in parallel (2026-11-24)

AutoQC used to send one packet at a time to the model and score its image
checks one after another. It now keeps up to 8 packets out at once, and only
when no answer still pending could change whether a packet is asked or what it
says. It also scores the checks on several threads. The decisions are the
serial run's, made with the same model calls.

## What runs side by side

| Packet | Waits for | Out beside |
|---|---|---|
| channel audit page | the scan | anything |
| score review (check × channel) | its check scored | anything (it reads only its own field) |
| stain check | its picture drawn | anything (a verdict changes no channel, cell or gate) |
| stain re-read, second score round | its own unit's previous answer | as above |
| visual pass | the whole bulk pass, every score review | stain checks only |
| candidate (confirm → scope → localize → grid) | the whole bulk pass, every score review, the visual pass | candidates whose map boxes do not meet, stain checks |
| final review | everything | nothing |

**Why the candidates stay in order.** Candidates that touch are decided in
the serial rank order. Four things couple one candidate's decision to
another's, and each needs their masks to overlap: `_explained_by`,
`_merge_target`, `transitions._absorb` and a confirm's neighbour list. So the
order of decisions over each overlapping pair is the serial run's. The run is
conflict-serializable, and its decisions are the serial run's.

**The serial run was not deterministic either.** Before this change, a
candidate could be looked at while the bulk pass was still scoring checks.
Whether a check's region absorbed it, and how its outline was traced, then
depended on which finished first: the look or the scoring. The result followed
the machine's speed: on the `mixed` scene, merely reordering the checks
changed the serial run's calls (14 to 13) and its regions. Candidates and the
visual pass now wait for the whole bulk pass in both the serial and the
parallel run (`QCEngine.region_checks_scoring`). The engine's own design
intent ("checks before the detector candidates") now always holds. Stain
checks, score reviews and audit pages still overlap with the bulk pass.

## How it was measured

`bench_qc.run_harness` (arms `harness:N`) runs the real `QCRun` with N lanes
against spatialagents' `FakeGateway`:

- **The stand-in model** waits each task's median latency from the local
  trace's 13 QC runs (`TASK_LATENCY_S`), scaled down and jittered, then
  answers from the scene's truth (`QCTruthAgent`, oracle).
- **Each arm gets its own copy of the scene**, so the stain ledger and the
  caches an earlier arm left behind cannot answer for it, and
  `reuse_answers: false`, so no arm replays another's answers.
- **`result_signature`** compares what each run decided: every region's class,
  action, state, channels and outline, every channel's state, every stain
  verdict and the failing cells.

Real latencies from the trace: stain check median 104 s (47 calls),
artifact inspection 9.9 s, blur and registration reviews about 4.8 s,
channel audit 7.1 s, visual pass 11.1 s, final review 3.8 s. The stain checks
are most of a run's model time.

## Results

**Four scenes at a quarter of real latency** (stain check 26 s), ±20% jitter:

| Scene | Lanes | Wall s | Model calls | Input tokens | Cache read | Same decisions |
|---|---|---|---|---|---|---|
| mixed | 1 | 101.1 | 9 | 153,987 | 120,498 | — |
| mixed | 2 | 57.7 | 9 | 149,418 | 115,936 | yes |
| mixed | 4 | 45.6 | 9 | 147,124 | 113,649 | yes |
| blur | 1 | 86.1 | 7 | 115,871 | 87,187 | — |
| blur | 4 | 42.5 | 7 | 109,010 | 80,340 | yes |
| dropout | 1 | 88.5 | 8 | 133,757 | 102,815 | — |
| dropout | 4 | 44.7 | 8 | 126,889 | 95,962 | yes |
| fold | 1 | 91.3 | 6 | 100,465 | 73,797 | — |
| fold | 4 | 42.8 | 6 | 93,593 | 66,950 | yes |

**The sixteen-channel `cyclic` scene** (12 stain checks) at a tenth of real
latency:

| Lanes | Wall s | Model calls | Input tokens | Speed-up |
|---|---|---|---|---|
| 1 | 153.7 | 21 | 377,535 | 1.0× |
| 4 | 51.4 | 21 | 346,698 | 3.0× |
| 8 | 40.8 | 21 | 339,528 | 3.8× |

Repeated at a twentieth of real latency (±30% jitter): 1 lane 87.4 s; three
4-lane runs 33.6–33.9 s; two 8-lane runs 27.1–30.5 s. Every run made 21
calls, every signature was identical (check states included), and nothing
was reissued.

**A race the benchmark found, now fixed.** Two blur checks scoring at once
both read-modify-wrote the project's blur pointer file (`current.json`), and
Windows refused the second replace (WinError 5). The check was skipped
(`failed: Access is denied`): one fewer score review, and a fallback
detector in its place. Two other outcomes were possible as well: a lost pointer,
whose result the sweep then deletes, or a later "scores no longer cached".
`blur._point` and `_sweep` now run under one lock, and both blur's and
registration's `_atomic_write` retry a refused replace. `result_signature`
now includes each check's state, so a skipped check counts as a different
run.

- **Calls:** never more than the serial run's, with no reissues and no invalid
  answers.
- **Tokens:** input is 4–10% lower in parallel, because each lane's rolling
  worker carries a shorter history. Cache writes are unchanged, since the warm
  gate writes each task's prefix once.
- **Consistency:** the signatures were identical in every comparison: 8 scenes
  at 1 and 4 lanes (±30% jitter), 12 jittered 4-lane repeats of `mixed` and
  `dropout`, and the runs above (the cyclic ones after the write-race fix).
  `tests/test_qc_parallel.py` pins this.

**What this means for a real image.** On a 40-channel image, about 40 stain
checks at 104 s take about 70 minutes serially. They are independent, so 8
lanes need about 9 minutes, rate limits permitting. The bulk pass's checks
now score 3 at a time (`PLEXORA_QC_CHECK_WORKERS`), with stain pictures and
registration first so the slow looks start early. The bulk pass was not
timed on a real project for this report: the stand-in runs use small scenes
where scoring takes seconds.

## What else changed

- **Gateway:**
  - A limiter (`_Throttle`) drops a lane on each rate-limit retry and adds it
    back after 10 clean calls.
  - The gateway's own `rate_limited`, once its retries are spent, now pauses
    the run so it can be resumed, instead of failing it.
  - The first call of each task goes alone until its reply's first piece
    (`WARM_WAIT_S`), so the prefix is cached once.
  - Keep-warm pings are shared per task.
  - Stop cuts a reply that is still streaming.
- **Engine:**
  - A reader told `busy` sleeps until an answer wakes it, instead of taking
    the session lock every 0.1 s.
  - It re-checks stop and pause each time it wakes. Before, a lane woken
    after a Stop could still draw a packet.
  - The memo reads each file once.
- **UI:**
  - The panel lists the looks out ("N at once", folded by default), keyed by
    packet id, so answers can arrive in any order.
  - The headline stays on the oldest look.
  - Progress counts never step back (`progress.at`).
  - Retries are shown on their row.
  - A pause says the looks already out are kept.
  - The run's final spend shows even after the session's `finished`.
  - QC's sidebar reloads are coalesced (400 ms, at most 2 s, one at a time).
  - Two existing bugs are fixed: QC's "paused" state was never shown, and the
    gating classifier ignored `answered.closed[]`.

## Not done

- **A live run against the real gateway.** The plan's live serial-against-4-lanes
  run needs credits and a project chosen by the user, so it waits.
- **A delegate block with lanes for QC's external MCP workers**
  (`delegation.py`). The engine already serves them in parallel if they pass
  `parallel`.
- **Packet building outside the session lock (Phase 5).** The stand-in runs
  showed no lock-bound stretch at 8 lanes; revisit if a live run does.
