# Remote data performance: viewer-on-cluster vs data-node

Measured 2026-08-28 against O2 (HMS RC) from a laptop on a home/office link.

## Summary

The working assumption was that putting the viewer on the cluster (scenario A)
renders faster than keeping the viewer local and pulling tiles from a data node
(scenario B), because A "keeps the tiles next to the compute".

**Measurement does not support that.** Scenario B was faster than scenario A on
every comparison taken, and on per-channel metadata it was faster by two orders
of magnitude. The reason is structural rather than incidental: in scenario A
every cache Plexora keeps sits on the far side of the tunnel, so a cache *hit*
still costs a full round trip. In scenario B the same caches sit next to the
browser.

There is exactly one place scenario B loses, and it is a fixable one: **direct
routing bypasses the primary's tile cache and the node has no tile cache of its
own.**

**The reported "slow zoom" turned out to be neither.** Zoom-for-zoom, scenario B
fills a viewport in 0.6–0.7 s against scenario A's 1.33 s — B is about twice as
fast at the very thing that felt slow. The slow zooms come from the data node
being restarted every hour by its `-t 1:00:00` SLURM limit, after which nothing
re-warms it, so the next interaction pays a cold pyramid open and a
full-resolution re-read per channel. See
[The zoom-in symptom](#the-zoom-in-symptom-measured-directly) — fix 0a there is a
one-line profile change and is the highest-value item on this page.

## Test setup

Both scenarios read the **same file on the same storage**, verified by
fingerprint rather than by name:

| | |
|---|---|
| Path | `/n/scratch/users/a/ajn16/LAMP/registration/NLU290.ome.tif` |
| Size | `31,787,445,961` bytes |
| `mtime_ns` | `1786482969318755288` |
| Filesystem | `/n/scratch` (NFS, 83% full) |
| Image | 34100 × 29760, 20 channels, 7 pyramid levels, 1024² tiles |

Both tunnels have the same shape — `ssh -N -J <login> <compute> -L …` — so
neither scenario has a network advantage.

- **Scenario A** — full Plexora on `compute-b-16-200:53491`, browser reaches it
  at `127.0.0.1:53491`.
- **Scenario B** — Plexora on the laptop at `127.0.0.1:8000`, data node on an O2
  compute node reached at `127.0.0.1:59616`.

A cross-check that the two really are equivalent: tile `ch_0/2/0_0` came back as
**4146 bytes from both**. `encode_tile_array` is pure over the array and the
quantization window, which is what makes the primary's verbatim forwarding
correct — and here it doubles as proof the comparison is clean.

## Results

### Round-trip latency

| | measured |
|---|---|
| Scenario A tunnel, keep-alive | **64 ms** |
| Scenario B tunnel, keep-alive | **65 ms** |
| Either tunnel, fresh TCP connection | 115 ms (ssh channel setup ≈ 50 ms) |

The two tunnels are indistinguishable. Every difference below is therefore about
software, not about the network.

### Tiles

Eight tiles at level 2, identical script, fresh channels per scenario so nothing
started warm.

| Path | cold/tile | repeat/tile | 8-way parallel | speedup |
|---|---|---|---|---|
| A — viewer on O2 | 161 ms | **82 ms** | 333 ms | 3.86× |
| B — proxy (browser → laptop → node) | 119 ms | **1.0 ms** | 228 ms | 4.18× |
| B — direct (browser → node) | 107 ms | **100 ms** | 236 ms | 3.61× |

### Per-channel metadata — what first paint blocks on

20 channels × (`get_image_channel_stats` + `get_channel_gmm`) = 40 calls, both
sides warm. This is the sweep `_warm_datasource_caches` performs.

| Path | total | per call |
|---|---|---|
| A — viewer on O2 | **3.10 s** | 76 ms |
| B — local viewer + node | **0.02 s** | 0.5 ms |

**155× in B's favour.** In A every one of those 40 calls crosses the tunnel
because the cache lives on O2. In B they are laptop-local dictionary hits —
`_image_stats_cache` and `_gmm_cache` are on the primary, and the primary is the
laptop.

### Other

- Project first load on A (pyramid open): **3.9 s**.
- The 8-way parallel speedup is ~3.6–4.2× in *all three* paths, so the ceiling is
  not node-specific.

## Why scenario A loses

**Every cache is on the wrong side of the tunnel.** This is the whole story.

`_tile_png_cache` ([data_routes.py:446](../plexora/server/routes/data_routes.py#L446),
1500 entries), `_image_stats_cache`, `_gmm_cache`, `_quantization_store_cache` —
in scenario A all of them live in the O2 process. They still do their job: they
save the zarr read and the encode. What they cannot save is the 64 ms of tunnel
that the answer has to cross anyway. So a cache hit costs 82 ms for a tile and
76 ms for a channel stat.

In scenario B those same caches sit in the laptop process, on the browser's side
of the tunnel. A hit costs **1.0 ms** and **0.5 ms** respectively, because
nothing leaves the machine.

This inverts the intuition that motivated scenario A. Co-locating the viewer
with the data does put the *reader* next to the pixels — but it also puts every
*cache* a whole internet away from the person looking at them. For an
interactive viewer, which is cache-hit-dominated once a project is open, the
second effect is larger.

## The one real weakness in scenario B

Direct routing is treated in the code as the optimization —
[resourceRouting.js](../plexora/client/src/js/services/resourceRouting.js):

> ```
> proxy    browser -> this server -> node.
> direct   browser -> node.  One hop instead of two.
> ```

Measured, direct is the **slowest** of the three on repeat access: 100 ms versus
proxy's 1.0 ms. "One hop instead of two" is a false economy when the second hop
is a cache hit on the near side of the tunnel. Direct mode skips
`_tile_png_cache` entirely, and the node has no tile cache of its own — every
image endpoint on the node caches stats, GMM and the quantization window via
`_cached`, but [`image_tile`](../plexora/server/node/api.py#L646) re-reads and
re-encodes on every request.

**Caveat, stated honestly:** the node sets `Cache-Control: private,
max-age=31536000` with an ETag, so a real browser serves exact repeats from its
own cache and never reaches the network. The 100 ms figure is what a
cache-missing client pays. It bites after a reload, after browser cache
eviction, in a new tab, and on any request whose URL differs — not on every pan.

## Secondary finding: parallelism ceiling

Eight concurrent tile fetches yielded only 3.6–4.2×, in every path. Two
different causes with the same signature:

- **Scenario B** — the node holds `resource.lock`, an exclusive `RLock`, across
  the whole read and compute in every image endpoint
  ([tiles](../plexora/server/node/api.py#L655),
  [stats](../plexora/server/node/api.py#L684), gmm, quantization, overview). Its
  docstring says its job is *"serializes load against read"* — protecting
  against a **reload** swapping the frame underneath a reader. That needs writer
  exclusion, not reader exclusion.
- **Scenario A** — the same shape via what
  [data_routes.py](../plexora/server/routes/data_routes.py#L481) calls "the
  (globally serialized) zarr/tifffile reader".

Worse in B, the warmup thread takes that node lock 40 times during project open
while the browser's tile requests queue behind it.

## Configuration finding

The node was started `--allow-origin http://127.0.0.1:8000`. Verified live:

| Browser origin | `Access-Control-Allow-Origin` | Result |
|---|---|---|
| `http://127.0.0.1:8000` | echoed | direct routing |
| `http://localhost:8000` | **absent** | CORS fails → proxy |

So which spelling you type in the address bar silently decides the routing mode.
Given the numbers above this is currently *backwards*: `localhost` (proxy) is
the faster of the two, because it keeps the laptop-side tile cache in the path.

## Fixes, ranked by payoff over effort

**1. Give the node a tile LRU.** Mirror `_tile_png_cache` inside `image_tile`,
keyed on `resource.generation` — the same immutability argument the primary
already relies on, and generations are already per-resource for exactly this
reason. Closes direct mode's only weakness and helps every client of the node,
not just this one. Roughly 20 lines.

**2. Keep a primary-side cache in front of direct routing, or prefer proxy.**
Measured 1.0 ms versus 100 ms. Either stop treating direct as strictly better,
or give the browser a cache that lives on its own side of the tunnel. The
simplest version is to leave direct routing off for tiles by default and let it
be opted into where the primary genuinely is a bottleneck.

**3. Make the node's image lock a read-write lock.** Many concurrent readers,
exclusive only on reload — which is what the docstring already describes.
Recovers the missing headroom between 3.6× and 8×.

**4. Parallelize `_warm_datasource_caches` for a remote image.**
([data_model.py:838](../plexora/server/models/data_model.py#L838)) It walks 20
channels twice in one thread. Against a node each call is a round trip, so the
sweep serializes ~2.6 s of pure latency that a bounded pool of ~8 would overlap.
Needs fix 3 to pay off fully, since the node's lock currently serializes exactly
what would be parallelized.

**5. Register both origin spellings in `--allow-origin`.** `_browser_origin()`
returns whatever the browser sent, so the routing mode is decided by how the URL
was typed. Accepting both the `127.0.0.1` and `localhost` forms makes the
behaviour deterministic instead of incidental.

**6. Later, if still needed: a batch tile endpoint.** At 65 ms RTT a viewport of
40 tiles is latency-bound however fast the backend gets. One request returning N
tiles would amortize it — but it is invasive, and 1–5 should be measured first.

## The zoom-in symptom, measured directly

The reported symptom is specific: *zooming into a region takes a while to become
crisp, and the navbar loading indicator confirms tiles are still arriving.* That
is a **viewport fill**, which is a different workload from the per-tile timings
above: OpenSeadragon creates one layer per active channel
([imageViewer.js:337](../plexora/client/src/js/views/imageViewer.js#L337) sets
`imageLoaderLimit: 10`), so one zoom issues *tiles-covering-FOV × active
channels* requests, which the browser then serves ~6 at a time per origin.

Simulated as 4 successive zooms to distinct dense-tissue regions, 4×3 tiles ×
3 channels = 36 tiles per FOV, level 0, 6-way concurrency. Identical FOVs,
identical channels, run in both scenarios:

| Path | per zoom | median tile | p95 tile |
|---|---|---|---|
| **A — viewer on O2** | **1.33 s** | 194 ms | 321 ms |
| B-direct | **0.72 s** | 86 ms | 265 ms |
| B-proxy | **0.60 s** | 85 ms | 169 ms |

Per-FOV byte totals came back identical in both scenarios (0.52 / 0.49 / 0.47 /
0.44 MB), so this is the same work moving the same bytes — scenario A is simply
**~2× slower per zoom**, consistent with the per-tile figures further up.

**Steady-state zooming is fast in B.** Sub-second to fill a viewport is not the
symptom being described.

### What actually produces the slow zoom

One measurement in the same session took **7.26 s for 12 tiles on a single
channel** — 10× the steady-state rate. SLURM accounting explains it:

```
51680246  FAILED   18:43:55 → 19:33:17  compute-b-16-177   <- data node dies
51685068  RUNNING  19:34:44 →  …        compute-b-16-177   <- data node restarts
```

The slow measurement landed immediately after that restart, against a **freshly
started node with nothing open**. The first tile request has to run
`_opened(resource)` — opening a 31 GB OME-TIFF and building its page index —
and it does so while holding `resource.lock`, so all six concurrent requests
queue behind one open.

Controlling for it, the pyramid level itself is *not* the driver. Same region,
one channel, 12 tiles, 6-way, warm pyramid:

| level | ms/tile |
|---|---|
| 3 | 32 |
| 2 | 26 |
| 1 | 28 |
| 0 | 54 |

Level 0 is about twice level 2 — real, but nothing like the 10× the symptom
describes. Tissue density matters more than level: empty background returns ~7 KB
per tile, dense tissue 40–80 KB.

### The likely root cause: the node keeps restarting

The profile requests `-t 1:00:00`. A data node therefore has a **one-hour life**,
after which the job ends and the node has to come back — observed live during
this session, with the node re-registering on a new port 88 seconds later
(`nodes.json` endpoint moved from `:59616` to `:50710`).

SLURM's own accounting confirms it independently, twice in one afternoon:

```
51669677  TIMEOUT  16:41:47 → 17:41:49   01:00:02
51685068  TIMEOUT  19:34:44 → 20:34:57   01:00:13
```

Two clean one-hour TIMEOUTs. Every such line is a point at which the next zoom
was going to be slow.

Reconnection is transparent by design — same-name re-registration absorbs the new
port and token, and no binding changes — so nothing *breaks*. But every restart
throws away the node's entire warm state:

- the opened pyramid (a multi-second reopen on first touch, under the lock),
- every `_cached` quantization window, which costs one **full-resolution read per
  channel** (~218 MB/plane) to rebuild,
- all stats and GMM packets.

So the pattern is: work fine for an hour, then hit a zoom that takes many seconds
while the pyramid reopens and the touched channels re-read their quantization
windows at full resolution — then it is fast again. That matches the reported
symptom far better than anything about the topology.

**The A/B contrast on cold start makes the mechanism explicit.** The same
measurement — first touch of pyramid level 0, 12 tiles, one channel — came back:

| | first touch of level 0 |
|---|---|
| A, after its load-time warmup ran | **0.50 s** |
| B, on a node that had just restarted | **7.26 s** |

That is a 15× gap on identical work, and it is not about where the process runs.
A calls `_warm_datasource_caches` when it loads a project, which opens the
pyramid and computes every channel's quantization window before the user touches
anything. **Nothing does that for a node that reconnects underneath an already
open project** — so in B the same work happens lazily, inside the first zoom the
user performs, under `resource.lock`.

So scenario A does not avoid this cost by being architecturally better; it avoids
it by being one long-lived process that warms itself once. A local project avoids
it too. That is very likely the comparison that produced the impression that B is
slow — and it is a missing re-warm, not a topology problem.

### Fixes for this specifically

**0a. Raise the node's time limit.** `-t 1:00:00` → `-t 8:00:00` (or whatever the
`interactive` partition allows) in the O2 profile's `srun` field. Costs nothing
and removes the hourly cliff entirely. Still worth doing, but note that it makes
the cliff *rarer*, not gone — a preemption, a dropped tunnel or a closed lid
lands in exactly the same place, which is why the fixes below were done instead
of relying on it.

## Implemented

Three changes, all node-side. Covered by `tests/test_node_warm_and_cache.py`.

**A node warms itself when it starts serving something.**
`app.warm_resources` opens each image and reads every channel's quantization
window on a background thread, started after the announce line so the parent is
never kept waiting. The same walk runs when an image is shared dynamically. The
mixture fits are deliberately excluded: they only refine contrast, cost about a
second each, and the primary keeps its own copies across a node restart, so
fitting them here would be twenty seconds of work for an answer nobody asks the
node for.

This is the gap the 0.50 s / 7.26 s pair above exposed. The primary has done
this since long before nodes existed; nothing was doing it on the far side.

**The node caches encoded tiles.** `_cached_tile` in `server/node/api.py`, a
bounded LRU keyed by resource, generation, channel, level, tile, quality and
tile grid — mirroring the primary's `_tile_png_cache`. Direct routing sends the
browser straight to the node, so the primary's cache is not in the path at all;
there was previously nothing on this side to take its place, which is why direct
was the *slowest* option on repeat access.

**The resource lock lets readers share.** `resources.RWLock`, writer-preferring,
with `with resource.lock:` still meaning "writer" so every existing call site
keeps the exclusivity it was written against. Only the image read paths were
moved to `lock.read`.

This does **not** make tile reads parallel and is not meant to: the pixels come
through a zarr view over a tifffile store, which takes its own per-file lock, so
readers still queue there for the I/O. What it removes is queueing for work that
is not I/O — a channel's GaussianMixture fit is a second of CPU over an
already-materialized overview, and it used to hold off every tile of every
channel for that second. During the per-channel warm that was twenty such
seconds, landing exactly when a user had opened a project and started to zoom.

Two things worth recording because they were nearly wrong:

- `_cached` cannot take `resource.lock` to hand out its per-key single-flight
  lock. It runs inside `_reading`, which holds the read lock, and asking a
  readers-writer lock to upgrade deadlocks against itself. It uses an
  independent module-level lock, held only for a dict lookup.
- The tile cache key has to be built *after* the pyramid is open, because
  opening is what sets the first generation. Built before, every tile is filed
  under generation 0 and never found again — a cache that stores everything and
  returns nothing, indistinguishable from having no cache at all. That one was
  caught by its own test rather than by reading the code.

## What this does *not* explain

- **A comparison against a fully local project** — image on the laptop's own
  disk, no tunnel at all — is genuinely faster than both scenarios here, and no
  amount of tuning will close that gap.

## Reproducing

Scripts used are in the session scratchpad (`three_way.py`, `perchannel2.py`,
`probe*.py`). The method that matters: use fresh channels per scenario so nothing
starts warm, pre-touch each channel once so the quantization window is cached at
both ends, then time cold / repeat / 8-way-parallel separately. Timing only cold
tiles hides the effect that dominates real use.
