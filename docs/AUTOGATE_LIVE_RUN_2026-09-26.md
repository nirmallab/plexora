# Automatic gating: first live run on real data (exemplar-001, 2026-09-26)

An agent (Claude) drove a gating session on `exemplar-001` (MCMICRO exemplar,
11,170 cells, 12 markers, log1p table), mirrored into an open viewer tab.
Session `gs_20260926T213846_149f9b`.

**Outcome.** Nine markers were gated and three DNA channels skipped. The run
took 18 packets and 28 images, about 10.6k vision tokens and 84k characters of
packet JSON.

| Marker | Result | Written |
|---|---|---|
| CD45 | accepted, moderate: 6.60 (GMM 6.79), T2 → T4 → regression | yes |
| CD16 | accepted, low confidence: 6.47 (budget ran out) | yes |
| NCAM | technically failed (flat channel, confirmed by eye) | no |
| CD11B, CD57, SMA, FOXP3, ELANE | manual review recommended | no |
| ECAD | insufficient information (a budget artefact, see B3) | no |

The user's hand-set ELANE gate (6.84) was kept.

## A. Fixed on the branch during the run

1. **The zero spike ruined the GMM.** In 6 of 9 markers, 62–76 cells sit at exactly 0 (cells quantification could not measure).
   - The 3-component fit spent a component on them. It gated at about 0.01, calling 99% of cells positive, with σ_bg = 0.001, so no candidate step could climb out.
   - `profile.floor_spike` now detects a spike at the column minimum, set well apart from the body (0.1–20% of cells, gap to the body's p1 > 2 IQR). `profile.fit_for` then fits the body only.
   - The candidates, sampler, regression checks and `resolve_gate` all use it.
   - After the fix, positive fractions are ECAD 32%, SMA 24%, CD16 21% and FOXP3 5.6%, where every one had been 99%.
   - **The sidebar's Auto button (`model.fit_for`) has the same defect and is not changed.**
2. **The gate-relative panel was solid white on log1p tables.**
   - `to_log` followed `fit_space` ("values" for a log table), so the panel went linear around 6.79 against pixels in the hundreds.
   - `render_collage` now converts the gate with `expm1` when `table.log_transformed` is set, and keeps the log panel.
3. **Answer schemas hid nested fields.**
   - `plausibility` was shown as `{"ref": "Plausibility"}`, and `entries` as a bare array.
   - `schema_for` now inlines `$ref`s, array items and nullable options, at 2.7 KB or less per kind. Partner fields are documented.
   - Three of my first answers were refused because the fields could not be seen: `caveats`, `plausibility.artifacts`, and `flips_assessment` (the real field is `intervals`).

The tests are `test_a_spike_of_unmeasured_cells_at_zero_is_not_the_background` and `test_answer_schemas_spell_out_nested_fields`. The gate-relative conversion has no test yet: the synthetic fixture is not log1p.

## B. Open issues, most important first

1. **The contradiction guard blocks every "too low" on low-separation markers.**
   - CD11B (D 0.9), SMA (D 1.4) and FOXP3 (D 1.7) all went to manual review, because the gate already sits within 0.5σ_pos of μ_pos.
   - With D < 2 the mixture means are not a meaningful ceiling. Apply the guard only when D ≥ 2, or use the cell-level evidence instead (for example, the partner contradiction the packet already shows).
2. **Hard QC flags do not fit CyCIF intensities.**
   - `weak_signal` (signal-to-background < 2 in linear units) is a hard flag on CD11B, NCAM, ECAD and ELANE. ECAD is plainly real (32%, traces the glands).
   - T1 scores are 0.0–0.45 for every marker, and `unstable_fit` fires on 8 of 9, so nothing auto-accepts.
   - Make `weak_signal` soft, or measure it on background-subtracted values. Calibrate the T1 rules on expert-gated CyCIF before relying on them.
3. **The per-unit budget throws away good answers.**
   - ECAD: QC + T2 + T4 used the 3-packet default, so the regression check never ran, and a confident c1 (7.32) ended `insufficient_information`.
   - CD16: T2 + T3 used the 4-image budget. The GMM gate was then *written* as low confidence, although the last answer said "too low".
   - Fix: QC and panel packets should not count against a unit, and a pending regression confirm should always be allowed. When the budget runs out with a direction on record, propose rather than write.
4. **The display window gets blown out by bright specks.**
   - CD57's window is [223, 44 896] (p99.5 of the overview), so the marker and merge panels are black for every cell. `calibration_flags` stays empty.
   - Cap the high end from the cell side (for example `expm1` of the table's p99.5), and flag windows wider than about 30×.
5. **Partner evidence is missing at T2.**
   - `partners: []` for CD11B, CD16, ECAD and SMA, even after CD45 had been accepted.
   - It only appears at T3. Compute it at packet time from the current run.
6. **The regression check contradicts the candidate generator.** The smallest move offered for CD45 (0.5σ) flips 12% of positives, and `flip_share` (limit 10%) then fails it, costing an extra vision packet. Scale the limit with the step.
7. **The guard band leaves one candidate on overlapping markers.**
   - For ECAD (D 1.7), a "too high, medium" answer got one candidate, because the 1σ step falls below μ_bg + 1σ_bg.
   - Magnitude is effectively ignored. Offer the guard edge itself as the last candidate, or widen the guard when D < 2.
8. **Routing.**
   - An `image_quality` flag with `compartment: cannot_tell` went to `qc_confirm` with the reason "stain in the wrong compartment" (untrue), and dropped an explicit request for the CD45 reference.
   - After `real_signal`, CD57 went straight to manual review instead of back to T2 or T3.
   - The reason text should name the actual trigger, and a reference request should outrank a soft artefact flag.
9. **A kept manual gate still costs a vision packet.** ELANE was `skipped_manual` in the first session but got a `qc_confirm` packet in the second.
10. **Mirroring.**
    - `set_hd_mode` exceeded the 5 s per-command timeout: the HD swap rebuilds every tile. The session was marked `degraded` on its first packet, even though the tab did switch.
    - `packet.mirror` is always `None`, so the agent never learns the mirror degraded.
    - Units interleave (CD16's T4 waited behind CD57, ECAD, SMA, FOXP3), so the live view jumps between markers.
11. **Bivariate plot.** The axes include the zero spike, squeezing the body into a thin strip. Use the body's range.
12. **Resume status.** After the driving process died, status read `bulk_running` with 9 `pending` units while packets were already being served. The by-state counts lag.
13. **Report (PDF).**
    - The "why" column runs off the page, and outcome text is truncated ("accepted (low confidence").
    - The histogram subtitle says "labels in raw units" on a log1p table.
    - The cell strip is tiny.
14. **Wording and small items.**
    - "estimators disagree about 295% of the positive calls" should say "3× as many cells".
    - The tool-path collage title reads "CD45t2" (missing separator).
    - An outstanding packet keeps a stale image after a renderer change, so a `rerender` option would help.
    - The project's image channel list contains a pseudo-channel "Area". The autogate ignored it correctly, but it shows in `thresholded`.
15. **Environment.** The long-running viewer on :8000 predates `/agent/v1`, so it cannot attach. Nothing tells the user; the agent simply finds no views. The server should say "restart the viewer to let an agent attach".

## C. What worked

- **Panel context.** It asked once about the three markers the vocabulary lacked, reordered the panel from my answer, and cached it by panel hash, so the second session did not ask again.
- **Resume.** The bulk job was resubmitted three times after process deaths, with no lost state.
- **Manual gate.** ELANE's user gate was never touched.
- **Records.** Writes were receipted and undoable, and provenance for every marker, proposals included, shows up in `get_all_gates`.
- **Viewer.** The tab showed calibrated channels (muted-blue DNA, yellow marker), HD, outlines and the active marker as the agent worked.
- **Evidence.** The T2, T3 and T4 collages, positive maps and overviews were readable, and the flip rows made T4 decisions easy.
- **Speed.** About 0.2–0.5 s per marker for the profile on 11k cells, and 1–10 s per answer-plus-next, including the mirror.
