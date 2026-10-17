# Handoff: contrast observations and vision-first gating (experimental)

Written 2026-10-16 (late evening, EDT). Branch `contrast-observations`, pushed to the `plexora`
remote (there is no `origin`). **Not merged to main and not to be merged until the user says
so.** Delete this file before any merge.

## What the user asked for

1. Use what the per-image contrast check's vision model sees (stain pattern, dim cells that
   are really stained, bright cells that are debris) to help gating. Keep QC light: bright
   artifacts only steer what gating looks at and how sure it is. The user's words: "Don't do
   too much of QC as we already have a separate module for it."
2. After the first real test, the user's direction was: "we should give less importance to
   data distribution and more to vision."
3. Their reference gates on `lsp35766_p193`, set by eye: **CD3E about 5.11, CD8a about 7.2**
   (the table's log units, as the gating panel shows them).

## What is on the branch

- **4900adc: contrast observations** (design in `docs/internal/AUTOMATIC_GATING.md` §2d).
  - The contrast check (`display_setup`) sends a second picture, the spread strip: per marker,
    6 bright and 4 dim cells spread across the image.
  - The answer gains `pattern_seen`, `stain_location`, `bright_artifact` and `dim_stained`.
  - These are stored on the calibration as `choices[ch].observed`.
  - Gating uses them only with `SessionOptions.observations: "use"`: as evidence in every
    look, a dim anchor for the start, soft flags, fields that avoid the debris, and T1
    routing.
- **The next commit: `vision_first`** (§2e). Option `SessionOptions.vision_first`, default
  off. It applies to weak distributions (`Engine.weak_distribution`):
  - one stained dim cell is enough, with no floor at the background onset;
  - `Engine.vision_start` starts at the 15% rung when there is no dim anchor;
  - the lattice gets rungs by share of cells (`lattice.rungs`);
  - cells below the gate judged "mixed" send the gate down (`verdicts.direction_from_rows`,
    `mixed_down`);
  - `run_regression` waives the distribution and spatial checks once a look saw the stain;
  - `apply_regression` keeps such a gate on `cannot_tell`, at low confidence.
- **Tests:** `tests/test_contrast_observations.py` and `tests/test_vision_first.py`. The
  subset below passed 348/348 through the test-runner, with paid tests included:
  vision_first, contrast_observations, gating_tiles, gating_session, autogate_units,
  agent_viewer_api, ai_harness, ai_tasks, plugin_boundary, docs_freshness, ai_skills,
  display_curve.
- **Nothing that runs on a data node changed.** The `hms-o2` node runs its own Plexora
  install, 0.0.28.

## Results on the real image (lsp35766_p193, held on O2 node `hms-o2`)

The table is the MCMICRO cellpose CSV with its own mask: about 820k cells, 51 channels.

**Contrast check:** in the end, 8 markers were checked (CD163, CD3E, CD8a, CD20, CD4, FOXP3,
CD11c, PD1), at about 17 credits.
- 7 contrast settings were picked by vision. FOXP3 kept the rule setting (no pattern seen).
- Debris (3 of 6 bright tiles each) was called only on PD1 (small specks) and FOXP3 (a large
  clipped blob). Both calls look right in the strip.
- Each of CD3E, CD163, CD4 and CD8a had just 1 of 4 dim cells seen stained.

**Gating, propose mode, all 8 markers**, with A = observations off and B = observations
`use`:
- A and B gave identical gates, and all 8 markers ended in manual review in both arms.
- The gates: CD163 7.12, CD3E 5.94, CD8a 8.00, CD20 4.10, CD4 7.03, FOXP3 5.12, CD11c 5.61,
  PD1 6.18.
- The user checked CD3E and CD8a by eye: most of the moderately dim cells were missed.

**Why** (diagnosed from the session records):
1. **The start sat at the noise ceiling.** On these markers the GMM gate calls 96–99% of
   cells positive, so it gets weight 0.1. The start comes from the noise ceiling
   (`scoring.py`, mode + 6 core sd) or the onset instead:
   - CD3E: start 5.94, 1.9% positive. The onset at 5.29 (15%) actually scored higher.
   - CD8a: start 8.00, 1.1% positive. The onset was 8.11, above p99.
2. **"Mixed" below the gate was read as "about right".** The looks said below "mixed", near
   "mixed", above "own_stain". `direction_from_rows` only moves the gate down on
   "own_stain".
3. **The whole-image checks then failed and nothing rescued the gate.**
   - The regression failed on `spatial_tiles` (T cells cluster) and `guard_band`.
   - The whole-image look answered `cannot_tell`.
   - CD8a had no reference, because its parent CD3E was under review.
4. **The observations' dim anchor needed 2 stained cells.** Only 1 was seen. For CD8a it
   would also have been held at the onset, 8.11.

**Vision-first run (CD3E and CD8a only, observations `use` + `vision_first`, about 37
credits):**

| | Before | vision_first | User | Positive |
|---|---|---|---|---|
| CD3E | 5.94 | 5.32 (accepted, low) | 5.11 | 14% |
| CD8a | 8.00 | 7.29 (manual review) | 7.2 | 13% |

- **CD3E:** the start came down to the dim anchor at 5.32. The look said mixed below, so the
  gate went down. The T4 rows were 5.32→5.28 mixed, →5.19 mixed, →5.18 mixed, →5.06
  neighbour_only. The gate stays on the first "mixed" row (`lattice.place`, where `MOVES
  ["down"]` is own_stain only), so it ended at 5.32.
- **CD8a:** the start came down to the dim anchor at 7.29. The look said mixed *above*, so
  the gate went up. The rows were all mixed or neighbour_only, so it stayed. The regression
  said `cannot_tell`, and `own_seen` was never set (the model was unsure throughout), so it
  went to manual review at 7.29.

## Suggested next steps (the user has not chosen yet)

1. **Let the gate go down past "mixed" rows.** With vision_first, going down, pass "mixed"
   rows and stop at the first neighbour_only or unstained row. On this run that would have
   given CD3E 5.18, against the user's 5.11. Going up is unchanged. The risk is heavy spill;
   the neighbour_only stop guards against it.
   - Where: `lattice.place` / `verdicts.MOVES`. Pass a vision_first flag from
     `transitions.apply_t4`.
2. **Then run all 8 markers** with `vision_first`, about 150 credits, and put the gates in
   the sandbox for the user to view.
3. **Maybe sample the dim tiles lower** (p70–92 instead of p85–92; `CELL_WINDOW` near band in
   `plexora/agent/evidence/cell_window.py`). This changes the capture, and the contrast rules
   use that band too, so check the contrast tests (`test_display_curve`).
4. **The CD8a manual review:** with vision_first, maybe let a dim anchor that the T4 rows
   did not contradict stand at low confidence even without `own_seen`.

## The sandbox

**Removed** (2026-10-16, at the user's request), along with its snapshots. To rebuild it:

1. Make `C:\pxobstest` and copy in, from `%LOCALAPPDATA%\plexora`:
   - a `config.json` holding only the `lsp35766_p193` entry;
   - the `lsp35766_p193\` folder;
   - `nodes.json`, `settings.json`, `datasets.json`, `remotes.json` and `servers.json`;
   - `license\`.

   **These hold licence and node-token files: delete the folder when testing ends.**
2. Run the contrast check once (`redo: true`). Then stop the servers and snapshot the
   project `.db`, `.agent\memo\gating\lsp35766_p193.jsonl` and `.agent\gating\panels`.
3. Before each gating run, stop the servers and restore that snapshot. Otherwise gates set
   by hand look like user edits.

**Servers**, run from a worktree of the branch at `C:\pxobs` (see Cleanup), with cwd `C:\pxobs`:
```
PLEXORA_DATA_PATH=C:/pxobstest python -m plexora --port 8931 --no-browser --no-detect --data-dir C:/pxobstest
PLEXORA_DATA_PATH=C:/pxobstest python -m plexora mcp serve --transport http --host 127.0.0.1 --port 8322 --no-auth --server http://127.0.0.1:8931 --data-dir C:/pxobstest
```
- The interpreter is `C:/Users/aj/.conda/envs/plexora/python.exe`.
- Use the full profile (no `--profile`): `ai_run_session` is not in the gating profile.
- `--data-dir` alone is not enough: always also set `PLEXORA_DATA_PATH`, and check
  `server_info.data_root == C:\pxobstest` before any write.
- After any code change, restart both servers.

**MCP client:** the Claude Code `plexora` MCP connection failed in this session, so the
tools were driven through the HTTP MCP server with a small client. The scratchpad copy will
be gone; to rebuild it:
- `mcp.client.streamable_http.streamable_http_client(URL, http_client=httpx2.AsyncClient(...))`
  yields `(read, write)`;
- then `ClientSession(read, write)`, `initialize()` and `call_tool(name, args)`;
- the result fields are `is_error` and `mime_type`.

**Calls used:**
- **Contrast:** `ai_run_session {kind: "contrast", project, markers, start_options: {redo: true}}`.
- **Gating:** `ai_run_session {kind: "gating", project, mode: "propose", markers,
  parallel_markers, start_options: {observations: "use", vision_first: true, reuse_answers:
  false, on_limit: "extend"}}`.
- **Waiting:** poll `job_get {job_id}` until done, then `gating_session_status
  {session_id}`. The unit records are under
  `C:\pxobstest\.agent\sessions\gating\<sid>\session.json` and `decisions.jsonl`.
- **Showing gates:** propose mode writes no gates. For the user to look, write them with
  `set_gate {project, marker, low}` and point them to
  `http://127.0.0.1:8931/lsp35766_p193?tool=gating`.
- **Observations:** read what the contrast check saw with `calibration.load(project)` and
  `calibration.observed(record, channel)`. Run with `PYTHONPATH=C:/pxobs` and
  `PLEXORA_DATA_PATH=C:/pxobstest`.

**Credits:** about 183 spent in this session (contrast 16 + 17, gating 73 + 41 + 37).
Gating is quoted at 25 credits per marker per run, but real runs came in lower. Check
`ai_balance` first.

## Traps hit in this session

- **Licence revoked mid-session.** The platform answered `certificate_revoked` on refresh,
  meaning the device had been signed out in the portal; it wasn't caused by a refresh. The
  user re-activated, which gave a new environment id. If `ai_*` tools say Free/revoked,
  ask the user to sign in again. Never re-activate yourself.
- **The project's first table was misaligned.** The earlier AnnData table
  (`reg_cycif_centroid_*_split_px` coordinates) put centroids away from the nuclei, so the
  tiles were black. The user switched the project to the MCMICRO CSV, which aligns.
  - Before trusting any vision result, render a cell or two (`explain_cell`) and check that
    the outline sits on a nucleus.
- **A scratch script named `fractions.py` became a fork bomb.** The MCP client imported it,
  which started it again. Never name a scratch file after a stdlib module.
- **The node tunnel expires.** `nodes.json` `expires_at` for `hms-o2` was about 01:02 EDT
  on 2026-10-17. The user's live server on 8000 owns the tunnel. If node reads fail, ask
  the user to reconnect O2, then copy `nodes.json` into the sandbox again.
- **Never tail or cat O2 node job logs.** They print the node token.
- **Never write to the user's live project at 8000** unless they ask.

## Cleanup when done

- **Already done:** the servers, the sandbox, the snapshot copies and the scratchpad are
  gone.
- **Worktree:** removed too. The branch lives on the `plexora` remote and in the main
  clone's local refs. To work on it again, run from the main clone:
  `git worktree add C:\pxobs contrast-observations`.
  - Then junction `plexora\client\node_modules` from the main clone, or about 110 JS
    probes fail.
  - Run servers and scripts with cwd `C:\pxobs` and `PYTHONPATH=C:\pxobs`: Plexora is
    editable-installed from the main clone.
