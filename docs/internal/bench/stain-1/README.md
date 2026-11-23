# stain-1: the Low quality staining bench

`plexora ai stain-bench` (`plexora/ai/stain_bench.py`), the gateway bar
`qc.stain_check` (biocognia-platform `workers/ai/src/ai/catalog.ts`). The second
and third stain readers are served on the same evaluation (`BENCHED_AS`).

## Data
`labels.json`: the owner's labels (2026-11-23) of every channel of two of the
seven labelled images, the two that hold the failures: lsp35766_p193_2 (CyCIF,
8 failed) and imc-immucan-example (IMC, LAG3 failed). Scored channels are the
ones the stain check reads (`stain.checked_channels`): 75 = 9 failed, 54 real,
12 weak. Whole images, no sampling. The full labelling of seven images is in
`../runs/2026-11-23-channel-labels/`.

The images stay on the owner's Mac: the bench runs in a data root that holds both
projects (lsp35766_p193_2 from the library, imc-immucan-example from the data
root), isolated (`PLEXORA_DATA_PATH`, `PLEXORA_LIBRARY_PATH`,
`BIOCOGNIA_MARKER_GRAPH` in scratch).

## Bar
At least 0.75 of the failed stains settled `failed` (7 of 9), at most 0.04 of
the real ones (2 of 54); weak stains called failed reported, not barred; no
invalid answers, every channel settled. Agreed with the owner 2026-11-23.

## Runs (2026-11-23, `claude -p`, effort high, readings settled best of three)

| renderer | model | failed caught | real called failed | weak called failed | readings | bar |
|---|---|---|---|---|---|---|
| stain/1 | Sonnet 5.5 | 7/9 (0.78) | 4/54 (0.074) | 4/12 | 94 | fail |
| stain/1 | Opus 5.5 | 8/9 (0.89) | 4/54 (0.074) | 4/12 | 91 | fail |
| **stain/2** | **Sonnet 5.5** | **9/9 (1.0)** | **2/54 (0.037)** | 3/12 | 91 | **pass** |
| stain/2 | Opus 5.5 | 8/9 (0.89) | 4/54 (0.074) | 4/12 | 93 | fail |

stain/1 -> stain/2: row E's contrast. The partner's range was pooled over its
own field, so a debris blob in that field (lsp35766_p193_2 E-cadherin: top 823
over the tissue, 5882 with the field) drew the partner black, and the reader
saw "no co-expression". Row E now takes each channel's range over the tissue
of the overview alone, as the prototype did (`coexp.py`).

Every reading is kept in `runs/<run>/` (`answer_<n>.json` per channel; the
pictures are not kept, `--write` draws them again); `score.txt` is the scorer's
output.

Sonnet, stain/2, real stains called failed: CD11b (P193; its only partner is
CD66b) and CD33 (IMC; no partner). Both were called failed by every reader in
the labelling study too. Opus adds CDX2 and HH3 (P193) and misses betaTUBULIN.
