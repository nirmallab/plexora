# bench3d — the 3-D viewer benchmark

Reproducible before/after numbers for the 3-D viewer: plane steps, scrubs, the
plane/section synchronisation, channel toggles, the 3-D view, contrast, zoom,
network and memory. Results and targets: `docs/internal/3d/PERF.md`.

```bash
PY=<the plexora conda env's python>
$PY tools/bench3d/fixtures.py make small twoplane flat        # seconds
$PY tools/bench3d/fixtures.py make medium large               # minutes
$PY tools/bench3d/fixtures.py register --ims <file.ims> --meso <stack.zarr> --prescan
$PY tools/bench3d/run.py --label S1 --datasets small,twoplane,ims,meso,flat
$PY tools/bench3d/report.py docs/internal/3d/perf/<before>.json docs/internal/3d/perf/<after>.json
$PY tools/bench3d/parity2d.py <before>.json <after>.json
```

- Everything generated lives under `$PLEXORA_BENCH_HOME` (default
  `~/plexora-bench`): fixtures, the scratch data root and HOME, run files,
  screenshots. Never inside a synced folder.
- Chrome runs with the GPU (`--use-angle=metal`); a software renderer is refused.
  `--headed` gives the display's own refresh (120 Hz on a ProMotion panel);
  every number that is "within a frame" is measured against the run's own F.
- The page is observed from outside (`probes.js`, `pagekit.js`, injected before
  the app): the same observers run against every build, so a "before" build with
  no marks of its own is measured exactly like an "after".
- To measure an older commit without stopping work: `git worktree add --detach
  ~/plexora-bench/tree-<sha> <sha>` and `run.py --tree ~/plexora-bench/tree-<sha>`.
- `legacy/` holds the scripts the numbers in TESTING.md were first taken with.
