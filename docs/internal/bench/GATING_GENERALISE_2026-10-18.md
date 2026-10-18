# Classic gating with an absence check, split mixed rows and whole-image restraint (2026-10-18)

Branch `feature/gating-generalise` (off `main` 8678444b). Design: `docs/internal/AUTOMATIC_GATING.md` §2d.
Records: `~/gatevision/gen` (session records, every packet picture, `score_gen.py`).

## What changed (each a `SessionOptions` switch)

| switch | default | does | prompted by |
|---|---|---|---|
| `absence_check` | on | before a marker's first look, six unmarked regions (most-positive twice, brightest, densest, borderline, weak; fields at least half as full of cells as the typical cell's); no judged region `clear` in two pictures of different regions closes it `no_positive_population`; a technical check's failed / empty / cannot-tell verdict goes there too; the lineage screen waits until after it | LSP11761 PD1, CD20L (absent to the user, gated by classic); LSP11321 CD20L (failed by a technical check, a real 1 % to the user) |
| `split_mixed` | on | a capped T4 chain keeps its nearest points; a `mixed` stop row wider than 2 background sds is split (`lattice.split`, lattice points) and looked at again | LSP11321 FOXP3 (one row 6.13-6.97; user 6.93), p193 CD20/FOXP3 |
| `whole_image_moves` | off | after a T4 look, a whole-image too low / too high keeps the gate at low confidence instead of closing it for review | LSP11321 CD45 right but in review, so 13 markers lost it as a partner |

## Protocol

Blind general-purpose subagent readers (12 packets a conversation), propose mode, scratch copies of
the projects with every gate cleared, `on_limit: extend`, one run per arm. New engine (N) on
lsp11385, LSP11321, LSP11761, LSP11361 and lsp35766_p193 (a second panel, 6 hand gates); today's
classic (O) on LSP11761, LSP11361, p193 the same afternoon; this morning's classic run (same
readers) for lsp11385 and LSP11321. Truth: `all_gates.csv` with the user's rechecks, including the
2026-10-18 LSP11321 round.

The absence check was revised three times during the run, each time on a failure seen in it (four
regions closed LSP11361 E-cadherin, a real 26 % population; a technical check's `cannot_tell`
bypassed it; LSP11761's edge band read `clear`; the lineage screen closed PD1 before it was shown).
LSP11361 was rerun from scratch on the revised check; markers a revision changed after they had
closed were re-run alone on the final code (LSP11761 CD20L and PD1, LSP11321 CD20L) and are
reported as "final code" beside the run as it went. **These images are now development images
for the absence check**; the held-out PCA images have not been run.

## Results (mean |gap| / share within 0.2 / share 0.4+ off)

| image | N as run | N final code | O today | classic earlier |
|---|---|---|---|---|
| lsp11385 (17) | 0.092 / 88 % / 0 % | same | – | 0.092 / 88 % / 0 % (this morning) |
| LSP11321 (17) | 0.289 / 53 % / 24 % | 0.268 / 53 % / 18 % | – | 0.284 / 59 % / 24 % (this morning) |
| LSP11761 (17) | 0.401 / 47 % / 29 % | **0.255 / 59 % / 18 %** | 0.402 / 41 % / 24 % | 0.422 (10-11) |
| LSP11361 (17) | 0.195 / 59 % / 6 % | same | 0.188 / 65 % / 6 % | 0.293 (10-11) |
| p193 (6) | **0.277 / 67 % / 17 %** | same | 0.365 / 50 % / 50 % | – |
| pooled (74) | 0.247 / 62 % / 15 % | **0.209 / 65 % / 11 %** | (40) 0.305 / 52 % / 20 % | (68) 0.273 / 57 % / 19 % |

On the 40 markers both engines read today: N as run 0.295, N final code 0.233, O 0.305.

- **Absence:** on the final code LSP11761 CD20L and PD1 close as absent (gaps 1.38 and 1.15 to
  0.04 and 0); no marker the user gated was closed (74 markers; the first version's false close of
  LSP11361 E-cadherin is fixed). LSP11761 PD-L1 (user 8.0, 0.01 % positive) was not closed: one
  region of six read `clear`.
- **Split:** p193 CD20 4.10 (O) -> 4.61 (user 4.51); lsp11385 FOXP3 split once, 5.84 (user's two
  sittings 6.00 / 6.42).
- **Whole-image restraint:** LSP11321 CD45 at 7.76 (user 7.8) is a low-confidence gate, not a
  review; gates unchanged where it fired.
- **Cost:** 3.1-4.3 packets a marker against classic's 3.1-3.3 (+0.8; the absence picture is one
  packet per non-T1 marker, two before a close).

## Still wrong (0.4+ on the final code)

LSP11321 SMA 5.97 (user at least 7.0: fibre-bordered round cells read as own stain), FOXP3 6.03
(accepted at the start by the first look, so no candidates to split; user 6.93), CD163 7.40 (7.9);
LSP11761 PD-L1 (above), CD11c 7.35 (6.6, the start; both engines), CD68 6.44 (7.0); LSP11361 CD4
7.37 (6.9); p193 CD31 4.94 (5.79). Reader variance on identical code is about 0.07 per image
(this morning's LSP11321 vs the 10-11 run), the size of several differences above.

## Next

The bar the plan set is judged on held-out images: twelve PCA images, both arms, two runs each.
