# Remote-access benchmark: data node vs SFTP streaming

Measures the two ways Plexora views an image that lives on a cluster, from
the same laptop, on the same file:

- **N, data node (direct access).** `plexora node serve` runs in a SLURM job
  beside the file and ships encoded tiles through an ssh tunnel; the browser
  fetches them from the node directly.
- **S, SFTP streaming (files only).** Plexora on the laptop reads the file's
  bytes over one `ssh -s sftp` channel to a transfer node, through its on-disk
  chunk cache, and encodes tiles locally.

## Files

| File | What it does |
|---|---|
| `campaign.py` | The runner: `c0` (first-ever open), `blocks` (paired cold + warm tours), `probes` (RTT, ssh set-up, raw SFTP throughput, CPU calibration on both hosts). |
| `o2.py` | The cluster side: the node in the job, its tunnel, page-cache drop for the image only (`posix_fadvise` + `fincore`), `/proc/<pid>/io`. |
| `tour.mjs` | One block's browser side: a fresh Chrome, the fixed tour, once per pass (C1 cold, C2 warm). |
| `tourpage.js` | The completion test ("full"): every visible channel fully loaded, drawing the finest level OpenSeadragon wants for the view, as an HD source, for two polls after two redraws. |
| `survey.mjs`, `waypoints.py` | A picture of the image, and deterministic waypoint sets on tissue from it. |
| `calibrate.py` | Tile-encode and chunk-read timings, run identically on both hosts. |
| `analyze.py` | Tables, statistics and figures from the raw rows. |

It reuses `../bench3d` (`drive.mjs` Browser and network log, `pagekit.js`,
`probes.js`, `serve.py`).

## Running

The configuration is a JSON file kept beside the results (it names cluster
paths and a node token, so it never lives in the repository):

```json
{
 "results_dir": "<results>", "bench_dir": "~/plexora-bench/remote.noindex",
 "primary_port": 8621, "node_name": "bench-o2",
 "sftp_remote": "<saved files-only server>", "sftp_target": "user@transfer-host",
 "projects": {"N": "tls13-node", "S": "tls13-sftp"},
 "waypoints": "<results>/waypoints.json", "z0s": [96, 160], "seed": 20261026,
 "cluster": {"login": "user@login-host", "job": 123, "python_env": "<conda env>",
             "checkout": "<plexora checkout>", "node_data_root": "<bench-only dir>",
             "file": "<image>", "cpus": 10, "token": "<hex>",
             "node_port": 24621, "tunnel_port": 24721},
 "tour": {"passes": ["C1", "C2"], "z0": 128, "channels": [0, 2, 4],
          "timeout": 120000, "volume": true, "volumeTimeout": 180000}
}
```

`bench_dir/data/remotes.json` holds the files-only server's saved profile.
End the bench directory's name in `.noindex` so Spotlight does not index the
chunk cache while a run writes it. Then:

```sh
python campaign.py --config cfg.json c0 --arm N --reps 1-3
python campaign.py --config cfg.json c0 --arm S --reps 1-2   # rep 1 leaves the S C1 snapshot
node survey.mjs --base http://127.0.0.1:8621 --dataset tls13-node-c0r1 --out survey.json
python waypoints.py survey.json --sets 6 --out <results>/waypoints.json
python campaign.py --config cfg.json probes
python campaign.py --config cfg.json blocks --blocks 0-7
python analyze.py <results> --timeout-s 120
```

Do not run pytest, a build or anything heavy on the laptop during a run; each
row records the laptop's load and whether a test run was seen.

## What each state means

| State | N | S |
|---|---|---|
| C0 | node derived files deleted, page cache dropped, new node and primary, new project | chunk cache deleted, new primary, new project |
| C1 | page cache dropped, new node and primary; `.ims_index` and quantization kept | new primary; chunk cache restored to what C0's import left (metadata) |
| C2 | same processes and Chrome, straight after C1 | same |

Not controlled, reported: the NFS server's cache (both arms), the transfer
node's page cache (S), other users' load on the shared compute node, the
home link's variability.
