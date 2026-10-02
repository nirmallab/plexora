# Automatic gating: live run on lsp11385 (primary cutaneous melanoma, 2026-10-01)

An agent (Claude Opus 5.5, `agent: claude-opus-5-5`) drove one gating session
on `lsp11385`: 202,049 cells, 40 channels, 0.325 µm/px, a log1p CSV table.
The run was mirrored into a browser tab. The session was
`gs_20261001T142835_c6b952`, in `mode: apply`, with `on_limit: extend` and
biology `{tissue: skin, disease: primary cutaneous melanoma}`.

QC from 2026-09-30 (`qr_20260930T210858_51da4d`) was applied in strict mode.
It left out 18,518 cells (9.2%).

**Outcome.** 35 markers were gated and 5 structural channels skipped. All 35
gates were written:

- 22 accepted
- 5 accepted at low confidence
- 8 manual review recommended

The run took 91 packets and about 21 minutes of wall-clock time.

## Tokens

The figures are from the session transcript, summed per API call (107 calls).
The main agent did all the gating, and no subagent ran during it.

| Model | Calls | Input (uncached) | Cache write | Cache read | Output |
|---|---|---|---|---|---|
| claude-opus-5-5 | 107 | 212 | 887,054 | 29,592,879 | 45,049 |

**Cache reads dominate.** Context grew from 46k to 480k tokens, about
4.5k tokens per packet (two webp images plus 4–9k characters of evidence).
Every later call re-reads all of it.

- A conversation that answers *n* packets therefore costs about
  n² × 2.25k tokens of cache reads.
- At list prices the gating phase cost about $23: $14 cache reads, $8 cache
  writes (46% of them one rebuild at call 89, when old thinking blocks were
  dropped), $1 output.

What changed after the run (AUTOMATIC_GATING.md §5, §5c):

- **Workers.** The coordinator hands the packet loop to a fresh worker every
  `ENGINE.markers_per_worker` (now 4) markers, from the session's `delegate`
  block, on the tier the package names (`judgement` for gating). gate-image
  went from 31k to 10k characters, and the worker reads gate-packets (5k).
- **Each brief once per reader** (`evidence: delta`). On this run's 91
  packets a depth-two, per-reader fingerprint would have removed 26% of the
  packet text (hierarchy `used`/`avoided`, scored candidates, partners,
  biology `said`/`expect`).
- **Repeat sheet rows left out** (`sheets: trim`).
- **Reference.** This run's gates are `docs/internal/bench/
  lsp11385_reference_gates.json`. `plexora ai bench gating --score-session
  <id> --truth json:<that file>` scores a later run over its 27 accepted
  markers; this session against itself scores 1.000.
## Delegated runs (same day, propose mode, `reuse_answers: false`)

Both runs used the new defaults (`evidence: delta`, `sheets: trim`), with nine
`plexora-gating-worker` workers of four markers each. Ki67 was skipped because
the user had approved its gate in the viewer. The worker costs are from
`tools/transcript_cost.py` at list prices; the coordinator is not included.

| Run | Workers | Worker tokens (write / read / out) | Cost | Wall clock | Median F1 vs ref | Code agreement vs ref | Same state as ref |
|---|---|---|---|---|---|---|---|
| ref `gs_…142835_c6b952` | none (one Opus conversation) | 887k / 29.6M / 45k | ~$23 | 21 min | 1.000 | 1.000 | 34/34 |
| A `gs_…170424_bf7d06` | Opus | 495k / 5.05M / 12k | $5.93 | 17 min | 0.980 | 0.779 | 26/34 |
| B `gs_…172141_610123` | Sonnet | 615k / 4.73M / 6k | $3.81 | 41 min | 0.983 | 0.727 | 21/34 |

- **Cost is ~4× lower with Opus workers.** Each worker starts at 8.3k tokens of
  context (a `tools:`-restricted agent gets only its four tools) and ends at
  48–81k, against 480k for one long conversation.
- **Gate placement barely moves.** Where both runs accepted a marker, the
  gates agree (median F1 0.98–1.00; A against B 0.996). The whole-panel code
  agreement is a strict endpoint: a difference on any one of 26 markers
  breaks a cell's code. Opus does not reproduce itself at 0.98 on this panel
  (A against ref: 0.779), so that bar was wrong. Compare a cheaper run with
  Opus's own run-to-run spread instead.
- **Triage does move.** Opus against Opus agreed on 26/34 accept/review
  states. Sonnet accepted six markers that both Opus runs sent to review or
  failed (CD31, CD11c, CD20L, LAG3, TIM3, CXCL10). Most were the
  CD3e-aggregate spill markers of B.1, whose spill Sonnet explained away.
  It also failed CD163, which both Opus runs accepted.
- **Decision:** gating stays on `judgement`. Sonnet saved $2.12 (36%), took
  2.4× as long, and its errors fall on the expensive side: a marker accepted
  instead of being flagged for a person.
- Waste seen: the start sent the coordinator the 20k-character reading guide
  it never uses. Fixed: a delegating start sends only `guide_version`.

## Per-marker result

| State | Markers |
|---|---|
| accepted (high) | SOX10 (T1), CD8a, CD103 |
| accepted (moderate) | CD45, Ecad, SOX9, CD3e, CD138, CD68, MART1, CD163, CTLA4 (within CD3e), FOXP3, GZMB, HLAABCL, HLADRL, Ki67, PCNAL, PDL1, TBX21, TCF1 (within CD3e), b2m |
| accepted_low_confidence | SMA, CD16, NGFR, PD1, TIGIT |
| manual_review_recommended | CD31, TubbIII, CD11c, CD20L, CD4, LAG3, TIM3, CXCL10 |

Biology seen in the looks (interpretation, one image, not a claim):

- One tumour nest is HLA-ABC/B2M-low against a positive surround.
- PD-L1 has an adaptive pattern at the immune interface.
- TCF1 stains epidermis and tumour off-target, so it is gated within CD3e.
- CTLA4 is dominated by antibody aggregates, so it is gated within CD3e.

## A. Fixed after the run

1. **The T4 chain dropped the middle of the lattice.**
   - When the cap bit with no anchor, `lattice.chain` kept the 3 nearest
     points plus the farthest one.
   - So the last row spanned 10k–131k cells, and the `ceiling` and `onset`
     points that would have split it were never shown. This happened on
     CD45, Ecad, MART1, Ki67, PCNAL and PDL1.
   - It now keeps the nearest points when there is no anchor.
2. **An anchor at the gate ended the chain.**
   - `bio:`, `ctrl:` and `within:` points 0.001–0.004 away from the gate
     made a whole round for nothing (PD1, and CD11c, which went to review).
   - An anchor within `ANCHOR_AT_GATE` (0.01) no longer stops the chain.
3. **Empty rows.**
   - A point that snapped onto an unsnapped gate (LAG3: 5.99 against
     5.98999) produced an `i1` row with zero cells.
   - Points within `SAME_POINT` (0.005) of the gate are skipped.
4. **The regression loop re-served the same packet.**
   - Regression said `too_low` right after a T4 answered `keep` (mixed)
     from the same gate in the same direction. The same candidates came back
     (LAG3 pk_0065 → pk_0067, same artifact ids).
   - That now closes to `manual_review_recommended`.
5. **The reading guide repeated 5 shared fields in 11 schemas.**
   - The fields are notes, artifact_flags, request, biology and ask_user.
   - They now appear once, in `reading_guide.answer_common`. The guide fell
     to 19.8k characters, about half of what it was.
6. **`get_qc_results(detail="brief")` returned 58k characters.**
   - The bulk was per-channel `reached_by` id lists, every module's decision
     and every residual row.
   - Brief mode now sends counts and states, plus the top 10 residual rows.
7. **Trailing-`L` names were unresolved.**
   - CD20L, PCNAL, HLAABCL and HLADRL needed a manual `set_panel_context`.
   - `vocabulary.canonical` now tries the name without a trailing capital L
     as a last resort.
   - The `LIGANDS` stop-list (CD40L, CD62L, OX40L, FASL, …) keeps real
     ligands off their receptors.
8. **A misleading close reason.** CD31 said "no reference marker is gated
   yet" when CD45 *was* gated. The real cause was that the only relation was
   low-confidence. The reason now says that.

## B. Open issues, most important first

1. **Exclusive contradictions do not cap confidence.**
   - CD68 (moderate) and CD163 (moderate) were accepted with a CD3e
     contradiction of 1.0.
   - The same holds for CD16 (low), CD11c and CD4: 30–60% of each one's
     positives are CD3e+ cells in dense lymphoid aggregates.
   - This is membrane spill between touching cells. No threshold fixes it.
   - Two directions:
     - Cap confidence on an exclusive contradiction above
       `needs_review_contradiction`.
     - Offer a "within NOT partner" condition (gate CD68 among CD3e− cells).
   - Either needs the bench before it ships.
2. **`gating_qc.needs_review` lists 23 of 35 markers.** Most are pairs
   driven by the aggregate spill above, so the list is too long to act on.
   It should rank by contradiction × positives, or group by the shared
   partner.
3. **A `qc_confirm` after a look's `no_positives` re-renders the same
   sheet** (CD20L). It should either show something new (the whole-image
   stain at a different window, or the partner channel), or skip straight to
   review on `cannot_tell`.
4. **CD31 ended in review because its only partner was low-confidence.** A
   vascular marker in skin could lean on SMA (pericytes, `coexpressed`
   adjacent). That needs a spatial-adjacency relation, which the vocabulary
   does not have.
5. **A continuous marker's background mode was the positive peak.**
   HLAABCL's `background.mode` was 7.97, above its gate. The gate was still
   right, but `onset` and `ceiling` were absent from its candidates.
6. **The MCP needed a running viewer.** No Plexora app was running at the
   start. The agent launched `plexora lsp11385 --browser` and the MCP
   attached on the next call. The `list_viewers` hint could say that
   directly.
