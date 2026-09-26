# Plexora Project Guide

Working guide for the `plexora` repository: what it is, how the pieces fit,
where the sharp edges are, and how to validate a change. Written for a coding
agent arriving cold.

## What Plexora Is

A viewer and analysis tool for large multiplexed microscopy images (OME-TIFF
whole-slide data, tens of thousands of pixels square, many fluorescence
channels). Python Flask backend served by Waitress; OpenSeadragon frontend with
a WebGL2 colorize pass. Runs as a local desktop web app, inside a Jupyter
notebook, or behind `jupyter-server-proxy`.

The user picks a subset of channels, assigns each a colour and contrast range,
and pans/zooms an additively-blended composite. Optional layers: a segmentation
mask with cell outlines, cell centroids, and marker-threshold gating.

Entry points:

- Desktop: `plexora` (`plexora/cli.py`), or `python -m plexora`. Starts
  Waitress, picks a free port if 8000 is taken, opens a browser when the
  environment looks interactive.
- Desktop APP: `plexora --desktop`, run inside the Tauri v2 shell under
  `desktop/`, never typed by a person. Loopback only, port 8420 preferred
  (else ephemeral), a per-launch token, and exactly one JSON line on stdout
  once the socket is bound (`cli.ready_line`, protocol `DESKTOP_PROTOCOL`) --
  fd 1 is otherwise redirected to stderr, so nothing else can land in the
  channel the shell parses. Exits when stdin reaches EOF (`plexora._lifetime`).
  Shares the platform-default data directory with the CLI and notebooks (see
  `paths.py` below — the old frozen-build data-beside-the-executable rule is
  gone). Built and released with `python scripts/release.py`.
- Housekeeping subcommands: `plexora where`, `plexora config show|set`.
  Dispatched on `argv[0]` by `cli.split_command`, NOT by an argparse
  subparsers action — see Key Invariants.
- Remote over SSH: `plexora --remote` on the server prints the tunnel command
  (scheduler-aware: two-hop `ssh -J` inside a SLURM/PBS/LSF job);
  `plexora connect user@host [--srun "…"]` runs locally and automates both
  ends (`plexora/connect.py`). Every connection also starts a data node on
  THIS (the user's own) machine by default -- `--no-local-node` opts out --
  because that is what lets a data-selection field's "Local" option mean the
  laptop the browser is running on. `plexora node serve --dynamic` lets the
  viewer add/remove resources on a node at runtime instead of only serving
  what `--serve` named at startup; `--manifest PATH` records what it ends up
  serving so it comes back the same way next session (a dynamic node with a
  `--node-id` and no `--manifest` defaults to `<data_root>/node-manifests/<id>.json`
  **on its own machine**, since an ssh command line cannot name a data root it
  has never seen).
- The mirror layout, and the one a data field's Remote option opens:
  `connect.NodeSession` keeps Plexora *here* -- with the browser, the project
  and the database -- and starts only a `--dynamic` node on the far side,
  forwarded with one `ssh -L` and registered into this machine's own
  `nodes.json`. No `srun`: it wants the filesystem, not an allocation.
- Notebook: `plexora.view("name")` → `plexora/jupyter.py`. `proxy="auto"` by
  default; `plexora/notebook_env.py` decides between a direct localhost URL, an
  Open OnDemand `/rnode/` mount, a jupyter-server-proxy path, and a Colab
  origin.
- Open OnDemand: `plexora --ood` from a session terminal — binds 0.0.0.0,
  mounts under `/rnode/<node>/<port>`, prints a token-bearing portal URL.
- CLI sidecar: `plexora-server` (`plexora/server_cli.py`) — what the notebook
  and the proxy entry point spawn, not something a user runs.
- Legacy/local desktop: `python run.py`. Still what the Docker image runs.
- Frontend build: `cd plexora/client && npm run build` — NOT `npm run start`
  or a bare `npx webpack`, both of which build in webpack's default
  `development` mode; see Sharp Edges for what that costs.
- Optional extras: `pip install 'plexora[wsi]'` adds OpenSlide, which only
  `.mrxs` needs — `.svs`/`.ndpi`/`.scn` are TIFFs underneath and tifffile reads
  them without it — and `wsidicom`/`pydicom`, which `dicom_wsi.py` needs for
  any DICOM whole-slide image; one extra install line covers both.
  `[jupyter]` for the notebook sidecar, `[dev]` for pytest. `[remote]` adds
  `gcsfs`/`adlfs` for opening an image, mask or table at a `gs://`/`az://`
  address — `https://` and `s3://` need nothing beyond core (`fsspec`,
  `aiohttp`, `s3fs`, declared there rather than leaned on transitively, so
  the zero-configuration case — IDR, a public bucket — never depends on what
  some other package happens to pull in).

## Repository Map

**Top level**

| Path | Purpose |
|---|---|
| `run.py` | Legacy/local desktop entry point. Keep working. |
| `plexora/server_cli.py` | Notebook sidecar CLI (`plexora-server`). Waitress, `threads=8`. |
| `plexora/__init__.py` | Flask app factory; base URL, notebook flag, plugin installation, the `PLEXORA_AUTH_TOKEN` guard (`AUTH_COOKIE`), and the app-wide `ResourceUnavailable` handler (503 + `_say_unavailable_once`). Holds **no** path constants -- see `plexora/paths.py`. `view()` splits `_DATA_ARGUMENTS` (`image=`/`adata=`/`table=`/`sdata=`) from the viewer-launch arguments and dispatches to `PlexoraViewer.from_memory` when one is given, or `from_anndata(to_disk=True)` for a bare `adata=` (the escape hatch back to disk). A lazy module `__getattr__` re-exports the rest of the public API (`_PUBLIC_API`) -- lazy because an eager import of `plexora.nodes` would pull `anndata` into a core build and break `tests/test_plugin_boundary.py`. |
| `plexora/memory.py` | **Kernel-as-node**: serves a notebook kernel's own in-memory objects to the sidecar over the EXISTING node API, no disk write. `KernelNode` runs a node app on a waitress daemon thread inside the kernel process, loopback only, port 0, its own token, `NODE_THREADS = 4`, and calls `data_model.prime_hot_code()` before answering (same deadlock rule as every other node -- see `server/node/app.py`). Registered in `nodes.json` as `NODE_NAME = "notebook-kernel"` with `role="kernel"`. `snapshot_anndata`/`snapshot_frame`/`snapshot_image`/`snapshot_mask` copy just enough to serve; `OBSM_WIDTH_LIMIT = 32` means a wide `obsm` array is not copied unless named. `register_memory_datasource()`, `serves_memory()`, `_decompose_spatialdata()`, and the `MemoryDataError` a bad in-memory object raises. |
| `plexora/paths.py` | The one resolver for every path. `data_root()` (`PLEXORA_DATA_PATH` -> `data_dir` in settings -> `PLEXORA_DATA_PATH_DEFAULT` -> `RULE_PLATFORM_DEFAULT`/platformdirs -- there used to be a `sys.frozen` step ahead of that, for a portable build that kept its data beside the executable; the desktop app ships a real interpreter instead of a frozen one, so it takes the same platform default the CLI and notebooks do, and that rule is gone), `shared_roots()`, `roots()`, `config_path()`, `project_dir()` (read side), `project_state_dir()` (write side, always the user's root), `derived_root()`, `figures_root()`, `captures_root()` (the figure-builder capture bin's `.captures` directory, user root only -- never shared, resolved on every call like the rest of this file). `PLEXORA_DATA_PATH_DEFAULT` is a *suggestion*, not an override -- it sits below the settings file rather than above it, because it is what `plexora connect` sends from a saved profile's `data_dir`, and a laptop's opinion about a directory on another machine must not outrank that account's own recorded answer. `_reconcile_suggestion()`, run in `_prepare_data_root()` after the write probe: an unclaimed account adopts the suggestion and it is written into the settings file (the module's only settings write); a claimed account whose suggestion agrees stays silent; a claimed account whose suggestion names a directory with no `config.json` keeps its own answer; a claimed account whose suggestion names a directory that DOES hold a `config.json` raises `DataRootError` (message fixed by `CONFLICT_MARKER`, matched by `connect.py`) rather than guess between two directories that both hold work -- this is the incident the whole channel exists to prevent: a profile's `data_dir` sent as an override once beat the account's own setting, so a dataset created over ssh landed in one directory while the viewer read another, invisible with no error anywhere. `data_root_notices()` reports what was decided (adopted, ignored, shadowed by an explicit `PLEXORA_DATA_PATH`, or silently created because a named directory did not exist); `describe()` (`plexora where`) catches `DataRootError` and lists every other configured root and its project count via `_also_configured()`, so the diagnostic itself does not traceback on the very condition it exists to explain. Leaf module: imports nothing from `plexora` -- `_registry_size()` re-implements the project count rather than call `project.read_config` for that reason. **Never snapshot these into a module constant** -- that is exactly what was removed, and it is what made `--data-dir` unreachable after the first `import plexora`. `remote_cache_root()` (`data_root() / REMOTE_CACHE_DIRNAME`, user root only, never shared -- what a person has looked at is theirs) and `remote_cache_budget()` (env `PLEXORA_REMOTE_CACHE_BYTES` -> settings key `remote_cache_bytes` -> `REMOTE_CACHE_DEFAULT_BYTES`, 10 GB; not cached, same reason as `mask_output_preference`, so a Settings save or `plexora config set remote-cache-gb` reaches a running server) back the chunk cache in `server/utils/remote_store.py`. |
| `plexora/cli.py` | The `plexora` command: serve, `where`, `config`, `connect`, `node`, `--remote`, `--ood` (`ood_mount`, `ood_instructions`). Also the **environment detection** a bare `plexora` runs: `should_detect` (gate), `detect_environment` (lazy, never raises), `apply_detection` (verdict -> flags), `detected_base_url`, `hub_instructions`, `colab_instructions`, `--no-detect`. And `connect_kwargs` (flags beat a saved profile; `remote_os` has no flag at all and is read off a saved workstation's `extra` only, because it is a fact about the machine, not a preference), `node_serve_argv`/`_start_side_node` (`--also-serve`); `_save_remote` carries `remote_os` into `extra["workstation"]` so `--save` under a new name does not produce a copy that has forgotten which machine it talks to; `plexora node serve --exit-on-stdin-close` is the CLI face of the Windows-remote lifetime tie (see `connect.py`/`server/node/app.py`). `--data-dir-default` (bare-serve only, exported as `PLEXORA_DATA_PATH_DEFAULT`) is what `connect.remote_command_line` sends for a profile's `data_dir`; `--data-dir` is unchanged, still an outright override exported as `PLEXORA_DATA_PATH`. `main()` resolves `paths.first_run_notice()`/`paths.data_root_notices()` before serving and prints them ahead of the URL, catching `paths.DataRootError` and exiting 2 rather than letting a route hit it first. `--node NAME` is on `dataset create`, `project create` and `project set` (in `_PROJECT_OPTIONS`, so it is one more entry in the same vocabulary `PROJECT_SPEC_KEYS` holds) and reaches `create_dataset(node=)`, a spec's `node` key, and `import_sample(node=)` on the detection branch. `_run_project` create calls its positional list `given`: it used to be `paths`, which rebound the module imported at the top of the function, so the `except paths.DataRootError` below it raised an AttributeError instead of printing the refusal. `_say_what_is_converting` prints one line per node still preparing a resource, one per resource whose conversion has already FAILED (`datasets.failed_conversions`, the node's own error, with a note that reopening the project retries it) and one per `datasets.conversion_warnings` (e.g. a read-only mask folder), human output only. `_run_dataset`/`_run_project` print `in <data root>` on every success path (`_said_where`) and the pending notices ahead of a mutating command, so a create that landed somewhere the viewer will not look is visible on the same screen as the success message; a `--json` dict payload carries the root as a `dataRoot` key instead (`_print_json`), a list payload is left alone. Both catch `paths.DataRootError` ahead of the narrower exceptions and exit 2. `_run_config` warns when a `PLEXORA_DATA_PATH` already in the shell will shadow the `data-dir` it just wrote. **Imports nothing from the `plexora` package at module level** -- see Key Invariants. Keeps its own copies of `REMOTE_ENV_VARS`, `PORT_PLACEHOLDER` and `DEFAULT_REMOTE_COMMAND`, pinned against the originals by `tests/test_cli.py`. |
| `plexora/connect.py` | Local side of `plexora connect`: builds ssh argv, runs one process (direct) or two (`--srun`: job + tunnel), health-polls through the tunnel. `Session` holds one connection -- `establish()` is separate from `wait()` so the app can own a connection a request does not block on. `_Watched` takes a dict of `matchers` (a viewer that starts a node announces twice on one pipe). `_Watched._pump` runs every line the far side sends through `strip_ansi` (`ANSI_RE`) before it is stored, matched or echoed: `-t` gives the remote a pty, so pip paints its ERROR red and a login banner bolds itself, and nothing downstream is a terminal -- the log pane and the failure notice in the browser rendered the escapes as the literal text `[31mERROR:`. Stripped at the pump because that is the single point every remote line passes through, so the matchers, `remote_sessions._diagnose`'s marker search, the log and a quoted failure tail cannot disagree about what was said. `_ssh_options` prepends `KEEPALIVE_OPTIONS` (`ServerAliveInterval=30`, `ServerAliveCountMax=3`, deduped when the caller already set the interval) to every ssh invocation, so a dead tunnel becomes an exit somebody can see instead of a hang. `_wait_for_health` takes `any_answer=True`, used ONLY at the viewer call site: an HTTPError with `code < 500` counts as proof of life, because a token-guarded remote viewer answering 403 through the tunnel is a viewer that is up. Node polls keep the strict reading, where a 403 means a wrong token. `remote_command_line()` sends a profile's `data_dir` as `--data-dir-default`, not `--data-dir` -- a saved profile is this machine's opinion about a directory on another machine, and as an override it once beat that account's own recorded setting, so a viewer it launched read a different directory than `plexora dataset create` wrote to over the same ssh. `data_root_conflict(lines)` recognises the remote's two-data-directories refusal (matched on `paths.CONFLICT_MARKER`, imported rather than copied so the two cannot drift) and is checked in `_wait_for_health` and `_wait_for_announce` BEFORE the `_Retriable` reading, because that refusal repeats on every retry and would otherwise cost another login or queue wait to see the same message again. Also `reverse_forwards` (`-R`), `parse_node_announce`, `register_node_through` (POST to the far viewer's `/settings/nodes`), `connect_node` (viewer here, data there). **Installing Plexora on the far side** when a profile asks (`install=True`) rides the launch's OWN ssh, chained ahead of it: `install_prefixed()` builds `pip … && echo PLEXORA_INSTALL_DONE && <launch>` -- one command because it is one login, and at a Duo site one buzz of the phone instead of two (it used to be a separate ssh; that was the second buzz). `&&` is the failure story: a failed pip short-circuits the chain and nothing launches from the half-upgraded environment. `_begin_install()` announces and phases; `_await_install()` blocks on the `installed` MATCHER -- keyed on `watched.found`, NOT the event alone, because `_pump` sets every event at EOF to unblock waiters, so a set event only proves the process stopped talking. `install_command_line()` is the one rule: *the environment is whatever gets you to the program, and the program is the last word*, so `conda run -n img plexora` becomes `conda run --no-capture-output -n img pip install --progress-bar off --upgrade plexora` and an env prefix becomes its own `bin/pip` -- which is why no separate conda field exists anywhere. `conda activate` is never used: a non-interactive ssh has sourced no rc file. Its own `INSTALL_TIMEOUT`, and the connection's deadline is taken AFTER the marker, so an install spends none of the node's answer-time budget. That budget is `DEFAULT_SRUN_TIMEOUT` (**18000s -- five hours**) whenever a profile says `srun`, because what it measures is a scheduler QUEUE and not a start-up, and expiring it cancels the allocation being waited for; `_wait_for_node` reports progress on a doubling interval (`QUEUE_NOTE_SECONDS` -> `QUEUE_NOTE_MAX_SECONDS`) and quotes the scheduler's own last line, so a queue reads as a queue rather than a hang -- backed off rather than fixed because `remote_sessions.LOG_LINES` keeps only 200 lines and a note a minute would flush the very output that explains the wait. Under a scheduler the chain puts pip BEFORE `srun`, so it still runs on the login node: shared filesystem, and the allocation is not there to be spent on pip. Stdlib only, same import rule as `cli.py`. For a Google Cloud profile, `gcloud_ssh_argv`/`gcloud_node_ssh_argv` are drop-in replacements for `direct_ssh_argv`/its node twin -- `gcloud compute ssh VM --tunnel-through-iap --command "<chain>" -- <ssh flags>`, one supervised process, because `--tunnel-through-iap` already carries an ordinary ssh (forwards, `-t`, keepalives) over Google's Identity-Aware Proxy, so the watcher, matchers, askpass relay and teardown downstream cannot tell which builder produced their argv. A second chained step, the MOUNT, is modelled on the install step the same way: `MOUNT_DONE_MARK`/`MOUNT_READONLY_MARK`/`MOUNT_TIMEOUT` (900s), `parse_mount_done`/`parse_mount_readonly`, `mount_prefixed`, `_begin_mount`/`_await_mount`/`_mount_failure`. `Session`/`NodeSession` take `gcloud=`/`mount_command=`/`mount_readonly`; the chain on the far side is `mount && MARK && pip && MARK && launch`. **Which operating system is on the far side** rides through every builder as `remote_os=None` (`normalize_remote_command`, `_pip_beside`, `install_command_line`, `install_prefixed`, `remote_command_line`, `node_command_line`), living in this file rather than a sibling module because it is loaded standalone off disk (see Key Invariants) and the quoting has to run inside the builders it serves. Only `"windows"` changes anything -- macOS agrees with every POSIX rule here, so a workstation profile records which of the three it is for what to SAY (a recipe note, the OS-mismatch warning) and the builders never branch on it. Windows specifics, all in the "remote operating systems" section: an environment prefix resolves to `Scripts\plexora.exe` (`WIN_ENV_PREFIX_BIN`) rather than `bin/plexora`; `_pip_beside` swaps the stem and keeps the suffix (`Scripts\pip.exe`), since `.exe` is the normal shape of an entry point there, not the wrapper-script mark a dot is on POSIX; there is no unbuffering `env` prefix, because `env` is not a program on Windows and the node has flushed its own announce since before a Windows remote could exist; `install_prefixed` wraps the install chain in `cmd /c "…"` because PowerShell 5.1 -- still what Windows ships and what a site may set as OpenSSH's DefaultShell -- treats `&&` as a parse error, skipped when the chain already holds a double quote, since `cmd /c` would strip the outer pair and re-split what was working. `direct_ssh_argv(..., tty=False)` drops `-t` for Windows: Windows sshd answers `-t` with a ConPTY, a terminal emulator that hard-wraps output at the console width, and the node's announce carries a 32-hex token well past 80 columns, so a pty means a working connection whose announce `NODE_ANNOUNCE_RE` can never match. The teardown a pty gives for free (SIGHUP on disconnect) is replaced by `node_command_line(..., exit_on_stdin_close=True)` plus `_Watched(hold_stdin=True)`: the node watches its own stdin for EOF, and the local side holds ssh's stdin open on a pipe it controls, because ssh forwards ITS stdin to the far side and an inherited one already at EOF (Plexora as a service, `< /dev/null`) would tell the node the connection was over a second after it started. `_Watched.stop()` closes that pipe *before* terminating ssh, or the channel is gone before the close can cross it. `NODE_PLATFORM_RE` + `parse_node_announce` add an optional `platform`, read the same separate-regex way as `hostname` so an older node still parses; `NodeSession._check_platform` compares it against the profile's `remote_os` and records `os_mismatch` -- echoed, never applied, and never fatal, because the launch that revealed the mismatch already succeeded. `NodeSession.establish` refuses `srun` together with a Windows `remote_os` up front (`ConnectError`, diagnosed) rather than let the attempt fail minutes later on `srun` not being a program over there. |
| `plexora/gcloud.py` | Google Cloud, standalone-loadable and stdlib-only beside `connect.py` (same import rule, same reason). Everything goes through the `gcloud` CLI behind one monkeypatchable seam, `_RUNNER` -- no google-cloud-* dependency, no service-account key, no credential Plexora ever sees. Queries (`account`, `projects`, `buckets`, `bucket`, `zones`, `instances()` for the bring-your-own picker, `zone_of_instance(project, name)` for finding a named VM's zone across a whole project); the reuse ladder `ensure_instance()` -- reuse a RUNNING VM, start a TERMINATED one, create one that does not exist, then `ssh_probe` until IAP SSH answers, in that order because each step costs wildly different amounts of somebody's time and money -- now returns `"created"`/`"started"`/`"reused"` rather than a bool, because a failed connection's teardown only stops what THIS attempt brought up; `create_instance`/`start_instance`/`stop_instance` (both take `block=`, using `--async` when False so the caller's HTTP request is never held open while Compute Engine works)/`delete_instance`; `ensure_iap_firewall` + `ensure_public_deny` (the pair that make "nothing but the tunnel reaches this VM" true whether or not it has an address), `network_egress`/`wants_external_ip`/`repair_egress` (the VM needs a route OUT to install anything -- see the invariant); `region_for_bucket_location`; curated `MACHINE_TYPES`/`REGIONS` catalogues (a live `machine-types list` returns hundreds of rows per zone -- nobody can choose from that); `prepare_command_line()` (the gcsfuse-mount-plus-venv chain run on the VM); `profile()` (the `extra["gcloud"]` schema, v4). `provisioning_models()`/`DEFAULT_PROVISIONING` -- a new VM is asked for as **Spot** by default (`--provisioning-model=SPOT --instance-termination-action=STOP`), which is defensible only because STOP keeps the disk: the data is in the bucket, so being preempted costs a reconnect rather than a rebuild. `exit_actions()`/`exit_action(record)` -- the one reading of "what happens to the machine when the session ends", `leave`/`stop`/`delete`, with a v3 `stop_vm_on_disconnect` boolean read as the two-valued version of it. `bucket()` falls back to `gcloud storage objects list --limit=1` when `buckets describe` is refused, because a world-readable bucket grants OBJECTS and not metadata -- so somebody else's published atlas can be named on the form, marked `public` with no location to fill the region in from. **Who owns the machine decides what may be done to it**: `vm_source` is `"plexora"` (rented -- may be created, stopped, deleted) or `"existing"` (a VM the user already runs -- never created, never auto-stopped, never deleted); `profile()` itself forces `on_exit` off Delete (and `idle_shutdown_minutes` to 0, and `external_ip` off) for `"existing"`, so a hand-edited or imported profile cannot remove, time out or re-network somebody else's machine -- though it may still be asked to stop one, which is a person answering a question about their own server. `made_by_plexora()`/`can_reach_storage()` read the instance's OWN description (a label, a scope list) rather than trust the saved record, and `delete_instance()` refuses unless the `created-by=plexora` label is on the machine -- the one Plexora verb that is destructive checks the thing being deleted, not the thing asking. `startup_script()` installs a systemd timer (`plexora-idle-shutdown.timer`) on first boot of a RENTED VM only, so a machine survives even if the laptop that started it dies -- the only billing safeguard that does not depend on a Plexora process still running. **Has no storage-deletion verb, and must never gain one** -- `delete_instance`'s argv cannot mention the bucket at all, which is what makes "deleting the VM never deletes the data" structural rather than a promise. |
| `plexora/askpass.py` | The SSH_ASKPASS helper: posts ssh's prompt back to the local Plexora over loopback (one-time nonce, plus `asking_process()` so the server can tell a second hop from a second attempt), polls for the answer, prints it on stdout. Run as a bare script by a generated wrapper, **never** `python -m plexora.askpass` -- that would build a Flask app to answer a password prompt. Stdlib only. |
| `plexora/_url.py` | The three meanings of "base URL": `clean_prefix` (no trailing slash), `prefix_with_slash`, `join_display` (accepts a full origin). Leaf module. |
| `plexora/_lifetime.py` | Leaf module, stdlib only: what ends this process cleanly. `ensure_std_streams()`, `flush_std()`; `watch_stdin(on_eof)` (a daemon thread that calls `on_eof` once stdin reaches EOF -- the shape `--desktop` and the data node's `--exit-on-stdin-close` both build on); `request_shutdown(reason, grace=...)` raises `KeyboardInterrupt` on the MAIN thread, because that is waitress's own clean stop (`serve()` returns, every `atexit` handler runs), with a 10s `os._exit` backstop if that wedges; `shutdown_requested()`, `install_signal_handlers()` (SIGTERM the same way), and `exit_when_stdin_closes(log=print)` -- the data node's old `os._exit(0)`-on-EOF behaviour, moved here so the desktop app's server can tie its life to stdin the same way without duplicating the watcher. |
| `plexora/_subprocess.py` | Leaf module, stdlib only, importable without the package for the same reason `cli.py`/`connect.py` are (see Key Invariants): `popen_kwargs()` returns `{"creationflags": CREATE_NO_WINDOW}` only for a console-less Windows process -- the desktop shell launches its server with `CREATE_NO_WINDOW`, and without this every child THAT process starts (an `nvidia-smi` probe, an ssh, a tkinter dialog) would flash its own console. `{}` everywhere else, including a Windows terminal launch, where a child sharing the console is correct (ssh prompting for a password needs it). Every `Popen`/`run` in the package passes `popen_kwargs()`; `cli.py`, `connect.py` and `gcloud.py` reach it through a lazy `_spawn_flags()` helper rather than importing it at module level. `tests/test_subprocess_flags.py` statically scans the tree for a spawn call that forgot it. |
| `plexora/notebook_env.py` | Which URL a notebook viewer should use, and what to bind. `resolve_display()` returns a `Resolved(server_base, display, bind_host, kind)`; ladder: explicit base_url -> `proxy=False` -> Colab -> Open OnDemand (`OOD_NODE_RE` matches the discovered prefix) -> jupyter prefix + remote evidence -> direct localhost. `verify_proxy_route()` asks the notebook SERVER whether it really proxies a port. |
| `plexora/jupyter.py`, `plexora/proxy.py` | Notebook display API, subprocess lifecycle, proxy entry point. `_start_server` returns `(port, base_url, token)`; the sidecar cache is keyed on bind host too. `PlexoraViewer.__init__` takes `tool=`/`overlay=`/`channels=`/`memory=` -- an ephemeral launch state carried in the entry URL and never persisted, built by `_launch_state()` and encoded by `_entry_query()` (`urlencode`, replacing the old `f"{url}?token=..."`, which was only ever correct for exactly one query parameter); the Colab iframe fallback shares `_entry_query()` too. Module-level `_launch_channels()` validates the `channels=` argument kernel-side before it ever reaches the server. `PlexoraViewer.from_memory()` is the kernel-as-node entry point (see `plexora/memory.py`); `refresh()`/`_reload_server()` POST `/reload_datasource` on the sidecar -- deliberately NOT `nodes._reload`, because a memory-served project's data lives in the kernel, not on a node's disk. `from_anndata(adata=...)` is now memory-served by default; `to_disk=True` is the documented escape hatch back to the old on-disk behaviour. |
| `plexora/datasource.py` | Programmatic datasource registration (`register_datasource`, `register_image_datasource`). `anndata_spec()`, `described_spec()` and `flat_table_spec()` are one translation of the read-spec answers, extracted so `register_anndata_datasource`, `register_datasource` and the memory path (`plexora/memory.py`) share it instead of drifting apart. |
| `plexora/nodes.py` | Programmatic **data node** API: `register_node`, `attach_table`/`attach_image`/`attach_segmentation`, `detach`, `inspect_table`. A node is a Plexora with the viewer off; see `plexora/server/providers/`. Also `client_node()` (the registered node on the browser's own machine, if any), `resource_id_for(path)` (derives an id from the path, never generates one), `share_path`/`resource_status`/`unshare_path` (add/poll/remove a resource on an already-running `--dynamic` node), `detect_on_node(node, path)` (ask a node what one of its own files is, before anything serves it -- the kind a `share_path` then names), `browse_on_node` (relay a native dialog to a node's machine) and `list_dir_on_node` (list one of its directories -- the only way to browse a machine with no desktop; copies `path`/`parent`/`crumbs`/`entries`/`truncated` out of the node's answer BY NAME, a whitelist that silently drops any field not listed there, so the picker can never learn to draw something this function was not also taught to pass through), and `open_file_on_node`/`write_file_on_node` -- the one exception to "a node names, never sends": a plugin's Upload/Download button needs the bytes, and the browser asking has no route to the node at all. Both stream (an unread response the caller must consume and release; a write read off the wire as it goes), and a write's already-there refusal comes back as data (`{"exists": True}`, via `http.request`'s `allow_status=(409,)`) rather than an exception. `attach_image`/`attach_segmentation`/`detach("image", ...)` all run `_same_image` first. `attach_table`/`attach_image`/`attach_segmentation` gained `reload=True`; `reload=False` skips `_reload()`, for the caller who already knows another process is the one serving (the memory/kernel-node path). `attach_image` also takes `image_type` (the import form's override) and reads the node's own verdict off the geometry response, so an H&E slide on a node registers as brightfield — see `_node_image_kind` and the node-image invariant below. `image_type_on_node(name, resource_id)` answers the upload form's question out of `/hello`, opening nothing. |
| `plexora/datasets.py` | Programmatic **dataset** API, over the same registry the server routes use (`server/models/datasets.py`). `create_dataset`, `create_project`, `configure_project`, `project_manifest`, `list_datasets`, `dataset(name_or_id)` (a `Dataset` handle), `project_from_spec`, `PROJECT_SPEC_KEYS` (the one list of every field a project spec may carry -- `cli.py`'s `_PROJECT_OPTIONS` and `create_project`'s validation both read off it, so a new field is added once) and `DatasetCreateError`. **A one-sided marker/metadata answer completes itself** (`_complete_columns`): a spec naming only `metadata` -- or only `markers` -- takes the other side from the columns registration already recorded, because storing the empty half would read as "unclassified" and put the classification question back on screen. Naming both still means exactly those two lists. **A spec says which machine each file is on.** The `node` spec key is the entry's default and `node=`/`--node` the batch's, while any role field may answer for itself -- a `node://<node>/<path-or-id>` string exactly as the import form posts one, or `{"path": …, "node": …}` where `"node": null` is the per-field opt-out that makes "slides on the cluster, table on my laptop" sayable. Precedence: field > entry > batch > local. `_locate()` reads the three spellings into a `_Located`; `_check_located()` refuses what is not there WITHOUT writing anything (`nodes.detect_on_node` is a read, which is what extends the "everything validated before anything is registered" promise across machines) and its cache is returned by `_validate_batch` and handed to each `create_project`, because detection reads pixels and asking twice about fifty slides is minutes; `_serve()` is the write half -- `nodes.share_path` under the kind the ROLE decides (`image`/`segmentation`/`data`->`table`), never the node's own reading, since the field somebody wrote is their statement. Four cases: all-local is what it always was; a node image goes `_serve` -> empty `Project(image=ImageSpec())` -> `nodes.attach_image` and **any failure deletes the project**, which nothing else here does because nothing else here created one; a local image with a remote mask or table registers locally and hands only the remote roles to `configure_project` (reaching `import_routes.attach_segmentation`/`replace_project_data`'s own `node://` branches); an adopted `exist_ok` project is never deleted and still applies only the mask, as adoption always has. Shares are deliberately NOT undone on failure -- an identical re-add is a no-op so a re-run is free, while `unshare_path` could pull a resource out from under another project. `pending_conversions(names)`, `failed_conversions(names)` and `conversion_warnings(names)` are the CLI's courtesy lines for a mask still converting, one that failed, and one with something worth knowing but nothing wrong, all sharing `_described_on_nodes` (one `node_api.node_resources` call per node these projects read, narrowed to their own resource ids); all three swallow an unreachable node. `_apply_columns` returns early for a table whose binding is a node, or the adapter would hand `node://…` to h5py. `tests/test_datasets_api_on_a_node.py` covers it against a real second process. Exported lazily off `plexora/__init__.py`'s `_PUBLIC_API`, same reason as the rest of it (see that row above). |
| `pyproject.toml`, `MANIFEST.in` | Packaging. Both must include frontend assets, shaders, and `client/src/js/**/*.js`. `MANIFEST.in` has no `plugins/*/static` glob, so each bundled plugin needs its own `recursive-include` line or an sdist installs fine and serves the tool with no client. Distribution is pip/wheel-only (`python -m build`) -- the old PyInstaller desktop-executable pipeline (`packaging/pyinstaller_entry.py`, `plexora/__pyinstaller/`, `package_win.bat`, `package_mac.sh`, `requirements.yml`) is gone. `pyproject.toml`'s own `version` is now the one source of truth for the DESKTOP app's version too -- see the `desktop/` row and `scripts/release.py`. |
| `desktop/` | The desktop app's shell: Tauri v2, Rust, wrapping an embedded Python that runs `plexora --desktop`. `src-tauri/src/` is one file per concern -- `lib.rs` (entry), `setup.rs` (spawns the server, waits for its ready line), `server.rs` (the stdout/stdin protocol described at `cli.ready_line`), `windows.rs` (the app's windows, splash included), `menu.rs` (the native menu, `plexora/client/src/js/services/desktopBridge.js`'s `SHELL_CHORDS` twin for the accelerators WebView2 eats -- see Sharp Edges), `commands.rs` (every IPC command the frontend may call), `downloads.rs` (native Save), `opens.rs` (file association / drag-open / second-instance handling, what reaches `/desktop/open`), `lifecycle.rs`, `smoke.rs`. `build.rs` declares every command in `commands.rs`; a command a remote origin (the web content) calls also needs an `allow-<command>` line in `capabilities/main.json`, or Tauri silently refuses it. Version, in `tauri.conf.json` and `Cargo.toml`, is kept equal to `pyproject.toml`'s by `scripts/release.py propagate`, never hand-edited. |
| `scripts/release.py` | One stdlib-only script, the desktop app's release pipeline end to end: `doctor` (checks the toolchain), `bump`, `propagate [--check]` (pushes `pyproject.toml`'s version into `tauri.conf.json`, both `Cargo.toml`s -- `src-tauri` and the workspace -- and both `package.json`s; `--check` is what CI and a pre-release run to confirm nothing was hand-edited out of step), `client` (the frontend build), `wheel`, `runtime` (the embedded Python), `bundle [--sign]`, `collect`, `validate`, `checksums`, `all`, `ci`, `clean`. Meant to be run directly, not imported. |

**Server** (`plexora/server/`)

- `models/data_model.py` — the high-risk file. Datasource loading, zarr/OME-TIFF
  access, tile extraction and encoding, GMM/contrast statistics, segmentation,
  spatial queries. Holds mutable module-level globals (`source`, `config`,
  `channels`, `seg`, `zarray`, `metadata`, `_loaded_source`).
  `generate_thumbnail(name)` is the Open Project grid's card and is the one
  image path that deliberately does NOT load the datasource -- a page of
  projects must not be a data load per card. It reads one coarse level and
  stretches it 1--99 through `_thumbnail_image`: `_local_thumbnail_plane` off
  this disk, `_node_thumbnail_plane` off the node (geometry, then
  `read_region`, both inside `http.speculative()` with
  `_NODE_THUMBNAIL_TIMEOUT`). Deliberately not the node's `overview` endpoint
  even though that is one round trip and already encoded: overview bytes are
  quantized against (0, the full-res max) because the viewer applies the
  contrast slider on top of them, so as a finished picture one hot pixel
  makes the card black -- and computing that window costs the node a
  full-resolution read. `_thumbnail_level` picks the coarsest level with both
  dims >= 200 that is also under `_NODE_THUMBNAIL_PIXELS`; nothing affordable
  means no thumbnail, which is the placeholder icon. Anything failing here
  returns None on purpose: a card, not a page.
  `_local_thumbnail_plane(channel_file, pyramid=None, rgb=False)` picks its
  level by the SPATIAL dims, whatever the layout, rather than assuming
  `(channel, y, x)`: an interleaved RGB slide is `(y, x, 3)`, and reading its
  level shape the channel-first way once found every level failing the
  "width >= 200" test on its own width of 3, fell through to FULL resolution,
  and took `array[0]` -- one row of pixels -- as the picture. That is what a
  Visium HD run's H&E looked like on the Samples page: a 56-byte strip.
  `rgb=True` (a brightfield project, `project.image.kind ==
  IMAGE_TYPE_BRIGHTFIELD`) returns `(y, x, 3)` in either layout so the card
  stays in colour; `_thumbnail_image` then builds an RGB `PIL.Image` (8-bit
  colour used as-is, anything wider stretched with ONE window over all three
  channels so the stain's hue survives) instead of a grey `'L'` plane -- an
  H&E card in grey is a different-looking tissue from the one the viewer
  opens. The cached file `project_routes.py`'s `GET project_thumbnail/<name>`
  writes and re-serves is named `_THUMBNAIL_CACHE_NAME`
  (`.thumbnail-v2.webp`), a version bumped in the name itself and not
  invalidated any other way -- a cached card is served forever, never
  re-checked -- so it must be bumped whenever what `generate_thumbnail` draws
  changes, or a fixed reader behind the same old name would never be asked.
  `_STALE_THUMBNAIL_NAMES` lists earlier names, removed from a project's
  derived folder once the current name is written.
  `image_status(datasource_name)` (behind `GET /image_status`, `data_routes.py`)
  is what a blank canvas cannot say for itself: `classify_image_error(exc)`
  sorts a failure into `missing`/`inaccessible`/`corrupt` (plus `unavailable`
  for a node not answering, and `offline` for a `RemoteUnreachable` -- a web
  address that cannot be reached, checked BEFORE `unavailable` since a
  `RemoteUnreachable` is a `ResourceUnavailable` subclass and the fix is
  different: there is no node to reconnect), and `viewerErrorState.js` puts one
  sentence per cause on screen. **Stats the file BEFORE consulting the
  loader** — `_stat_image(src)` — because `load_datasource` short-circuits for
  a project that is already `_loaded_source`, which would otherwise answer
  "ok" for a file deleted, moved or truncated since the load, exactly the
  case a mid-session probe exists to catch. Uses `Project.load(datasource_name)`,
  never the module's own `_project`, since that helper reads THIS module's
  loaded-project globals and answers an empty record with no image path for
  the very case this function exists to cover — a project that cannot load at
  all. `_image_failures` (scope -> status/detail/src/at) is the record a loud
  `load_datasource` failure writes on its way past (`_record_image_failure`/
  `_clear_image_failure`, both under `load_lock`); `IMAGE_STATUS_TTL_S` (10s)
  bounds how long a burst of failing tiles is answered from that record rather
  than re-opening a file that is still not there. The load itself is
  unchanged and stays loud — classification happens beside it, never in place
  of it. `load_datasource` clears `_feature_column_cache` on every reload.
  `_feature_reader()` is `None` for an ordinary table and the loaded
  provider's `read_feature_column` for a WIDE one (see the adapters row's
  WIDE mode); `_filter_columns_from_frame`/`_all_cells_from_frame` take
  `read_missing=` and call it for a column the frame does not carry, rather
  than raising. `_cached_feature_reader` is a bounded LRU
  (`_feature_column_cache`, `PLEXORA_FEATURE_CACHE_MB`, default 512 MB,
  evicted oldest-first by summed `nbytes`) keyed on the datasource and column
  name, because a viewer coloured by one gene asks for that column on every
  tile. `get_datasource_description` merges in a lazy adapter's
  `describe_features()` when `_feature_reader()` is not `None`.
- `server/utils/ome_zarr.py` — **OME-Zarr / NGFF images**, the second format the
  multichannel pipeline reads. `open_image(path, extension=None)` returns an
  `NgffPyramid` shaped like the zarr *group* tifffile produces for a pyramidal
  TIFF (`group[str(level)]`, `len(group)`, levels indexed `[channel, rows,
  cols]`), which is why `read_tile`, `_zarr_level`, `quantization_window_of`,
  the tile route and the node read path needed **no changes at all**. Three
  clauses of that shape are load-bearing and easy to break: it must NOT be a
  `zarr.Array` (`read_tile` and `quantization_window_of` isinstance-branch on
  that), it must NOT have `.shape` (`node/api.py` reads `hasattr(pyramid,
  "shape")` as "single plane"), and level order comes from
  `multiscales[0].datasets[i].path` — spatialdata names its arrays `s0`/`s1`,
  never `0`/`1`. `open_image` also **drops every level past a break in the
  halving chain** (`dyadic_prefix`): the client's tile source computes a level's
  size as `size >> level`, so a 4x-step level draws the wrong rectangle at the
  wrong zoom with nothing to say so, and `len(pyramid)` — which is what
  `maxLevel` is recorded from — has to mean "levels that can actually be
  drawn". `resolve_image_path` turns a store root into the image group
  inside it (bioformats2raw series, SpatialData `images/<element>`, HCS plate
  field `<row>/<col>/<field>`), raising with the candidates named when there is
  more than one. The plate branch runs **before** the numbered-series one — a
  plate is written by bioformats2raw and carries its layout stamp too, but its
  rows are letters, so the series branch would find nothing and report the wrong
  reason; a plate holds hundreds of images, so its error names one pasteable
  field rather than all of them. `build_extension`
  derives the coarse levels a store arrived without, into
  `<project>/image_pyramid.zarr`, keyed by **absolute** level index and never
  duplicating level 0; `ImageSpec.pyramid`/`pyramid_key` record it, the
  `derived`/`source_key` pattern `SegmentationSpec` established. Everything
  dispatches on the *path* (`is_zarr_image_path`), never on the recorded kind,
  which is what lets a data node serve a store it has no project for. Five
  `str(path).endswith(".zarr")` checks in `segmentation_pyramid.py` and one in
  `providers/local.py` used to dispatch on the name instead — so a mask
  *inside* a store (`store.zarr/labels/nuclei`) failed the check its own
  sibling image passed — and are `is_zarr_image_path` calls now too.
  `pyramid_transform()` was added beside `physical_metadata` for the layer
  work — `physical_metadata` itself is deliberately unchanged, because it
  answers a different question (pixel size, not where a layer sits).
- `server/utils/remote_store.py` — reading a zarr store from a web address
  (`https`/`s3`/`gs`/`gcs`/`az`/`abfs(s)`) through an on-disk chunk cache,
  because zarr's own reader keeps nothing: every tile refetches the whole
  chunk it sits in, and against IDR forty 256px tiles cost 123s uncached
  versus 0.12s cached. `canonical_url`/`split_store_url` are the one URL
  spelling a project records and the store root inside it (the last segment
  ending in `.zarr`), so `x.zarr` and `x.zarr/0` share one cache tree and one
  connection. `ChunkCacheStore` is a zarr `WrapperStore` over `FsspecStore`:
  a read comes from `<data root>/.remote_cache` when it can, is fetched once
  otherwise (concurrent readers of one key single-flight), and a 403 is read
  as missing (a bucket with no listing permission answers 403 for a key that
  is not there) — missing keys are cached too, briefly, since zarr probes
  several metadata names per node and each wrong guess is a round trip over
  HTTP. `CacheIndex` is one SQLite file beside the bytes tracking size and
  last-read time; the byte budget (`paths.remote_cache_budget()`, settings
  key `remote_cache_bytes`, env `PLEXORA_REMOTE_CACHE_BYTES`) is enforced
  across every store and across restarts, least-recently-read first, and a
  store somebody pinned ("keep offline") is never evicted. Not zarr's own
  experimental `CacheStore`: that one's accounting is per-instance and in
  memory (no global or persistent budget), caches no byte ranges, caches no
  misses, and has no single-flight. `_supports_sync_io` is **False**, so
  zarr's sync fast path cannot bypass the cache. **All reads run on zarr's
  own IO loop** (`zarr.core.sync.sync` is how a Waitress thread gets there) —
  never call `sync()` from a coroutine already on that loop. `probe`,
  `fingerprint`/`fingerprint_key` (an ETag/Last-Modified based staleness key)
  and `RemoteSupportMissing` (gs/az without the `plexora[remote]` extra) round
  it out. fsspec, aiohttp and s3fs import inside functions, so building the
  app never pays for them. Credentials are never stored here — see
  `models/remote_sources.py`.
- `server/models/remote_sources.py` — remote data as the data root sees it.
  The address book (`<data root>/remote_sources.json`) keeps options — an S3
  endpoint, "use my AWS profile", a region, an account name, a label
  (`OPTION_KEYS`) — per URL prefix, never a key or a secret, because one IDR
  endpoint serves every IDR image and the import dialog needs the options
  before any project exists. Usage and budget for Settings, and two
  background jobs over `layer_jobs.start_task`: *warm* (`REMOTE_WARM_ID`),
  started when a project with a remote image opens and fetches only the
  coarse levels (`WARM_BUDGET_BYTES`) before anybody asks; and *pin*
  (`REMOTE_PIN_ID`, "Make available offline"), which brings a whole store
  into the cache and exempts it from eviction. Both run under
  `PIN_SAMPLE`/a sample name, since a store can serve several projects or
  none yet.
- `server/utils/ngff_transform.py` — reads NGFF `coordinateTransformations`
  (without importing `spatialdata`, so a core build does not grow that
  dependency — the coordinate systems of a SpatialData store are plain JSON in
  each element's `.zattrs`/`zarr.json`) and turns them into the six-number
  `[a, b, c, d, e, f]` affine `LayerSpec.transform` stores, in canvas/SVG
  order (`x' = a*x + c*y + e`) rather than a numpy 2x3, because the client's
  hot path is `ctx.transform(...layer.transform)`. A layer whose transform
  could not be read gets `None`, not the identity — "aligned by assumption"
  is a real state the Layers panel says out loud, not silent behaviour.
- `server/utils/spatial_scene.py` — what a spatial store holds and where each
  piece sits, for the two shapes Plexora reads: a SpatialData store (elements
  under `images/`, `labels/`, `points/`, `shapes/`, read as plain JSON) and a
  Xenium run directory (`morphology_focus/`, `morphology.ome.tif`,
  `transcripts.parquet`, `cell_boundaries.parquet`, `nucleus_boundaries.parquet`,
  `experiment.xenium`, all in microns in one common frame). Returns a list of
  `LayerSpec`s ready for `Project.with_layer`, the first image chosen as the
  reference and every other element's transform composed through it —
  nothing opened, nothing converted, no pixel read. `XENIUM_FILES` names
  every morphology candidate under the one layer id `morphology`, and
  **order is preference**: `morphology_focus/` (or `morphology_focus.ome.tif`)
  first, then `morphology_mip.ome.tif`, then the raw `morphology.ome.tif`
  Z-stack last, because the focus image is the 2-D composite Xenium Explorer
  itself draws and the stack's fourteen planes are not aligned with each
  other. `_xenium_path(root, name)` resolves a directory entry to the FOLDER
  when it holds several channel files (`xenium_focus` composes them) and to
  the single file when it holds one, so nothing downstream has to know which.
  `xenium_manifest(root)` reads `experiment.xenium` once (`{}` on anything
  missing or unreadable, since a directory somebody copied files out of is
  still a Xenium run); `xenium_image_path(root)` honours the manifest's own
  `images.morphology_focus_filepath`/`images.morphology_filepath` paths
  first — an instrument-written path beats a filename this module guessed —
  falling back to `XENIUM_FILES`' preference order only when the manifest
  names nothing usable. A third shape, **Visium HD**: `is_visium_hd_run`/
  `visium_hd_root`/`visium_hd_levels`/`visium_hd_segmentation`, checked
  BEFORE `is_visium_run` everywhere (a lone `square_008um/` folder has the
  `scalefactors_json.json` both look for; `bin_size_um` in it is the marker
  that says HD). Not a Visium run with smaller spots — the positions are a
  parquet per bin size under `binned_outputs/square_XXXum/spatial/`, and the
  grid is 30 million squares — so detection, the proposal and the rendering
  are all their own. `fit_similarity(src, dst)` is Umeyama's least squares
  WITH REFLECTION ALLOWED (a plain rotation would put every square on the
  wrong side of the slide, since the grid is mirrored against the microscope
  image) — a similarity by construction, so fit noise can never produce the
  shear or anisotropy the viewer refuses. `visium_hd_bin_transform` fits it
  off one parquet record batch (`VISIUM_HD_FIT_ROWS`, tens of milliseconds,
  not thirty million rows) and adds **+0.5** to the grid coordinates before
  fitting, because a square `(c, r)` is drawn as the unit square
  `[c, c+1) x [r, r+1)` while the parquet states square CENTRES.
  `read_visium_hd_scene` turns a run into the same `SceneElement` list shape
  the Xenium/SpatialData readers return; `read_scene` dispatches to it,
  checked BEFORE `is_xenium_run`.
- `server/models/layer_sources.py` — tiles for a layer that is not the
  reference image. Deliberately bypasses `data_model`'s single open-datasource
  globals rather than generalizing them: the requirement is N layers of one
  open project, not N open projects, and `data_model.read_tile`/`_zarr_level`/
  `quantization_window_of`/`encode_tile_array` are already pure over the
  pyramid they are handed, so this adds only a small keyed cache of open
  pyramids. The reference image's own tile path never touches it —
  `test_layer_sources.py` monkeypatches `data_model.load_datasource` with a
  counter and asserts zero calls while serving a wall of layer tiles.
  `OpenLayer` also carries `windows`/`stats`/`gmm`, three per-channel caches
  that die with the pyramid: `window_of(opened, channel_num)` memoises
  `data_model.quantization_window_of` (a full-resolution-plane scan that
  `layer_tile` used to redo once per TILE), and `layer_channel_stats`/
  `layer_channel_gmm(project, layer, channel)` are the same stats/GMM packets
  `/get_image_channel_stats`/`/get_channel_gmm` return for the reference
  image, built the same way (`data_model.channel_stats_of`/
  `channel_gmm_of`/`quantization_window_of` over `opened.overview`) so a
  registered layer's channel controls are the SAME controls rather than a
  second implementation that looks like them — this is what makes a
  registered image layer get the reference image's own channel controls.
  Both return None for points/rgb/segmentation/unknown channels, through the
  private `_channel_plane(project, layer, channel)` gate that is the single
  place deciding which layers have channel controls at all; the
  zero-`load_datasource` invariant covers these two the same as tiles.
  `parse_style`/`layer_tile`'s docstrings now say it plainly: a layer WITH
  channel controls is drawn through the client's GL colorize pass and its
  tile asks for no colour (`style=None`); the `style=` path — recolouring
  server-side into interleaved RGB — is for what that pass does not touch, an
  rgb layer or the transcript density raster.
  `density_scale(manifest, level, bin_pixels=None)` is the unrounded density
  window (the rounded-to-int version, `density_window`, feeds the plain
  count raster; the unrounded one is what `_density_groups`' smoothing sigma
  and the ramp path's ceiling are computed against, so it is not re-derived
  twice with two different roundings).   `bin_pixels` is the bin's size in
  IMAGE pixels rather than the old fixed `2 ** level`, so a 40-micron bin
  stays 40 microns at every zoom instead of doubling with the level; it comes
  from `transcript_tiles.bin_pixels_for`, which is now EXACT (see above) --
  the ceiling goes as the bin's area, so a bin size that drifted 10% between
  levels moved every colour by 20%.
  `parse_style` parses `genes`, `colors` and `minq` off a layer's style
  payload — `minq` deliberately, not `q`: that already names this route's own
  encoding-quality parameter, and reusing it for the transcript quality floor
  would have made one query key mean two different numbers depending on which
  layer answered it. It also parses the density map's own three controls:
  `bin` (bin size in image pixels), `ramp` (a name from
  `server/utils/colormaps.py`, present only when the density is drawn as one
  field rather than a gene-coloured composite) and `dlo`/`dhi` (the contrast
  window as 0..1 FRACTIONS of the automatic one, because the count that means
  "dense" quadruples with every zoom level and a threshold set at one zoom
  must keep its meaning at the next), plus `log` (count through log1p before
  the window — a 2 micron bin holds one or two molecules and an islet holds
  hundreds, and a linear stretch shows the islet and nothing else), plus
  `agg` (mean/sum/max/min, `None` when not named — a bin layer's heatmap
  combining several genes into the one field its ramp reads;
  `bin_tiles.aggregation` turns `None` or anything unknown into its default),
  plus `comp` (a bin layer's COMPOSITION glyphs — which of `genes` are
  grouped and how each group combines, as indices — present means "draw the
  glyphs instead of a ramp", parsed and validated by `bin_tiles.
  parse_components` where the gene count is known; a bad `comp` string is a
  new `BadStyle(ValueError)`, not a silent fallback). `_style_key` hashes all
  of it, `log`/`agg`/`comp` included, into the tile ETag/cache key.
  `_points_tile` dispatches on `layer.render.pointKind == "bin"` — not a
  modality, so core names no vendor — to `_bins_tile`, which reads
  `bin_tiles`' manifest/stats, `bin_tiles.requested_pooling(style.get("bin"))`
  (the square size asked for, before the level floors it) and effective
  pooling for the level. A `comp` style short-circuits straight to
  `_composition_tile` before genes resolve to a ramp or an RGB composite;
  otherwise `_bins_tile` passes `how=bin_tiles.aggregation(style.get("agg"))`
  and `scale_pooling=requested` into `ramp_tile`/`rgb_tile` — the colour
  scale is measured at the REQUESTED square, not whatever a coarse level
  draws, so a region keeps its colour as the view zooms out — and returns
  `(payload, mimetype, revision)`; the revision rides the tile's ETag because
  the tile is derived per request from a store that can be rebuilt without
  the project record changing. `_composition_tile` resolves `genes=` names
  against the store POSITIONALLY (a name the store does not hold is dropped
  from its component, an emptied component too) and calls `bin_tiles.
  composition_tile`, then `data_model.encode_tile_array(..., lossless=True)`
  — always exact PNG, because lossy WebP would smear the glyphs' hard edges
  into colours no gene has. `generate_layer_tile` (`data_routes.py`) forwards
  both `agg` and `comp` from the query string (`agg` previously reached
  `parse_style` but nothing read it back out — Mean/Sum/Max/Min had no
  effect until `ramp_tile` grew `how=`) and turns a `BadStyle` into an HTTP
  400, not a 404 (a layer card reads that as "Preparing…") or a fallback
  picture (cached for a year).
- `server/utils/colormaps.py` — the four named colour ramps a density tile can
  be drawn through (`viridis`, `magma`, `cividis`, `coolwarm`; `DEFAULT_RAMP`
  is `viridis`). `ramp(name, stops=STOPS)` expands a handful of hex anchors
  into a `(256, 3)` uint8 table by `np.interp` per channel — anchors and not
  256 literal rows, because a ramp written out in full is source nobody can
  check and interpolating is exact at every anchor anyway. The anchors are
  `views/gradientRange.js`'s `PlexoraColorRamps`, to the digit, on purpose —
  the one client-side definition, now that `cellExplorerColors.js`'s own
  `RAMPS`/`ramp`/`rampStop` are thin delegations to it rather than a second
  copy: `is_ramp(name)` tells a colormap request apart from the per-colour
  one, and `apply(level8, name)` is a uint8 intensity raster through one ramp
  — the same colours a density map and a coloured cell overlay of the same
  slide draw in, checked by `tests/js/transcript_points_probe.mjs` (which
  preloads `gradientRange.js` before reading this file) to assert the
  client's `TranscriptLayer.RAMPS` still matches it. `tests/test_colormaps.py`
  covers the Python side.
- `server/models/transcript_tiles.py` — transcript points, tiled so a
  viewport read is a seek. Structurally a sibling of `centroid_tiles.py` (same
  manifest, staleness rule, atomic temp-dir-and-move, per-datasource lock) but
  a deliberate copy rather than a shared base, because the two differ in what
  matters: a transcript has no id (dropping it saves 4 bytes/record, 200 MB at
  50M rows), a tile is gene-major with a small header locating each gene's run
  so reading 10 of 300 genes is 10 short `np.fromfile` ranges, and the coarse
  levels are AGGREGATES computed per request rather than a stored pyramid
  (see below — this reverses the original "no coarse point levels, at
  whole-slide zoom a transcript is density" decision). `CACHE_VERSION = 2`: `POINT_DTYPE` is 11 bytes/record
  (`gene:uint16, x:float32, y:float32, q:uint8`, `RECORD_DTYPE_NAME` the
  string form stamped into the manifest), the cache dir is
  `transcripts_v{CACHE_VERSION}`, and `is_current` compares `record_dtype`
  too — a dtype change is a cache-format change even when the version number
  is bumped by hand elsewhere. The manifest also carries `gene_counts`
  (`np.bincount` over the gene column) and `units` (`"reference_pixels"`).
  Builds run under `_lock_for` and sweep old-version cache directories on
  completion. Readers take `min_q`, applied by `_above()` after the read
  (a quality floor is a filter on what is already on disk, not a second
  index). `bin_pixels_for(tile_size, level, bin_pixels)` and
  `grid_for(tile_size, level, tile_x, tile_y, bin_pixels)` are the one place
  bin geometry gets decided, and **the grid is anchored to the IMAGE, not to
  the tile** — `grid_for` returns the global index of the first bin the tile
  touches, so box 108 is box 108 in every tile and at every level. They
  replaced `bins_for`, which rounded the bin COUNT to fit a tile: a 188-pixel
  bin came out 204.8 wide at level 0 and 186.2 at level 1, so the boxes
  resized and the grid shifted every time the viewer crossed a level and a
  patch of tissue visibly changed colour (the ceiling goes as the bin's area,
  so 10% of bin became 20% of colour). The size is now exactly what was
  asked for, with one floor: a bin cannot be finer than `2 ** level`, which
  is what one tile pixel covers.

  The price is that bins straddle tile edges, so `_overhang` makes both
  rasterizers read the RING of level-0 tiles around the one being drawn —
  otherwise an edge bin counts only the half inside the tile, which is both a
  seam and (since how much falls outside depends on the level) the same
  zoom-dependent colour by another route. `tests/test_transcript_tiles.py`
  pins it bin-for-bin across three levels.
  `density_tile`/`density_rgb_tile` take
  `bin_pixels=` (renamed from `bin_size=`, absolute image pixels rather than
  a level-relative count, so a 40-micron bin is 40 microns at every zoom) and
  both now always return a tile-sized raster regardless of the bin count.
  `density_rgb_tile(..., groups=[(indices, rgb, hi), ...])` rasterizes several
  gene groups into one RGB tile in one pass, each smoothed by `_smooth()` —
  a Gaussian over a padded rasterisation, `DENSITY_SMOOTH_PIXELS = 3.0` scaled
  by `2 ** level` — so a whole-slide density view reads as density and not as
  a sparse scatter of single pixels. `density_ramp_tile(...)` is the third
  path: one field (the selected genes summed, or the whole panel) read off a
  named ramp from `server/utils/colormaps.py` rather than composited per-gene
  colours — and it paints EVERY bin, an empty one included, so the map covers
  the layer edge to edge and zero is a colour rather than a hole. That works
  only because the client composites this one path `source-over`
  (`TranscriptLayer.densityBlend`); the per-gene composite is still `lighter`,
  where black does not draw, and still leaves an empty bin black. The two
  halves are one decision and changing either alone is the bug.
  `density_ramp_tile` therefore returns **RGBA**, which `encode_tile_array`
  accepts alongside RGB (WebP codes the alpha losslessly, so the box edges
  stay exact). The alpha is the GRID: `_gutter(tile_size, bins)` masks a strip
  `DENSITY_GUTTER = 0.06` of a bin wide off the LEADING edge of every box --
  leading so the gaps line up across a tile boundary -- and it is the only
  place the morphology shows through. Below `DENSITY_GUTTER_MIN_BLOCK = 8`
  tile pixels the gap it is owed is thinner than a pixel, so it is drawn as
  ONE pixel at partial alpha rather than as a whole transparent pixel — which
  at the whole-slide levels (a bin is ~3 px there) would take a third of the
  map instead of a twentieth. `DENSITY_GUTTER_MIN_COVERAGE = 0.5` is the
  floor under that, because the user asked for the grid to be visible when
  fully zoomed out and a line at 18% is not. `_cut_gutter` does the same job
  by scaling towards zero for the two `lighter` paths (`density_tile`,
  `density_rgb_tile`), where nothing already means nothing.

  **Aggregated points are what Points mode does at low zoom, and nothing
  switches to density on the user's behalf any more.** `aggregate_tile(ds,
  layer, level, tx, ty, genes=, min_q=, tile_size=, bins=AGGREGATE_BINS)`
  reads the `4**level` level-0 tiles under one coarse tile and merges the
  molecules in each bin, PER GENE, into one `AGGREGATE_DTYPE` record (14
  bytes: `gene:uint16, x:float32, y:float32, count:uint32`, string form
  `AGGREGATE_DTYPE_NAME`) carrying the count and the position of ONE OF THE
  MOLECULES it merged. Records come back most populous first, so the small
  dots land on top of the large ones rather than under them.

  **The position is a member of the bin, not the mean of it, and that took
  three tries.** The bin centre is a lattice. The MEAN is also a lattice
  once a bin is crowded, because with two hundred molecules in it the mean
  converges on the centre — measured: ACTB at whole-slide zoom came out as
  a perfect grid of evenly spaced dots, an artifact of the binning drawn as
  though it were the data. A member of the bin lands where that molecule
  was, so the field reads as a scatter and every dot drawn is somewhere a
  transcript actually was. WHICH member is `_priority`, a hash of the
  molecule's own position, and THREE of its properties are load-bearing.
  Picking by read order takes the bin's right-hand edge every time (a
  level-0 tile is x-sorted and a bin sits inside one — measured as a mean
  offset of 0.92 across the bin, a lattice again, just shifted). Picking by
  anything derived from buffer order makes every crowded dot jump when the
  user switches a second gene on. And the hash must AVALANCHE, which the
  first one did not: the winner of a bin is the MAXIMUM of the hash, so the
  dot is placed by its HIGH bits, and `qx * 73_856_093` never reaches
  2**63 on real coordinates, so those bits were a monotone ramp in x.
  Taking the maximum of a ramp picks the same place in every bin — measured
  on forty molecules in a bin, the winner landed in the middle two tenths
  75% of the time and in the outer four tenths never, and an abundant gene
  at whole-slide zoom came out as a vertical comb of dots exactly one bin
  apart. Same artifact the mean is rejected for, reached by another road: a
  pick that is nearly always central is a centroid with extra steps. It is
  now xor-of-two-odd-multiples into the splitmix64 finalizer, and every
  tenth of the bin takes 9.7–10.3% of the winners in both axes
  (`test_a_crowded_bin_is_not_represented_by_its_own_middle`, which asserts
  on `_priority` directly because the tile-level test cannot see it: a bin
  holding three molecules spreads the pick wide whatever the hash does).
  Sparse bins hold one molecule and so sit exactly on it either way, which
  is most of a panel: the median gene on this run puts three molecules in a
  whole-slide bin. `AGGREGATE_BINS = 45` bins across a tile at every
  level (a bin is therefore `tile_size / 45 * 2**level` image pixels, and
  the client reads the number out of `/manifest` rather than assuming it);
  it is **the only continuous control over how dense the zoomed-out overlay
  is**, because the client's level is a quadtree step and so cannot change
  the dots on screen by less than a factor of four — asking the level rule
  for half as many gave 69% fewer at one zoom and none at the next. 45 is
  64/√2 rounded, i.e. half the dots per unit area, and it need not be round:
  bins are laid out by a float `scale` over the tile's span. Changing it
  costs no rebuild (aggregates are per request) but does need
  `AGGREGATE_REVISION`, which with the bin count rides the aggregate ETag —
  these tiles go out with a year-long `max-age` and `source_mtime_ns`
  cannot see a change to code that derives them;
  `aggregate_levels(w, h, tile_size)` is the level ladder, identical to the
  one the density pyramid and `TranscriptLayer.densityLevels` climb, because
  "level 3" has to mean one patch of slide at both ends of the wire.
  Internally `_aggregate_chunk` folds buffered points into a bin-by-gene
  accumulator with three `bincount`s per flush (`AGGREGATE_FLUSH = 4M`
  points), chunking the gene axis when the grid would exceed
  `AGGREGATE_MAX_CELLS = 2M`.

  **Why per request and not a stored pyramid.** An aggregate is a function of
  the gene selection AND of the quality threshold, both of which are controls
  the user turns, so a precomputed pyramid would be one pyramid per
  selection. The only selection-independent version — every gene at every
  level — is very nearly a record per molecule per level, because a 480-gene
  panel almost never puts two molecules of the SAME gene in one bin until the
  bins are very coarse; that is roughly 1.5 GB on top of a 359 MB cache for
  the Xenium run this was measured on. What a pyramid would have bought is
  bought by the tile addressing instead: a level-L tile covers `2**L` level-0
  tiles, so a screenful is about six requests however far out the view is.
  Measured on that run (19.1M molecules, 480 genes, 45450x27241, tile 1024):
  a whole-slide level-5 tile for three genes is **237 ms and 45 KB** (the
  view is two of them), a level-4 tile 68 ms, a level-2 tile 13 ms, a raw
  level-0 tile 7 ms. About a fifth of that is the `_priority` argsort —
  numpy radix-sorts a stable integer key, so it is linear. The pathological
  case, all 480 genes at the coarsest level, is a couple of seconds for one
  tile, and the client's budget guard is what drives it there.
- `server/utils/tiff_series.py` — **the axes of a TIFF's `series[0]`**, and the
  only place anything reads them. Every other TIFF reader here indexes the
  series positionally (`shape[0]` channels, `shape[1]` height, `shape[2]`
  width), which is right for a plain channel stack and for a pyramidal
  OME-TIFF and wrong for an **ImageJ hyperstack**, whose series is `(T, C, Y,
  X)` or `(Z, C, Y, X)`: a CODEX stack of 23 cycles x 4 channels registered as
  23 channels four pixels tall, and then failed to load at all because the
  overview heuristic wants a level with both non-channel dimensions >= 200.
  `channel_series(tiff)` returns `series[0]` **by identity** whenever it is
  already `CYX` — that is the load-bearing half, since it keeps the format
  Plexora is built around on the path it was already on — and otherwise
  rebuilds it as a `tifffile.TiffPageSeries` over the same pages with the
  leading axes collapsed row-major, which is page order. Because the result is
  a real series, `aszarr()` still hands back a genuine `zarr.Array`, so
  `read_tile`'s isinstance branch, `_zarr_level`, `quantization_window_of` and
  `node/api.py`'s `hasattr(pyramid, "shape")` test all needed no changes.
  Called from `LocalImageProvider.open`, `image_geometry`, `convertOmeTiff`,
  `_local_thumbnail_plane` and figure_builder's `SourceImage` — all five, or a
  node's geometry check and the primary's recorded shape disagree. **Masks are
  not routed through it**: a label image is a single 2-D plane and `read_tile`
  indexes it with two subscripts. A **Z-stack** is a third layout, and the one
  a Xenium run ships: `morphology.ome.tif` is 14 focal depths of DAPI, not 14
  channels, each autofocused per field of view, so read positionally it would
  register as fourteen "channels" that each light a different block of
  tissue. `focal_planes(tiff)` reads `plane_sizes(tiff)` — the file's own
  OME-XML (`SizeC=1 SizeZ>1`, the marker looked for in the first
  `OME_MARKER_WINDOW` = 4096 bytes of `ImageDescription`) first, an ImageJ
  header's `channels`/`slices`/`frames` second — and returns `(count, middle)`
  only for a genuine single-channel Z-stack, `(0, 0)` otherwise (including
  when the file states nothing about its own layout either way — the
  conservative reading, since an Akoya/CODEX export puts its CYCLES on the Z
  axis and `SizeZ > 1` alone does not mean focus); `channel_series` then routes a
  Z-stack through `single_plane_series(tiff, index=middle)` and a lone 2-D
  series through the same function at index 0. `single_plane_series` pulls
  the one plane out of **every pyramid level** and chains the results through
  `.levels` exactly as tifffile chains a pyramidal series' own, because a
  Xenium focus image can be 45450x27241 with eight SubIFD levels and a series
  rebuilt from level 0 alone would decode a gigabyte of JPEG 2000 for every
  zoomed-out tile; it falls back to the series it was given if the plane is
  missing at some level. Recorded the same way DICOM's z-stack collapse is:
  `focalPlanes`/`focalPlane` on the channel info.
- `server/utils/xenium_focus.py` — a Xenium `morphology_focus/` folder read as
  ONE image. From XOA 2.0 a run writes its in-focus morphology as a *folder*
  rather than a file: v2 has one file (DAPI), v3 has four
  (`morphology_focus_0000..0003.ome.tif` — DAPI, boundary stain, interior RNA,
  interior protein), each a separate single-channel OME-TIFF over the
  identical pixel grid, not four planes of one file. Every other reader here
  opens one path and reads `shape[0]` as the channel count, so four files
  would otherwise mean four cards with no way to composite them. `FocusPyramid`
  is shaped exactly like the zarr *group* a pyramidal TIFF yields
  (`pyramid[str(level)]`, `len(pyramid)`, `[channel, rows, cols]`), the same
  contract `RgbPyramid`/`NgffPyramid`/`DicomPyramid` meet, so `_zarr_level`,
  `read_tile` and `node/api.py`'s `hasattr(pyramid, "shape")` test need no
  changes. The channel axis is the FILE axis; each file's own pyramid is read
  through `tiff_series.channel_series`, so a focus file that is itself a small
  z-stack collapses to its middle plane before it becomes a channel here. A
  folder holding a single file never reaches this module — `spatial_scene`
  resolves it to that file, since an ordinary single-channel OME-TIFF has the
  more heavily used reader. Dispatched **first**, before the zarr / DICOM /
  brightfield tests, in `data_model.convertOmeTiff`,
  `providers.local.LocalImageProvider.open`/`image_geometry`/
  `detect_image_type`, and `datasource._channel_names_from_image_metadata` —
  every test below it reads a FILE, and a folder is not one.
- `server/utils/xenium_cells.py` — normalises a Xenium `cells.parquet`
  **in the project's own copy**, never the run directory. `normalise_cells_
  table(path, pixel_size=, root=)` adds a numeric `cell_index` (from
  `cell_boundaries.parquet`'s `label_id` when that file is reachable off
  `root`, else row position + 1) and rescales `x_centroid`/`y_centroid` from
  the run's microns into the reference image's pixels. Idempotent, so
  re-registering the same sample does not rewrite what a prior import already
  fixed. Called from `import_routes.replace_project_data` before the copied
  file is inspected, which is what lets `roles.cell_id` be set to
  `"cell_index"` immediately rather than asked for.
- `server/utils/tenx_matrix.py` — a 10x feature-barcode `.h5` (CSC with
  BARCODES as the columns — byte for byte CSR of barcodes x genes, so one
  GENE, the question every viewer asks, is a scan of the whole matrix)
  converted ONCE into a gene-major `.h5ad` (`X` as `csc_matrix`) under
  `<derived>/tenx/`, so it is registered and read as an ordinary AnnData —
  gating, ROI write-back, the notebook's `plexora.view`, SCIMAP all work on
  bins unchanged, because by the time anything looks at it, it is one.
  `ensure_converted` reconverts only when the source's fingerprint moves
  (`is_current`). Adds `obs.in_tissue/array_row/array_col`, one categorical
  per Space Ranger clustering, and `cell_id` for segmented cells (the integer
  a cell's polygon is labelled with, so the row and its outline share an id
  with no lookup table); `obsm["spatial"]` in the run's FULL-RES microscope
  pixels and `obsm["X_umap"]` when Space Ranger computed one; `var["plx_*"]`
  (count/sum/mean/std/quartiles/min/max) and `varm["plx_hist"/"plx_log_hist"]`
  computed during the write, so a wide table is described (see
  `AnnDataAdapter.describe_features`) without reading a single value.
  Memory is bounded by one bucket of entries (`BUCKETS = 48` gene-range
  buckets sorted in memory) whatever the matrix size; `h5py`/`anndata`/
  `pyarrow` are imported inside functions, which is what the boundary tests
  pin.
- `server/utils/boundary_mask.py` — a Xenium `cell_boundaries.parquet`
  (one row per polygon VERTEX, `cell_id`/`vertex_x`/`vertex_y`/`label_id`),
  rasterized into the same tiled pyramidal label OME-TIFF every other
  segmentation mask is, so a run whose boundaries were registered as a
  `shapes` layer nothing drew gets Outlines, Filled, colour-by-gating and cell
  picking too. `is_boundary_table`/`read_polygons`/`rasterize`/`build`/
  `resolve_mask`/`geometry_for`/`describe` is the surface. Two invariants:
  the label VALUES are the table's `label_id`, the same number
  `xenium_cells.normalise_cells_table` writes as the cell table's
  `cell_index`, which is what joins a mask pixel to a gated row — a table
  with no `label_id` is refused rather than numbered, since inventing an
  order the cell table does not share would colour every cell as its
  neighbour, silently. And the mask is drawn at the REFERENCE IMAGE's width,
  height and level count (`Project.all_layers` gives `__mask__` the image's
  own geometry, see `geometry_for`), with **every pyramid level re-rasterized
  from the polygons**, never downsampled from the level above — a cell that
  is sub-pixel at a level is stamped as one pixel rather than vanishing,
  which is what keeps the whole-slide view from coming back empty. Drawn with
  OpenCV (`cv2.fillPoly` into an int32 raster, read back as uint32 -- cv2 has
  no unsigned 32-bit raster), which is why `opencv-python-headless` is a core
  dependency. The three candidates were measured against each other on this
  run: cv2 1x, Pillow's `ImageDraw.polygon` 1.6x, scikit-image's
  `draw.polygon` 36x. On a 45450x27241 run of 247,636 cells / 6.19M vertices:
  17 s, 73 MB, 8 levels. Vertices are rounded in LEVEL coordinates BEFORE the
  tile origin is subtracted, so two tiles sharing an edge round a straddling
  cell identically and the seam is exact -- measured z = -0.05 against the
  local column-to-column baseline at level 0. Writes through `segmentation_pyramid.write_label_pyramid`, so
  `generated_mask_kind` and the staleness machinery read it exactly as they
  read a converted raster mask. `is_boundary_source` widens the surface to
  `is_boundary_table` OR `is_boundary_geojson` — Space Ranger 4's Visium HD
  segmentation states its polygons as `cell_segmentations.geojson`, a
  FeatureCollection with one `Polygon` per cell and an integer
  `properties.cell_id`, detected on a 64 KB schema read since the file itself
  is hundreds of megabytes. `read_geojson_polygons` reads it by a byte-pattern
  fast path over Space Ranger's own layout (geometry first, `cell_id` first
  among the properties) and falls back to `json.loads` for anything else. A
  GeoJSON mask's own registration — a Visium HD run's polygons are in the
  microscope's full-res pixels while the bins beside them are in grid squares
  — is recorded on the mask itself (`Project.segmentation.transform`, below)
  rather than inferred by `geometry_for`'s usual sibling-layer rule, which
  `geometry_for` now checks FIRST, before it looks for a layer sharing the
  mask's path.
- `server/utils/brightfield.py` — **H&E / brightfield images**, the third
  reading of an image file and the only one that is not a channel stack. Two
  jobs. **`detect_image_type(path) -> Detection(verdict, confidence, reason)`**
  is a six-rung ladder, structural evidence first and pixels last, and *never*
  channel count: a whole-slide suffix (`.svs/.ndpi/.scn/.mrxs/.bif/.svslide`);
  interleaved RGB storage (Bio-Formats' `isRGB()`); OME-XML
  `ContrastMethod`/`IlluminationType`/`SamplesPerPixel`; fluorophore vs R/G/B
  channel names and omero colours; then QuPath's thumbnail heuristic, which
  here needs a real light background (`_LIGHT_FRACTION`, because uniform 8-bit
  noise clears "more light than dark" by arithmetic) **and** correlated planes
  (`_channel_correlation`); then fluorescence by default. `is_rgb_layout`
  deliberately requires **interleaving** on top of `photometric=RGB` —
  tifffile writes separate-component RGB for any three-plane uint8 array with
  no photometric argument given, so a large share of 8-bit fluorescence stacks
  declare themselves colour without meaning it. The DICOM-vs-TIFF dispatch in front of this
  ladder is `providers.local.detect_image_type(path)` — one function, called by
  `convertOmeTiff`, by `/detect_image_type` and by a node's `Registry.add`, so a
  slide cannot read as H&E on one machine and as a channel stack on another. A
  file whose planes are `minisblack` is read as colour only because the
  *project* says so, which is
  what `LocalImageProvider(..., rgb=True)` carries (set from
  `image.kind == 'brightfield'` in `providers/__init__.py`). **`open_rgb`**
  returns an `RgbPyramid` with the same three load-bearing clauses as
  `NgffPyramid` (not a `zarr.Array`, no `.shape`, `pyramid[str(level)]`), and
  each level exposes both the usual `[channel, rows, cols]` *and* `.rgb[rows,
  cols]` — the single seam where colour leaves the module, used by
  `read_tile`'s `rgb` sentinel branch. Its levels are **virtual**: the full
  halving chain the viewer's tile source assumes, each read from the nearest
  native level and resampled in flight (`_pick_source`/`_affordable`), so an
  Aperio 4x pyramid needs no conversion at all and a 300 MB SVS registers by
  reading its header. Only a slide written *flat* stops short, and
  `needs_extension` (asked as "did the chain reach one tile", not as a size
  threshold like the NGFF one) sends it to `ome_zarr.build_extension` — whose
  input contract the CYX views already satisfy — into
  `<project>/brightfield_pyramid.zarr`. `physical_metadata` reads the scale
  from wherever each format hides it (Aperio `|MPP = …|`, OME PhysicalSize,
  Leica `<sizeX>`, TIFF XResolution, `openslide.mpp-x`). `.mrxs` needs the
  optional `[wsi]` extra; `BrightfieldSupportMissing` carries the install line.
- `server/utils/dicom_wsi.py` — **DICOM whole-slide images**, read via
  `wsidicom` (+ `pydicom` for header sniffing), the fourth reading of an image
  file. A DICOM slide is a **collection** of `.dcm` instances, not one file:
  `assemble_slide(path)` groups instances by (StudyInstanceUID,
  ContainerIdentifier -> FrameOfReferenceUID -> SeriesInstanceUID) into a
  frozen `SlideSource(kind="files", files=..., label=...)` — `kind="web"` is
  the designed-in DICOMweb seam, and raises "not supported yet" today. Picking
  a folder assembles the slide (a folder holding 2+ slides raises a
  `ValueError` naming them); picking one `.dcm` selects that instance's slide
  and gathers its siblings from the metadata. Directory scanning is
  breadth-first and bounded to depth 4 (`_MAX_SCAN_DEPTH`), because a real
  export nests as `<slide>/<study uid>/<series uid>/*.dcm`; `WsiDicom.open()`
  is handed an explicit file list rather than a folder, because its own folder
  mode globs only one level and finds nothing in a nested export.
  `open_image()` returns `DicomPyramid`, honouring the same duck-typed
  contract as `RgbPyramid`/`NgffPyramid` (not a `zarr.Array`, no `.shape`,
  `pyramid[str(level)]`/`len`/`__iter__`/`__contains__`, levels
  `[channel, rows, cols]` plus `.rgb[rows, cols]` for colour) — which is why
  DICOM needed no second viewer pathway. Optical paths ARE the biological
  channels (one per instance in a real multiplex export); channel order is
  imposed by `_sorted_identifiers` because wsidicom reports a level's optical
  paths in a *different order at different levels* — a sharp edge worth
  remembering. Channel names ladder, all-or-nothing: Optical Path Description
  -> specimen-preparation staining record ("Channel" NCIt C44170 +
  "Component investigated" SCT 246094008) -> illumination wavelength -> None.
  Focal planes are pinned to the middle plane and only counted
  (`focal_plane_count`), never mapped to channels; label/overview instances
  set `has_label`/`has_overview` flags only. `wsidicom.read_region()` takes
  coordinates in the *requested level's own* system (unlike OpenSlide, which
  always uses level 0) and raises `WsiDicomOutOfBoundsError` on an
  out-of-bounds region rather than clipping it, so the caller clips; 16-bit
  monochrome comes back as PIL mode `"I"` (int32) and is cast to uint16.
  Concurrent reads through one `WsiDicom` handle were verified correct and
  faster than serialised, so there is no lock. `image_kind` gained `"dicom"`
  for DICOM fluorescence (behaves like `ome_tiff`); DICOM H&E registers as
  `"brightfield"` and reuses the `rgb` channel sentinel — a brightfield
  override on a monochrome multiplex slide is refused with an explanatory
  `ValueError` rather than borrowing three markers as red/green/blue. Needs
  the same optional `[wsi]` extra as OpenSlide; `DicomSupportMissing` names
  it. Extension store is `dicom_pyramid.zarr` via `ome_zarr.build_extension`,
  rarely needed since DICOM WSI almost always ships a full pyramid already.
- `models/project.py` — **the project record**: one typed view of one
  config.json entry (`Project`, `ImageSpec`, `SegmentationSpec`, `DataSpec`,
  `ColumnRoles`, `ColumnGroups`). The only place that knows the on-disk shape;
  everything else asks it questions (`project.roles.x`, `project.has_table`).
  Two invariants: keys it does not model round-trip through `extra`, and every
  change goes through `patch()`, which merges. There is deliberately no API for
  replacing an entry wholesale — that is what used to destroy AnnData projects
  on save. It also owns the **file access**: `read_config()` / `write_config()`
  are the only sanctioned way to touch config.json, and every other module must
  go through them (`config_transaction()` for a read-modify-write spanning
  several calls). Writes go via a temp file and a rename, so a reader never sees
  a half-written file — reading or writing it directly reintroduces the race
  that made an import fail the next page with `JSONDecodeError: Expecting value:
  line 1 column 1`. `write_config` also bumps a process-local
  `config_generation()` counter, under the lock, AFTER the rename — deliberately
  not `data_model.load_generation` (which moves when a different project loads,
  not when a layer is added to the one already open) — so anything caching
  something derived from a project's record (a layer pyramid, a layer tile, its
  ETag) knows when to throw it away. `project.py` also gained `LayerSpec` (one
  registered layer of a spatial scene — `kind` is `"image"`/`"labels"`/
  `"points"`/`"shapes"`, a rendering strategy, not a modality) and
  `Project.spatial_layers` — stored as `spatialLayers` in config.json, because
  `project.dataset.layers` already means the AnnData expression matrices, and
  the two must not collide. `Project.reference_layer`, `all_layers` (the
  reference image, mask and centroids synthesized from `ImageSpec`/
  `SegmentationSpec`/the table's coordinate roles, `__image__`/`__mask__`/
  `__centroids__` reserved ids), `layer()`, `with_layer()`, `without_layer()`
  and `with_layer_order()` are the read/write API. **The reference image is no
  longer always first.** `Project.image_depth` (serialized `imageDepth`,
  omitted when 0) is how many registered layers `all_layers` draws beneath
  it — 0 is the ground, where every project starts and where all but a
  handful stay; `all_layers` inserts the reference at that depth, clamped to
  `len(spatial_layers)` because the stored depth can outlive the layers it
  was counted against. `Project.image_render` (serialized `imageRender`,
  omitted when empty) is merged into `reference_layer.render` with
  `imageKind` last so it cannot be overwritten — today it carries only the
  ground colour the image's own channels composite onto. `/config` now
  returns each project's `all_layers` as a computed `"layers"` list, added to
  a copy of the entry so nothing that later saves a project writes a derived
  key back to disk. `with_layer_order(ids)` restacks `spatial_layers`
  bottom-first under the **same partial-order rule the client's
  `LayerStack.setOrder` follows** — an id it is not told about keeps its
  place underneath rather than falling off — because the Layers panel sends
  the order it is showing and a stored order that dropped what it had no card
  for would lose it on the round trip. The mask and the centroids are still
  refused, not ignored: where those composite is `all_layers`' answer every
  time it is read, so a caller naming one believes it can move something it
  cannot. **`__image__` is the one reserved id `with_layer_order` now
  accepts**, once, and what is kept for it is `image_depth` — how many of the
  named layers it sits above — rather than a position in `spatial_layers`,
  because it is not one of them: it is synthesized, carries no transform, and
  every other layer's registration is expressed against it. Named without the
  rest, the stored depth is left alone rather than reset, following the same
  partial-order rule. `SegmentationSpec.transform` (serialized
  `segmentationTransform`, omitted when unset) is where a mask stated as
  POLYGONS — never a raster, which is in the reference frame by construction
  — records its own registration, for the one case the usual "share a
  transform with a sibling layer" rule gets wrong: a Visium HD run's cell
  polygons are in the microscope's full-res pixels while the bin layer beside
  them is in grid squares, so sharing the bin layer's transform would draw
  every cell seven times too small. `boundary_mask.geometry_for` checks it
  BEFORE the sibling-layer rule.
- `models/datasets.py` — the **dataset registry**: a dataset is a folder a
  cohort of projects lives in (a trial's forty slides, a TMA series), and
  nothing else — it holds names, not data. Lives at `<data_root>/datasets.json`,
  a SIBLING of config.json and never a key in it, because every top-level key
  of config.json is a project and a `"datasets"` key would become a phantom
  one. User root only, like `figures_root`/`captures_root` — a shared root is
  somebody else's install and the user's own grouping of what they found there
  belongs on their own machine, though a shared project can still be a member.
  **Multi-root datasets were reconsidered and refused** when the data-root
  suggestion channel went in (see the `paths.py` row): `Project.is_shared` is
  root identity, so a user's own second root would render read-only badges and
  refuse deletes, and `datasets.json` keys members by bare name with no owner
  marker. Stitching two roots together is not the fix for an account that
  should have one — the fix is that it now can only have one.
  Keyed by an opaque id (`uuid4().hex[:12]`, never a name, so a rename is
  free); names are unique after casefold. A project belongs to at most one
  dataset. `Dataset` is the frozen record and `DatasetError` (a `ValueError`,
  so it already turns into a 400 or an exit code 2) is what a bad name or a
  stale id raises. API: `datasets_path`/`load_all`/`find`/`get`/
  `find_by_name`/`resolve`/`create`/`rename`/`describe`/`remove`/`assign`/
  `forget_project`/`membership`. **Membership is pruned in the view and never
  on read**: an unmounted shared root simply has its projects absent from
  `load_all()`'s answer; rewriting the file to match on read would let one
  missing drive permanently erase a grouping, so the name stays on disk until
  the file is next written for some other reason. Writes go through
  `project.read_config`/`write_config` under `project._CONFIG_LOCK` — the
  same lock and the same temp-file-plus-rename discipline as config.json,
  because two files that can each half-write are two ways to corrupt state
  instead of one.
- `models/manifest.py` — **the one reader of "what does this project have"**.
  Nothing else may reimplement that question. `PRESENT`/`GUESSED`/`MISSING`/
  `NOT_APPLICABLE` are the four states a field can be in; `KEYS` is every
  field manifest knows about and `LABELS` names them for a UI; `GIVEN_KEYS`
  are the ones a human typed and so are never a guess (mirrors `Requires`'
  own given-keys set). `status(project, key)`, `manifest(project)` (every
  key's state), `answered(project, key)`, `summary(project)` (the compact
  shape `GET /projects` and the Open Project badges draw from: imageKind,
  segmentation, table, tableType, unresolved, needsSetup),
  `needs_setup(project)`, `open_questions(project, keys=None)` and
  `never_confirmed`. `api/plugin.py`'s `_answered` is now one line delegating
  to `manifest.answered` — the progressive-requirements machinery and the
  Open Project page now agree by construction rather than by two
  implementations staying in sync.
- `models/adapters/` — input-format layer. `base.py` defines
  `NormalizedDatasource` and `TablePlan`; `csv_adapter.py` /
  `anndata_adapter.py` / `spatialdata_adapter.py` take a `DataSpec`;
  `get_adapter(type)` is the factory and `detect_data_type(path)` routes a
  dropped path to one of them. `classify.py` is the single marker-vs-metadata
  predictor (it replaced three drifting denylists); `inspection.py` reads a
  not-yet-registered file and proposes a read spec. `csv_adapter.py`'s
  `load_table` is split into `_read_frame()` + `_normalize()`, the same
  read/shape separation `memory_adapter.py` (below) reuses for a
  kernel-supplied frame.

  **`flat_table.py` owns "the file IS the table".** `DATA_TYPES` is
  `csv, parquet, anndata, spatialdata`, and the first two are FLAT: their
  columns are the table's columns, the marker/metadata line is not drawn by
  the file, and there is nothing inside to choose between. A dozen places
  branch on that distinction — whether to copy the file into the project,
  whether to offer the column classifier or the AnnData read-spec controls,
  whether `adata.layers`/`obsm` are worth opening the file for — and every one
  of them asks `is_flat_table(data_type)` rather than comparing with `"csv"`.
  Spelled as a comparison, adding a flat format means finding all dozen, and
  the one that is missed gives a **wrong answer silently** rather than an
  error. `read_flat_table` / `write_flat_table` are the one read and the one
  write; `CsvAdapter` is registered under both keys and dispatches its single
  format-specific step on `DataSpec.type`, so a parquet and the CSV of it
  normalize identically. Guarded by `tests/test_parquet_tables.py`.
  `import_routes.replace_project_data` gained a `spatial=` kwarg —
  `{"pixel_size", "root"}` — for the one case where the flat file as shipped
  is not readable as it stands: a Xenium `cells.parquet`. When given, the
  COPIED file (never the run directory) is rewritten by
  `xenium_cells.normalise_cells_table` **before** inspection, so the header
  the roles are predicted from is the header the adapter will actually read —
  a `cell_index` column added afterwards would be one no role could name. The
  returned record is kept as `DataSpec.derived`, which is what lets
  `roles.cell_id` be set to `"cell_index"` immediately rather than asked for.
  `import_sample.register_sample` builds that `spatial=` payload via
  `_spatial_context(table, reference)` and patches `ImageSpec.pixel_size` from
  the run's own scale. `_SPATIAL_FORMATS` now also names `"visium_hd"`:
  `_spatial_context` returns `{"format": "visium_hd", "root",
  "frame_scale": reference.render.frameScale or 1.0}` instead of a pixel
  size, because a converted 10x matrix's positions are rescaled through
  `coordinates.scale` on the AnnData spec (see `AnnDataAdapter.
  _resolve_coordinates`), not through `xenium_cells`' rewrite of a flat file.
  `import_routes.replace_project_data`'s `_tenx_context(name, source,
  spatial)` converts any 10x feature-barcode `.h5` it is handed
  (`tenx_matrix.is_tenx_matrix`) into the project's `<derived>/tenx/*.h5ad`
  and registers THAT as `data_type == "anndata"`, with `coordinates =
  {"source": "obsm", "obsm_key": "spatial", "scale": tenx["scale"]}`; a
  segmented-cells matrix additionally sets `obs_id_field`/`roles.cell_id =
  "cell_id"` — the integer the cell polygons are labelled with, so a cell's
  row and its outline share an id without a lookup table, the same pattern
  `xenium_cells.normalise_cells_table`'s `cell_index` established.
  `attach_segmentation(..., transform=None)` records the mask's own
  registration as `Project.segmentation.transform` when given (see the
  project.py row) — `import_sample.register_sample` passes the layer's
  `transform` only when the mask is `boundary_mask.is_boundary_geojson`, a
  raster mask needing none. `memory_adapter.py` is deliberately **not** in
  `_ADAPTERS`/`get_adapter` -- it is reached only from the memory path
  (`plexora/memory.py`), never from a project's `DataSpec`. Its
  `MemoryAnnDataAdapter` overrides only `_open_group()` (a zarr group over a
  `MemoryStore`, the same seam `SpatialDataAdapter` already used for
  `zarr.open_group`); `MemoryFrameAdapter` overrides only `_read_frame()`. All
  the shared machinery below -- `plan()`/`stream()`, `_LazyObs`, `_node_take`
  -- runs unmodified.

  **The read is split in two, and the split is load-bearing.** `plan()` answers
  from `obs` and `var` only and never opens the matrix; `stream(plan, sink)`
  reads the matrix in row blocks afterwards. `load_table()` is now just those
  two composed, for callers that genuinely want an eager frame.

  `load_table()` used to open with `adata = ad.read_h5ad(path)` — the whole
  file, unbacked — *before* it looked at `subset`, so picking one image out of
  sixty cost more than loading all sixty. And `datasource.py` calls the adapter
  at REGISTRATION purely for the marker/metadata split and the
  obs/layer/obsm vocabularies, all of which are metadata: that is why a large
  multi-image `.h5ad` could not be imported at all. **Every user-facing
  `ValueError` about a read spec is raised by `plan()`** — that is the contract
  `tool_routes._reload_or_restore` depends on to validate an answer and restore
  the previous project without a full read. `tests/test_anndata_adapter.py::
  test_plan_never_opens_the_matrix` fails loudly if a read is put back.

  `_open_group()` is the ONE format-specific method: `h5py.File` for `.h5ad`,
  `zarr.open_group` for a SpatialData table. `read_elem`, `sparse_dataset` and
  array slicing all work against either, so everything downstream is shared.
  `AnnDataAdapter` itself now opens zarr too (`_is_zarr_source`/
  `open_anndata_zarr`, a directory or a remote address, dispatched off
  `is_remote_locator` before local `is_dir()`) — fixing a pre-existing gap
  where a local `.zarr` AnnData was opened with h5py and failed. A remote
  table is read by key through `spatialdata_adapter.remote_node`/
  `read_remote_table` — **never `anndata.read_zarr`**, which lists a group's
  children, something a non-listing host (plain HTTPS, no bucket listing)
  answers empty for. `probed_obsm`/`has_obsm` ask a handful of well-known
  names (`WELL_KNOWN_OBSM`: `spatial`, `X_spatial`, `centroids`, `X_umap`, …)
  one by one, concurrently through the chunk cache, rather than `obsm.keys()`
  — a store that cannot list has nothing to enumerate.

  Reads are sized by the subset, not the file: `_LazyObs` reads one obs column
  at a time, `_node_take` slices a column's rows **on disk** across the three
  encodings anndata actually writes (plain array, `categorical`,
  `nullable-*`), and the matrix is read dense-slab / CSR row-block /
  CSC column-wise. Markers land as float32; coordinates stay float64 (two
  columns against forty, and a centroid rounded in the seventh digit is a cell
  drawn somewhere else). Measured on a 1.2M-cell × 40-marker × 60-image file,
  loading one image: **746 MB → 338 MB peak RSS, 1.25 s → 0.43 s** (the import
  baseline alone is 295 MB, so 451 MB → 43 MB of actual data);
  `inspect_anndata` **573 MB → 312 MB, 1.19 s → 0.42 s**.

  **WIDE mode: past `WIDE_FEATURE_LIMIT` (1024, env
  `PLEXORA_WIDE_FEATURE_LIMIT`) feature columns, `load_table` returns the
  NARROW frame** — id/X/Y/obs id/celltype, no feature values — with
  `NormalizedDatasource.lazy_features=True`. A whole transcriptome (18,000
  genes over a million bins) materialised as float32 is tens of gigabytes;
  loading every column is not slow, it is impossible. Every feature name is
  still listed in `feature_columns` (the marker list, gating's channel list,
  `TableHandle.markers` all still work), but no value of any of them is read
  at load time. `read_feature_column(name)` reads one column on demand,
  subset and transformed exactly as `stream` would have: one contiguous slice
  for a `csc` matrix (what a converted 10x matrix is — see
  `server/utils/tenx_matrix.py`), a correct-but-slow scan for `csr`/dense
  (what a notebook's own wide AnnData costs). `describe_features()` reads the
  per-gene `plx_*`/`plx_hist` statistics `tenx_matrix` wrote when they are
  there and describes every gene with no value read at all; otherwise one
  column at a time through `read_feature_column`. Wide tables never hold
  features in the frame — every caller reads genes via the provider
  (`read_feature_column`/`get_filter_columns`/`TableHandle.columns`), never
  `frame()[gene]` (see `providers/local.py`, `data_model._feature_reader`,
  `centroid_tiles._load_filter_table`, `tableops.gmm`, below).
- `models/consistency.py` — **do this project's table, mask and image
  describe the same sample?** Nothing in any of the three files says they do,
  so a table paired with the wrong image, or a mask exported at a different
  resolution than the image it was segmented from, produces a viewer that
  WORKS and answers about something else. `report(project, description,
  mask_size)` is pure and returns `[{code, message}]`, worst first; the
  gathering half is `data_model.get_consistency_report` (cached beside the
  description, cleared by the same reload) and the route is
  `GET /get_consistency_report`. Findings, never errors — a legitimately
  cropped region genuinely does cover a corner of its slide, so the caller
  shows them quietly and the user decides. The mask's own pixel dimensions are
  the one input that costs a file open: `segmentation_pyramid.plane_size` reads
  the level-0 shape from metadata alone, and is the only thing in Plexora that
  ever asks — the viewer serves mask tiles in the IMAGE's coordinate system
  (`Project.all_layers` gives the mask layer the image's width and height), so
  a mask of another size is stretched over the wrong pixels rather than
  refused. Core's and not gating's for the reason cell opacity is: every plugin
  that draws per-cell results has the question.
- `models/database_model.py` — SQLite `ChannelList`, per-datasource UI state,
  now also `ViewTransform` (`{degrees, flipH, flipV}` as JSON, one row per
  datasource) — the viewer's own state, not the project's definition, which is
  why it lives here and not in config.json. Plugin state and result tables go
  through `plexora.api.store` instead, which namespaces them
  `plugin_<plugin>_<name>`. `data_model.get_view_transform`/
  `save_view_transform`/`normalize_view_transform` are the one validator for
  both the GET and PUT sides of `/view_transform/<datasource>`
  (`data_routes.py`); a missing table or a row that fails to parse reads as
  "never turned" rather than failing the page.
- `models/centroid_tiles.py` — prebuilt binary centroid records (`id/x/y`), gzipped.
  Unrelated to pixel tiles. `build_cache` writes into a
  `{cache}.tmp.{pid}.{thread}` scratch dir and sweeps stale siblings of that
  pattern left by a prior crashed build (`_sweep_stale_builds`) before
  starting, and removes its own in a `finally` regardless of outcome —
  without that a failed build was invisible until somebody found a pile of
  empty `.tmp.*` folders in the project directory. Raises `ValueError` naming
  the offending column when a non-empty table casts to zero valid rows: a
  Xenium `cell_id` like `aaaacidg-1` casts entirely to NaN, and the cache used
  to build "successfully" and draw nothing. `_load_filter_table` now falls
  back to `data_model.get_filter_columns` when a gate column is not in the
  loaded frame at all — a wide table's genes, which are never materialised
  (see the adapters row's WIDE mode) and so are read through the provider,
  cached there, the same way `plugins/gating/server/tableops.gmm` reads a
  channel through `dataset.table.columns` rather than `frame()[channel]`.
- `models/bin_tiles.py` — the store for a counted grid (Visium HD's 2 micron
  squares, and anything shaped like it): a sibling of `transcript_tiles.py`
  copied rather than subclassed, because a bin is already a count and needs no
  rasterizer's blur or density estimate. Per store tile (`STORE_TILE = 256`
  grid units a side): a dense `(offset, count)` gene index — one extra row for
  the `TOTAL` pseudo-gene — so reading one gene is two seeks whatever the
  panel size, plus the records themselves, gene-major. Stored at pooling 1 and
  every power of four above it (1, 4, 16 for a 2 micron grid) — EXACT
  power-of-two pooling from the grid origin, which is precisely how Space
  Ranger makes its own 8 and 16 micron bins out of the 2 micron ones, so the
  8 micron picture here is the 8 micron matrix, square for square; a tile
  reads the coarsest stored pooling that divides what it draws.
  `gene_stats.npy` holds a per-gene p99 window (`WINDOW_PERCENTILE`) off a
  geometric histogram (`HIST_EDGES`) for the automatic contrast. `rgb_tile`
  composites a gene-colour group RGBA and `ramp_tile` draws one field through
  a `colormaps` ramp, both drawn source-over and both taking `log=` (count
  through log1p before the window — a 2 micron square holds one or two
  molecules and an islet holds hundreds, and a linear stretch shows the islet
  and nothing else). `ramp_tile` also takes `how=` (`AGGREGATIONS = ("mean",
  "sum", "max", "min")`, default `"mean"`, via `aggregation(name)`/
  `aggregate(values, how)`): several genes' raw counts combined this way
  BEFORE the window is applied, and `_window` aggregates the genes' own
  automatic windows the identical way, so a mean of three genes saturates at
  the mean of their three windows rather than whiting out (a sum against one
  gene's window) or going dark (a min against the largest). Mean is the
  default because a heatmap of three genes should read on the same count
  scale as a heatmap of one, which a sum does not. `square_at` answers hover.
  **The frame is the grid, not the reference image** — a tile's pixel
  coordinates are the bin grid times `supersample`, and the grid's
  registration onto the reference (for Visium HD a 180-degree turn and a
  mirror) is the LAYER's transform, drawn by the viewer; nothing about it is
  baked into the store, so re-registering a layer never rebuilds it. The
  reader lives in the vendor plugin (`plugins/visium_hd/server/tenx.py`);
  `build` takes blocks of a CSR matrix with a row and column per barcode,
  which is also what lets the tests build a store with no h5 file in sight.
  `effective_pooling(manifest, level, requested=None)` now calls out to
  `requested_pooling(requested)` (the square size asked for, floored to a
  power of two) and takes the level floor's max with it; a caller that only
  wants the requested square without the level's own floor — the colour
  scale, not what gets drawn — calls `requested_pooling` directly.
  `_scaled_fields(fields, pooling, scale_pooling)` divides a coarser level's
  pooled SUMS by `(pooling/scale_pooling)^2`, putting sixteen merged 2-micron
  squares' counts back onto the ONE requested square's scale, and `auto_window`
  is measured at that same requested pooling: the invariant this buys is that
  the same count reads the same colour at every zoom, because zoom only
  changes which squares get merged for drawing, never what a colour means.
  `rgb_tile`/`ramp_tile` both take `scale_pooling=` (None measures the window
  at `pooling` itself, the old behaviour). The gutter between squares
  (`_alpha_grid`) is unchanged above one pixel per square
  (`transcript_tiles._gutter(..., min_coverage=0.0)`, the `0.0` a new keyword
  transcripts still defaults away from) but below it — the coarsest levels,
  where a square has shrunk to a single tile pixel and there is no strip left
  to draw — every pixel now pays `GUTTER_MEAN_ALPHA = (1 -
  transcript_tiles.DENSITY_GUTTER) ** 2`, the strip's average cost, uniformly;
  without this a ramp colour over the H&E changed shade between zoom levels
  as the gutter's visible fraction changed.
  Beside `rgb_tile`/`ramp_tile` there is a third drawing path, COMPOSITION:
  `composition_tile`, one glyph per square showing several genes' relative
  shares rather than one blended colour. `parse_components(text, n_genes)` reads
  `comp=` (`0|1|2:mean,3` — a group's members joined by `|` plus `:how`, a
  lone gene is just its index; strict about structure, lenient about the
  aggregation word exactly as `agg=` is) into `[(positions, how or None)]`.
  `composition_shares(fields, components)` turns that into every square's
  unit-glyph rectangles as a TWO-LEVEL SQUARIFIED TREEMAP (`_squarify`, Bruls
  et al.: items largest first, rows along the shorter side while the worst
  aspect ratio does not get worse; vectorised over every square with masks,
  ties keep component order): OUTER, one cell per component sized by its
  share of the square's raw counts (a group's share is its members
  aggregated by `how`); INNER, a group's cell is itself squarified among its
  members ALWAYS by their raw count proportion, so a group's genes are one
  rectangle together,
  whatever `how` is — the rule decides how much area a group earns, the split
  shows who is inside it, so a `min` group with one member absent earns
  nothing in that square and a `max` group's area is its strongest member's
  count but its colours are all its members'. A zero-total square is
  transparent. `_paint_glyphs` places each tile pixel by its CENTRE in its
  square's unit glyph and takes the one leaf rectangle's colour holding it
  (over a base of the square's largest leaf, so a float sliver is never black) —
  no blending, no anti-aliasing, because a composition glyph promises exact
  shares. `composition_tile` draws the full glyph once a square is at least
  `COMPOSITION_MIN_GLYPH_PX = 4` tile pixels a side; below that a glyph's
  cells would be a pixel or less and unreadable, so `_dither_leaves` picks
  each tile pixel one leaf's colour instead of blending: `_dither_threshold`
  (interleaved gradient noise, indexed by the pixel's position in the whole
  level so the pattern is seamless across tiles) gives each pixel a
  threshold in [0, 1), and the pixel takes whichever leaf's cumulative share
  first passes it — so over a patch each colour's pixel fraction equals its
  share, and the mix reads the same whether or not the glyph can be drawn.
  The old rule, filling the square with its single largest-share gene's
  colour, made a region read as turning solid red on zooming out even where
  that gene was 60% of the signal, not 100%. Shares are ratios of raw
  counts, so — unlike the ramp — they need no window and no `scale_pooling`:
  a merged square's shares are its sub-squares' summed counts, exact at every
  zoom for free. `_assemble` gained `pixels=` for this path: `colour` arrives
  already at tile resolution (a glyph) rather than one value per square, so
  only the alpha is still expanded from squares.
- `server/utils/gene_groups.py` — the read behind each layer plugin's own
  `POST /plugins/<name>/groups`: `read_upload` (file or a pasted path, the
  same two ways `/upload_channels` takes), `groups_from_grid` (a parsed grid
  → `([(group, [gene, ...])], [unknown gene, ...])`, matched case-
  insensitively and given back in the layer's own spelling, order preserved
  because a curated list has one worth keeping), `vocabulary_of` and
  `answer(files, form, names)` — the whole route in one call. Core's, like
  `views/geneGroupModal.js` that posts to it, because Transcripts and Visium
  HD would otherwise hold two copies of one parse and drift; what stays each
  plugin's own is the VOCABULARY (its own gene list) handed in as `names`.
  Reads through `channel_file.read_grid`, the same reader `/upload_channels`
  uses, for the reason it gives: a browser that sniffed a CSV's delimiter or
  unzipped an `.xlsx` itself and got it subtly wrong would report a group
  with the WRONG genes rather than an error.
- `routes/` — `data_routes` (tiles, channel stats, cells), `page_routes` (viewer
  pages, `/client/<path>` static), `project_routes` (open/edit/save/delete,
  plus the three per-layer verbs: `DELETE /project/<name>/layers/<layer_id>`,
  `PATCH` on the same address for `{visible?, render?}` — `render` MERGED so a
  card changing the colour does not drop the window somebody set a minute
  earlier, and a `null` value REMOVES a key, which is how "use the file's own"
  is said — and `PUT /project/<name>/layers/order` for `{ids}`. `DELETE`
  still refuses every reserved id with a 400 rather than ignoring it, and the
  mask and the centroids are refused by `PATCH` and `PUT` too — but
  `__image__` is not: both accept it now, `PATCH` storing whatever `render`
  arrives (minus `imageKind`, which is the file's to say) as
  `Project.image_render`, and `PUT` returning the resulting `imageDepth`
  alongside `ids`, because naming `__image__` in the order is how the depth
  is set (see `Project.with_layer_order`). The reference image's `visible` is
  not stored by `PATCH` at all — an eye is about this tab, and it is the one
  layer whose absence leaves the viewer with no world. **The `PUT` is
  registered BEFORE the `PATCH`**, because Flask matches in registration order
  and `<path:layer_id>` would otherwise swallow `/layers/order`; a layer
  literally named `order` still reaches the PATCH, and
  `tests/test_project_layer_routes.py` pins both halves. These exist because
  the Layers panel used to write every one of those choices into memory and
  none of them anywhere, so a colour, a window, an eye and a place in the
  stack all survived exactly until the page reloaded),
  `dataset_routes` (`GET`/`POST /datasets`, `POST /datasets/<id>`,
  `POST /datasets/<id>/delete`, and the ONE assign verb every move-a-project
  gesture calls: `POST /projects/assign {projects, dataset: id|null}` -- the
  Open Project page's drag-and-drop, its "Move to…" picker and its unassign
  crumb all post here rather than each inventing its own request shape.
  Registered by side-effect import in `create_app`, the same pattern as every
  other route module), `import_routes` (`POST /import/inspect` -- runs
  `import_proposal.inspect_paths` and answers with a `Proposal`; `POST
  /import/sample` and `POST /import/layers` -- the two writes, a new project
  or more layers on an existing one, that the Import Sample dialog's proposal
  step commits to; `GET /import/status` -- polls `layer_jobs` for a sample's
  progress; `/inspect_data`, and `POST /upload_data_file` -- stages a
  CSV/TSV/TXT the browser sent, 512 MB cap, answers with a path on the
  server), `browse_routes`
  (`POST /browse_path` -- a native dialog, on this server's machine by default
  or, with a `node` field, relayed to that node's, a 400+`fallback` for either
  a bad node name or one that answered "no" rather than a real relay failure,
  and a 502 reserved for a node that could not be reached at all; `POST
  /list_dir` -- one directory's names/sizes/is_dir/path, the picker that
  stands in when no dialog is possible, also taking `node` to walk that
  machine's filesystem instead, and `show_hidden`; `GET`/`POST /picker_prefs`
  -- the picker's last directory, recents (`RECENT_LIMIT=8`) and pins
  (`PINNED_LIMIT=30`), one record per machine under `path_picker.places.
  <node-or-"">` in settings.json, keyed by node name because "" is this
  server's own filesystem and `/n/scratch/aj` means nothing on the laptop),
  `tool_routes` (opening a tool and collecting what it needs),
  `data_routes` also owns `GET /resource_status` (what could not be read and
  why, plus `profiles` -- which saved connection THIS server could open, which
  is what turns the note into a button) and `POST /reload_datasource`, the only
  thing that re-reads a project: `_ensure_loaded` is keyed on the NAME, so a
  project that opened with its image missing keeps that shape for the life of
  the process and a browser reload changes nothing. `/resource_status` answers
  from TWO sources and needs both: the load-time record (`_resource_errors`),
  and `_nodes_that_have_gone` -- a registry read, no probe -- for a node that
  left the map AFTER the project loaded, which is the same keyed-on-the-name
  rule seen from the other side and the commonest way to hit it (disconnect
  between two looks at one project and the load is skipped, so the load-time
  record is still clean). It calls `ensure_loaded` first, because the viewer
  asks while it is still setting itself up and nothing it has called by then
  loads the project -- without that the route answered out of whichever project
  was loaded BEFORE. That load is wrapped: a project that cannot open at all
  (a moved LOCAL image is deliberately fatal) must still get an answer here,
  since 500 to "what is wrong?" is how a blank page stays unexplained.
  `/resource_status` also answers `masks` (`_node_mask_report`/
  `_local_mask_report`, one row or none): for a node-served mask, a single
  short-timeout `resource_status` GET against the node it lives on --
  `state`/`error`/`warning`/`progress`/`mode`/`version` -- and, the one
  exception to "no probing" in this route, a failed conversion is retried
  once per `load_generation` via `nodes.prepare_again` (`_mask_retries`,
  keyed on generation/node/id so a recurring failure is not retried every
  poll). For a LOCAL mask it says only whether the pyramid is off beside a
  read-only source folder, inferred from where `refresh_segmentation_mapping`
  already put it, never probed. `client/resourceStatus.js` watches `masks`:
  a chip via `PlexoraSegmentationWait.start({modal: false})` while `state` is
  `preparing`, its `.progress`/`.ready`/`.failed` calls as it moves, and on
  `ready` calls the reloader `main.js` registers with `onMaskReady` --
  `ViewerManager.reloadLabelLayer(version)`, which drops the label layer's
  tiles and loads it again at the new `v=` so the browser's year-long tile
  cache is never asked to reuse a raw-mask tile as a converted one.
  `serialize_and_submit_json` gzips a JSON answer past `GZIP_JSON_BYTES`
  (256 KB) when the client's `Accept-Encoding` says it takes gzip — a
  whole-transcriptome description is 18,000 histograms, 20 MB of JSON down to
  ~1 MB compressed, fetched on every viewer boot.
  `transfer_routes` (`POST /fetch_file` -- streams one file's bytes back, from
  here or, with a `node` field, forwarded from the far side chunk by chunk;
  `X-Plexora-File-Name` carries what to call it -- and `POST /put_file` --
  a multipart `{file, node, dir, name, overwrite}` write, 409 + `exists: true`
  rather than a silent replace. Deliberately its own module rather than a
  fourth route in `browse_routes.py`, whose header contract is "neither
  returns file bytes" -- weakening that next door would have been the easy
  way to add these), `system_routes`, `settings_routes` (the Settings page; `GET /data_places` --
  every machine a data field could name a file on, each carrying both `node`
  (the name a session THIS process owns opened) and `registered_node` (the
  name the registry holds, via `_registered_node_for`, which is all that is
  left after a restart); and `POST
  /nodes/<name>/resources` / `GET .../status` / `DELETE .../<id>`, which relay
  to a `--dynamic` node's own resource endpoints; see below).
- `utils/dir_listing.py` — `listing(raw, limit=LIST_DIR_LIMIT,
  show_hidden=False)`, one directory as `{path, parent, crumbs, entries,
  truncated}`. Shared, because both machines answer the same question now: the
  viewer about its own filesystem (`/list_dir`) and a node about the far
  side's (`/node/v1/list_dir`). Never opens a file. A path naming a FILE opens
  the folder that holds it, so a field's current value can be handed straight
  back as a place to open at; dotfiles are skipped unless `show_hidden`; a
  `PermissionError` becomes `ListingError("Permission denied: …")` rather than
  a bare crash, because on a cluster that is a fact about the account, not a
  bug. **Sorts the whole directory before cutting at `limit`** -- cutting
  first made the 2000 shown an arbitrary slice of scandir order, which on a
  scratch mount is no order at all -- and stats only the entries kept after
  the cut, since a stat per entry across a hundred thousand of them is a
  listing that takes a minute on NFS. Every entry carries its own `path`, and
  `crumbs` is the breadcrumb trail up to the root: **every path the picker
  navigates to is built server-side**, which is the only correct behaviour
  when the node is a Windows box and the browser is on a Mac.
- `utils/file_transfer.py` — the sibling that answers with bytes instead of
  names: `open_read(raw)` (`(path, size, mimetype, name)` for a file that can
  be sent), `safe_name(name)` (a bare filename or a refusal -- no separator of
  either flavour, no `.`/`..`, because the directory came from a picker that
  walked the real filesystem and the name came from a text box), and
  `write_file` (atomic: bytes land in a temp file beside the target and are
  moved onto it with `os.replace`, so a transfer that dies halfway leaves the
  previous file intact). `TransferError.exists` is set for specifically "there
  is already a file there" -- the one refusal a caller can turn into a
  Replace? question -- so `write_file` never silently overwrites.
  `WRITE_MAX_BYTES = 512 MB`, matching `/upload_data_file`'s cap and
  `providers/http.MAX_BUFFERED_BYTES`. Used from both machines a session has:
  the primary's `/fetch_file`/`/put_file` for its own filesystem, and a
  node's `/node/v1/read_file`/`write_file` for the far side's.
- `utils/channel_file.py` — the reader behind `POST /upload_channels`: a
  CSV/TSV/TXT or `.xlsx`/`.xlsm` into a rectangle of stripped strings, plus
  `autodetect()` (does the file say which names it holds?) and `describe()`
  (what the column picker draws). Openpyxl is imported lazily inside it.
- `utils/native_dialog.py` — the server-side native file/folder picker behind
  every "Browse…" button. `FILTER_NAMES` is the allowlist `browse_routes`
  validates against; there are TWO filter tables (tkinter and AppleScript) and
  a new filter needs an entry in both. Mode `"any"` ("a file OR a folder") is
  answered by `hybrid_available()`/`_browse_for_path_macos_hybrid()`: on macOS
  a JXA script (`osascript -l JavaScript`) drives an `NSOpenPanel` with both
  `canChooseFiles` and `canChooseDirectories` set — the only true hybrid
  dialog on any platform, since AppleScript's `choose file`/`choose folder`
  and Tk's dialogs are single-kind by construction. The hybrid panel sets NO
  file-type filter, deliberately — the path is sniffed downstream instead
  (`/inspect_data`, `check_path_existence`). Off macOS, or against a node too
  old to know mode `"any"`, `/browse_path` answers with the ordinary
  `fallback: "list"` refusal and the client opens `pathPicker.js` instead.
  Runs `sys.executable -c ...` as a subprocess (`popen_kwargs()` from
  `plexora/_subprocess.py`, so a console-less Windows process does not flash
  one for it) -- fine under the desktop app too, which ships a real
  interpreter rather than a frozen one; there the Browse buttons reach
  `desktopBridge.js`'s own native dialog first (`browsePicker.js`'s
  `browseCapability`), and this route only answers for an "Open in Browser"
  tab of the same server.
- `models/data_migration.py` — moving one data root's contents into another,
  as a background job. Nothing is ever merged (any name collision refuses the
  whole migration), a failure stops rather than carrying on, and progress is
  counted in top-level entries because a byte total means walking the tree
  before anything visibly starts. Its `can_write()` is the preview-safe
  writability probe: `paths.is_writable()` mkdirs what it is asked about and
  caches the answer, both of which are wrong for a directory a user is only
  considering. `migratable()` skips `paths.REMOTE_CACHE_DIRNAME`
  (`.remote_cache`) too — it holds nothing but bytes that can be fetched
  again, often gigabytes of it, and copying it would only make the move
  slower.
- `plugins.py` — plugin discovery and installation. Finds descriptors via the
  `plexora.plugins` entry point group and by scanning `plexora/plugins/`, then
  mounts each under `/plugins/<name>/`. **Discovery imports nothing it was not
  asked for**: names come from directory entries and entry-point metadata, so a
  core-only build never pays for an addon's dependencies. A plugin's package
  name must therefore match its declared `PLUGIN.name`. `installed(app)` is
  plugins only, still; `tools(app)` is `CORE_TOOLS + installed(app)` — core's
  own Rotate & Flip descriptor first, so a plugin can never shadow `rotate` by
  name. `find`, `tools_for` and `ready_tools` read `tools(app)`;
  `nav_items`, `installed` and `layer_sections_for` stay plugin-only, because
  core's tools mount no blueprint, carry no assets and are never a layer
  section. `tools_for`/`ready_tools` now both exclude a `Plugin.is_layer_section`
  plugin (see `api/plugin.py`'s `LAYER_SECTION_SLOT`) — it is already on
  screen, not something the Tools menu opens, and a stale `?tool=<name>`
  bookmark must not "activate" it a second time. `layer_sections_for(app,
  project)` is its mirror, gated on `requires.applies_to` rather than
  `satisfied_by`: a transcript layer whose tile cache is still building still
  APPLIES (the run has transcripts in it), and requiring readiness would make
  the section vanish for exactly as long as it had something to say.
- `core_tools.py` — the tool core ships itself: Rotate & Flip (`ROTATE`, name
  still `rotate` so `?tool=rotate` links and a sample's remembered arrangement
  keep working), an ordinary `Plugin` descriptor (`menu="view"`,
  `excluded_image_kinds=("rgb", "blank")`) with no blueprint, no assets and no
  package, so a core-only build (`PLEXORA_PLUGINS=""`) still has it. `CORE_TOOLS
  = (ROTATE,)` — turning the image and mirroring it are one question with one
  state, so one tool, one card, one View-menu row, not two that folded each
  other away. Its JavaScript and CSS are already on every viewer page
  (`services/viewTransform.js`, `views/viewTransformTools.js`, `viewer.css`),
  so `scripts`/`styles` stay empty and the panel route hands the loader
  nothing to fetch. Its panel is `client/templates/tools/rotate_panel.html`,
  named in `panels={"tool_panel_slot": ...}` like any plugin's. `CORE_TOOLS` is
  read at call time by `plugins.tools`, never bound as a default argument, so a
  test can monkeypatch it and assert on exactly the plugins it installed. The
  orientation itself lives in the per-datasource database, not on the card —
  see `data_model.py`/`database_model.py` below.

**Data nodes** (`plexora/server/providers/`, `plexora/server/node/`)

A project's three *scientific* resources -- image, segmentation, cell table --
are reached through a **provider**, which is either local (the file is here) or
node-backed (it is on another Plexora process, reached over `/node/v1/`).
Everything else a project owns -- config.json, the per-datasource SQLite, the
plugin store, figures, ROIs -- stays on the primary and is never distributed.
One authoritative database; nodes are data services with no project state.

- `providers/base.py` -- `ResourceLocator`, `Fingerprint`, the typed failures
  (`ResourceUnavailable` is the recoverable one and the only one callers
  degrade around), and `node://<node>/<resource>`, the string written where a
  path would go. **Test `is_node_locator()` before any path fixup**:
  `Path("node://hpc/cells")` is a valid relative path that exists nowhere.
  A `ResourceUnavailable` that reaches the route layer is answered by
  `create_app`'s handler -- **503 with the exception's own sentence and its
  `node`, never a 500**, because the sentence is the whole diagnosis and the
  traceback adds nothing to it. `_say_unavailable_once` prints it once per
  `data_model.load_generation` (a reload being the only thing that can change
  the answer): the viewer asks one tile at a time, so a single screenful of a
  project on a disconnected node used to print dozens of identical stacks.
  `is_remote_locator(path)` is the same idea for the OTHER kind of address a
  path slot may hold -- an `https://`/`s3://`/`gs://`/`gcs://`/`az://`/`abfs(s)://`
  string -- and it must be tested **before** any `Path()` of an image, mask or
  table source, because `Path("https://host/x.zarr")` silently folds the
  double slash and returns a path that is not the URL. `RemoteUnreachable`
  (a `ResourceUnavailable` subclass) is the network-down case for a remote
  source, carrying its own `offline` status rather than `unavailable`'s --
  the fix is different, since there is no node to reconnect. `client/src/js/views/locators.js`
  is the browser's copy of this same test (`isRemoteLocator`/`isNodeLocator`),
  regex-only and never touching the network, so the import dialog, the home
  page's path box and anything naming a dataset before the server has seen it
  ask the one question the one way.
- `providers/local.py` -- the incumbent reads. Also what a NODE runs: one
  implementation, two transports. `open()`, `_missing_pyramid()` and the
  module-level `image_geometry()` all dispatch DICOM (`dicom_wsi.is_dicom_path`)
  **before** the colour/OpenSlide branch -- a DICOM H&E project carries
  `rgb=True`, and taking the colour branch first would hand the slide to
  OpenSlide, which reads DICOM too but flattens it to RGB.
  `LocalTableProvider.lazy_features`/`read_feature_column` are the seam a WIDE
  table (see the adapters row's WIDE mode) reads through: `load()` keeps the
  adapter it built the frame from (`self._adapter`), and `describe`/
  `all_cells`/`filter_columns` all pass `self._missing_reader()` — the
  adapter's own `read_feature_column`, or `None` for an ordinary table --
  down into `data_model._describe_frame`/`_all_cells_from_frame`/
  `_filter_columns_from_frame`, which read a gene the frame does not hold
  through it rather than raising `KeyError`. `MemoryTableProvider` keeps the
  same `_adapter` reference so the memory path gets it for free.
- `providers/memory.py` -- the in-memory (kernel-as-node) provider layer.
  `Snapshot`/`TableSnapshot`/`ImageSnapshot`/`SegmentationSnapshot` wrap what
  `plexora/memory.py` copied out of the kernel; `MemoryTableProvider`
  subclasses `LocalTableProvider` and overrides only `load`/
  `read_obs_column`/`fingerprint`; `MemoryImageProvider` (which additionally
  has a `geometry()`) and `MemorySegmentationProvider` are the image/mask
  twins. `MEMORY_SCHEME = "memory://"` is node-internal only -- it is never
  written into a project's config.json, the same way `node://` never leaves
  `resources.py`'s bookkeeping. Pyramids for a kernel array come from
  `server/utils/memory_pyramid.py`: `image_pyramid()` (mean-pooled via
  `ome_zarr._reduce2`) and `label_pyramid()` (strided views, nearest-neighbour)
  both return an `ome_zarr.NgffPyramid`, so the tile route needs no third
  pathway; `_NumpyLevel` materializes slices of a lazy level-0 (dask/zarr) on
  demand, and `level_bytes()` sizes them.
- `providers/node.py` -- the primary's side of the wire. `_NodeBacked.node`
  resolves lazily (`resolve_providers` runs inside `load_datasource`'s lock and
  must not read `nodes.json` there) and **re-resolves whenever
  `nodes.address_generation` changes**. Without that a reconnect was invisible
  to an open project: the tunnel returns on a new local port, `nodes.json` is
  rewritten, and reopening the project is a no-op (`load_datasource` returns
  early for a name already loaded), so every tile, stat and GMM was refused
  against the port that had gone -- while `/remote_health`, which resolves
  freshly, called the machine Healthy and `/resource_status` reported nothing,
  because the load that cached the old address had SUCCEEDED. Only a node still
  on the map is picked up: a re-resolve that raises keeps the cached entry, so
  a DISCONNECTED node still reports itself in its own words rather than as a
  project to reopen. **Unreachability is
  raised, never swallowed**, and both places it used to be were silent
  failures: `node_for()` turns the registry's KeyError into a
  `ResourceUnavailable` (a project pointing at a node Disconnect has forgotten
  is the ORDINARY end state, and the KeyError reached the browser as a 500 on
  `/init_database` after the page had rendered); `NodeImageProvider.open()`
  re-raises `ResourceUnavailable` and only then falls back to `metadata = {}`
  (it asks for the optional OME header, so catching every `ResourceError`
  reported a dead machine as a project in perfect health); and
  `NodeSegmentationProvider.open()` now asks
  `/node/v1/resources/<id>/status` -- it has nothing to LOAD, but
  `load_datasource` is asking each provider "can this be read?", and answering
  None without asking made a mask on a machine that had gone look fine while
  every label tile 404'd. `geometry()` and `read_region()` take a `timeout` for
  the thumbnail path.
- `providers/remote.py` -- `RemoteImageProvider`, the channel image read from
  an `https`/`s3`/`gs`/`az` address through the chunk cache. **Still
  `is_local = True`** -- every computation happens in this process and only
  bytes travel over the network, so nothing here is proxied to a node and
  `data_model._remote`/`has_remote` stay False (that flag means node-proxied,
  a different thing). Subclasses `LocalImageProvider` and overrides three
  things: opening always takes the OME-Zarr branch (a web address is never a
  TIFF, a DICOM folder or a Xenium focus directory); identity comes from the
  metadata document's ETag/Last-Modified (`Fingerprint.of_remote`), not a
  stat; and the quantization ceiling is read from the coarsest level plus a
  spread sample of level-0 chunks (`WINDOW_SAMPLE_CHUNKS`, headroom
  `WINDOW_HEADROOM`) rather than every pixel of level 0, which would be
  gigabytes over a network before the first tile draws -- a store that has
  been pinned offline (see `remote_sources.py` below) reads the exact window
  instead, at local speed.
- `providers/operations.py` -- `@table_operation` / `@table_stream`. The seam
  for work that must run where the table's FILE is, because it reads the file
  and the loaded frame together (the ROI spatial join, every scientific
  write-back, the CSV export). Payload and result must survive `json.dumps`;
  refusals are returned as data, never raised across the wire.
- `providers/wire.py` -- length-prefixed frames for arrays. Numbers go raw,
  text goes as JSON: numpy's object dtype only round-trips through pickle.
- `providers/http.py` -- `request()`'s `raw_body`/`content_type` send a
  file-like object as-is rather than JSON-encoding it, so a write to a node
  streams instead of buffering a whole export in this process first;
  `allow_status` names a status that is an ANSWER rather than a failure (the
  409 a refused overwrite carries, with `exists`), so the caller reads it
  instead of matching a `ResourceError`'s message for a substring. `_check`
  now reads a failed response's body even when the caller asked to stream --
  skipping it left an error surfaced as a truncated sentence with the file
  name cut off. A 401/403 carrying a sentence that does not mention `token`
  is reported as the node said it, because a node refuses for two unrelated
  reasons with the same status -- a wrong token, and a node started without
  `--dynamic` declining to take on new resources -- and blaming the token for
  both sent people to re-register a node that was answering them perfectly
  well. `FILE_NAME_HEADER` (`X-Plexora-File-Name`) is what a
  `/read_file` answer's body cannot carry, because the body IS the file.
- `server/node/` -- the node process. No viewer, no registry, no database.
  `resources.py` keys everything by resource id because a node serves several
  at once, which is exactly why data_model's single-loaded-datasource globals
  are the wrong shape there. A resource has a `state` (`ready`/`preparing`/
  `error`); most reads are refused (`node/api._ready`) while a freshly-shared
  segmentation mask is still converting into a servable pyramid, which the
  node now does for itself off the request thread rather than requiring an
  already-converted file. `seg_tile` is the one route NOT behind `_ready`: a
  mask in `preparing` or `error` is drawn from the raw file it was shared as
  (`data_model.read_tile` -> `_label_region`, below) rather than served blank
  or 404 for as long as the conversion takes or forever if it failed. A mask
  also carries `warning` (something that worked but is worth knowing, e.g. its
  pyramid landed off to the side because its folder is read-only) and
  `progress` (`{stage, done, total}` while converting), both in `describe()`
  and both additive -- an older node/primary pair omits them. `Resource.repoint`
  bumps `generation` when a resource was already loaded (not on the first
  `add`), so a mask served raw during conversion and then repointed at its
  finished pyramid gets tiles and ETags that cannot be mistaken for the raw
  ones still sitting in a browser's cache. `Resource.memory` carries a
  `providers/memory.py`
  snapshot; `Registry.add_memory()` registers one and `_replace_snapshot()`
  swaps it under a write-lock with a generation bump, so a kernel that calls
  `refresh()` on a live viewer replaces the served objects without a client
  ever seeing a half-swapped resource; `load_table` branches on
  `resource.memory` before falling back to the on-disk path. `create_node_app`
  (`app.py`) now accepts a caller-owned `registry=` and waives the "nothing to
  serve" refusal for it -- the shape `KernelNode` needs, since a kernel node
  starts with nothing shared and gains resources only as `plexora.view(...,
  adata=...)` calls are made. `node/api.py`'s `image_geometry` prefers
  `resource.provider.geometry()` when the provider defines one, which is how
  `MemoryImageProvider`'s geometry reaches the wire without a disk read.
  Started with `--dynamic`, `server/node/api.py`
  additionally exposes `POST /node/v1/resources` (start serving a file on the
  node's own machine), `GET .../resources/<id>/status` (poll), `POST
  .../resources/<id>/prepare` (retry a failed mask conversion; a mask that is
  `ready` or already `preparing` is left alone and just described, so a
  second viewer tab asking at the same moment costs nothing), `DELETE
  .../resources/<id>` (stop; nothing on disk is touched), `POST
  /node/v1/detect` (what one path on the node's machine IS -- `{kind, mask,
  reason}` from `resources.detect_kind`, adding nothing to the registry, for
  the one caller that cannot name a kind; see the Import Sample section),
  `POST /node/v1/browse` (open a native dialog on the node's machine), `POST
  /node/v1/list_dir` (one directory on the node's machine, via
  `dir_listing.listing`, with `show_hidden` passed through), and `POST
  /node/v1/read_file`/`write_file` (`file_transfer.open_read`/`write_file`
  against THIS machine's disk; a write's directory and name arrive as query
  parameters, because the body is the payload and parsing a multipart envelope
  would mean buffering the file first). Without
  `--dynamic` all nine 403 by name, because the token holder gains arbitrary
  file reads AND WRITES on that account the moment they work. A node's
  quantization windows persist across jobs: `node/api._quantization` consults
  `<data_root>/node-quantization/<resource id>.json` (fingerprint
  `size:mtime_ns` of the served file, the primary store's identity rule)
  before scanning, so the startup warm-up costs a JSON read on every job
  after the first. Without that store the in-process cache died with every
  `srun` job, and RECONNECTING -- the natural response to a node that
  stopped answering -- restarted the very whole-image scan grind that had
  made it stop answering. **A request thread never runs that scan at all**:
  a miss in both caches answers immediately with a provisional window read
  off the in-memory pooled overview (`_provisional_window`) and queues the
  full-resolution read on the node's single scan thread (`_scan_soon`,
  demanded channels `appendleft`), which banks the result to the store the
  moment it lands. The synchronous version -- even slabbed and gated --
  wedged the node deaf to `/health` on both clusters the day 0.0.10 shipped,
  because a page restoring channels put every waitress worker behind the
  first two plane reads. Anything rendered under a provisional window goes
  out `Cache-Control: no-store` with no ETag (`_image(durable=False)`), and
  the window pair is part of the tile-cache key and the ETag, so the exact
  rendering replaces the guess on the next fetch instead of a year-long
  max-age freezing it; `/image/<id>/quantization` reports `"exact"` so a
  reader can tell. The suite runs scans inline -- a conftest autouse fixture
  sets `node/api._WINDOW_SCANS_INLINE`, and `tests/node_harness.py` exports
  `PLEXORA_WINDOW_SCANS_INLINE=1` to its subprocess nodes -- because nearly
  every assertion is byte-equality that needs the exact window on the first
  answer; the asynchrony's own tests (`test_node_warm_and_cache.py`, "the
  scan thread" section) flip it off and drive `_drain_window_scans()` by
  hand, with `_ensure_window_scanner` stubbed so the real daemon thread
  never starts inside pytest. **No request thread may run a first-time
  initialization either**: `data_model.prime_hot_code()` runs before the
  node's announce line (and in both primary CLIs before waitress) because a
  first `Image.save` (PIL plugin imports: dlopen with the GIL held, wants
  glibc's loader lock) racing a first `GaussianMixture.fit` (threadpoolctl's
  `dl_iterate_phdr`: holds the loader lock, wants the GIL for its ctypes
  callback) deadlocks the entire interpreter -- proven live on O2 with
  py-spy (identical dumps 20 s apart, 2.8% CPU, all 77 threads sleeping),
  and impossible on macOS, which has no `dl_iterate_phdr`; that asymmetry is
  why "local works, remote doesn't" pointed here. Order is tested in
  `tests/test_prime_hot_code.py` (prime < announce < warm < serve).
  `--manifest PATH` persists
  the resulting resource set (kinds, ids, paths -- never a project, a role or
  a read spec) so it is re-served identically at the next startup.
  `app.py`'s announce line also carries `platform=` (`_resources.platform_word()`,
  one definition shared with the vocabulary a workstation profile is validated
  against, so the two ends cannot drift). `--exit-on-stdin-close`
  (`_exit_when_stdin_closes`, now a thin call into
  `plexora._lifetime.exit_when_stdin_closes` -- see that row above) is the
  lifetime tie for a node launched with no pty (a Windows remote -- see
  `connect.py`): a daemon thread blocks on `sys.stdin.readline()` and calls
  `os._exit(0)` the moment it returns empty, `os._exit` rather than `sys.exit`
  because the watcher runs on its own thread and neither a raised exception
  nor `sys.exit` there would touch waitress's accept loop on the main one;
  started AFTER the announce, so a channel that closes mid-startup cannot end
  the process before it has said where it is.
  `node/api.py`'s `/hello` additionally answers `machine=_resources
  .machine_facts(disk_path=data_root)` -- cores, memory, GPUs, free disk,
  every key optional and nothing here ever raises -- additive with no
  `API_VERSION` bump: an absent key is a node too old to say, not an error.
- `server/models/nodes.py` -- `nodes.json` (0600), holding the two addresses a
  node has: how this server reaches it, and how the BROWSER does. They differ
  under an OnDemand portal and under a tunnel. `extra["managed_by"]` marks an
  entry a saved connection rewrites every session, and `extra["role"] ==
  "client"` marks the one node -- there is ever at most one -- running on the
  machine the browser is on; `nodes.client_node()` is the only reader.
  `CLIENT`/`KERNEL` are now named role constants (`"client"`/`"kernel"`), the
  latter set on a `KernelNode`'s registration. `Node.browser_reachable` is
  False for a kernel node: its address is the notebook process's own
  loopback, which means something different to a hosted browser than it does
  to this server, and treating it as reachable would carry the node's token
  to the user's laptop. `routes/data_routes.py`'s `/resource_routing` skips
  any node that is not `browser_reachable`, falling through to the proxied
  (server-relayed) tile path instead of handing the browser an address it
  cannot use.
  `plexora connect` is the only thing that sets `role`, because it is the only
  thing that can know it. `extra["expires_at"]` (with `Node.expires_at` /
  `Node.time_left`) is when the job serving this node runs out, written here
  because a node OUTLIVES the process that started it -- after a restart the
  tunnel is up, the session that knew about the allocation is gone, and this
  entry is the only thing left that knows there is a clock. `remove()` also records the `(name, endpoint)` it
  retired in the in-memory `_disconnected` set, and `providers/http.py` refuses
  that address before opening a socket: taking a tunnel down does not reach
  into the providers, warm-up threads and in-flight requests still holding it,
  and left alone each spends two connection attempts and a backoff -- plus a
  urllib3 warning apiece -- rediscovering what the disconnect already knew.
  `save()` clears the pair, which is how reconnecting on the port the last
  session used works; `http.hello` is exempt (`allow_disconnected=True`),
  because verifying an address is how it stops being disconnected. The
  endpoint is half the key on purpose: a session that comes back on a
  *different* port must not revive work that still holds the old one.
  `save()` also bumps `address_generation(name)` when the endpoint or the token
  is not what was stored -- **including when nothing was stored**, because
  `remove()` deletes the entry and so the commonest reconnect of all
  (disconnect, then connect again) writes over an absence. Exempting that as
  "a first registration has nothing cached to invalidate" is wrong, and was the
  first version of this: it is exactly when a provider IS holding a retired
  address. It is a comparison rather than "bump on every save" only because
  `record_handshake` rewrites this file after every probe to keep `last_seen`
  current. That counter is what lets a cached node notice a reconnect -- see
  `providers/node.py`.
- `server/models/secret_store.py` -- `write_private_json`: the atomic
  chmod-**before**-rename writer both `nodes.json` and `remotes.json` use. The
  ordering is the whole module; a rename-then-chmod leaves a world-readable
  window on a shared cluster filesystem.
- `server/models/remotes.py` -- `remotes.json`, saved remote servers. Field
  names are `connect.Session`'s parameter names so `as_session_kwargs()` is a
  rename-free hand-off. **No password field exists**, deliberately. `srun` is
  three-valued: `None` (no scheduler), `""` (site defaults), a string.
  `as_node_kwargs()` is the second hand-off, to `connect.NodeSession`: it
  carries everything that describes REACHING the host -- `remote_command`,
  `srun`, `bind_node`, `jump`, `ssh_opts`, `plugins`, `node_name`. **The
  profile is the source of truth and a data node inherits all of it**,
  `srun` included: serving tiles is sustained read I/O, and a site that
  keeps Plexora off its login nodes means it for that too. Only what
  configures a viewer that is not being started stays behind (`datasource`,
  `data_dir`, `forwards`), plus `serve`. `data_dir` is a *suggestion* for
  where the far account keeps its Plexora work, sent as `--data-dir-default`
  and adopted only into a vacuum -- never an override, and never passed to a
  node for the same reason it stays behind here. `install` crosses over for the same
  reason: **the one field on a profile that makes connecting WRITE to the far
  machine** -- `pip install --upgrade plexora` before anything is launched --
  and it is off by default and written to the file only when switched on.
  There is deliberately **no separate conda-environment field**: the launch
  command already names the environment, and
  `connect.install_command_line()` reads it (see below), so a second box
  would be two answers to one question with the launch and the install free
  to disagree. A Google Cloud profile carries its project/bucket/machine
  choices under `extra["gcloud"]` -- the `Remote.gcloud` property reads it,
  and `_gcloud_kwargs()` (folded into both `as_session_kwargs()` and
  `as_node_kwargs()`) turns it into `connect.Session`'s `gcloud=`/
  `mount_command=` by calling `plexora.gcloud.prepare_command_line()`. The
  no-secret rule holds here the same way: what rides in `extra["gcloud"]`
  describes a connection, never a way into one -- the Google credential stays
  in `gcloud`'s own store. A workstation profile carries `extra["workstation"]
  = {"os": ...}` the identical way -- `Remote.workstation` reads it, validates
  the OS against `WORKSTATION_OS` (`_resources.PLATFORMS` re-exported here so
  the profile and the node's own announce cannot drift onto different
  vocabularies) and returns None for anything else, so an unrecognised or
  hand-edited word can never reach a command line; `_workstation_kwargs()` ->
  `{"remote_os": ...}`, folded into both `as_session_kwargs()` and
  `as_node_kwargs()`, since which shell is over there is a fact about the
  machine rather than about which half of the connection is opening.
- `server/models/remote_sessions.py` -- live connections, one daemon thread
  each. **Two kinds**, `KIND_VIEWER` (Plexora over there, browser tunnelled to
  it) and `KIND_NODE` (Plexora stays here, only the far side's files come
  over). Both can be live for one profile at once, so `_key()` namespaces them
  -- the viewer keeps the bare name it always had. States
  `preparing_compute/connecting/authenticating/mounting_data/installing/
  waiting_for_job/tunneling/waiting_for_app/connected/failed/exited`; phases
  come from
  `Session.on_phase`, not from matching echoed text (the queued-job line is
  only printed five seconds in). `installing` exists only for a profile with
  `install` on, and it is a state rather than a background errand because it
  is minutes long, it writes to the far machine, and it is the step most
  likely to be the one that failed. `preparing_compute` and `mounting_data`
  are the Google Cloud preset's own two: `RemoteSession._prepare_compute()`
  runs `gcloud.ensure_instance()`'s ladder before `_build`, and the bucket
  mount is a chained step the same shape as install. **A new state has to be
  added in FIVE places: `OPENING_STATES`, `PHRASES`, `_on_phase`'s map,
  `remoteState.js`'s `OPENING`/`LABELS`, AND `connectionModal.js`'s
  `STEPS`** -- one missed and a connection mid-pip (or mid-mount) reads as
  settled, or the dialog never shows the step at all. `redact()` strips
  `token=`/`password=` from every served log line. Secrets live in
  `_Prompt.answer` and are handed over exactly once. **One connection
  authenticates three times** -- the job, the login node again as a jump
  host, then the compute node -- so a repeatable answer is kept in
  `_secrets` for the length of ESTABLISHMENT and replayed, and the person
  types once. Guarded twice: `prompt_secret_kind()` replays only a
  password or a key passphrase, never a one-time code, a `(yes/no)`
  host-key confirmation, or wording it does not recognise; and one ssh
  asking the same thing twice counts as a refusal, so the cached answer is
  dropped and the person is asked. Telling a second hop from a second
  attempt needs `askpass.asking_process()` (the ppid, which is the ssh
  itself because the POSIX wrapper `exec`s), since the two hops to the
  login node ask identically. `_forget_secrets_locked()` closes the window
  on connected, failed and stopped. `node_name` is the
  profile's own `node_name` when it has one, its session name otherwise --
  `status()["node"]` reports it for a `KIND_NODE` session rather than the
  profile name, so a node registered under a different name is still the one
  Settings' `_forget_node` matches on disconnect. **The job's clock** is
  `time_limit` (from `recipes.srun_seconds(remote.srun)`), `job_started_at`
  (stamped by `_start_the_clock_locked` on the transition OFF
  `waiting_for_job`, and again at `connected` for a job that never queued --
  queue time is not allocation time), `expires_at` and `time_left`.
  `expires_at` is None unless the session is LIVE (opening or connected):
  disconnecting stops a session but deliberately keeps its record, and a
  deadline computed from `job_started_at + time_limit` alone went on counting
  down for a connection the user had closed on an allocation cancelled with it.
  `status()` reports `time_limit` and `time_left` as DURATIONS rather than a
  deadline, so a browser whose clock disagrees with this machine's still counts
  down correctly. `_register_node` is the wrapper that carries `expires_at`
  into the registry entry when the node announces. **`_tidy_after_end()` is
  the teardown for a session nothing will ever press Disconnect on** — run
  when establishment fails, or when `wait()` returns without `stop()` having
  been called (a walltime, a dropped network, a crash on the far side). It
  stops sibling watchers (under `srun` the tunnel is a second ssh that the job
  leg exiting does not end), removes the askpass helper dir, and calls
  `self.drop_own_node()` — a `KIND_NODE` session whose `session.registered` is
  set calls the route-supplied `unregister(node_name)` through it, so the dead
  entry leaves
  `nodes.json`; left standing, it kept `/resource_routing` offering a dead
  address and `/resource_status` reporting the project fine while every tile
  timed out. A **deliberate** `stop()` skips the tidy on purpose — the
  disconnect route forgets the node itself, and skips `unregister` while doing
  it (see below). `_shut_down_all()` (atexit) now also calls
  `session.drop_own_node()` on every session after `stop()`, since `stop()`
  itself never unregisters and at exit there is no disconnect route to do it
  either — the entry used to survive the app quitting, naming a loopback port
  nothing would ever listen on again, and the NEXT run opened a project
  reading from it with a warning about a connection nobody had made in that
  session (the browser only calls a node disconnected if it saw it up in the
  same tab — see `resourceStatus.js`'s `upThisSession` above — so a hard kill
  that skips even `_shut_down_all` is tolerated, just not this clean an exit).
  `start()` takes the `unregister=` callable and now calls
  `existing.stop()` before replacing a dead (failed/exited) session, closing a
  `connect._ACTIVE` watcher leak a bare dict overwrite used to leave behind.
  `_release_compute(after_failure=False)` is the Google Cloud preset's own
  teardown and is called from BOTH `stop()` and `_tidy_after_end()`, so **all
  five** ways a session can end (the Disconnect button, a failed connect, a
  connection dying on its own, an atexit handler, and the VM's own idle timer)
  now consult the profile, where before only the HTTP disconnect route did.
  After a failure it **stops** only a VM this attempt created or started —
  never one that was already running when the attempt began, and never
  deleting even when the profile says Delete, because that disk holds the two
  logs the next connection prints to explain the failure. After a normal end
  it does what `gcloud.exit_action(record)` says: leave it running, stop it,
  or delete it. Never deletes for `vm_source="existing"` — `exit_action`
  refuses to return Delete for one, `gcloud.profile()` will not store it, and
  the failure branch checks ownership again on its own. A profile's
  viewer and node sessions share one VM, so `_other_live_session()` stops
  `_release_compute` from switching off the other session's machine when one
  of the two ends first. `status()` also carries `platform`/`os_mismatch` off
  a `KIND_NODE` session (see `NodeSession._check_platform` above), and
  `_diagnose` gained two free functions consulted on a failure, beside
  `connect.scheduler_refusal` and for the same reason -- a judgement about a
  profile and some output with no session state in it: `unreachable_advice`
  (nothing answered at all -- `_UNREACHABLE` markers -- with a checklist item
  that depends on the profile's `workstation.os`, since a workstation is, far
  more often than a cluster login node, simply asleep or unreachable rather
  than misconfigured) and `os_mismatch_advice` (a shell's own "no such
  program" wording -- `_WINDOWS_SHELL` vs `_POSIX_SHELL` -- read against what
  the profile claims the machine is). Both run BEFORE
  `looks_like_missing_command`: the cmd.exe marker for "no such program" is
  itself one of `MISSING_COMMAND_MARKERS`, so a Linux box answering a
  Windows-quoted command line would otherwise be diagnosed as a PATH problem
  when the operating system on the saved profile is what is wrong.

`data_model` dispatches on one module-global boolean (`_remote`), set under the
load lock. It is False for every project with no `resources` block -- which is
every project that predates this -- so the single-server path costs one global
read and one branch, and the warm-tile path is untouched.

**Settings** (`/settings`, `settings_routes.py` + `client/templates/settings.html`)

A left rail of sections; `SECTIONS` in the route module is the only list and
the rail is generated from it. Four sections today: the data directory, saved
remote servers, the data-node address book, and Web data. Adding one is that
tuple plus a `<section>` in the template plus a prototype in `settingsPage.js`.

**Web data** (`GET/POST /settings/webdata`, `models/remote_sources.py`) is the
chunk cache for images and tables read from a web address, named "Web data"
rather than "Remote data" deliberately -- "Remote servers" already exists two
rows up and means something else entirely (an SSH machine, not a URL). It
shows usage against the budget, lets the budget be changed
(`remote_sources.set_budget`), lists sources with `clear`/`pin`
(`/webdata/pin`, "Make available offline")/`unpin`/`remove_source`, and polls
`/webdata/jobs` for the warm and pin background jobs. `set_budget` raising
`OverBudget` is what a pin request over the remaining budget answers with.

**Neither section configures where data lives any more.** Remote servers stores
reusable SSH connection profiles and nothing else — the `serve` / `local_serve`
/ `node_name` boxes are gone, because filling them in meant naming, before
Plexora started, the path of a file you were about to go looking for. Data
nodes is a status board: most entries now appear and disappear on their own
(a data field's Remote option opens one; `plexora connect` opens one on the
laptop), and the manual add is behind a disclosure as the exception it now is.
**Remote servers has no hand-written form of its own any more** —
`settings.html`'s whole `.remote-card` (the `settings_remote_*` boxes, the
Advanced disclosure, Save/Cancel, the "Use preset…" button) is gone, leaving
one empty `.settings-recipes` slot that `settingsPage.js`'s
`RemotesSection.drawCatalogue()` fills from `connectionModal.js`'s
`recipeGrid()` — the same two card grids the modal draws for "Add a server"
anywhere else (the five shapes, then "Additional presets" over the two named
institutions — see `recipes.institution`), now fetched once per Settings load
rather than once per dialog. `.settings-recipes` sets `color:
var(--text-primary)` and that is **not decoration**: `.connect-recipe` is a
`<button>` saying `color: inherit`, which inside the dialog reaches
`.connect-modal`'s own colour and out here reached a `<body>` that sets none —
so every card TITLE fell back to the browser's default button text (black) on
a near-black card while the blurb, which names its own colour, stayed legible. `edit(remote)` is one call, `openRecipe(remote.recipe, remote)`,
which opens `PlexoraConnectionModal.open({view: "recipe", recipe, remote})`
straight onto that one preset's form, prefilled from the saved profile,
skipping the catalogue entirely — the presets are not a starting point
offered beside a form, they ARE the form, for adding and for editing alike.
`_remote_payload(payload, name, existing)` **preserves** the dropped fields
from the stored record, so a profile written by `plexora connect --save` does
not lose them when somebody edits an address in the UI. It also accepts a
`gcloud` key straight out of a recipe's `compose()` body into `extra` (a
`workstation` key the identical way, beside it — `Remote.workstation` is what
validates it, so a hand-written body cannot put an unchecked OS word where a
command line will read it), a bare `extra["recipe"]` string (`Remote.recipe`,
read back by `recipes.for_remote` — unvalidated here, since whether it still
names a real preset is that function's question, not this route's), and
`_remote_view` reports both back out, plus `description` — the composing
recipe's `summary`, resolved server-side because the card is drawn from `GET
/settings/remotes` and making it fetch the catalogue as well would be a second
request to say "cluster".

**Saved connections are a boxed grid, and a card says four things.** The
panel is two objects: a `.settings-remotes-box` (its own `--surface-1` ground,
a border, a `shadow-sm`, the title "Saved connections" and a one-line helper)
holding the grid, and then "Add a server" below it on the page's own ground.
They were a grid, a heading and another grid on one flat background, which
read as one long list in which the presets looked like more saved servers with
the wrong buttons on them. `.settings-remotes` inside it is a `repeat(3,
minmax(0, 1fr))` grid (two columns under 1080px, one under 720px) with
`align-items: **stretch**` — `start` let a card grow on its own, which is right
for a password prompt or an opened log and wrong the other ninety-nine percent
of the time, where it left three cards holding identical content at three
heights because one description wrapped. Equal heights across the row, and
`margin-top: auto` on `.settings-remote-actions` puts Connect on the bottom
edge of all of them; `.settings-remote-card` is `--surface-2`, one step
lighter than the box, so it reads as a card in a container. The description is
`-webkit-line-clamp: 2` with a two-line `min-height`, so a long one cannot set
the row's height and a card in the second row still lines up with one in the
first (and `white-space: normal`, because `.settings-meta` sets `pre-line`,
which a clamped box must not honour).

Each card carries the name, that `description` line, a status DOT, and
Connect — **and nothing else at rest**. The dot keeps the
`settings-node-state is-*` classes and paints them with `background:
currentColor`, so its colour has the same one definition as every other
machine on the page, and its word lives on `title`/`aria-label` because a dot
cannot be read aloud. Edit and Delete are `iconButton()` pencil/bin controls
in the head rather than buttons in the action row — Connect is what a card is
FOR, and as three (or six, on a cloud profile) equal buttons the card had no
primary action at all; Delete carries `is-danger`, which is grey until hovered
because a bin that is red all the time is an alarm on a page where nothing is
wrong. **Deleting confirms for every kind of profile**, not just a rented one:
that was defensible for a button captioned "Forget" and is not for a bin icon
eight pixels from a pencil.

**Nothing configured reaches the face, and nothing reaches a tooltip either.**
The address, the bucket, the region, the machine type, the OS word, the
environment pip writes to, "installs Plexora in <env>", the cloud exit action
and "Serving files to this Plexora as …" have all left the card. The
intermediate design put them on the description's `title` via `detailLine()`;
that function is gone, because a hover that shows configuration is still
configuration on the card, it is just configuration nobody can find. The
description's `title` now repeats the description itself, which is what makes
a clamped line readable. All of it is asked for, and read back, on the recipe
form behind the pencil. `settingsPage.js` names the machine on
the card by its OS word (`OS_WORDS`: `windows` → "Windows", `macos` →
"macOS", `linux` → "Linux" — the stored value is a lower-case identifier for
a command line, not a label) and, separately from `error`, draws an `osNote`
slot for a working connection whose machine disagreed with the profile
(`half.osMismatch`) — its own slot because the connection SUCCEEDED, and
writing that into the error slot would read as a failure it was not.
Disconnecting a Google Cloud remote honours
`gcloud.exit_action(record)` (v4 of `gcloud.profile()`'s schema replaced the
`stop_vm_on_disconnect` boolean with `on_exit`: `leave`/`stop`/`delete`,
default **stop**, so a fresh rented-VM profile stops billing on its own unless
somebody chooses otherwise — and Delete now genuinely deletes rather than
being a UI option nothing acted on), and Settings shows the VM's state on the
card, its machine type with `spot` beside it, and which of the three endings
it is set to.
The VM's state is on the `title` of the buttons that change it — "This machine
is running." — rather than on a row of its own (`VM_STATE_WORDS` via
`askVmState`/`paintVmState` in `settingsPage.js`); WHICH of Start and Stop is
offered already says which way the machine is, and `VM no VM yet` on a card
read as debug output. It offers Start VM, Stop VM or Delete VM… accordingly, plus
a notice slot, that call `GET/POST
/settings/remotes/<name>/vm{,/start,/standard,/stop,/delete}` (`gcloud_routes.py`,
alongside `GET /settings/gcloud/{status,projects,buckets,bucket,zones,instances}`
and `POST /settings/gcloud/auth`, which back the recipe form's own lookups --
`instances` backs the bring-your-own picker). `vm/start` starts a stopped VM
without connecting to it and is the one VM verb that does NOT end live
sessions first, because there are none to end -- a stopped VM has no session.
`vm/standard` is the odd one out among the five: it is reached only from a
failed connection's "Reconnect with Standard" button (`connectionModal.js`),
not from the Settings card, and it neither ends a session nor touches Compute
Engine -- it only flips the saved profile's `provisioning_model`, refused for
`vm_source="existing"`.
Stop and Delete both end this profile's sessions first
(`gcloud_routes._end_sessions`) and both keep the SAVED PROFILE — Stop leaves
the disk and costs only that; Delete removes the VM and its boot disk but
never the bucket, and connecting again simply builds a new VM against the same
data. `POST …/vm/delete` 400s outright for a `vm_source="existing"` profile,
before it ever reaches `gcloud.delete_instance`'s own label check. Delete VM is
also hidden on the card for `vm_source="existing"`, and for a VM that no
longer exists. Forgetting the profile itself is a different button.
`vmStatus` is fetched on demand only -- once per card, plus a re-check after
any VM action or a disconnect -- and deliberately never rides the page's 1 Hz
poll, since that would be one `gcloud` subprocess per cloud profile per
second; a connected session is treated as proof the VM is running, with no
round trip. `services/remoteState.js`'s `vmStart` verb does not call
`refresh()` afterward the way `vmStop`/`vmDelete` do, since starting a VM
changes no saved profile or session.

`server/models/recipes.py` also owns the walltime: `split_srun`/`join_srun`
are the form's three boxes over one stored string, and
`walltime_seconds`/`srun_seconds` read a `-t` value into seconds for every
countdown. Slurm's `-t` is genuinely ambiguous and the rule is not optional --
a bare number is MINUTES, and it is the day separator that makes the colon
groups hours (`30` and `30:00` are both half an hour; `1-2` is a day and two
hours). Anything unparseable, absent or `UNLIMITED` comes back None, all three
the same way: a countdown must never invent a deadline, because somebody told
they have twenty minutes left on a job with no clock saves and reconnects for
nothing.

`server/models/recipes.py` is the "Add a server" preset catalogue behind
`services/connectionModal.js`'s recipe flow, reached through `GET
/settings/recipes` and `POST /settings/recipes/<id>`. `Recipe` dataclass, the
`RECIPES` tuple, `all_recipes()`, `find()`, `compose()`. Seven presets, in
order: generic SSH, generic Slurm, a **workstation** (`site=False,
tested=False`, so no badge — it asserts nothing about a specific machine,
only about the three operating systems Plexora has words for), Google Cloud
(`site=True, tested=True`: unlike AWS it is not a shaped-from-documentation
ssh target at all, it drives `plexora.gcloud` for real, through the user's own
`gcloud` sign-in, to create and mount a VM, and it has been run end to end
against a real account), AWS (`site=True, tested=False`, shaped from
published documentation — the only preset still carrying the badge), then
HMS O2 and MGB / BWH ERIS (`mgb-eris`, ERISTwo — both pinned to observed
behaviour).

**`institution` is what the order is FOR, and is not the same flag as
`site`.** The five shapes come first and the two named clusters last because
`recipeGrid()` draws them as two grids — the shapes, then a disclosure
("Additional presets"), then the institutions — and it filters rather than
sorts. A shape (any ssh host, any Slurm cluster, a workstation, either cloud
account) fits everybody who will ever open the dialog; a named cluster fits
the people with an account there, and in one grid of seven those two took a
seventh of the attention from the five and read to everybody else as evidence
this was a tool for somebody else's institution. **Both cloud presets are
shapes by this reading and `site=True` by the other** — anyone can open an AWS
account, so it belongs on the first screen; Plexora still asserts things about
it that could be wrong, which is what `site` (and therefore the untested
badge) is about. `test_a_named_institution_is_exactly_a_preset_that_fixes_the_
address` keeps the flag honest: for every non-`flow` recipe, `institution` is
true exactly when `target_template` has no `{host}` — a preset that hard-codes
somebody's login node, versus one that asks. Within the shapes the order is
increasing commitment: an ssh host you already have, a scheduler, a machine on
your desk, then the two clouds that bill you.

`Recipe.summary` is a second, much shorter description beside `blurb`, and the
two answer different questions on purpose. `blurb` is the sales pitch on a
catalogue card, read by somebody CHOOSING. `summary` ("Slurm compute cluster",
"Google Cloud VM", "Harvard O2 compute cluster") names the kind of machine on
a SAVED server's card in Settings, read by somebody who chose months ago and
wants to know which of their machines this is — so it has to fit on one line
of a card a third of a column wide. `to_dict()` serialises `summary or label`,
so a recipe that never sets one still names itself.
`unverified
= site and not tested` is what renders the badge — presenting a guess with the
same confidence as a verified fact is how somebody spends an afternoon on a
partition that never existed. Composing happens **server side**, through
`POST /settings/recipes/<id>` → the same `_remote_payload` save
`POST /settings/remotes` uses — a recipe is a filled-in form, not a second
way to write a profile, and in particular there is still nowhere in one to
put a password. `compose()` reads the switches off the RAW answers and the
boxes off the trimmed ones: `str(False or "")` is `""` and `str(True or "")`
is `"True"`, so a boolean through the text pass is true in one direction and
empty in the other — which is why `compose()` reads `data_dir`, `forwards`,
`bind_node` and `install` off the raw answers too rather than the trimmed
ones: the trim pass would turn a ports LIST into a stringified one. `compose()` also
stamps `body["recipe"] = recipe.id`, so a profile always remembers which
preset composed it. `split_target(recipe, target)` is the inverse of
`Recipe.target_template` — a fixed-host template gives its own host back, an
address with no `@` is all host — and `for_remote(remote)` uses it to answer
"which preset's form edits this profile": the stored recipe id first (unless
its fixed host no longer matches the profile's target), then gcloud, then
workstation, then a site recipe whose template host matches, then slurm or
ssh by whether there is a scheduler. It never answers `""` — `ssh` fits any
host and is what "no idea" looks like.

The **workstation** preset is placed BEFORE the generic SSH one and is
deliberately NOT a `flow`: `test_a_preset_with_its_own_flow_composes_its_own_
address` pins that a flow means an empty `target_template` and an empty
`ask`, and this preset wants the ordinary username/host boxes PLUS one more
question, added through the `ASK_OS = "os"` vocabulary entry rather than
through a flow of its own. `srun=None` (no scheduler -- a workstation has
nothing to queue for), and `extra` carries `os_choices` (the per-OS Settings
hint, e.g. that Windows ships OpenSSH Server disabled) and `default_os` --
riding down with the recipe rather than costing a route of its own, the same
arrangement as Google Cloud's machine-type catalogue. `compose()` validates
the answer against `WORKSTATION_OS` when `ASK_OS in recipe.ask` and emits
`body["workstation"] = {"os": ...}`, under its own key rather than the top
level so it lands in the profile's `extra` the way the Google Cloud record
does. The plain-SSH preset's blurb was reworded to defer a workstation to
this card, with a note pointing back at it.

**`Recipe.install` is the one default that writes to somebody else's
account, and `mgb-eris` is the only preset that sets it.** It rides through
`to_dict()` and through `compose()` on the same membership rule `bind_node`
uses — `bool(raw["install"]) if "install" in raw else recipe.install` — so an
absent key is a caller that never drew the switch rather than somebody
answering no. The bar for turning it on is deliberately higher than for any
other field: the site must be `tested=True` (a preset shaped from
documentation cannot know whose account it would write into), and its
`remote_command` must resolve to an environment that site's users own rather
than a module the cluster provides. ERISTwo meets both; O2 does not set it,
and no generic shape may. It matters because **a failed install aborts the
connection** — `connect._await_install` raises `ConnectError` — so a preset
that guessed wrong would turn working connections into failing ones. Two
things contain that: `connect._install_failure` names the fix in prose
(including "turn *Install or update Plexora* off"), and `recipeForm` opens
the Advanced panel on arrival when `recipe.install` is set, so the switch is
readable before Connect rather than one click behind a summary.
`mgb-eris` also carries `bind_node=True` — ERISTwo refuses the second ssh into
the compute node its job landed on, so a connection with it off queues, gets a
node and dies at the last hop. `tests/js/connection_modal_probe.mjs` pins both
switches arriving on and reaching the POST untouched; `test_recipes.py::test_
the_mgb_preset_forwards_from_the_login_node_and_installs` pins the server half
and that no other recipe sets `install`.

`recipeForm(recipe, saved)` is the ordinary (non-`flow`) form every other
preset draws, built on the same `formFields(boxes)` factory as
`gcloudForm`. Beside the username/host boxes it carries three controls that
used to live only on the retired Settings form: `data_dir` (labelled "Data
directory on this server", in the main body, not behind Advanced, because
where Plexora keeps projects over there is the third thing anyone adding a
server knows, after what to call it and where it is -- a suggestion used only
the first time that account runs Plexora, never a browse-folder default: a
field once read as one got filled with wherever the images happened to sit,
one path segment away from the directory that account's own `plexora dataset
create` wrote to, and a cohort vanished with no error on either side);
`forwards`, a `portsField` — one box, an Add button and chips, never a
textarea, so a list stays a list all the way to `compose()` (see above); and
`bind_node`, a switch drawn only when `recipe.srun !== null`, since forwarding
from the login node is a question a scheduler creates and a plain SSH target
has no login node to ask it about. Both switches take their starting
position from the preset — `saved ? saved.X : recipe.X` — so a site's answer
is what an untouched form sends. `pickField` gained `choose(value)` so
these — and the Google Cloud bucket/VM pickers below — can be filled in from
a `saved` profile on an edit, the same prefill `recipeForm`/`gcloudForm` do
for every other box.

Google Cloud is also the one preset with a bespoke form rather than the
standard username/host boxes: `Recipe.flow` (`FLOW_GCLOUD`, read off
`extra["flow"]`) tells `connectionModal.js` to draw `gcloudForm(recipe)`
instead, built on a shared `formFields(boxes)` control factory. It is **four
pages with Next/Back**, in the order the answers depend on each other:
**Google Cloud** (sign-in, name, project) → **Data** (bucket, mount location)
→ **Compute** (create-new vs use-existing, machine type, Spot/Standard,
region and zone, plus Advanced) → **When Plexora exits** (leave/stop/delete).
Every control on all four pages is built once and only ever hidden, which is
the whole of "going back loses nothing"; the strip at the top is buttons, and
a page already reached can be jumped back to while one not yet reached is
disabled. `blocker(index)` is why a page will not be left, in a sentence
beside the button that will not go — a disabled button whose explanation is
the control that has been switched off is the commonest way a form becomes
unusable. The three-way questions are `choiceField`, radio rows with the
selected option's consequence written under the group. `Recipe.extra` also
carries the curated machine-type, region, provisioning-model and exit-action
catalogues the form is drawn from, riding down with the recipe rather than
costing a route of their own — the prose about what Delete VM actually does is
server-side for the same reason everything else here is.
`compose()` branches on the flow to `_compose_gcloud()`, which requires a
project and a bucket and returns a body carrying `target=<vm name>`,
`data_dir=<mount path>` and a `gcloud` key — still through the same save as
every other preset, so the no-password invariant holds for it exactly as for
the rest. `_compose_gcloud` resolves `vm_source` BEFORE region/zone, because
it decides which of the two may determine the other: for `vm_source="existing"`
the VM's own zone wins and the region is derived from it — a machine that
already exists is somewhere, and that is a fact, not a choice the form gets to
make — so the "zone must be in region" refusal applies only to the rented
path. If no zone is given for an existing VM, `gcloud.zone_of_instance`
resolves it, so typing a bare instance name is enough.

`/settings/remotes*` drives `remote_sessions`: connect answers **202** and the
page polls, because an srun connection legitimately waits a quarter of an hour
in a queue and a route that waited with it would pin a Waitress worker. They
take `?kind=node` to open a data node instead of a viewer — same profile, same
askpass, same polling — and `disconnect?kind=node` also forgets the node entry,
but only one whose `managed_by` proves this route created it (resolved by the
session's own `node_name`, not the profile name, since a node reports the name
it is actually on the map under). The connect route also passes an
`unregister=_forget_node_entry` callable for a `KIND_NODE` session, which is
the other half of `register`: when a node session ends on its own instead of
through this route, its own `_tidy_after_end()` is the only thing left that
knows to take the entry back off the map. `_forget_node` (called on a
deliberate disconnect) and the session's own teardown now share one
implementation, `_forget_node_entry(node_name)` — same `managed_by` guard,
just resolved from a node name instead of a profile name, since the session
has no request to resolve a profile from. The two `_askpass` routes are authenticated
by the session nonce and carry the app's own auth token, so they need no
exemption from the rule that nothing is exempt. `GET
/settings/remotes/<name>/status` takes `?log=N` (`_log_lines()`, clamped to
`remote_sessions.LOG_LINES`): the list of every profile carries a short log
tail, a surface watching ONE connection — `services/remoteState.js`'s focused
fetch — asks for the whole buffer instead. `/data_places` carries the last
eight lines per profile for the same reason, so a card has a terminal to draw
before anyone has focused it.

`GET /remote_health` is the only health probe in Plexora: for each profile
with a live node session it times one `http.hello` (`HEALTH_TIMEOUT = 4.0`)
and reports `{state: healthy|stale|unreachable|unknown, ms, detail}`, keyed by
PROFILE name and probed by NODE name. `stale` is checked FIRST and contacts
nothing: `data_model.held_node_addresses()` (→ `ProviderSet.held_addresses`,
which reads each provider's cached `_node` and never resolves one) says where
the open project is actually sending its requests, and if that is not the
registry's current endpoint the probe would otherwise report a machine as well
while the viewer failed to read a single tile from it. `remoteGlobe.js` renders
it as "Reconnected", which is neither of the other two words: the machine IS
answering, and the server DOES know what is wrong. **Asked for, never polled** — the
navbar panel calls it once when it opens. Session state is what Plexora
*did*; whether the node answers now is a different claim, and a background
poll of it would be a second opinion that disagreed with the session state at
a moment nobody was watching. A profile with no node open is not contacted at
all.

Since `RemoteSession._tidy_after_end()` unregisters a node that dies on its
own, a walltime death now removes the registry entry rather than leaving a
stale one behind — so `/resource_routing` stops offering that address and
`/resource_status` reports the layer missing outright (this is the case the
reconnect modal fires for), instead of the machine answering "stale" forever.
The browser side of the repair lives in `main.js`: `repairRouting()` (exposed
as `window.__plexora.repairRouting`) calls `PlexoraRouting.refresh()`,
re-applies routing over the `origSrc` every channel stashed at boot, and
rebuilds tile layers only when the resolved routes actually changed. It runs
on the `plexora:remote-nodes-changed` window event — fired by
`remoteState.js`'s `publish()` when a snapshot diff shows a profile's node
half changing being-up, map name or registry name, and by `remoteGlobe.js`'s
`staleNodes()` as a backstop for a reconnect made from another tab — and on a
30-second-throttled `tile-load-failed` handler, for the case nothing else in
the tab was watching.

**One connection concept.** The machine Plexora runs on is Local; anything
reached from it over SSH is Remote. Nothing in the app opens a `KIND_VIEWER`
session any more: Settings' Connect used to run Plexora on the far machine and
tunnel the viewer back, which made the Settings page somewhere the host
Plexora runs on could be redefined from inside the running app. That
capability lives in `plexora connect` on the command line, which reads the
same saved profiles — which is why the form's advanced fields (`datasource`,
`forwards`, `plugins`) are still there. The server-side viewer kind and its
routes are unchanged and still tested; what changed is that no UI creates one.

**Changing it records a preference; it never repoints the running process.**
`data_root()` resolves once per interpreter and data_model is holding an open
image against it, so `paths.reset()` here would fail as a stack trace from
whichever tile read got there first. `/settings/data` therefore reports
`in_use` and `pending` separately, and the page asks for a restart. Two other
rules, each pinned by `tests/test_settings_page.py`: the setting is written
only **after** a migration succeeds (written first, a failed copy leaves the
pointer on an empty directory while the projects sit where the app no longer
looks), and a `PLEXORA_DATA_PATH` in the environment makes the write a **409**
rather than something recorded and silently ignored -- the notebook sidecar and
`plexora --data-dir` both export it.

**Public plugin API** (`plexora/api/`) — the only surface a plugin may use.
A third-party pip package and a bundled one get exactly the same thing.

- `dataset.py` — `project_data(name)` returns a `ProjectData`: `image` (always
  present, the floor of the contract), optional `segmentation` and `table`,
  and a `DatasetSchema` mapping roles (`cell_id`, `x`, `y`, `celltype`,
  `image_id`) to column names. Plugins read roles, never literal column names,
  and never the raw config entry — `TableSource` is the typed view for the
  rare plugin that must open the file itself (gating writes gates into an
  AnnData's `uns`). A role the project has not collected yet is `None`; that
  is not an error, it is what a plugin declares in `Requires` so core can ask
  for it. **`ProjectData` used to be called `Dataset`**, before a Dataset
  became the folder a cohort of projects lives in (`plexora.datasets`) — one
  word cannot mean both a project's own data and a group of projects.
  `Dataset = ProjectData`, `dataset = project_data` and
  `_dataset_for = _project_data_for` are kept as documented aliases, so
  existing plugin code keeps working; new code should read `project_data`.
  Elsewhere in this codebase the bare word `dataset` still means what it
  always did — the config-key/`Project.dataset` feature table, `ctx.dataset`,
  `datasetName` — and was deliberately **not** renamed, because renaming it
  would have touched every plugin and every template for a collision that,
  read in context, never actually confuses anyone.
- `store.py` — `PluginStore`: `get_state`/`put_state` for plugin-private state,
  `get_table`/`put_table` (Parquet) for derived measurements, annotations and
  classifications written back to the app.
- `plugin.py` — the `Plugin` descriptor a plugin exposes as module-level
  `PLUGIN`, plus `Requires`, which lets core hide a tool whose needs the
  datasource cannot meet. `Requires.first_layer(project)` returns the project
  layer its first `layers` entry names, or None — which LAYER a layer-section
  plugin is a section FOR. Needed server-side because the panel is now the
  body of that layer's card and the card is built from `/config` before the
  plugin's own JavaScript runs; `page_routes` puts the answer on each
  `layer_sections` entry as `layer_id`/`modality`. The first entry rather than
  all of them, because a section is one card. `requirement(key,
  optional=False)` (public, was `_requirement`) builds one `Requirement`
  descriptor from its key alone —
  core calls it for things no plugin declared, like the Cells control's
  "Add Data" button and a Python caller naming a key directly.
- `layers(project, kind=None, modality=None)`, `layer(project, id)` and
  `sample(project)` — what is IN this sample, beside what its table holds.
  **`kind` is core's and `modality` is the plugin's**, and the split is the
  whole contract: a kind is a rendering strategy and there are four of them
  (`image`, `labels`, `points`, `shapes`), while a modality is what the data
  MEANS (`transcripts`, `cell_boundaries`, `visium_spots`, `he`) and is an
  open string core never holds a list of. A transcripts tool asks for
  `modality="transcripts"` and does not care that it draws as points; core
  gains no branch when the next instrument ships. `manifest.layers(project)`
  is the same list flattened for a UI, and `manifest.summary()["layers"]` is
  the count and modality set a library card is drawn from.
- `plexora/api/__init__.py` also re-exports `manifest` (the module in
  `server/models/manifest.py`), so a plugin that wants to ask "does this
  project have a table" uses the same answer core does rather than
  reimplementing the question.

**Plugins** (`plexora/plugins/<name>/`) — each is one self-contained directory
holding its own `server/`, `static/`, `templates/<name>/` and `tests/`. Its
Blueprint carries its own `template_folder` and `static_folder`, so core never
needs to know where a plugin's files live. `gating` and `roi` are the bundled
examples. `transcripts` is a newer one: a Xenium reader, a gene selector and
LOD for transcript-point layers, everything transcript-specific (the gene
vocabulary, the point tiles, the build job) kept out of core so a core build
does not need `pyarrow`. `tests/test_plugin_boundary.py` is what enforces that
— it now has a sixth golden, `tests/golden/boundary_transcripts.json`, and
`WATCHED` in `tests/_plugin_boundary_probe.py` gained `pyarrow` and
`plexora.plugins.transcripts` to its list. Its `PLUGIN.panels` is
`{Plugin.LAYER_SECTION_SLOT: "transcripts/panel.html"}` — a LAYER SECTION
rather than a tool, because it is on from the moment the page loads for any
sample that has transcripts, and opening a real tool (Gating) must not turn
it off; see "A tool can also need a LAYER SECTION" below. Its panel is not a
section at all any more — it is the BODY of the transcript layer's card in
the Layers list. Core owns that card's grip, chevron, title, eye and X (and
the layer earns it either by `LayerStack.claim`/the plugin layer API's
`claim`/`release`, or simply by having staged markup, which is what gives a
layer still being built its card); the panel is left to answer only what a
card cannot: which genes, in what colour, drawn how. There is no core opacity
slider on that card, because the panel's own already writes the stack and
reads it back through `syncVisibility`. `requires=Requires(layers=("transcripts",))`, no
shortcut (there is nothing to open), `owns_cell_layer=False` (it colours
POINTS of its own, never claiming the shared cell-layer range table), and
`scripts=("transcriptsApi.js", "transcriptPoints.js", "transcriptLayer.js",
"transcriptsSidebarController.js")` — the gene tree and its group dialog are
no longer plugin scripts at all: `transcriptGroupModal.js` moved to core as
`views/geneGroupModal.js` (`PlexoraGeneGroupModal`, see the Repository Map
entry below), because the Visium HD bin layer opens the identical dialog over
its own vocabulary. The CSV/remote gene-group import still answers through
`POST /plugins/transcripts/groups`, now a thin call into core's
`server/utils/gene_groups.py` against this plugin's own gene list.

Inside the plugin, `server/xenium.py` gained `read_gene_panel(path)` — the
run's declared PANEL (every gene it was designed to detect, from
`gene_panel.json`'s `payload.targets` where `type.descriptor == "gene"`),
which is a different list from the genes that happen to appear in the
transcript table: a gene with zero calls still belongs in the selector,
greyed at zero, because "we looked and found none" is a result and a missing
row is not. `read_transcripts` now returns five arrays (`genes, gene_index,
x, y, q`) rather than the old shape, and takes `transform=` — the layer's own
affine into reference pixels, which **outranks `pixel_size`** because it is
composed from the run's own manifest at import time and survives a project
whose `ImageSpec` never recorded a pixel size (the state every Xenium import
was actually in, and why a 19-million-point cache was once built five times
too small and drawn in the slide's top-left corner) — and `vocabulary=`, the
panel list to index against so the gene table does not silently omit
zero-call genes. `server/routes.py`'s `/manifest` reports `status: "stale"`
(not `"ready"`) for a cache written by an old `CACHE_VERSION`, and also
returns `pixel_size` (the layer's own, falling back to `image.pixel_size`)
and `density_stretch` (`layer_sources.DENSITY_STRETCH`) — the two numbers the
panel's bin-size and contrast controls need to translate microns and
fractions into the query params `parse_style` reads. `/points`
takes `tile=<x>_<y>` (ETagged, long `Cache-Control`, the mode the viewer uses
while panning) as well as a rectangle query, plus `level` and `minq`: above
level 0 it answers with `transcript_tiles.aggregate_tile`, and the ETag folds
in the level, the gene list, the threshold and `AGGREGATE_BINS`/
`AGGREGATE_REVISION`, because a count is a count OF those and an aggregate
is derived rather than stored. At level 0 none of the four is in the ETag —
the score
rides in the record and the shader discards, so keying on it would throw
every cached molecule tile away on each tick of a slider that does not change
the bytes. `/manifest` carries `aggregate_bins`; new `GET`/`POST
/plugins/transcripts/state` holds the panel's own gene/colour selection per
project. New client files: `transcriptsApi.js` (this plugin's own HTTP
client, added to `tests/js/datalayer_globals_probe.mjs`'s `SOURCES`) and
`transcriptPoints.js` (a WebGL2 point-sprite renderer on its own canvas,
inserted into `viewer.canvas` above the tile layers and below the 2-D
overlay — a 2xN gene-colour lookup texture, 8 SDF glyphs for shape, and the
quality score `q` discarded in the vertex shader rather than the fragment
shader, since a point below the floor should cost nothing past vertex setup).

`visium_hd` follows the transcripts split exactly, and for the same reason:
which vendor files Plexora can read is a question that grows, and each answer
is a plugin rather than a branch in core's importer. The RENDERING of a
counted grid is core's — `server/models/bin_tiles.py`, served through the same
`/generated/layer` route every registered layer uses — and so is the TABLE,
which the importer converts into an ordinary AnnData
(`server/utils/tenx_matrix.py`); what the plugin owns is which genes exist,
which the user picked, what colour, how coarse the squares are, and reading
Space Ranger's matrix into the store. `PLUGIN.panels` is
`{Plugin.LAYER_SECTION_SLOT: "visium_hd/panel.html"}`, `requires=Requires(
layers=("visium_bins",))`, `owns_cell_layer=False` (it colours SQUARES of its
own, never the shared cell layer). `server/tenx.py` reads a Space Ranger run
into `bin_tiles.build` blocks (h5py imported inside functions, the same
core-import-light rule as everywhere else); `server/routes.py` registers
`build_layer` for modality `"visium_bins"` via `layer_jobs.register_builder`
(`BIN_STAGES`) and serves `/manifest`, `/stats`, `/bin`, `/build`, `/status`,
`/state`, `/groups` — no tile route, because tiles are core's
`/generated/layer`. `/groups` is `server/utils/gene_groups.answer` against
this layer's own bin-store manifest, the same call transcripts' `/groups`
makes against its own vocabulary, behind the one dialog both panels open.
Client: `binLayer.js` (bin ladder, colour ramp, the heatmap's aggregation and
the Composition glyph mode, and — now core's `views/geneList.js` — the
selected-genes tree and its groups), `visiumHdApi.js`,
`visiumHdSidebarController.js`,
`templates/visium_hd/panel.html`. Its own new golden,
`tests/golden/boundary_visium_hd.json`, and `"plexora.plugins.visium_hd"` added
to `WATCHED` in `tests/_plugin_boundary_probe.py`, are what
`test_plugin_boundary.py` polices, the same way transcripts is.

`PLEXORA_PLUGINS` controls which are active: unset means every plugin found,
`""` means a deliberate core-only build, `"a,b"` means exactly those. Any number
can be active at once, and each plugin that draws cells gets a LAYER of its own
(`ImageViewer.registerCellLayer`) — its own colours, gate, mode and opacity,
composited in the order its sidebar card sits in.

**Client** (`plexora/client/src/js/`)

- `views/slider.js` — the one slider in Plexora. `class PlexoraSlider`,
  assigned to both `window` and `globalThis`, loaded from `base.html` with
  `defer` immediately BEFORE `gradientRange.js` and `toolLoader.js`, because
  both build with it. `new PlexoraSlider(mount, options)` — `mount` is an
  element to append into, an existing `<input type="range">` to adopt in
  place, or `null`; options cover `mode` (`"single"`/`"range"`),
  `min`/`max`/`step`, `value` or `low`/`high`, `minGap`, `scale`
  (`"linear"`/`"log"`), `steps`, `decimals`, `format`/`parse`, `unit`,
  `label`/`labelAbove`, `field`/`fields`, `fieldWidth`, `fieldsSlot`
  (an element to parent the number boxes into instead of the slider's own
  row, for a caller whose fields need to sit apart from the track -- the
  figure builder's panels are the remaining user; NEITHER the channel window
  nor the gating threshold uses it any more, both having moved their two boxes
  back onto the track's own row -- see viewerSidebar's `syncChannelSlider` and
  gating's `syncGateSlider`; `destroy()` reaches out of the root to take them
  back), `id`/`ids`,
  `fieldId`/`fieldIds`, `fieldMax` (the box types PAST the end of the track:
  the thumb pins at `max`, the value is the typed one up to `fieldMax`, and
  `aria-valuetext` reports it because a pinned `aria-valuenow` cannot -- the
  transcripts point size drags 1..20 and types to 100, see
  `TranscriptLayer.POINT_SIZE_MAX`), `ariaLabel`/`ariaLabels`, `disabled`,
  `accent`, `className`, `onInput(value, end)` and `onChange(value, end)`.
  Methods: `get()`, `set(v, {silent})`,
  `setBounds({min,max,step,minGap,fieldMax})`,
  `setDisabled()`, `setUnit()`, `setAccent()`, `destroy()`. Statics: `THUMB`
  (12, matching `--plx-thumb`), `LOG_STEPS`, `format`, `decimalsFor`, `snap`,
  and `numberField(opts)` — the
  typeable number box on its own, with no rail at all; Enter blurs it itself
  (`input.blur()` from inside its own keydown handler, right beside the commit
  it fires first), which is the only reason a slider's own text-until-focused
  box (below) ever gets its box back without a click elsewhere. A native
  `<input type="range">` sits underneath, reduced by CSS to its thumb, so
  arrow keys, Home/End, a tab stop and a screen-reader announcement come
  free; the rail and the fill are sibling divs driven by `--plx-lo`/
  `--plx-hi`, which is why a one-handle and a two-handle slider are the same
  CSS and very nearly the same code. Every slider also gets a real
  `<input type="number">` field, because half the values in this app are
  read off a paper and a range alone cannot express "0.5 exactly"; its
  spinner arrows are removed globally in main.css (see Key Invariants), so
  `format` must return a plain float string. Migrated onto this one
  primitive: `views/gradientRange.js` (range mode), `views/layerManager.js`
  (layer opacity), `views/brightfieldAdjust.js` (brightness/contrast/gamma/
  opacity), `views/viewerControls.js` (cell point size and cell layer
  opacity), `views/viewerSidebar.js` (channel contrast window),
  `plugins/gating/static/gatingSidebarController.js` (gate range),
  `plugins/transcripts/static/transcriptsSidebarController.js` (size/
  opacity/minQ, the bin ladder), and `plugins/figure_builder/static/
  figureShapePanel.js`/`figureLinePanel.js` (`upgradeSliders()` after each
  innerHTML render). Deliberately NOT migrated: `views/channelList.js`'s
  `addSlider` and `plugins/gating/static/csvGatingList.js`'s `addSlider`, the
  two d3-simple-slider lists that render into `#legacy_controls_mount`, which
  viewer.css parks at `left: -10000px` — nobody sees them, so the vendor
  bundle carrying d3-simple-slider is unchanged.
  **Every `.plx-number` beside a slider reads as text until it is hovered or
  focused** — `1 ---o===o--- 255` — not just the ones on a narrow row sharing
  it with their own values: `is-plain-numbers` is gone, and `.plx-slider
  .plx-number`/`.gradient-range-scale .plx-number` in **main.css beside
  `.plx-number` itself** cover every slider outright. A `.plx-number` built
  through `numberField(opts)` with no slider around it (a caller wanting the
  typeable box alone) keeps its ordinary boxed appearance, because the
  text-only look is a rule on `.plx-slider .plx-number`, not on `.plx-number`
  itself. The tint is `--plx-number-hover`/`--plx-number-focus` (defaulting to
  a white wash), so a light caller — `figure_builder.css` — can darken them
  instead of drawing white on white. `.plx-slider.is-range` (not
  `is-plain-numbers`) sets `--plx-slider-gap: 4px`, closer than the slider's
  own 8px: the numbers are the ends of the line, not two more controls on it.
  The muted 20px Auto/Revert glyph that ends the row is `.slider-auto-button`,
  moved to **main.css beside the slider block** (it used to live in
  viewer.css) because `gradientRange.js` — an icon button now, `fas
  fa-wand-magic-sparkles`, tinted `.is-active` with the channel accent — loads
  from `base.html` on every page, not just the viewer; the channel contrast
  window and the gating threshold still share the same class, and a plugin
  reaches for it freely, since `tests/test_plugin_css_boundary.py` polices
  ids, not classes.
  The sibling shape is **`.control-row`** (viewer.css, beside `.control-label`
  itself): a caption BESIDE its control rather than above it, which is what
  `.control-label`'s bottom margin has to be taken back for. Three panels had
  written the same six declarations out privately — Point size, Opacity and
  the gate's marker picker — before it.
- `views/imageViewer.js` — still the big one, but four closures that used to
  live inside `ImageViewer`'s constructor were lifted out into their own
  modules (Phase 0 of the spatial-layer work): `views/labelTile.js`
  (`renderLabelTile`), `views/tileColorize.js` (`createTileDrawing`),
  `views/tileDecode.js` (`TileDecoderPool`/`decodeLabelTile`/
  `createTileLoadedHandler`) and `views/glInit.js` (`GLTileTextureCache`/
  `createGLRenderer`/`createGLInit`). What each does is unchanged; only where
  it lives moved, so it can be tested without building a viewer. All four are
  loaded as plain `<script>` tags in `base.html`, in dependency order, BEFORE
  `imageViewer.js`: `cardList.js`, `layerStack.js`, `glInit.js`,
  `tileColorize.js`, `tileDecode.js`, `labelTile.js`, then `imageViewer.js`.
  The cell-layer registry itself also moved: `ImageViewer._cellLayers`/
  `_cellLayerOrder`/`_activeCellLayer` are gone, replaced by
  `this._cellStack`, a `SubLayerStack` (see `views/layerStack.js`). Every
  public method that read or wrote the old fields
  (`registerCellLayer`, `setCellColorLUT`, `maskDrawList`, ...) kept its exact
  signature and semantics, so nothing outside `imageViewer.js` had to change.
  `initMiniMap()` wires up the mini-map lens alongside `initProjectLabel()`/
  `initLegend()`, and calls into it (`invalidate({refetch:true})` on active-channel
  changes, `invalidate()` on range/colour changes) so the lens stays in sync
  without owning its own state. New `tileCachePlanes(config)` sizes
  `maxImageCacheCount` off the reference image's channels PLUS every ready
  non-rgb image layer's channel count (capped at 15 each, an rgb layer
  counting as 1) — OSD has ONE shared `TileCache`, and a layer with channel
  controls is now N world items drawing from it like any other channel
  stack, so undersizing it evicts tiles about to be redrawn. New
  `referenceItem()` is the world item every screen↔image conversion goes
  through, found via `layerStack.anchorIndex()` rather than assumed to be
  `world.getItemAt(0)`. Six call sites (`getMouseSelect`'s right-click,
  `viewportImageBounds`, `getVisibleCentroidTileState`, `getCentroidLevel`,
  and the two centroid draw paths) now go through it: the reference image's
  card can be dragged now, and a registered layer at index 0 carries its OWN
  affine, so converting a click through it would land on the wrong slide by
  however far the two are apart, silently. `referenceItem()` falls back to
  `getItemAt(0)` only when there is no stack to ask (a world with items but
  `syncLayers` not yet run, or a test harness).
- `services/viewTransform.js` (`window.PlexoraViewTransform`) — the ONE state
  for how the viewer turns and mirrors the image, `{degrees, flipH, flipV}`,
  owned here and written only by core's Rotate & Flip tool
  (`views/viewTransformTools.js`). Reaches OpenSeadragon through the viewport
  (`setFlip`/`setRotation`), never per tiled image, so every channel, the
  mask, every registered layer and the Visium HD bins turn together for free;
  `osdStateFor` maps the pair of flips plus an angle onto OSD's single
  built-in horizontal flip (`V = flip + 180°`, since `Fv = Fh·R(180)`). Saved
  to `/view_transform/<datasource>` debounced (`SAVE_DELAY_MS`); `adopt()`
  applies a saved state without saving it back, which is how `main.js` uses it
  at boot. `goHome` is wrapped to be rotation-aware — OSD's own
  `getHomeZoom` fills from the unrotated content aspect, so a 90-degree view
  of a wide image used to go home with side margins and a cropped height.
  Everything that draws or picks in screen space reads its helpers
  (`pointFromPixel`/`pixelFromPoint`/`orientContext`/`screenBoxOfImageRect`/
  `imageToScreen`/`screenToImage`) rather than OSD's viewport directly — see
  the screen-space invariant under Key Invariants.
- `views/layerStack.js` — `LayerStack`/`SubLayerStack`/`OverlayHost` plus the
  affine maths: the one ordered list of everything the viewer draws (image,
  labels, points, shapes), because the viewer used to have two layer stacks
  that did not know about each other (OSD's `world` and the cell-layer
  registry) and "which layer is on top" had two answers. Three composite
  surfaces, in a fixed sequence — `tiles` (inside OSD's world), `overlay`,
  `gl` — and a layer cannot be dragged across that boundary. `anchorIndex()`
  is the one place that decides which world item an overlay's `onRedraw`
  should actually draw on (see "Two stacks, not one" below). `claim(id, by)`
  sets a layer's `drawnBy` field — the fact a card cannot work out for
  itself, that something other than core is actually drawing this layer —
  and is not a permission gate: the layer is in `/config` and in the stack
  either way, `claim` only changes whether `layerManager.js` offers it a
  card. A plugin claims through `main.js`'s `pluginLayerApi.claim`/`release`,
  which also releases every id it claimed when the plugin tears down, so a
  tool switched away cannot leave an orphaned card behind. A layer record also
  carries `pinned`: `setOrder` lifts every pinned id to the top before it
  writes the order (`_liftPinned`), `register` re-lifts, and
  `applyWorldOrder` biases a pinned layer's rank by `PINNED_RANK_BIAS` so it
  wins the OSD world order too. The cell mask is pinned — it lost its Layers
  card (see `layerManager.js` below) but not its place above every raster.
  Exported `ITEM_Z` (the string `"_plexoraItemZ"`) is the property a world
  item may carry for its place WITHIN its layer — the one layer that owns
  items that are not interchangeable is a registered layer's channel set,
  drawn as a cover blit and a paint blit per channel (see `viewerManager.js`'s
  `addLayerChannelSet` below), and every cover blit has to stay below every
  paint blit however an HD toggle or a routing repair happens to re-add them.
  `applyWorldOrder` sorts on `(rank, z, current index)` rather than just
  `(rank, index)`; an item with no `ITEM_Z` sorts as `0`, which is every world
  item outside a channel set.
  **`placementFor` now pivots a rotated or mirrored layer about its own
  centre, because OSD does.** OSD rotates a `TiledImage` about the centre of
  its UNROTATED bounds and mirrors it in place inside them, so a layer pixel
  `p` lands at `centre + R(degrees) * F * (p - layerCentre) * scale` — which
  equals the affine exactly only when the bounds' centre is the affine image
  of the layer's centre. With no turn and no mirror the two placements agree
  (that branch is kept verbatim, so an already-registered unrotated layer
  does not move by a rounding error); a turned or mirrored one now computes
  its centre through the affine and reads `x, y` back off THAT, which needs
  the layer's height as well as its width (`placementFor(transform,
  layerWidth, referenceWidth, layerHeight = layerWidth)`) — a Visium HD bin
  grid is what first needed a rotated, non-square layer placed correctly.
  Callers in `viewerManager.js` now pass the layer height.
- `views/cardList.js` — the sidebar's one draggable-card-with-an-eye
  implementation, built once and shared: `toolLoader.js`'s tool cards and
  `layerManager.js`'s layer cards are the same object with the tool-specific
  or layer-specific parts handed in. The title button holds its name in a
  `${prefix}-title-text` span rather than as its own text, and takes an
  optional `hint` — the printed shortcut, drawn beside the name as a
  `${prefix}-key` keycap. Both exist because a tool card's label is the Tools
  menu row's, into which `keyboardShortcuts.js` has already printed the chord:
  taking the row's `textContent` titled the card "Cell Explorer⌘E", one word
  in the title's own weight. Truncation lives on the text span, so it is the
  name that elides and never the key. `buildCard` takes an `extras` option —
  nodes placed in the header after the title and before the eye, which is
  where a plugin card's kebab menu lands (the base image card's channel
  counter used to land there too; it has since moved down into the card's own
  footer, beside the "Reference layer" alignment line — see `layerManager.js`
  below). `buildCard` also takes `lockFixed`: draw the padlock but disable it
  (`disabled` + `aria-disabled`), for a card whose lock is a structural fact
  rather than the user's choice — the base image card is the only one. Every
  card header is now foldable by a click anywhere on it
  (`${prefix}-header-foldable`), not only the chevron: the handler lets
  through anything matching `button, input, select, a, label,
  .${prefix}-grip` (a drag that ends where it started arrives as a click on
  the grip, and must not also fold the card), and the title itself only when
  the card has no `onSelect` — a tool card's title still selects the tool. Both
  card headers now carry a tint and a bottom rule (`.layer-card-header`/
  `.tool-card-header`), suppressed and re-rounded when the card is collapsed;
  the tool card header's gradient starts at 3px so it does not cover the
  card's inset accent stripe. FontAwesome replaces `<span class="fas
  fa-...">` with an inline `<svg>` before any click can reach it, which is why
  a two-state button is built as two glyph spans that CSS toggles, never as a
  class rewritten from JS.
- `views/layerManager.js` — the Layers panel, and now the ONE visual layer
  stack: every data modality is a card, and the card is where that modality
  is configured, rather than a scattering of sidebar sections above a
  separate list of rasters. Says out loud two things the viewer never used
  to: whether a layer has been registered against the reference image at all
  ("aligned by assumption" otherwise), and when a layer's transform has shear
  or anisotropic scale, which OpenSeadragon's `x, y, width, degrees, flipped`
  placement cannot express — refused on the card rather than drawn silently
  wrong. `CARDED_KINDS` is now just `{"image"}` — `labels` was dropped,
  because the cell mask gets no card of its own now that the Cells footer
  already owns how cells are drawn. `isCarded(layer)` is a carded kind, OR
  `layer.drawnBy` (a plugin claimed it, via `LayerStack.claim`), OR the layer
  has markup staged for it in `#layer_section_slot` (`stagedFor`, by id then
  by modality), OR it already adopted some (the `adopted` set — adopting
  MOVES the staged node out of the slot, so the lookup that found it cannot
  answer twice). `__centroids__` is still never carded: core draws it, the
  Cells section owns its one switch, and a second eye here would be a second
  answer to one question. Three card shapes (`cardStyle`): the BASE image
  card (`__image__`, always titled "Image" — `labelFor` ignores `layer.label`
  for this id, because the server fills it with the PROJECT's name
  (`Project.reference_layer`, `label=self.name`) and that name is already in
  the navbar; no X (the way to remove it is to remove the sample), but its
  grip drags and its padlock is a REAL one now (`lockFixed: false`), not the
  disabled `lockFixed` padlock it used to be drawn with. It is no longer
  sorted to the foot of `cardedLayers()` or hoisted to the front of
  `syncOrder`'s list, and `ensureSortable`'s `onMove` no longer refuses a drop
  beneath it — dragging the base card above or below a registered layer is
  what "the reference image can be reordered" means to the model
  (`Project.image_depth`/`with_layer_order`, above), and it composites as a
  cover/paint pair over whatever ends up under it rather than as an opaque
  black-backed tile, so there is no longer a floor to defend. `persistOrder()`
  now names it too — `isUnstorable(id)` (mask and centroids only) gates what
  it filters out, not `isReserved(id)` (image, mask and centroids: the ids
  `all_layers` synthesizes) — and `persist(id, patch)` stores the reference
  image's `render` the same way, through the layer PATCH route, but never its
  `visible`. Its body is whichever
  of Image Channels/Image Adjustments markup was staged for it, plus a
  `buildBaseFooter` that MOVES the channel counter (`data-layer-count`) down
  beside the "Reference layer" alignment line, and a `.layer-card-actions` row
  shared by the "Upload channel names" button and, for the fluorescence case,
  a compact `buildOpacityControl` — a `.layer-opacity-trigger` reading
  "Opacity 100%" that opens a portaled `.layer-opacity-popover` slider, found
  via `data-layer-opacity-slot` in the staged markup and NOT to be confused
  with `data-layer-opacity`, which marks a brightfield project's own opacity
  row and means the card gets neither control; an `opacityReadouts` map lets
  `paint()` keep that button's percentage honest when opacity changes from
  elsewhere) and, last in the header extras (after the ground dot, before the
  eye and lock), the reference card's own `•••` — `hasRenderMenu`/
  `renderMenuFor`, read off the same `data-layer-opacity-slot` staged markup so
  it never appears on a brightfield or blank image — opening
  `views/popoverMenu.js` with two rows, "Channel names" and "Rendering", each
  a copy (`fas fa-copy`) and a paste (`fas fa-paste`) glyph over that noun —
  "Copy/Paste channel names" and "Copy/Paste rendering settings" survive as
  each button's tooltip. Channel names go through
  `services/renderClipboard.js`'s `names` slot, `POST /rename_channels`,
  `main.js`'s `adoptChannelNames` on success; rendering settings go through its
  `rendering` slot, applied through `ViewerSidebar.applyLaunchChannels`.
  A module-level `transferring` flag mutes both Paste actions for the length of
  one paste, so a second click cannot start a rename over one still being
  written; PLUGIN cards (body is the plugin's whole panel, adopted from its
  `data-layer-body` mount, and core adds no opacity slider of its own because
  the plugin's own panel writes the stack — `hasStagedOpacity`); and RASTER
  cards. A raster with a channel panel (`hasChannelPanel(layer)`) gets the
  SAME compact `buildOpacityControl` as the base card, inserted first on the
  panel's own `.layer-card-actions` line (marked `data-layer-opacity-slot` by
  `layerChannelPanel.js`'s `buildMarkup`, same attribute) — a multiplexed
  image imported as a layer has the identical opacity control and "Upload
  channel names" button on one line as the reference card. Only a card with
  no such line (an rgb layer, a build without the panel module) still gets
  the plain `buildOpacityRow` label+slider row; alignment note, build
  state, all rebuilt in place on every render since
  core owns everything in the card; a channel select, colour swatch and
  contrast window too, but only when `hasChannelPanel(layer)` is false —
  `buildChannelRow`/`buildWindowRow` return `null` for a panelled layer
  instead). `hasChannelPanel(layer)` and `channelPanelFor(layer)` are the
  gate: an image layer with channel planes (`_channelPlane` server-side, via
  `layer.spec.channels` client-side — an rgb layer or the base image excluded)
  gets a
  `PlexoraLayerChannels`-mounted second `ViewerSidebar` in its card instead
  of the single-row controls — the reference image's own channel slots,
  colours, log contrast sliders, Auto and Add Channel, on a registered layer
  too. A module `panels` Map mounts each layer's panel ONCE; `refreshBody`
  moves the existing node back rather than remounting it, and
  `dropChannelPanel(id)` (called on unregister and on layer removal) destroys
  it — including the compact opacity control it carries: a sibling module
  `opacities` Map (id -> `{trigger, destroy}`) beside `panels` and `grounds`
  holds that control, built once per layer for the same reason the panel
  itself is, and `dropChannelPanel`'s `destroy` closes the popover, detaches
  it from `PopoverPortal`, and drops the `opacityReadouts` entry along with
  the map entry, so a rename rebuild or a layer removal takes the popover off
  the portal rather than orphaning it there.
  The panel **reconciles, it does not rebuild**: `render()` never clears the
  list; cards are built once, kept in a module `cards` Map by layer id, and
  reordering re-`appendChild`s the same nodes (`appendChild` MOVES a node the
  list already holds) — load-bearing now that a card can hold markup another
  controller owns handles into, which a wipe-and-rebuild would orphan. A card
  is only dropped from the cache once its layer is gone AND it never adopted
  anything; an adopted card is kept detached instead, so a layer that comes
  back (a re-adopt after import, a build finishing) comes back with its
  controls intact. Cards are still grouped by surface (tiles below overlay/gl)
  and `onMove` still refuses a cross-surface drag and a drop beneath the base
  image, but the labelled divider that used to announce the boundary
  ("Points and shapes draw over images", `data-surface-break`) is gone — the
  refusal stands, it is just no longer spelled out, and the grouping itself is
  what is left of it. `syncOrder()` sends **every** stack id to `setOrder`,
  not only the carded ones — an uncarded layer keeps its surface's place
  (rasters among the cards, overlays above them), because `setOrder` files an
  id it is not told about at the bottom, which used to sink the centroids
  below the base image. `setCollapsed(id, on)` is the public fold/unfold, used
  by the sidebar's own collapse and by `toolLoader`'s "make room for a tool" —
  and it now means **openOnly**, not a plain toggle: unfolding one card folds
  every other layer card (`openOnly`) and calls
  `window.PlexoraToolLoader.collapseAllCards()`, because the sidebar keeps ONE
  card open at a time across both its lists. `collapseAll()` (exported on the
  api) is the mirror `toolLoader.collapseOthersFor` calls when one of ITS
  cards opens; neither module calls back into the other, deliberately, or they
  would fold each other forever. `init()` seeds the rule with the base image
  card left open. Clicking anywhere in a card's header now folds or unfolds
  it — `cardList.js`'s `${prefix}-header-foldable` — not just the chevron. The
  X really removes a layer now: a confirm dialog, then
  `DELETE /project/<name>/layers/<id>`, then dropping the world item,
  unregistering, and re-`adoptLayers()`. `persist(id, patch)` (debounced
  500 ms per layer) and `persistOrder()` are what write a card's changes —
  opacity, order — back to the project through the two new
  `project_routes` below, rather than that state living only in the tab. A
  RASTER card's opacity control and the window row's two number boxes
  (`.plx-number layer-card-number`) are a `PlexoraSlider` (see
  `views/slider.js` above); the 500ms debounce hangs off its `onChange`.
  Every image card now carries a `.layer-card-ground` dot, built with
  `ColorSwatchPicker` (`groundDotFor`) and persisted through
  `persist(id, {render: {background}})` — the base card's writes
  `applyViewerGround` through `viewerManager.applyViewerGround`, a
  registered layer's writes `addLayerChannelSet.setGround`. The dot is
  drawn as one of the header's own buttons (an 11px round swatch in the
  same 4px/5px pad and hover background as the grip, chevron, eye, lock
  and X), not a bare swatch on its own. Its palette comes from
  `groundPresets(base)`: a slash (`transparent`), black and white, then
  `ColorSwatchPicker.DEFAULT_PRESETS` filtered so nothing repeats —
  `GROUND_COLUMNS` (4) is handed to the popover as `--ground-columns` so
  the grid comes out a whole number of rows. Both the reference image and
  a registered layer's popover lead with the slash: "Default" (
  `DEFAULT_GROUND`) for the reference, "None" (`NO_GROUND`) for a
  registered layer — both `hex: "transparent"`, both send `null` to the
  PATCH route to remove the key, which for the reference falls back to
  whatever `viewer.css` picks rather than meaning "no ground" (the canvas
  always has some ground colour).
- `views/layerChannelPanel.js` (`window.PlexoraLayerChannels`) — mounts a
  SECOND scoped `ViewerSidebar` instance inside a layer's card (see
  `layerManager.js`'s `channelPanelFor`/`hasChannelPanel` above), so a
  registered image layer gets the reference image's own channel slots,
  colours, log contrast sliders, Auto and Add Channel rather than a second,
  smaller implementation. Exports `mount`, `prefixFor`, `savedRowsFor`,
  `slotsToChannels`, `slotsToDrawn`, `MAX_SLOTS`; everything a slot looks
  like, what Auto does, and how a window is stored stays `ViewerSidebar`'s.
  Only three things differ from the reference image's own sidebar instance:
  where stats/GMM come from (the layer's own routes,
  `layer_sources.layer_channel_stats`/`layer_channel_gmm`, not the
  datasource-wide ones), where saved channels come from and go
  (`render.channels` on the layer record, through the Layers panel's own
  PATCH), and what a change repaints (the layer's `LayerChannelSet`, not the
  reference image's world items). Persistence is done by OVERRIDING
  `sidebar.persistChannelList` on the mounted instance rather than binding
  bus events, because `scheduleSaveChannels` does not check `this.persist` —
  so the override inherits the class's own 400 ms debounce, `_restoring`
  suppression and first-run write for free. `render.channels` is stored as
  `[{index, name, color: "#rrggbb", range: [rawLo, rawHi]}]`, enabled
  channels in slot order, RAW 16-bit units, written WHOLE every save (the
  `PATCH` merges `render` one key deep); `render` stays unmodelled so
  `LayerSpec` is unchanged, and the legacy `render.channelIndex`/`color`/
  `range` shape (one channel, no slots) is still read for a layer saved
  before this. `buildMarkup` always builds a `.layer-card-actions` row as the
  panel's first child, marked `data-layer-opacity-slot` — the same attribute
  index.html puts on the reference card's own action row — so `layerManager.js`
  always finds a line to put the compact opacity control on; the "Upload
  channel names" button is still built into that row only when `mount` is
  given a `rename` callback. `views/viewerSidebar.js` itself gained `slotId("channel_slot",
  index)` ids on every `.channel-slot` row and a `slotRow(index)` accessor
  that `applySlotExpansion`/`syncSlotAutoButton`/`syncSlotDom` resolve
  through instead of a document-rooted `q()` — load-bearing for a SECOND,
  layer-scoped instance, which must never rewrite the reference image's
  rows — plus a `destroy()` that drops its `plexora:hd-mode-changed` window
  listener and destroys its sliders/pickers/selects, called by
  `dropChannelPanel` when a layer's card goes away.
- `views/layerSections.js` (`window.PlexoraLayerSections`) — shrank to a
  staging registry once `layerManager.js` took over section chrome. It no
  longer owns collapse, visibility or a switch of its own —
  `bindCollapse`/`bindVisibility`/`isVisible`/`setVisible` are gone. What
  remains: `mounts`, `isLayerSection`, `register`, `bodyFor(layerId)`,
  `bodyForModality(modality)`, `extrasFor(layerId)`, `extrasIn(mount)`,
  `names`, and forwarding the viewer's own `plexora:viewer-hidden`/`-shown`
  events so a section drawing on the canvas stops when the canvas goes away.
  `#layer_section_slot` is now a hidden STAGING area, not a place on the
  page — `layerManager.js` moves its `data-layer-body`/`data-layer-extras`
  contents into cards (`stagedFor`/`extrasFor`) on first render, which is why
  the panel's contents and state are still entirely the plugin's even though
  the section itself no longer renders anywhere. `transcripts` is still the
  only `LAYER_SECTION_SLOT` plugin; its panel is no longer a `<section>` with
  its own heading checkbox at all — it earns an ordinary Layers card by
  naming its layer through `Requires.first_layer` (see `api/plugin.py`
  below), and `TranscriptsSidebarController.setLayerVisible()`, which used to
  drive that checkbox, was deleted along with it.
- `views/miniMap.js` — the bottom-left circular lens (`class MiniMap`, a global,
  loaded the same way as `imageViewer.js`): expands into a circular overview of
  the whole tissue per active channel, fetched from `/generated/overview/...`.
  Its `.viewer-mini-map-orient` layer (the picture and the viewport indicator,
  not the note) turns and mirrors with the main view's Rotate & Flip state — see
  the screen-space invariant under Key Invariants — and `_stagePoint` takes a
  pointer event back through that same transform before normalising it. The
  lens toggle shows a close glyph while expanded (`LENS_CLOSE_ICON`) instead of
  moving, and no longer moves when the map opens (viewer.css).
- `views/viewerManager.js` — tile source definition: `getTileUrl`, `getTileKey`,
  `toTileLevels`, and one `addTiledImage` per active channel.
  **The tile QUALITY a change of which rebuilds every item** (the HD toggle)
  goes through `handOverWhenReady(items, commit)` so the old items keep drawing
  until the new ones can — `addChannelItems(srcIdx, outgoing)` for the
  reference image, `addTiledLayer`'s `prepareStyle(next)` for a registered
  layer, `referenceItemsFor(url)`/`dropWorldItems(items)` for finding and
  retiring a channel's items. `getTileUrl` reads the quality off the tile
  source (`hd`), not off the module-global `tileQuality`. See "Changing tile
  quality without blanking the canvas" under The Rendering Pipeline, which is
  where the reasons live. Every world item
  it adds carries `source.layerId` and goes through `claimWorldItem`/
  `releaseWorldItem`/`applyWorldOrder`, which hand it to the `LayerStack`
  (`views/layerStack.js`) and re-apply that stack's z-order — the replacement
  for the old `raiseLabelLayer`, which only knew how to special-case one
  layer. `claimWorldItem` also sets `pinned` on the mask, which is the other
  half of it having no card. `claimWorldItem(layerId, item, z)` takes a third,
  optional argument: where the item sits WITHIN its layer
  (`views/layerStack.js`'s `ITEM_Z`), needed only by a channel set's cover/
  paint pair (see `addLayerChannelSet` below).
  **This is where the model reaches the renderers.** The constructor
  subscribes to the stack, and `applyLayerState()` makes the tiles surface
  match it: a registered layer's handle gets `setVisible`/`setOpacity`, and
  `__image__` goes through `applyReferenceState` → `hideReference()` /
  `showReference()` / `referenceOpacity()`. Before this, the Layers panel's
  eye and opacity slider wrote the stack and nothing read them back, so a
  card promised two controls that moved nothing. The mask is deliberately
  skipped — the Cells footer owns `sel_outlines`. Hiding the base image
  REMOVES its world items (an item at opacity 0 still fetches, decodes and
  draws every tile) but leaves `channelList.currentChannels` standing, which
  is what `showReference` rebuilds from; `channel_add` records a slot and
  returns without adding while `referenceHidden`, and `load_brightfield_base`
  returns early for the same reason, so a routing repair cannot put the slide
  back under a closed eye. `seedLayerState(spec)` puts a layer's stored
  `visible`/`render.opacity` into the stack on FIRST sight only — the same
  rule `ImageViewer.syncLayers` follows, repeated because either can see a
  layer first and the two are on opposite sides of the bundle seam. The
  `addTiledLayer` handle gained `setBlend(op)` (in place via
  `item.setCompositeOperation`, with a drop-and-re-add fallback) — no core
  caller uses it any more (the per-layer Add/Over card control was removed,
  see "Sharp Edges" below), but the transcripts plugin's density raster still
  calls it directly to keep its own blend in sync with zoom.
  `blendOperation(spec)` is GONE — an rgb layer's `compositeOperation:
  "source-over"` is now inline at the `addTiledLayer` call site in
  `syncLayerImages`, and a channelled layer's pair of operations is owned
  entirely by `addLayerChannelSet` (below); there was no longer one blend per
  layer for a helper to decide. Its internal `add()`/`drop()` also carry
  a `generation` counter now — two
  style changes in quick succession used to leave an orphaned item nothing
  could ever remove, because the first add's arrival raced the second add's
  `shown = true` (see the Validation entry below for the mechanics); only the
  holder of the current generation number may install its item.
  `addTiledLayer` now also takes `spec.channel` (a mutable channel record),
  `spec.srcIdx` (default `` `${layerId}:${src}` ``, interpolated into
  `getTileKey` — the GL texture-cache key — because every layer used to
  answer `0`, the reference image's first channel, and so shared its GL
  texture cache slot with it) and `spec.z` (passed straight to
  `claimWorldItem`); its success handler now calls `self.restoreView()` first,
  and `setStyle(undefined)` means keep the current style and just refetch.
  New `addLayerChannelSet(spec)` returns a composite handle shaped like
  `addTiledLayer`'s (`placement`, `remove`, `setVisible`, `setOpacity`,
  `setStyle` — no `setBlend`, because where a layer sits relative to what is
  under it is now stack order and opacity, not a composite operation) plus
  `setChannels(list)`, which is what gives a registered layer the reference
  image's own channel controls: it DIFFS the new list against the current one
  by channel name and mutates the channel record the tile source already
  holds by reference, so a colour or window change adds and removes zero
  world items. **TWO world items per channel, not one**, module constants
  `COVER_OPERATION`/`PAINT_OPERATION` (`"destination-out"`/`"lighter"`) and
  `COVER_Z`/`PAINT_Z` (`0`/`1`, claimed via `spec.z` above): OpenSeadragon
  composites every world item straight onto one canvas — there is no
  group — so a registered layer sitting OVER the picture beneath it, while
  its own channels still add among themselves, has to be said with a pair per
  channel rather than one operation. The COVER blit takes the base away in
  proportion to the channel's windowed intensity; the PAINT blit then adds
  the channel's colour, exactly the addition the reference image's channels
  do. `entries` (per channel set) maps `name -> {handles: [cover, paint],
  record}`. Both items of a pair address the SAME tile url, so OpenSeadragon
  gives them one fetch, one decode and one shared cache record (see
  `tileDecode.js`'s `shareDecoded` below) — the pair costs one extra blit and
  nothing else. Module helpers `channelSetLayer(spec)`,
  `seedChannelsFor(spec, channels)` and `hexToChannelColor` back it.
  `setHdMode` now also refetches every layer item, not only the reference
  image's. **`channel_add` now draws the reference image's own channels as
  the same cover/paint pair** (`COVER_OPERATION`/`PAINT_OPERATION`, tagged
  `coverageAlpha: true`, `layerId: REFERENCE_LAYER_ID`) instead of one opaque
  `lighter` blit — the change that stopped the reference image being a
  special kind of picture; against a black ground the pair is pixel-identical
  to what it drew before. `channel_remove` now collects and removes EVERY
  item drawn from a channel before touching the world, not the first one
  found — it used to `break` at the first match, which with a pair left half
  the channel behind (a lone paint blit adding forever, or a lone cover blit
  punching a channel-shaped hole). Sharp edge found doing this:
  `compositeOperation` is read off `addTiledImage`'s OPTIONS, never off the
  tile source — the `compositeOperation: "lighter"` that used to sit inside
  `channel_add`'s tile source was inert, and the blend that actually ran was
  the viewer-wide default (also `lighter`, which is why nothing looked wrong
  for years).
  New `applyViewerGround(hex)` sets `--plexora-viewer-ground` on the
  `#openseadragon` element — the REFERENCE layer's background is the
  canvas's own, because the reference is the scene's frame and has no
  transform to hang a world item's ground on. New `addLayerGround(spec)`
  draws a REGISTERED layer's ground as one world item at `GROUND_Z` (`-1`,
  under both halves of every channel) from its own one-tile/one-level/
  one-pixel tile source built by `groundTileUrl(hex)` (a memoised 1×1 PNG
  data URL, shared across every layer asking for the same colour, keyed on
  OSD's own tile-cache-by-url) — it cannot share the channels' tile source,
  because two world items on one URL share one cache record and one tile
  canvas, which is exactly what makes the cover/paint pair cost one decode
  and exactly what would make a ground and a channel overwrite each other's
  pixels. `addLayerChannelSet` gained `setGround(hex)` (drops and re-adds
  rather than recolours, because the colour IS the tile); `syncLayerImages`
  seeds it from `spec.render.background` on every re-sync.
- `services/glRenderer.js` — the WebGL2 core. Shader compile, quad buffer,
  default draw path.
- `workers/tileDecoder.js` — off-main-thread WebP tile decode.
- `services/desktopBridge.js` — `window.PlexoraDesktop`: the desktop app's
  frontend-side bridge, `null` in any ordinary browser (`window.__TAURI_INTERNALS__`
  absent). Loaded from `base.html`'s `<head>`, first and NOT deferred, so it
  exists before a menu accelerator, a native file drop or another script's
  feature test can arrive. Sets `html.is-desktop` (what CSS and `base.html`'s
  "Open in Browser" row key off), listens for the shell's menu events (File >
  New/Open/Quit, mod+N/W/Q, mod+shift+B, F11 off macOS — Windows menu
  accelerators never fire while WebView2 has focus, so this is what actually
  runs them; see Sharp Edges), and exposes `saveBlob()` (a native Save dialog,
  what `roiApi.js`'s and the gating CSV's offline path calls instead of an
  anchor download in the app's window), `pickPaths()` (`browsePicker.js`'s
  `browseInShell`, the app's own Browse dialog), `quit()` (Quit is the app's
  own affordance there -- it closes the window and the shell stops the server
  it started, so `navbarControls.js`/`settingsPage.js` skip the confirm
  dialog and the `/shutdown` fetch entirely and call this instead), and
  `notifyIfAway()` (a system notification for a long job finishing while the
  window is not in front — Import Sample's watcher and a data-folder move in
  Settings both call it), and `toggleFullscreen()` (`imageViewer.js`'s
  fullscreen button asks the window to go full screen rather than the page's
  own HTML fullscreen, which a WebView does not draw the same way a browser
  does). Native file drops arrive as a
  cancelable `plexora:native-drop` `CustomEvent` carrying paths, not bytes; a
  handler that does not cancel it falls through to
  `PlexoraImportSample.dropPaths`. `tests/js/desktop_bridge_probe.mjs`
  (`tests/test_desktop_bridge.py` runs it) is what exercises it without Tauri
  actually present.
- `services/pointerDrag.js` — pointer-driven intra-page drags (`cardList.js`'s
  reordering, anything SortableJS would otherwise own), used only inside the
  desktop app's window: its native file-drop handling swallows the HTML5 drag
  events SortableJS is built on there, so it runs with `forceFallback: true`
  and this is what draws the fallback drag under a pointer instead.
- `services/appStatus.js` — `window.PlexoraStatus`, the app-wide status
  indicator. See its own section below.
- `services/viewerLoader.js` — `window.PlexoraViewerLoader`, the centre spinner
  over the image itself. A sibling of `appStatus.js`'s `watchViewer` (both
  watch the same OSD viewer) answering a different question: the navbar chip
  says "work outstanding", this says "nothing to look at yet". Loaded by
  `index.html` only, deferred and before `main.js` — the element is
  server-rendered visible so the spinner shows before any script runs, and
  `watch()` has to be defined by the time `imageViewer.js` calls it. See
  "Status Indicator (`PlexoraStatus`)" below.
- `services/appRouter.js` — `window.PlexoraRouter`, internal navigation that
  does not throw the viewer away. See "Navigation and the App Shell" below.
- `views/panCursor.js` — grab/grabbing on OpenSeadragon's canvas. The resting
  `grab` is a rule in `viewer.css`; this file adds `.is-panning` for the length
  of a drag, because OSD's `preventDefault` on the press kills the mousedown
  `:active` is driven by. Document-level CAPTURE listeners, since OSD also
  `stopPropagation`s what it handles and the canvas element belongs to whichever
  viewer built it. A tool that writes `style.cursor` on that element (roi's
  `roiTools.js`, figure_builder's `figureCaptureTool.js`) still overrides both;
  those two clear the inline value instead of naming `default`/`grab` so the
  canvas's own pair comes back. Loaded by `index.html` only, deferred.
- `services/pageBoot.js` — `window.PlexoraPage`, the registry every page
  controller mounts through instead of `DOMContentLoaded`.
- `src/shaders/{vert,frag}.glsl` — the colorize/composite shaders.
- `pluginRegistry.js` — `window.Plexora.registerPlugin`, the client half of the
  plugin contract. Documents two optional hooks, `captureCarryState()` /
  `applyCarryState(state)`, for a panel that has something worth carrying to
  the next sample in a dataset (see `services/carryOver.js`). Core names no
  plugin; gating, cell_explorer and transcripts implement the pair, roi and
  figure_builder deliberately do not, since their state (a region's geometry,
  a capture) is inherently about one image. Also documents an optional `help:
  {summary, notes?, shortcuts?: [{keys, label}]}` — core draws the `?` and the
  modal (`toolLoader.js`'s `attachHelp`, `views/pluginHelp.js`) and adds the
  open/close chord row itself, so a plugin writes only the descriptor. Gating
  is the first adopter. Also documents the `data-viewer-furniture` attribute:
  chrome a plugin appends to `#openseadragon_wrapper` (a dock, a floating
  panel) may carry it, and core's own canvas popups — the dataset thumbnail
  grid below — measure those elements and keep off them, so core never has to
  name a plugin's class. Figure Builder's `.fb-dock` sets it. Also documents
  `lazy: boolean` — the definition's script is on every viewer page rather
  than fetched when the tool opens (core's Rotate & Flip,
  `views/viewTransformTools.js`), so `main.js` activates it at boot only when
  the page already staged its panel (`?tool=`), and `toolLoader.js` activates
  it on open otherwise, as it does a plugin whose scripts it just fetched —
  without the guard a definition that is always registered would be set up
  against a panel that is not there. And `hasLayer: boolean` (default `true`):
  `false` means the tool draws nothing, so `toolLoader.js`'s `buildCard` gives
  its card no eye and no `onToggle` — there is nothing of its to hide.
- `views/datasetNav.js` — `window.PlexoraDatasetNav`, the Previous/Next chip
  top-right of the canvas, muted until the pointer is near it. Walks
  `Dataset.projects` — the order samples were added — never the Samples
  page's default "last opened" sort, since every open rewrites that key and
  a walk following it would reshuffle under the user. One `GET /datasets`
  resolves which dataset the open sample belongs to and its neighbours;
  bare `PageUp`/`PageDown` move the same way, disarmed while a routed page
  (Settings, Figures) sits over the viewer. It mounts off its own fetch and
  asks `main.js` for nothing, so it still works on a sample whose image
  failed to load and has no viewer at all — the way out of that sample IS
  this control. `go(target)` calls `carryOver.stash(target)` before a full
  page navigation (`PlexoraRouter.go`, or `window.location` if the router is
  absent): the server holds one loaded datasource and the viewer has no
  teardown path, so this cannot be anything softer. `go()` also closes the
  thumbnail grid below, since a B/N walk with it open has nothing left to
  show. The "2 / 12" counter is itself a button now
  (`aria-haspopup`/`aria-expanded`); pressing it opens `datasetStrip.js` under
  the whole chip, and a pick there calls this same `go()`.
- `views/datasetStrip.js` — `window.PlexoraDatasetStrip`, every sample's
  thumbnail (`GET project_thumbnail/<name>`) in a grid under the "2 / 12"
  counter, for reaching the fortieth sample rather than the next one. Exactly
  the chip's measured width (key caps included), one thumbnail per row, tiles
  sized from that width at 4:3 and set inline; a full column is followed by
  another beside it, reached by scrolling sideways (a vertical wheel is mapped
  to it). `fit()` is pure
  arithmetic over the boxes of whatever else already sits on the canvas (the
  caption, the sidebar expand button, the channel legend, the mini-map lens,
  and any `data-viewer-furniture` — see `pluginRegistry.js` above) and is
  re-measured on a `ResizeObserver` and on resize, since the legend grows with
  channel count and the sidebar collapses without an event. It only picks —
  `open()`'s `onPick` is `datasetNav.js`'s `go()`, so a jump carries exactly
  what Next does; it never navigates or persists anything itself. Dismissal:
  `Escape` alone, captured ahead of the page (B/N/PageUp/PageDown still walk
  with it open), an outside `pointerdown`, `plexora:viewer-hidden`, or the
  counter pressed again. Lives in `#openseadragon_wrapper`, not a portal,
  because fullscreen fullscreens the whole page and OpenSeadragon only listens
  on `#openseadragon`. Its z-index is 250 — above the plugin dock and scale
  float at 240, below tooltips at 400.
- `services/carryOver.js` — `window.PlexoraCarryOver`, what survives that
  navigation. `capture()`/`stash(to)` on the way out, `take(datasource)` on
  the way in, both through `sessionStorage` (key `plexora:carry-over`,
  `VERSION = 1`, dropped after `MAX_AGE_MS` = 2 minutes so a tab left
  overnight cannot apply a stale arrangement). **The rule: an ARRANGEMENT
  travels, a MEASUREMENT does not** — which channels are on and their
  colours, which layers and tools are visible, which cell mode and column,
  travel; a contrast window, a gate threshold, a viewport are readings off
  one image and are left for the next sample's own saved restore to supply.
  Every capture and every restore point is wrapped independently
  (component-wise and fault tolerant), and what could not be carried is
  collected and said ONCE through `report()`/`flush()` — a
  `services/toast.js` notice, or silence when everything applied, or silence
  when `viewerErrorState.js` is already showing something louder. Nothing
  captured here is persisted back to the new project; it rides the same
  launch-state path a notebook's `?channels=` argument uses.
- `services/renderClipboard.js` — `window.PlexoraRenderClipboard`, the Image
  card's copy/paste (its `•••` menu, `views/layerManager.js`). Two independent
  `sessionStorage` slots under key `plexora:clipboard` — `names` (the
  reference image's channel names, Area excluded, what `POST /rename_channels`
  takes back) and `rendering` (marker/colour/on-off/contrast per slot, the
  Image layer's opacity, HD mode; never the names, never image data) — copying
  one never drops the other. `sessionStorage`, not `carryOver.js`'s carry, for
  the same reason `carryOver.js` itself gives: a full page load intervenes
  before there is anywhere to paste. The planners `mergeNames` (by position, as
  far as the two lists overlap) and `resolveSlots` (by name, then position) are
  pure and exported so a node probe can pin a paste without a page.
- `views/popoverMenu.js` — `window.PlexoraMenu.open(anchor, items, {align})` /
  `close()`, a small action menu floated under a button (the Image card's
  `•••`). Items are `{label, onSelect?, disabled?, className?, checked?,
  title?}`, `{separator: true}`, or a row of glyph actions over one label —
  `{label, actions: [{icon, title, onSelect?, disabled?}]}`, drawn as
  `.plx-menu-row`/`.plx-menu-row-label`/`.plx-menu-row-actions`/
  `.plx-menu-action` — for a menu whose items come in copy/paste pairs over
  the same noun, where four sentences would say it twice each; each action's
  `title` is both its tooltip and its accessible name. `checked` (a boolean)
  makes a row one of a radio set — `role=menuitemradio`, `.is-checkable`/
  `.is-checked` in `main.css`, a tick in a gutter every row of the set keeps
  so the labels line up whichever is current — the Visium HD heatmap's Mean /
  Sum / Max / Min; a plain item's `title` is now also read as its tooltip. A
  second `open()` on the SAME anchor closes the menu rather than reopening it
  on top of itself — caught in `open()`, not left to the document-click
  listener, because an anchor inside a card header stops its own click from
  propagating (or the header would fold) and the document never hears it;
  ROI's tree had the same bug before this primitive existed. One menu at a
  time; through `PopoverPortal` like every other viewer popup, not `<body>` —
  a menu appended to `<body>` opens under the fullscreen backdrop and cannot
  be seen. Modelled on `plugins/roi/static/roiTree.js`'s `popup`/`menu`, which
  stays where it is; ROI's and Transcripts' menus may move onto this later.
  `.plx-menu-item.is-destructive` (an item's undo-ish action, red on
  hover/focus) is now `main.css`'s rather than scoped to one plugin's
  stylesheet, since the gene list's menu — shared by Transcripts and Visium HD
  (see `views/geneList.js` below) — needed the same rule over squares as over
  molecules. Loaded from `base.html`, before `searchableSelect.js`.
- `views/geneList.js` — CORE'S, not either plugin's, because two plugins draw
  the identical selected-genes tree: the Transcripts layer (a Xenium run's
  molecules) and the Visium HD bin layer (its squares). Two copies of one
  interaction drift apart one fix at a time; one copy cannot. Three classes:
  `PlexoraGeneGroups` (pure functions of a layer's own `state` — create/
  rename/remove a group, assign a gene into one or out of every one, fold —
  which is what lets a node probe check them with no page). `create(state,
  name, extra = {})` takes a plugin's own fields for the group — the Visium
  HD composition's `agg` — written beside the name and genes, so Visium HD's
  groups carry an aggregation rule Transcripts' never need; `normalize(state,
  known)` keeps whatever extra fields a group already has (`{...group, name,
  genes}`) rather than rebuilding the object, so a plugin's own field
  survives a reload unmentioned. `PlexoraGeneTree`
  (rows, group headings, `bindListActions` for the whole list's own eye and
  fold, `openListMenu` for the `+` button beside the search box — "Create
  gene groups…", reset colours, clear all genes — and `addGroups` for what a
  group-file import hands back). `options.groupExtras(group) -> [nodes]`
  inserts a plugin's own controls between a group heading's name and its
  delete button — Visium HD's per-group `vhd-agg-button--group`, the
  composition's Mean/Sum/Max/Min for that group — the same slot
  `options.rowExtras(gene) -> [nodes]` already gave a single row (the
  Transcripts icon button). `onChange(kind)` fires "list" after
  anything that changed which rows exist, and "color" after a colour pick —
  which must NOT repaint the tree, because the swatch picker that made it is
  still open inside it. `PlexoraGeneVocabulary` is the search behind a
  vocabulary too long to list — eighteen thousand genes on a Visium HD run —
  `match(query)` returns at most 50 (exact, then prefix, then substring, each
  in order of abundance; an empty query lists the most abundant rather than
  the first fifty alphabetically), handed to `SearchableSelect` as its new
  `match:` option, which replaces the substring filter over `options` for
  exactly this case. A layer offers the tree the same short list of methods
  both layers already had under the same names (`state`, `countOf`,
  `isHidden`, `setGeneHidden`, `colorFor`, `setColor`, `addGene`,
  `removeGene`, ... and the group methods, delegated to `PlexoraGeneGroups`);
  the tree never learns whether a gene is drawn as molecules or squares.
  Classes are `gene-*`, moved into `main.css` out of `transcripts.css`.
  Loaded from `base.html`, non-deferred, right after `colorSwatchPicker.js`
  and before `geneGroupModal.js` — plugin scripts are not deferred either, and
  both plugins' panels reach for these globals on mount.
- `views/geneGroupModal.js` — also core's, for the same reason: making a
  group over molecules and making one over squares are the same act. Moved
  here from `transcripts/static/transcriptGroupModal.js`.
  `PlexoraGeneGroupModal.open({parse, genes, match, existing, onApply})` —
  `parse` is the one thing that still differs, a callback rather than a
  hard-coded route, because each plugin posts the chosen file to its OWN
  `/groups` (`server/utils/gene_groups.py`, see the Repository Map entry
  above) against its own vocabulary. Two ways in, side by side: name one group here, or bring a file
  shaped like Xenium Explorer's own import (a gene column, then one column
  per group it belongs to). A `<dialog>` opened with `showModal()`, same as
  `requirementsModal.js`; classes are `gene-modal-*`, also moved into
  `main.css`. The transcripts plugin's `scripts=` no longer lists
  `transcriptGroupModal.js` at all.
- `views/pluginHelp.js` — `window.PlexoraPluginHelp.open(...)`, what the `?` in
  a tool card's header opens (`toolLoader.js`'s `attachHelp`, drawn only for a
  plugin whose definition carries a `help` descriptor — see `pluginRegistry.js`
  below). The modal is `PlexoraConfirm.tell` with a `content` node built here
  with `textContent`, so a plugin's help strings stay text all the way to the
  screen; the open/close chord row is read off the tool's own Tools-menu
  binding, not restated by the plugin, so it can never disagree with it. Its
  CSS is `.plx-tool-help*` in `main.css` — a DIFFERENT namespace from
  `importHelp.js`'s `.plx-help*` below; the two colliding once made the plugin
  help modal 720px wide. Loaded from `index.html`.
- `services/toast.js` — `window.PlexoraToast`, core's first toast: bottom
  right, twenty seconds, hover or focus pauses the clock, one notice at a
  time (a second `show()` replaces rather than stacks). `show({..., actions,
  onDismiss, tone})`: `actions` is `[{label, onSelect, primary?}]`, small
  buttons under the text — a press dismisses the notice and then runs
  `onSelect`, unless `onSelect` returns `false` (still busy); `onDismiss(why)`
  fires once, `why` one of `user` (the ×), `action`, `timeout`, `replaced` (a
  newer notice took its place) or `caller`; `tone: "warning"` draws
  `.plx-toast.is-warning`, an amber edge for a notice about something broken.
  The returned handle gains `isLive()`. This is what `resourceStatus.js`'s
  disconnection notice and its Reconnect button are built from — it used to be
  a strip across the top of the page and is now a corner toast; see that entry
  below. Distinguished from
  `appStatus.js` (three things about the app as a whole) and
  `confirmDialog.js` (a decision) by being neither — a thing that
  already happened, that nothing is waiting on.
- `views/viewerErrorState.js` — `window.PlexoraViewerError`, the canvas
  saying why there is no picture on it. Three statuses, three sentences —
  `missing`/`inaccessible`/`corrupt`, from `data_model.image_status` — over
  the canvas with a "Repoint this sample" / "Back to samples" pair; the
  fourth status, `unavailable` (a node not answering), is deliberately
  excluded, since `resourceStatus.js` already owns that case with a modal or a
  Reconnect toast. Only the card itself takes pointer events, so the navbar,
  status chip and above all `datasetNav.js`'s Previous/Next go on working
  under it.
- `services/datasetContext.js` — client mirror of the server dataset contract,
  handed to each plugin as `ctx.dataset`.
- `services/dataLocation.js` — `window.PlexoraDataLocation`, the compact
  **L | R** switch every data-selection field gets (one letter each because it
  sits inside the field's row; the meaning is on the per-button `aria-label`
  and a `data-tooltip` on the group reading "Data Location — (L)ocal |
  (R)emote"). **One render, everywhere** — the two surfaces that give the
  switch a row of its own rather than a field to share, the home page and the
  import dialog, spell the missing word out around it instead: a `Label`-step
  kicker before the chip ("Image location" / "Data location") and a caption
  after it ("this computer", local only — on Remote the switch's own place
  button stands there and names the machine, and unlike the caption it is
  clickable). Two renderings of the switch itself would be two things to learn
  for one question, and inside a dialog the tooltip is clipped by
  `.plx-dialog { overflow: auto }` anyway, so the label and caption are what
  carry the meaning there. `attach()`
  renders on **every** launch (`available()` is unconditionally true) because
  there is always somewhere else a file could be. What each half means is
  derived, not configured — `plainPath()` is the one predicate everything hangs
  off, and it is true only when the machine holding the file is the machine
  running Plexora:

  | switch | server is here | server is elsewhere |
  |---|---|---|
  | This computer | plain path (today's behaviour) | the `role: "client"` node, or a CSV upload |
  | Remote → the server | (not offered) | plain path |
  | Remote → a saved connection | that profile's node | that profile's node |

  Produces the same shapes every form already took — a path, an uploaded
  file's server-side path, or a `node://<node>/<resource>` locator — so
  nothing downstream of the form learns a new shape. `setVerbatim()` (not
  `setWhere`) is what the node chips call: it picks whichever mode submits the
  box unchanged, which differs by where Plexora runs.

  **Choosing Remote is a 0/1/many flow (`choosePlace()`), not always a list.**
  No machine reachable opens `PlexoraConnectionModal` directly, with an
  `intent` sentence explaining why; exactly one reachable machine is adopted
  silently — nothing to choose from is not a choice; several open
  `PlexoraPlacePicker` as before. "Reachable" means the server itself when
  Plexora runs elsewhere, or any saved connection with a data node already
  open — a saved-but-not-connected profile does not count, since offering it
  would adopt a machine the field cannot yet read. The place chip (the pill
  showing which machine is chosen) always calls `choosePlace(force=true)`,
  which skips the 0/1 shortcut and opens the list regardless — the one-machine
  case is a shortcut, not a one-way door.

  **`attach()` never calls `onChange` at mount.** It used to, and that reached
  the caller's handler before `attach` had returned the handle the handler is
  written against — a TypeError that escaped the loop mounting all three import
  fields, so the form shipped with a switch on the image and nothing on the
  mask or the table. Nothing has changed at mount; there is no event to send.
  The mounting loop in the now-deleted `importFormValidation.js` also
  try/catched per field, so one field could never again cost another; anything
  that still mounts `attach()` in a loop over several fields needs the same
  guard.
- `services/remoteState.js` — `window.PlexoraRemotes`, the one owner of "what
  are the remote connections doing?". Four surfaces used to ask that
  independently, with their own timer, their own copy of the state list and
  their own idea of which prompts are secret — so Settings masked a host-key
  fingerprint the machine picker showed in the clear. `subscribe(cb,
  {active, focus}) -> unsubscribe` delivers a merged snapshot: `GET
  /settings/remotes` (viewer halves) joined with `GET /data_places` (node
  halves) into one `entries` row per saved profile, `half(entry, kind)`
  picking the one a caller wants. The node half carries TWO names --
  `node.node` from the session and `node.registered` from the registry. A
  surface asking only "is anything up?" may test either; one MATCHING a name
  (against `/resource_routing`, say) must use both, or it compares the empty a
  session-less node leaves with the empty a local project routes to and calls
  that a match. It also carries the job's clock: `node.timeLeft` /
  `node.timeLimit`, plus `at` (when the snapshot arrived) and
  `remaining(entry)` / `duration(seconds)` / `WARN_SECONDS` (600). `remaining`
  INTERPOLATES against `at` rather than reading `timeLeft` straight, because
  the poll deliberately stops when everything is settled — which is the state
  a four-hour job sits in for four hours, and a countdown that only moved when
  a request came back would sit frozen for all of it. `isOpening(state)`, `label(state)`,
  `isSecret(text)` and `promptChoices(text)` are the one implementation of
  each judgement, shared so no two surfaces can disagree about them again.
  **`promptChoices` is `isSecret`'s divergence repeating one layer up**: the
  two surfaces agreed about which prompts are legible and then disagreed about
  what to do with a legible one — the connection dialog drew Yes and No, the
  Settings card drew a bare box and a Send button, so a host-key question was
  one click on one screen and a guess at a magic word (`yes` spelled out; ssh
  rejects `y`) on the other. It returns `[{label, value}]`, `[]` for anything
  with nothing to press, and is deliberately NARROWER than `!isSecret`:
  `isSecret` also lets through anything mentioning a fingerprint, and Yes/No
  pinned under a question that is not a yes/no question is worse than a plain
  box. Both surfaces make the FIRST choice the primary button and demote
  `Send` beside it, and both refuse to send an empty box when there are
  choices — `Send` used to be primary over an empty field, so the most
  prominent button on a host-key prompt submitted nothing and ssh asked again.
  The box itself never goes away: OpenSSH takes the fingerprint back as a
  third answer, and that is the one answer that verifies the host rather than
  trusting it. Pinned in `remote_state_probe.mjs` (the predicate) and in both
  surface probes (that they draw from it). `connect`/`disconnect`/
  `answer`/`forget` all act through the profile name plus a
  `KIND_VIEWER`/`KIND_NODE` kind and refresh the snapshot afterwards. **There
  is no `save`** — it was the browser's only caller of `POST
  /settings/remotes`, and once Settings' own form was replaced by the recipe
  catalogue nothing wrote a profile that way any more; every profile the
  browser writes now goes through `POST /settings/recipes/<id>`. `focus`
  may be an object, an ARRAY of them, or a FUNCTION returning either — the
  Settings page passes a function, because which cards have their log expanded
  changes as the user opens and closes them and re-subscribing on every toggle
  would tear down the subscription in order to preserve what it is preserving.
  **The
  poll (`POLL_MS = 1000`) is scoped**: it runs only while there is at least
  one subscriber AND (a session is in an `OPENING` state, or an `active`
  subscriber exists) — a settled connection watched only by the navbar globe
  costs nothing at all, which is what lets the globe sit on every page for
  free. One in-flight request is shared across every subscriber, so a modal
  open beside the Settings page is one round trip, not two. `publish()` also
  diffs consecutive snapshots (`nodeChanges`) and, when a profile's node half
  changed being-up, map name or registry name, dispatches a `window`
  `CustomEvent("plexora:remote-nodes-changed", {detail: {changed}})` — the
  event `main.js`'s `repairRouting()` listens for, since `main.js` resolved
  tile routing once at boot and is not itself a `PlexoraRemotes` subscriber.
  Only a real transition fires it: the first snapshot has nothing to diff
  against, and a failed poll republishes the same `entries` object.
  `merge()` also carries `workstation` (the profile's `{os}`, or null) and, on
  the node half, `osMismatch` (`{expected, found}` off `/data_places`'
  `os_mismatch`) -- a note on a connection that WORKED, since everything it
  needed was built before the mismatch could be known, so it rides beside the
  connection rather than in place of it.
- `services/connectionModal.js` — `window.PlexoraConnectionModal`, the one
  place a connection is watched from wherever it was started.
  `open({name, kind, intent}) -> Promise<{connected, name, node, kind, label,
  detail}>`. A native `<dialog>` + `showModal()` — top layer, so it is NOT a
  `PopoverPortal` case, unlike `remoteGlobe.js` below. Its progress steps map
  1:1 from the server's own states, with two drawn only for a profile that
  actually does them: the scheduler step for one that waits in a queue, and
  "Installing Plexora" for one with `install` on — labelled with the
  environment's name when the server sent one (`install_env`, derived once by
  `connect.environment_label`; **nothing here parses a launch command**); the log pane is
  `services/logTerminal.js`, fed by the focused connection's `?log=200`
  status; the ssh prompt is shown verbatim, masked only when
  `PlexoraRemotes.isSecret()` says so; a failure is drawn against the step
  that was running, with a retry; and closing the window is offered as a
  choice separate from ending the connection ("Continue in background" vs
  "Stop connecting"), because a queued job is a real fifteen minutes and the
  ssh belongs to the server, not the dialog. "Continue in background" and
  Escape both call `leave()`, which does more than close the dialog when the
  connection is still opening: a module-level watch (`watchInBackground`, an
  ACTIVE `PlexoraRemotes` subscription so it can hear a FAILURE, keyed
  `kind:name`, persisted to `sessionStorage`
  `plexora.connectionModal.background` and picked back up by
  `adoptBackground()` through `PlexoraPage.register` on a full page load)
  reopens the same dialog the moment the connection needs somebody again — a
  NEW question, not the one on screen when it was backgrounded, which
  instead gets a sticky "waiting for an answer" toast with an Answer action —
  defers while another dialog is already open, shows a "Connected to
  “name”" toast on success, and ends quietly when the connection simply
  stops. `open({name})` and `begin()` both clear any watch on that name
  first, since opening it by hand (or the watch itself reopening it) makes
  this window the watcher again. Exports `adoptBackground`, `_background`
  (for a probe: which connections are being watched unseen). Also owns the "Add a server"
  recipe flow (`GET /settings/recipes`, `POST /settings/recipes/<id>`),
  composed and connected without a detour through Settings. The recipes cache,
  `loadRecipes()` and a single card, `recipeCard(recipe, onPick)`, are MODULE
  scope rather than dialog-local, because `recipeGrid(onPick)` — the
  `.connect-recipes` grid built from them — is EXPORTED
  (`return { open, recipeGrid, STEPS, stepStates }`) so `settingsPage.js` can
  draw the identical catalogue straight into its own page, not just inside
  the dialog; the fetch happens once per page load either way, not once per
  dialog open. `open({view: "recipes"})` lands straight on the catalogue —
  reachable from `main.js`'s "Connect another machine…" and from flipping a
  data field to Remote with nothing saved. `open({view: "recipe", recipe,
  remote})` skips the catalogue for one preset's form directly, `remote` a
  saved profile or `null`; `recipeForm(recipe, saved)`/`gcloudForm(recipe,
  saved)` prefill every box from `saved` when given one, the title reads
  `Edit "name"`, there is no Back, and the button is "Save changes" —
  `submitRecipe(recipe, boxes, editing)` saves and closes WITHOUT connecting,
  since editing an address is not the same request as opening one.
  `recipeForm()` draws one more control, `choiceField`, when `"os"` is in
  `recipe.ask` and the recipe carries `os_choices` — the workstation preset's
  one extra question, appended after the standard boxes rather than replacing
  them, since this recipe has no `flow` of its own (`choiceField` returns a
  handle, not an element, so it is `.wrap` that gets appended).
- `services/logTerminal.js` — `window.PlexoraLogTerminal.create({title,
  empty})`, the connection log, shared by the modal and the Settings cards.
  Follows its own output while the reader is at the bottom, stops the moment
  they scroll up, and follows again when they return; `paint(lines)` compares
  before touching the DOM, so an unchanged poll costs nothing. One element per
  line, and a line ssh relayed from the far machine (`  [ssh] …`, as
  `connect._Watched` writes it — the remote command's stdout AND stderr, which
  ssh merges) is marked `is-relayed` so the machine's own words read as output
  rather than as narration. **Keep the element and repaint it**: the Settings
  cards used to be rebuilt on every poll, so the pane was a new element once a
  second and started at the top once a second, which is exactly when there is
  something in it worth reading.
- `services/failureMessage.js` — `window.PlexoraFailureMessage.paint(element,
  message)`, the layout of a connection failure, shared by the modal, the
  Settings cards and the navbar globe's panel for the same reason
  `logTerminal.js` is. What `connect.py`
  builds is three things in one string — a headline, the last lines the far
  machine printed (indented four spaces), and the fix — and both surfaces
  assigned it to `textContent`, which collapses the newlines and makes one
  grey paragraph of all three with a cluster's login banner in the middle.
  Indented runs become `.connect-failure-quote`, everything else
  `.connect-failure-text`, all inside one `.connect-failure` wrapper (the
  Settings notice is a flex row, so siblings would sit side by side). **Only
  the quote is capped** (main.css, five lines and the top of a sixth, with its
  own scroll): the last paragraph is almost always the fix, and `.settings-
  remotes` is a grid of equal-height rows, so an uncapped forty-line pip
  failure made the two healthy cards beside it forty lines tall too.
  Repainted, never rebuilt — the card repaints once a second.
- `services/remoteGlobe.js` — the navbar globe and its connection panel,
  mounted on `#remote_globe` (an empty mount in `base.html`, before
  `#app_status`). A passive `PlexoraRemotes` subscriber until its panel opens,
  which is what keeps it free while everything is settled. Uses
  `PopoverPortal` (it is in `tests/test_popover_portal.py`'s
  `VIEWER_POPUPS`). Mounts **once**: the navbar markup is never swapped by
  `appRouter`, so `PlexoraPage.register` returns `null` for it and a
  module-level `mounted` guard makes a re-run a no-op — the same reason
  `segmentationWait.js` guards its chip. The panel is a **status board with a
  switch on it**: one two-line row per saved profile (name + state; then health
  + latency + a per-row connect/disconnect + a monitor icon saying whether the
  image on screen is being read from that machine), and nothing identifying or
  typeable on it — no address, no username, no ssh option — because it opens
  over the viewer and in every screen-share. Adding a machine is a link to
  `settings#remotes`. Two fetches, both once per panel open and neither polled:
  `resource_routing` (which node the image comes from) and `remote_health`.
  `machineSummary(probe.machine)` draws one line ("24 cores · 128 GB · RTX
  3080 · 188 GB free") when the health probe answered and said anything at
  all, joining only the parts it was given — never for a row that is not
  currently answering, since these facts arrive on that same probe and a
  stale row describing an unreachable machine would be describing what it
  cannot currently reach; a GPU name is trimmed of its vendor/marketing prefix
  to fit the row. Its tooltip says these are what the machine HAS, not what
  this session gets, because a shared workstation is shared.
  The monitor is matched through `nodeNameOf(entry)` (session name, else
  registry name) and **both sides must be a real name**: a local project routes
  to null and a node that outlived its session had a null session name, so
  `null === null` lit the cluster's monitor while the picture was being read
  off the user's own disk -- and lit the local row saying the opposite in the
  same list. Exactly one monitor in the list is lit. A row inside a scheduled
  job also carries `PlexoraRemotes.remaining()` as a clock, amber in the last
  ten minutes; rows with no walltime carry nothing, because most connections
  have none and an empty slot per row would spend the panel's width saying so.
  `staleNodes(datasource, candidates)` compares `PlexoraRouting.held(datasource)`
  (what THIS page's tiles were actually built from) against a fresh
  `/resource_routing` answer; on a mismatch the row draws "Reconnected" over
  an otherwise-healthy probe AND the panel dispatches
  `plexora:remote-nodes-changed` itself — a browser-side counterpart to the
  server's `stale` health state above (both catch a project still addressed to
  where a node was before it reconnected), and the backstop for a reconnect
  made from another tab, which no poll in this one was awake to see.
- `services/sessionExpiry.js` — `window.PlexoraSessionExpiry`, the dialog that
  interrupts before a scheduled job ends. Loaded on every page after
  `connectionModal.js` (whose dialog its one button opens) and mounted once
  through `PlexoraPage.register` with a module-level `started` guard, the same
  shape as the globe's `mounted`. **Two moments only**: ten minutes out, and at
  zero. Subscribes PASSIVELY and counts down locally off
  `PlexoraRemotes.remaining()`, with a 15 s interval that runs only while
  something has a clock — an active subscription would turn a settled
  four-hour job into a request a second. `told[name]` is cleared when a
  connection's remaining time goes back UP, which is exactly what a reconnect
  does, so a fresh job is warned about again and a running one is not warned
  twice. `staleAtBoot` is the same idea about the PAGE rather than the
  profile: a job already at zero on this page context's FIRST loaded snapshot
  was not alive in this session, so it is never announced as expired — a
  fresh profile, a cleared site, or a second machine used to open straight on
  "has run out of time" about yesterday's job — and a name drops out of the
  set once its remaining time climbs back above `WARN_SECONDS`, so a fresh job
  on the same machine is watched again from then on. An open dialog closes
  itself when its clock goes away -- somebody who
  reads it and goes and disconnects has answered the question. "Start a new session" disconnects the node FIRST (the old entry names
  a port whose tunnel has gone, and it is what `nodes._disconnected` keys on)
  and then opens the connection dialog.
- `services/resourceStatus.js` — `window.PlexoraResourceStatus`, why a layer
  is missing. The `.resource-status-banner` strip across the top of the page
  is GONE, and so is `main.js`'s sweep for it on a routing repair — a
  disconnection is now a `services/toast.js` warning notice in the corner
  (`announce`), raised ONLY for a node this tab has actually seen up
  (`upThisSession`, a `Set` fed by `report()`'s own `boundNodes(routing)` on an
  answer with nothing missing, and by `plexora:remote-nodes-changed` rows with
  `up: true` — the globe, a dialog or Settings connecting something counts). A
  node in `status.nodes` that was never in that set is not a disconnection —
  typically an entry a previous run's session left on `nodes.json` — and
  raises nothing here at all; the navbar globe already shows every machine's
  state, and a warning about something nobody did this session was the noise
  this replaced. **A modal only for a node this server could reconnect on its
  own** (`offerToConnect`, unchanged: `/resource_status`'s `profiles`, asked
  once per tab); a node that was up and dropped gets the notice's Reconnect
  button instead, through the new exported `connectAndReload(datasource,
  profile, kinds)` (shared by the modal's Connect and the notice's Reconnect —
  open `connectionModal.js`, then `POST /reload_datasource` since the server
  keys "which project is loaded" on the NAME, then reload the page); a node
  with no profile that can reach it gets "Open Settings" instead of Reconnect.
  Two per-tab memories: `asked` (the modal has been answered, so
  navigating does not re-ask) and `dismissed` (the notice too, only on the ×
  — see `onDismiss` in `toast.js`); both are
  dropped by `forget()` the moment the project opens whole, so connect-work-
  disconnect-reopen in one sitting is asked about again rather than met with
  the silence of an answer given about a situation since fixed and rebroken --
  which is why the route is asked even when the notice was dismissed. Declining
  or a
  connection that fails BOTH leave the notice — the promise resolves when the
  connection attempt settles, not when the dialog closes, or a cancelled
  connect left a missing layer with nothing on screen about it. `report(datasource,
  routing)` no longer takes a `host` element to draw into — a toast mounts
  itself. Test file renamed `tests/test_resource_status_banner.py` ->
  `tests/test_resource_status_notice.py`.
- `services/fileLocation.js` — `window.PlexoraFileLocation`, "which machine?"
  asked of every file button at once, so every plugin's Upload/Download honours
  Local/Remote without its form changing. `dataLocation.js` asks this question
  per FIELD by building the switch in; this asks it at the one place every
  button passes through -- the click -- with one bubble-phase delegate on
  `document` for `input[type="file"]` and `a[download][href]`. Does nothing at
  all when `remoteAvailable()` (synchronous, off a passive `PlexoraRemotes`
  subscription -- the check runs inside a click handler, and a promise cannot
  be awaited before `preventDefault()` without losing the transient user
  activation a file dialog needs) says nowhere else is reachable, so an
  install with one machine is untouched. `deliver(blob, filename)` is the
  documented way in for anything built in the tab rather than clicked --
  `form.submit()` fires no event and a detached anchor never bubbles here --
  and with nowhere else to send it, saves locally without touching the
  network, which matters because one caller is the emergency export offered
  when the server has stopped answering. `data-file-location="local"` on an
  element or an ancestor opts it out, for a core field that already has its
  own switch (`dataLocation.js`, `views/channelNamesUpload.js`) and must not
  ask the same question twice in two shapes. Loaded on every page from
  `base.html`, after `connectionModal.js` (whose "Connect another machine…"
  escape hatch it opens) and mounted once at parse time rather than through
  `PlexoraPage`, because its listener is on `document` and survives a routed
  page swap. `plugins/gating/static/gatingApi.js`'s `downloadGatingCSV` forks
  on `remoteAvailable()` between the streamed hidden-form download (one
  machine) and a fetch + `deliver()` (more than one); `plugins/roi/static
  /roiApi.js`'s `saveBlob` calls `deliver()` the same way, keeping its anchor
  as the offline path; `plugins/gating/static/csvGatingList.js`'s upload arrow
  now uses `elem.click()` rather than a hand-rolled, non-cancelable
  `initEvent`, because a click this layer cannot intercept is a click that can
  never reach a remote machine.
- `services/placePicker.js` — `window.PlexoraPlacePicker`, the modal behind
  Remote when there is more than one machine to choose from. Lists `GET
  /data_places`. Its own password-prompt renderer and state chip are gone —
  pressing Connect on an entry that is not up now opens
  `connectionModal.js` on top of the list, which is still there if the modal
  is cancelled; the connection modal and the Settings cards are the only two
  surfaces left that render a prompt inline. Resolves `{id, kind, label,
  node}`; `node` is the only part a field uses. This is the whole of "choose
  where the data lives when you add it" — nothing is configured in advance.
- `services/pathPicker.js` — `window.PlexoraPathPicker`, a directory-listing
  modal (`POST /list_dir`, optionally `{node}`) that stands in for a native
  dialog on a machine with no desktop (a compute node). Not a file manager —
  no rename, delete or upload — and not a replacement for typing a path.
  `pick({mode, filter, start, title, node, multiple})`; `multiple: true`
  answers `string[]` but no caller wires it yet. DOM is built node-by-node, no
  `innerHTML`, so `tests/js/path_picker_probe.mjs` can run it. Back/Up/Refresh,
  an address bar, an in-folder name filter, a hidden-files toggle, a Type
  column, keyboard nav with listbox ARIA, and a places sidebar
  (Home/Pinned/Recent) backed by `/picker_prefs`. The address bar
  (`.path-picker-address`) is one wide strip holding the crumbs: clicking a
  crumb navigates (its handler `stopPropagation`s), and clicking anywhere else
  in the strip turns the whole thing into a path box with its contents
  selected. It was a pencil glyph at the end of the trail, which nobody found.
  For mode `"any"`, a folder whose name ends in `.zarr`/`.ome.zarr`
  (`STORE_SUFFIXES`/`isStore()`) SELECTS on a single click instead of
  navigating, double-click chooses it, a "›" button (`.path-picker-enter`) on
  the row is the way into it, and a "Use this folder" footer button answers
  with whichever folder is open — the escape hatch for a store not named
  `*.zarr`. `last_dir` is written on the dialog's `close` — not on a successful pick —
  because Esc and the backdrop close without going through `finish()`, and
  because browsing is the part that costs the effort: cancelling is not an
  instruction to forget. `add_recent` still rides only on a real pick, and a
  close that never moved writes nothing at all. Three rules the rest of the
  file follows: **the client does
  no path arithmetic** -- every path it navigates to came from the server
  (`entry.path`, `crumbs[i].path`, `parent`); **`state.here` is assigned in
  exactly ONE place, from a server answer**, so a failed listing changes
  nothing; and **nothing about remembering places may block browsing** -- a
  failed `/picker_prefs` means no Recent list, not a picker that will not
  open. Esc inside the filter or path box must `preventDefault`+
  `stopPropagation`, or the `<dialog>` reads it first and cancels itself.
  `browsePicker.js` passes `start` (the field's current value, read at click
  time) through as where the listing fallback should open, and relays `node`
  so the fallback lists the SAME machine the native dialog would have opened
  on -- the only branch a cluster field ever takes, since it has no desktop.
- `views/channelNamesUpload.js` — `window.PlexoraChannelNames`, the dialog
  behind the sidebar's channel-rename button. One `<dialog>` with three stages
  (which file → which column → the count did not match); the server decides
  which comes next. `main.js`'s `adoptChannelNames` is what takes the result on
  without a reload. See "Naming an image's channels" below. Still keeps its
  own private copy of the Local/Remote/Upload row rather than calling
  `views/fileSourceRow.js` below — a known duplication, not yet unwound.
- `views/fileSourceRow.js` (`window.PlexoraFileSourceRow`) —
  `create({...}) -> {element, focus, busy, say, reset}`, a reusable
  Local/Remote/Upload file-source row lifted out of `channelNamesUpload.js`
  (still the fullest example of what it is for). It settles which of the
  three answers applies — a server path, a node relay upload, or the
  browser's own bytes — from the same Local/Remote derivation
  `services/dataLocation.js` makes, and hands its caller a path or a `File`
  without reading it. Loaded from `base.html` after `dataSourceField.js`.
- `views/confirmDialog.js` — `window.PlexoraConfirm`, core's way of asking a
  short question: `ask`/`tell`/`choose`/`prompt`, plus `modalOpen()` and
  `escapeHtml()`. Replaces `window.confirm` (browser-drawn, names the page's
  origin, blocks the main thread) and a Bootstrap modal (markup-per-dialog) with
  one native `<dialog>` per call, torn down when it closes rather than kept and
  refilled — the same shape as Figure Builder's `figureConfirm.js`, on
  purpose, so neither surprises somebody who has read the other. `choose`/
  `tell` take an optional `content` DOM node, inserted after the body and
  before the buttons, for a dialog that needs more than paragraphs (a
  plugin's shortcut table, `views/pluginHelp.js`) — built by the caller with
  `createElement`/`textContent`, since the no-HTML rule still stands and this
  only lets structure through; `tell` also takes a `confirm` label. Its CSS
  (`.plx-*`) lives in `main.css`, and it is loaded from `base.html` — a dialog
  a page swap could style only on one page would render unstyled on the rest.
- `views/datasetPicker.js` — `window.PlexoraDatasetPicker`, the "Move to…"
  picker: a single click per folder, no `<select>`+confirm pair and no Cancel
  button (Escape and the × already close it). Page-only — mounted from the
  Open Project page, not loaded from `base.html`.
- `views/openProjectPage.js` — the Open Project page, rewritten as a
  lightweight file browser over the dataset registry: folders first, the
  current dataset carried as `?dataset=<id>` in the URL via `replaceState`
  (so Back/Forward and a bookmark both land where they were), multi-select
  (tick, ctrl-click, shift-click), native drag-and-drop carrying the
  selection as `text/x-plexora-projects` (drop on a folder moves the
  selection there, drop on the root crumb unassigns it), a selection bar, and
  badges drawn from `manifest.summary()`. The Bootstrap `#deleteProjectModal`
  is gone, replaced by `confirmDialog.js`. Mounted through `PlexoraPage`, so
  it returns a teardown that removes the `document` keydown listener it adds
  for Escape (`<dialog>` traps focus but not keydown).
- Other views: channel list, colour picker, import/config forms. (The gating
  sidebar lives in the plugin, not here.)

Note: `imageViewer.js` and `miniMap.js` are loaded as **plain `<script>`** tags
from `base.html`, not bundled by webpack — and so are the modules `imageViewer.js`
was split out of (`cardList.js`, `layerStack.js`, `glInit.js`, `tileColorize.js`,
`tileDecode.js`, `labelTile.js`), `viewerSidebar.js`, its new
`layerChannelPanel.js` (loaded right after it, before `layerManager.js`, so a
layer's channel panel can mount a second `ViewerSidebar` instance), and the
two new panels (`layerManager.js`, and `cardList.js` again for the tool
cards). Only
`vendor.js`, `viewerManager.js` and `glRenderer.js` go through webpack into
`client/dist`. So none of these have a module system — top-level `class`
declarations are globals, and `node --check` is a valid syntax gate for any of
them.

## Import and Progressive Requirements

The rule: **import the minimum, then ask for more only when a feature needs it.**

**One dialog, not a form.** `views/importSample.js` (`window.PlexoraImportSample`)
is the whole of importing: one `<dialog>` with three states — `pick` (Local/
Remote, "Choose a file" / "Choose a folder", or a pasted path), `proposal` (one
CARD per sample, a role badge per row, a question as an attached callout,
never blocking), and `importing` (the rows become the shared progress rail;
the sample opens as soon as its record exists). There is no tab per format and
no page of its own — the same dialog opens from the File menu, the Open
Project library, the home page's footer and a sample's Layers panel
(`layer_add_button`), scoped to "+ Add Layer" when it opens from inside a
sample. Every one of those is bound by id in `importSample.js` itself
(`sample-import`, `sample-import-empty`, `sample-import-home`,
`sample-import-menu`), so a new surface is a button with one of those ids and
no controller. The header carries a one-line subtitle rewritten per phase
(`paintSubtitle()`, from `summarize(proposal)` in `proposal`) and a `?` button
that opens `views/importHelp.js` — a second `<dialog>`
(`window.PlexoraImportHelp`) with its own Overview/Formats/Examples/Good-to-know tabs
built from top-level consts (`FORMATS`, `QUESTIONS`, `EXAMPLES`, `NOTES`) that
carry the server's own modality/bundle/question strings, so a new format is
one entry there and nothing in `importSample.js`. `tests/test_import_help.py`
holds that catalogue to what `import_proposal.py` actually emits.

**The home page is not one of the dialog's surfaces; it is an entry point of
its own.** `index.html`'s `{% else %}` branch (no datasource registered) is
`views/quickViewLanding.js` + `css/quickView.css`: the Local/Remote switch, a
Select File / Select Folder pair, a path box and Load — the page's own
controls, because the one thing somebody does on an empty install is open an
image and a modal over an empty page is a step rather than a shortcut. The
dialog is the *footer* link there, for what the page cannot ask: a name, a
dataset, several samples out of one folder.

That page is **not** a second importer, which is the whole difference from the
`POST /quick_view` it used to call. Load POSTs `{paths: [path]}` to
`/import/sample` — the dialog's own route — so detection, naming, deduplication
and the multi-layer result are decided once, in `import_proposal` and
`import_sample`. It can therefore do things the old landing refused: a Xenium
run or a folder of matching files arrives as one sample with its mask, table
and transcript layers, and the viewer fills them in from `/import/status` while
they build. `tests/test_home_landing.py` holds both halves — that the page is
its own controls, and that the engine under it is the shared one. `POST /import/inspect` runs `import_proposal.inspect_paths` and answers
with a `Proposal`; the dialog draws whatever comes back and decides nothing
about what a file IS, so a new vendor format is a row in this list with no
change to the client. Pressing Import posts the same paths and answers to
`POST /import/sample` (a new sample) or `POST /import/layers` (add to one that
exists), which call `import_sample.import_sample`/`add_layers` — the one
function every entry point reaches, including the Python API and the CLI.
`GET /import/status` polls `layer_jobs` for a sample whose layers are still
building. The only controls that appear at all are the ones the *file* forces:
a table picker for a multi-table `.zarr`, an image picker for a store with
several, a mask-or-image choice for an ambiguous single-plane TIFF. None can be
guessed — picking for the user silently loads the wrong cells, or thresholds
raw counts as if they were log values.

**A file on a data node is SERVED during inspection, not looked up.** With the
dialog's Local/Remote switch on a node, `importSample.js` posts each pick as
`node://<node>/<the path the browse returned>` — and a path is not a resource
id, so reading it as one answered "`<node>` is not serving '/n/scratch/…'" for
every file anybody picked. `import_proposal._detect_node` now tells the two
apart by shape (`_looks_like_a_path`: an id from `nodes.resource_id_for` is a
slug and a hash, and neither it nor a `--serve kind:id=path` id can hold a
separator) and hands a path to `_serve_on_node`, which is the same thing a
landing-page data field does when somebody picks a file on another machine:
ask the node what it is (`POST /node/v1/detect`), then `nodes.share_path` it
under that kind. **The kind comes from the node, never from the name** —
`cell.ome.tif` out of an mcmicro run is a segmentation mask and says so
nowhere, while the plane count and dtype that do say so are readable only over
there. A three-valued `mask` verdict is carried through, so the ambiguous
single-plane case raises the same `mask-or-image:<name>` question a local file
raises, with the same id, and answering it re-serves the resource under the
other kind (`unshare_path` then `share_path`) — unless a project is already
bound to it, which `_bound_project` checks first and refuses by name. A node
path is also the one case where `src` cannot be read for naming: the address
carries the derived id, so `LayerProposal.filename` holds the name the node
reported and `_named_by()` is what grouping and `_sample_name` read, or a
slide and its mask would be two samples called `lsp11641-3f9c2a11`. A folder on a node is
refused as a folder (bundling needs a walk this side has not got) rather than
as a missing resource, and a node too old to have `/detect` produces
`providers/http._check`'s upgrade sentence. `tests/test_import_from_a_node.py`
covers all of it against a real second process, ending in a tile drawn from
the node. Two things are deliberately not done yet. A freshly shared mask that
is still converting registers anyway — the row says "converting on `<node>`"
and the cell layer draws the raw mask, more slowly, until the node finishes
(`seg_tile` is not behind `_ready`; see the node API row above), where a data
field would have polled `resource_status` and held the form. And removing a
pick from the
dialog does not `unshare_path` it, so a node accumulates the files somebody
browsed past. Not for want of knowing which one any more — `LayerProposal.pick`
names the exact index in the caller's own `paths` array (see "The dialog now
knows exactly which pick it removed" below) — the unshare call itself is
still a follow-up nobody has written.

**The same thing from a script or the command line** is `plexora.datasets`
(see its row above) — `plexora dataset create PCA --from projects.json --node
hms-o2`. It exists because the dialog is the only way to place a file on a
node, and fifty slides is fifty rounds of Import Sample → Remote → pick. It
shares by the ROLE the spec names rather than asking the node what the file is
(`_serve`), because a spec has already said; it validates with `detect_on_node`
first, which writes nothing, so a typo in the last entry costs nothing; and it
leaves every share in place on a failure, since an identical re-add is a no-op
and `unshare_path` could pull a resource out from under another project.
Registry local, bytes remote, addressed exactly as the dialog addresses them —
this is a front door onto the architecture that was already here, not a second
one beside it.

**Which kind of image it is, is read from the file, not from its name.** The
sniffer (`_sniff_quick_view_kind`) has three answers. A directory is
`ome_zarr` unless it is a DICOM slide's folder (`dicom_wsi.is_dicom_path`,
checked next — a folder holding 2+ slides is refused there, while the user is
still choosing), in which case it is `ome_tiff` too; `.png/.jpg/.jpeg` is
`rgb` (the flat untiled quick view); and *everything else tiled* —
`.tif/.tiff/.qptiff/.svs/.ndpi/.scn/.mrxs/.bif/.svslide`, plus any `.dcm`/one
picked out of a DICOM folder — is one answer, `ome_tiff`, meaning "hand it to
the full pipeline". `convertOmeTiff` then decides whether it is a channel
stack or a brightfield slide by reading it (`brightfield.detect_image_type`,
or `dicom_wsi.detect_image_type` for a DICOM path, checked *before* the TIFF
detector so a `.dcm` is never opened as a TIFF), and records `image_kind` as
`ome_tiff`, `ome_zarr`, `brightfield`, `rgb`, or `dicom` for DICOM
fluorescence (DICOM H&E records as `brightfield` and reuses the `rgb` channel
sentinel instead — every client `image_kind` test is `== 'brightfield'` or
`== 'rgb'`, not a membership check, so a fifth string would have had to be
added everywhere `dicom` behaves exactly like `ome_tiff` already). `brightfield`
is a **new** kind rather than a reuse of `rgb`: string comparisons across the
app (`main.js`, `index.html` twice, `api/plugin.py`'s `excluded_image_kinds`,
`data_routes.generate_rgb_image` — moved there from the deleted
`quick_view_routes.py` when the quick-view flow was replaced by Import Sample)
mean "flat, untiled, plugins excluded" by `rgb`, and a whole-slide image is
none of those — the new kind gets the full plugin pipeline for free. The user
can override the detector per project with `imageTypeChoice` (Auto / H&E /
Fluorescence) on the edit page; changing it calls `datasource.reregister_image`, which
re-reads the same file under the other reading and **never** writes to it. A
monochrome multiplex DICOM slide refuses an H&E override outright — an optical
path is a marker, not a camera's red/green/blue — rather than serving a wrong
picture. `.mrxs` and any DICOM slide are the formats needing the optional
`[wsi]` extra, and the sniffer probes for OpenSlide/`wsidicom` there so a
missing install is reported while the user is still choosing a file.

**An image may be a folder.** OME-TIFF/TIFF/SVS/QPTIFF/PNG/JPEG are files;
OME-Zarr is a directory, and so is the SpatialData store somebody points both
the Image and the Data field at — and so, sometimes, is a DICOM slide, since a
slide is a collection of `.dcm` instances rather than one file and a folder is
the normal way to arrive at one (picking a single `.dcm` inside it works too;
`dicom_wsi.assemble_slide` gathers the siblings either way). So every
import/picker field asks the browse route for mode `"any"` — "a file OR a
folder" — and gets ONE `Browse…` button, not a pair; `"directory"` survives
only for the genuinely folder-only case (`settingsPage.js`'s data root) and
`"file"` for the genuinely file-only one (`channelNamesUpload.js`'s channel
list, the one caller left of `check_file_existence` — the deleted
`importFormValidation.js` was the other, and `check_path_existence` now has no
client caller at all). `import_proposal.inspect_paths` tests `.exists()`, not
`.is_file()`, on every path it is handed. The store is copied first and resolved after
(`_copy_if_requested` then `_resolve_image` in datasource.py) — resolving
first would copy an image element away from the tables that describe it.
`_resolve_image` is identity for a DICOM path (file or folder): there is
nothing inside to resolve *to*, since which instances belong to the slide is a
question `assemble_slide` answers from metadata every time the slide is
opened, and recording one instance would freeze a 252-file slide to whichever
file happened to be picked. The project is named for what the user pointed at,
not for what it resolved to: dropping `sample.zarr` gives a project called
`sample`, never `morphology`. Mode `"any"` is on `dataSourceField.js`, and —
newly able to pick a `.zarr` mask at all, since they were file-only before —
`projectEdit.js`'s mask field and `requirementsModal.js`'s segmentation field.
**Not** the Import Sample dialog's `pick` state, which asks the question in the
control instead — see "The dialog's `pick` state is one vertical run" below.

**The dialog's `pick` state is one vertical run**, and the only surface built
this way — every other field still gets a mode `"any"` browse control inside
its own row. `views/importSample.js`'s `renderPick()` is, top to bottom: the
where-row — a direct child of the `<dialog>`, not of the body, because
`renderProposal`/`renderImporting` clear the body with `innerHTML` and would
tear the mounted switch out from under itself — carrying a "Data location"
kicker, the Local/Remote switch mounted once via `dataLocation.attach()` and a
"this computer" caption painted by `paintCaption()`, which is the home page's
arrangement to the word (the switch decides whose filesystem the two halves
below browse, so it sits above them rather than inside a row it would qualify,
and is hidden outside `pick` by CSS rather than rebuilt); a `buildSplitControl(
"sample", pickWith, {file: "Choose a file", directory: "Choose a folder"})`
panel, its first half `autofocus` so `showModal()` does not focus the ×; a
path input for pasting (Enter adds it), and — unlike `quickViewLanding.js`,
which asks the same first question on the home page and never took a drop —
an actual dropzone: `dragover`/`drop` on the same container upload small files
(under 64 MB) through `/upload_data_file` and refuse anything bigger with a
`PlexoraConfirm.tell` explaining that a browser can hand Plexora a dropped
file's bytes but never its path, which is fine for a table and wrong for a
slide. Below the panel, not inside it, one formats sentence ends in an "All
formats" link that opens `importHelp.js` on its Formats tab. Both halves are
drawn on every platform — `buildSplitControl` is called directly with each
half's own kind, so the "file or folder?" popup a bare mode
`"any"` control raises can never come back here.

**A project starts as an image, however many layers came with it.**
`import_sample.register_sample` writes the reference frame first — the image,
a node-backed resource, or `register_blank_datasource` when the proposal has no
raster image at all — then the table, then every other proposed layer, then
`Project.mutate`'s `_apply` (which is where a run's own pixel size lands on
`ImageSpec.pixel_size` when nothing had set one), and the mask **last of
all**: attaching it any earlier draws a boundary-polygon mask (see
`boundary_mask.geometry_for`) before the pixel size it needs to place the
polygons in the reference frame exists, which drew a whole Xenium run's cells
at one pixel per micron — a fifth-scale mask in the corner of its own slide.
`_preferred_mask(layers)` picks which of several `role="mask"` candidates
that ordering attaches: a raster mask the user supplied outranks a run's own
boundary polygons, and the polygons are demoted to `role="layer"` rather than
dropped, so the run still records what it shipped. A CSV's marker/metadata
split is not a confirmation screen the import
blocks on: it is a `Requires` role like any other, asked by the requirements
modal the first time a tool needs it, so a sample opens on its image
immediately whether or not that split has been made. AnnData and SpatialData
never ask it at all — `var` and `obs` already draw that line.

**A Visium HD run asks which table to analyse, because there are several.**
`import_proposal._visium_hd_bundle` proposes the hires H&E as the reference
image, one `points` layer of the bins at the FINEST level (the viewer pools
them on the fly — see `bin_tiles` — so 2/8/16 micron pictures are one store),
and a `bin-size` question when the run has more than one candidate table: a
bin level (`VISIUM_HD_DEFAULT_BIN = 8` µm, Space Ranger's own analysis
recommendation, is the default and is marked "(recommended)"; the 2 µm level
warns "slow to gate" past `LARGE_TABLE_ROWS = 5,000,000`) or, for a Space
Ranger 4 run, its segmented cells — whose polygons then become the mask ONLY
when the cells table is the one chosen, since a mask's pixel values are cell
ids and would otherwise join to whichever bin happened to share the number.
Every layer's `LayerProposal.frame == "fullres"` states its transform in the
run's own FULL-RESOLUTION microscope pixels — the frame every Space Ranger
position is written in — and `_align` composes it with the reference's
`render.frameScale` (the hires PNG is `tissue_hires_scalef` of that frame)
exactly once, then clears `frame` so finishing twice cannot scale twice. The
bundle records `name` (the folder above `outs`/`binned_outputs`/
`square_XXXum`, so a library of samples is not all called "outs") and
`fullres_pixel_size`; `register_sample` writes `frameScale` onto the
project's `visium_hd` bundle record and `import_sample._scoped_sample` reads
it back for "+ Add Layer", so a layer added later still knows the frame it
must compose against. Client catalogue: `importHelp.js`'s `FORMATS` gained a
Visium HD card and a `visium_bins` chip (`"expression bins"`), and
`QUESTIONS["bin-size"]`; `importSample.js`'s `BUNDLE_WORD.visium_hd`;
`tests/test_import_help.py` pins both.

**A data source can be registered before it is readable.** A multi-table
`.zarr` with no table picked, or a multi-image table with no subset chosen,
used to be refused outright at import; `datasource.deferred_spec(src,
data_type, *, table, subset_by)` instead records the path and marks what is
still missing on `DataSpec.unresolved` (`"table"`, `"subset"`, or both), and
`register_spatialdata_datasource` no longer requires `table` up front. This is
what lets the project exist and open as an image immediately: `DataSpec.
is_resolved`/`resolved(**answers)` say whether the source can be read yet and
strike a question off once it is answered, and `Project.has_table` now means
**readable** (`self.dataset is not None and self.dataset.is_resolved`), not
merely "a data block exists." `Project.has_data_source` is the separate,
weaker question — has the user named a file at all — asked by the edit page
and the requirements modal so they can show the stored path back rather than
an empty box; `Project.unresolved` mirrors the spec's own list.
`role_columns`/`role_answers`/`role_defaults`/`coordinate_options`/
`feature_options` all guard on `has_table` now, not on `dataset` being set,
because an unresolved source has no column vocabulary yet — nothing has
opened the file — and offering an empty picker is asking a question with no
answers in it. `replace_project_data` — the one route the edit page, the
requirements modal and now `import_sample.py` all attach or swap a table
through, since the deleted `import_routes._register_anndata` folded into it —
routes through `deferred_spec` too, so an image beside a six-table `.h5ad`
behaves the same way a `.zarr` store does.

Once a source is unresolved, the same requirements machinery that asks for a
role asks for the missing table: `GET /<ds>/requirements?keys=a,b` answers a
tool-free ask for named keys (400 on a key nobody recognises; role keys are
withheld until `table` itself is answered, since there is nothing to choose a
role FROM yet), and `POST /<ds>/requirements` with no `tool` now reports the
real `stillMissing` computed from `payload["keys"]` — it used to silently
answer `[]`, which is why `PlexoraRequirements.ask(datasource, keys)` exists
on the client: the Cells control's "Add Seg Mask / Add Data" CTA opens the
modal through it now instead of navigating to the edit page (the `<a href>`
stays as the no-JS fallback). `_needs` is split into `_needs_payload` (the
generic three-list shape) and `_core_needs` (what core itself asks about,
independent of any plugin's `Requires`); the requirements payload gained
`keys` (what was actually asked for) and `data` (`{src, table, type,
unresolved}`, so a form can show what is already known about the source it is
completing). Attaching a segmentation mask mid-session still triggers a full
page reload rather than a live patch: attaching one inserts the "Area"
placeholder at `imageData[0]`, and `viewerManager.load_label_image` reads
exactly that position, so anything already relying on the old indices would
read the wrong channel. `main.js` gained `__plexora.watchSegmentation()`
(idempotent) for this, and `refreshDataset()` now returns `{maskAttached}` so
a caller can tell whether that reload is actually needed.
`dataSourceField.js`'s `mount()` gained `{table, inspect}`: `table` carries
along a table already chosen inside a store whose image was not, and
`inspect: true` is the exception to "nothing already answered is asked
again" — it opens a stored-but-unreadable path on mount, because an
unresolved source is precisely the question that has no answer to skip.

**Everything else is deferred.** A plugin declares what it needs in `Requires`
(`table`, `segmentation`, `markers`, `features`, column `roles`, plus an
`optional` tier); `missing_from(project)` returns typed `Requirement` descriptors and
`tool_routes` turns them into a form the client renders without knowing which
plugin asked. Answers are stored **on the project**, so a role collected for one
plugin is found already-answered by the next — that reuse is the whole point.

**A tool can also need a LAYER.** `Requires(layers=("transcripts",))` names a
modality (or `kind:<kind>`), and it is the one requirement that is about the
scene rather than the table. It changes `applies_to`, not just
`missing_from`: a transcripts tool is meaningless on a sample with no
transcripts in it, and listing it there is how a Tools menu fills up with
things that open onto "nothing to show". A layer that IS present but still
building does not fail `applies_to` — it is reported as
`Requirement(kind="layer")`, which the requirements modal renders as an "Add
layer…" row rather than a path field, because what it wants is data to import
and the import dialog already knows how to take some. `optional_layers` is the
same vocabulary in the non-blocking tier. **Every `Requires()` that predates
this has `layers=()`**, so no existing plugin changes behaviour.

**A LAYER SECTION is a different thing from a layer requirement, and a plugin
can be both.** `Requires(layers=(...))` is *what a plugin needs to exist at
all* — it is asked whether the plugin belongs in a menu. `Plugin.
LAYER_SECTION_SLOT` (`api/plugin.py`, also `Plugin.is_layer_section` when a
plugin's `panels` dict uses that key) is *how the plugin is presented*: on
from page load for every sample it `applies_to`, never opened from the Tools
menu and never closed with an X. `plugins.tools_for`/`ready_tools` exclude a
layer section entirely (see the `plugins.py` entry above);
`page_routes.image_viewer` calls `layer_sections_for` instead and folds each
section's `scripts`/`styles` into `active_tool_scripts`/`_styles` so
`_fragment.html` still loads them on a route-level navigation.

**A layer section is not a section any more — it is a card's BODY.** It used
to be a `<section>` of its own with a heading, a chevron and a visibility
checkbox, sitting above the Layers list; once the plugin also claimed its
layer that made one thing appear in two places, switched in a third.
`index.html` still renders one `.layer-section-mount[data-layer-section=NAME]`
per section into `#layer_section_slot`, but that slot is now `hidden`
STAGING: each mount also carries `data-layer-body="<layer id>"` and
`data-layer-modality`, and `layerManager.js` MOVES it into that layer's card
on first render (`stagedFor` matches by id, then by modality for a layer
imported after the page was rendered). The same slot stages the base image
layer's own controls under `data-layer-body="__image__"` — whichever of the
former Image Channels or Image Adjustments markup applies. The fluorescence
case carries no `data-layer-extras` any more: the channel counter and the
CSV-rename button both used to sit in that header slot and have since moved
into the body, for opposite reasons — the counter is a caption and now sits
on the card's own footer line, the button is an action and now sits, labelled
("Upload channel names"), in a `.layer-card-actions` row it shares with the
opacity control. Only the brightfield case still stages
`data-layer-extras="__image__"`, for the reset-to-scanned-image button. Moved
and never cloned,
because every controller involved (channelList's slots, brightfieldAdjust's
sliders, the plugin's own) takes its element handles by id once and keeps
them. Which layer a section belongs to is answered server-side by
`Requires.first_layer(project)`, because the card is built from `/config`
before the plugin's JavaScript has run. **The Cells control stays last in the
sidebar and is pinned there**
(`.sidebar-section.compact`: `margin-top: auto` for a short sidebar,
`position: sticky; bottom: 0` for a long one). It is not a per-layer section
and must not be filed among them: a project has exactly one segmentation mask
whatever it was drawn from, and every plugin that colours cells is styling
THAT mask (`imageViewer._cellStack` — "a plugin's cell layer is a STYLING of
the segmentation mask, not a second mask"). Two consequences for anyone
editing the sidebar: it has to remain the LAST child, because a sticky box is
clamped to its containing block and anything after it slides out from
underneath; and `.viewer-sidebar` carries `padding-bottom: 0` with the gutter
moved onto `> :last-child`, because a gutter on the container is 16px the
pinned bar cannot paint over. `transcripts` is the first plugin to use the
slot: see its entry under "Plugins" above.

In the browser the same split reaches `ctx`: `ctx.layers.find({kind, modality})`,
`ctx.layers.addTiled({...})` — the tiled counterpart of `addOverlay`, where core
owns the world item, the placement and the z-order and the plugin owns what is
drawn — `ctx.layers.claim(id)`/`release(id)` (the plugin's own name for
`LayerStack.claim`, see `views/layerStack.js` above — what earns the layer an
ordinary Layers card) and `ctx.sample` (`name`, `bundles`, `modalities`,
`reference`, `blank`) with live getters, because layers are adopted
mid-session and a snapshot taken at activation would be wrong the moment
somebody pressed "+ Add Layer". The transcripts plugin's density raster goes
through `addTiled`, which is what keeps core from having to know what a
transcript is, and it claims that same layer so its eye, its place in the
stack and its X are core's while everything inside the card stays its own.
Note that a claim is no longer the ONLY way to earn a card: staged
`data-layer-body` markup earns one too, which is what gives a transcript
layer still being built its card and its "Preparing…" line before anything
has claimed anything.

**A guess is not an answer.** The column predictor fills in most of a
conventionally-named table, so a well-named import leaves *nothing* missing —
and a tool would open having silently decided five things. `Requires` therefore
distinguishes three states, and `_needs()` sends three lists:

| list | meaning | field |
| --- | --- | --- |
| `missing` | nothing stored | empty |
| `confirm` | stored, but the predictor put it there | prefilled, shown once |
| `optional` | absent, never blocking | empty |

`Project.confirmed` is what separates the first two: a flat list of requirement
keys the user has actually answered. It is written by the requirements modal
and the edit page — both are places a human looked at these values —
and the table-scoped part of it is dropped by `forget_table_answers()` when the
data file is replaced. `table` and `segmentation` are exempt from confirmation
(`_GIVEN_KEYS`, now `manifest.GIVEN_KEYS - {"image"}` — `manifest.py` is the
one place that set is defined, and the image is given but has no confirmation
state of its own to exempt): a path the user typed was never a guess.

Four properties worth not breaking:

- **Nothing already answered is shown.** A confirmed requirement is absent from
  every list, never rendered as a field the user has to dismiss.
- **An optional field offered and skipped is answered.** `optional_missing_from`
  filters by `confirmed`, so it is offered once, not on every open. A plugin
  that genuinely cannot proceed without one uses `requested_from` instead
  (`GET .../requirements?keys=...`), which ignores `confirmed` on purpose.
  A plugin whose `Requires` is *entirely* optional (ROI) has nothing in
  `missing` or `confirm` and so would never reach the modal at all through
  `COLLECT` — `tool_routes._resolve()` has a fourth outcome, `OFFER`, for
  exactly this: nothing blocks, but `optional_missing_from` is non-empty.
  `tool_panel()` treats it like `COLLECT` (the modal shows); the no-JS
  `<a href>` path in `open_tool()` treats it like `OPEN` on purpose, because
  nothing is blocking and detouring to the edit page there would be wrong.
  Since core's generic subtitle ("Plexora filled these in from the data") is
  false on a form with nothing filled in and nothing required, the plugin
  supplies its own line via `Plugin.intro`, and the modal's secondary button
  becomes "Skip" rather than "Cancel" — Skip saves through the same path as
  Continue (recording the offer as declined), because the caller re-enters on
  a `true` result and a plugin that requires nothing must not become
  permanently unopenable. `satisfy_requirements()` returns `stillOptional`
  alongside `stillMissing`, and the modal's save loop closes only when both are
  empty: attaching a data file makes a role like `cell_id` newly *offerable*,
  not newly *missing*, so `stillMissing` alone would close the form one
  question early.
- **The ask loops.** Naming a data file is what makes "which column holds the
  cell id" answerable, so `missing_from` reports roles and markers *only* once a
  table exists, and the modal re-asks after each save.
- **Compatible-but-not-ready still lists the tool.** Hiding it hides the only
  route to fixing it (`tests/test_plugins.py` pins this).

**The cell layer is a default, not a requirement.** `Project.cell_layer`
resolves to the best the project can draw — the mask when there is one,
centroids otherwise — and the stored value only records a user overriding that
on the edit page. It used to be `cell_layer=True` in `Requires`, asked before a
cell-drawing tool could open; a user who supplied a mask wants the mask, so that
was a dialog with a foregone conclusion. Nothing is drawn over the image on load
— `viewerControls.init()` binds the toggles and stops — and `enableCellLayer()`
turns the resolved one on when a plugin registers its cell layer in `main.js`.
It is asked per layer, not of the control as a whole: with several plugins
loaded, "something is already showing" is true as soon as any of them turned the
mask on. A mask whose pyramid is still converting is **waited for, not
substituted**: `enableCellLayer` sets `seaDragonViewer.cellLayerAwaitingMask`
and turns nothing on, because centroids standing in for a mask are a different
representation of the same cells rather than a rougher one, and the substitution
was silent and could last minutes. `main.js` polls `/get_segmentation_status`
and announces every reading as `plexora:segmentation-progress` / `-ready` /
`-failed` — one loop asking the server, whatever number of panels are showing a
wait (Cell Explorer's `renderMaskWait` is the one that does). When the job
lands, `adoptSegmentation()` loads the layer in place and turns on whatever was
waiting, or swaps a fallback over (it used to reload the page, minutes into a
session). `viewerControls.fallBackToCentroids()` is the way out for a user who
would rather not wait; it marks the centroids as a fallback, so the mask still
replaces them when it arrives. `hasSegmentation()` and `maskPending()` are the
two halves of this: "no mask" and "not yet" are different projects.
`tests/js/cell_mode_control_probe.mjs` pins all of it.

**The conversion is waited for IN the viewer, not on a form.** Saving the edit
page goes straight to the viewer even with a job pending; `segmentationWait.js`
shows it there, as a dismissible modal that hands off to `#segmentation_chip` in
the navbar ("Pyramidizing segmentation mask…", click to reopen). It polls
nothing — it is a listener on the three announcements above, and `main.js` opens
it right after `viewerControls.init()` rather than at its own poll, which runs at
the bottom of `init()`. Two endings, told apart deliberately: **ready** reopens
nothing (the mask going on IS the message, and a modal would cover it), **failed**
reopens once (nothing else on the page would ever mention it). The overlay goes
through `PopoverPortal` because this runs over a viewer that can go fullscreen,
and the fullscreen `::backdrop` covers siblings whatever their size — the same
card the deleted `segmentationProgress.js` used to draw over the old import
pages, which had no viewer and no way to go fullscreen and so needed no portal.
When the job lands
on a viewer drawing nothing, `adoptSegmentation()` turns the mask on:
`viewerControls.userChose` is what separates "none, because None was the only
enabled button for the last four minutes" from "none, because the user clicked
it", and all three surfaces that move the control set it.
`tests/test_segmentation_wait.py` pins the flow and the four files that have to
agree on it.

**The Cells control shows what the project HAS.** Three outcomes per mode, and
`viewerControls.shownModes()` is the one place that decides between them for
the sidebar buttons, its only reader now — the View menu no longer mirrors
Cells, Sidebar or HD mode at all (`navbarControls.js`); those were each a
second answer to a question the sidebar already answered, kept in step by
hand. The View menu now holds Rotate & Flip (core's own tool,
`server/core_tools.py`, opened by `toolLoader.js` like any Tools-menu row) and
Scalebar, the one checkbox left because nothing else shows or hides the bar.
The sidebar's own collapse button carries the `mod+\` chord that used to
toggle the menu's Sidebar checkbox. A mode the active *plugin* does not use is
hidden. A mode whose resource is
**missing outright** is hidden too, and `#cell_data_cta` — a plain `<a>` to
`/edit_config/<project>`, so `appRouter` swaps the page in — appears reading
"Add Seg Mask", "Add Data" or "Add Seg Mask / Data". A mode whose resource is
**present but cannot do this** stays visible and disabled with the reason on it
(`unusableReason()`): a mask stored as boundaries has nothing to fill, a table
whose x/y roles are unanswered has no positions, and a mask still converting is
about to work. That distinction is the whole design — telling either of the
latter to go and add a file it already has is the wrong instruction. With
nothing left but None the buttons go entirely and the link takes the row. The
options ship `hidden` and `disabled` from `index.html` and are shown by
`refreshAvailability()`, but they stay in the DOM, which is what lets
`adoptSegmentation()` bring Outlines and Filled back mid-session.

**Opacity belongs to the canvas, not to a tool.** `#cell_layer_opacity_row`
shows exactly while something is drawn (`maskWanted() || pointsWanted()`), not
while a plugin holds a layer, and it moves the active layer's `opacity` or --
with no plugin -- core's own, through `ImageViewer.setCellDisplayOpacity()`.
`layerAlpha()` and `tileColorize`'s blit both honour `layer.opacity`
unconditionally; the old `layer.lut ? layer.opacity : 1` made the slider inert
for exactly the two cases it was most often on screen for (Thresholding, whose
layer carries no LUT, and a plain mask, where it was not offered at all). The
two defaults are unchanged and deliberately different --
`DEFAULT_CELL_LAYER_OPACITY = 0.7` for a registered layer, pinned against Cell
Explorer's `state.DEFAULT_OPACITY` by
`plugins/cell_explorer/tests/test_cell_explorer_state.py`; 1 for core's own, so
a plain viewer draws what it always did.

**Hiding the cells is a redraw; turning them off is not.** `ViewerControls`
binds one bare letter, `OVERLAY_KEY` (`t`), to
`ImageViewer.setOverlayMuted()` -- one boolean that
`labelOutlinesEnabled()` and `shouldDrawCentroids()` both consult at draw time,
with the mask item loaded, every tile's `_layerContexts` intact and the centroid
tiles still cached, so both directions cost one frame. It went through
`selectMode("none")` and the card's eye first, and both mean *I am done with
this*: they unload the pyramid and call `dropLayerContexts`, so hiding was
instant and showing cost a pyramid read, a filter round trip and a boundary
re-render per visible tile. Muting changes nothing in the sidebar on purpose --
what comes back has to be what went away. The key is printed on the canvas under
the filename as a `<kbd>` cap plus a sentence (`#viewer_overlay_hint`, built by
`ImageViewer.initProjectLabel` inside `.viewer-canvas-caption`, filled by
`paintOverlayHint()`), which is also the only place that says the cells are
hidden -- the Cells buttons still read Outlines. `selectMode` clears the mute
before its own no-op early return, so clicking the already-selected mode is the
way back for somebody who has forgotten the key. Bare letters are otherwise
each plugin's (ROI's v/p/f/r and Space, Figure Builder's C and S, gating's Z/X
to step the marker, `views/datasetNav.js`'s B/N to walk the dataset); core owns
the modified chords in `services/keyboardShortcuts.js`, and this is the
documented exception, taken because the control is the canvas's and the key is
pressed repeatedly while comparing. OpenSeadragon owns W/A/S/D on the canvas
once it has focus — worth checking before a new bare letter is bound anywhere,
since none of the above collide with it or each other on purpose.

**The hint is printed for what is DRAWN, not for what could be.**
`paintOverlayHint()` asks `maskWanted() || pointsWanted()` -- exactly the pair
`toggleOverlay()` consults before it acts, so the printed key and the key's
effect cannot come apart. It used to ask `offeredModes()`, which answers a
question about the PROJECT: any dataset with a mask or with coordinates carried
the caption, so a viewer sitting on **None**, with nothing over the image at
all, still offered to toggle cells that were not there. On None the hint is
gone; choosing Centroids/Outlines/Filled brings it back, and so does a plugin's
layer -- whose eye, switched off, takes it away again. Muting is deliberately
not a mode change, so both predicates stay true while the cells are hidden and
the "Selected cells hidden" caption survives, which is the whole point of it.
`applyMode()` repaints the hint AFTER writing the layer's mode, for the same
reason `paintLayerOpacity()` is called there: with a plugin holding the layer,
`paint()` runs before the write and would read the mode the layer had a moment
ago. `tests/test_cell_mode_control.py::test_the_hint_is_printed_only_while_something_is_drawn`
and five checks in the probe pin it.

**Drawing the mask needs no feature table.** `renderLabelTile` reads cell ids
out of the label pyramid itself, so image + mask + no data is a project that
draws — and one `attach_segmentation` explicitly supports, inserting the "Area"
channel whether or not a table exists. Per-cell rows are what PLUGINS need, and
each is already gated on a table. `NumericData.loadCells()` therefore returns
empty arrays rather than throwing whenever `hasCellTable()` is false (no data
block, or roles nobody has answered); `bindSegmentationBuffers` and
`forceRepaint` already no-op on zero cells. It used to destructure the null
schema, so the first click on Outlines threw before the pyramid was requested
and `selectMode` read the TypeError as a mask that would not load.
`tests/test_mask_without_table.py` pins both halves.

**`features` is which numbers, not which columns.** A plugin that reads marker
intensities declares `features=True`, and core asks — once, in the `confirm`
tier — which matrix they come from (`X` or one of `adata.layers`) and whether to
`log1p` them on the way in. Never asked for a CSV: one table of numbers is not a
choice. It is the mirror image of `markers`, which is asked *only* for a CSV.
Both halves rewrite the read spec, so answering either re-reads the datasource —
a threshold set against raw counts is not approximately right on a log-scaled
panel, it is meaningless, and nothing about the values themselves says which
they are.

Client side: `requirementsModal.js` renders the form (core-owned CSS in
`main.css`), `columnClassifier.js` is the two-box drag component shared by the
modal and the edit page, and `ctx.requirements.require(keys)` lets a plugin ask
mid-session — which is how gating gets an image-id column at AnnData-save time
instead of shipping its own "type a column name" box.

**Anything base.html loads needs its CSS in `main.css`, not `import.css`.**
`import.css` is linked only by `project_edit.html`; the requirements modal and
the channel-names dialog open over the *viewer*, which links neither. The classifier's `.column-*` rules and
the shared `.field-hint` both started in `import.css`, so the modal drew the
marker/metadata split as two bare `<ul>`s — Sortable was attached and the drag
technically worked, but with no chip to grab and no box-shaped target it read as
a printed list of column names. `tests/test_column_classifier_css.py` pins the
pairing.

**Editing is generated from the record.** `project_edit.html` renders a section
only when `project.has` says it applies, and `POST /project/<name>` merges. The
image is the one thing that cannot change. The old path did the opposite — it
read every project as a CSV and rebuilt the entry from `{}`, which silently
destroyed AnnData projects; `tests/test_project_edit_routes.py` is the guard.

## Naming an Image's Channels

An OME-TIFF routinely arrives with its channels called `Channel_0 … Channel_n`
and the panel that says what they really are in a separate CSV or spreadsheet.
Until that list is in, gating matches markers to channels **by name** and so
matches nothing — which is what the sidebar's `#channels_upload_icon` is for.

**A path, and Upload only where it earns its place.** The dialog's main
control is a path box plus a `Browse…` that opens a native picker **on the
server**, filtered by `native_dialog.py`'s `"channels"` entry. On a desktop
launch Browse and a browser upload do the same thing — both write a path on
the machine running the server — so offering both there is a choice between
two spellings of one act before the user has done anything, and Upload stays
hidden. The moment Plexora runs somewhere else that stops being true: Browse
lists the *server's* filesystem and a marker list on the user's own laptop —
which is exactly where one usually is, having arrived from a collaborator by
email — has no way in at all. So `channelNamesUpload.js` offers `Upload…`
beside Browse exactly when `serverIsElsewhere()`, sending the bytes with
`multipart/form-data`; not offered for a file on a data NODE, since the path
box means the server's filesystem and a node path typed into it names nothing
the server can open. `session.file` and `session.path` are mutually
exclusive — Load clears `file`, choosing a file clears `path` — so the route
never receives both.

**The reading is server-side.** `server/utils/channel_file.py` reads the file;
the client parses nothing, and cannot: a path names a file only the server can
open. `.xlsx`/`.xlsm` go through openpyxl (a core dependency, imported lazily so
a drifted environment refuses Excel with a sentence instead of failing to
start); `.xls` is refused by name with the fix.

**`POST /upload_channels` answers one of three ways** and the dialog
(`views/channelNamesUpload.js`) has a stage for each:

- **applied** — the file said which names it holds without being asked: one
  column, and a length that is either the channel count or one more than it (a
  header row). This is the common case and costs one request and no questions.
- **needs_column** (HTTP 200, not an error) — several columns, so there is no
  such thing as "the" column. The whole description of the file comes back in
  the same response — preview, per-column counts, a header guess — rather than
  in a second inspect call, because a file read twice is a file that can be
  edited in between.
- **mismatch** (HTTP 400) — the names were read and there is the wrong number
  of them. **Nothing is applied.** Half a panel renamed looks named, and every
  wrong name in it would be believed by gating.

A single-column file never reaches the picker: there is nothing to choose, so a
count that fits neither reading goes straight to the mismatch.

**`POST /rename_channels`** (JSON, `data_routes.rename_channels_json`) is
`/upload_channels` without its front half, for the one caller that already has
a complete, ordered name list and no file to read: the Image card's Paste
channel names (`views/layerManager.js`, via `services/renderClipboard.js`).
Both routes share `_reference_channels` (the non-Area count) and
`_finish_reference_rename` (the rewrite that follows any successful reference
rename) so the two cannot drift apart on what "everything else that stored a
channel by name" means.

## What One Pixel Is Worth

A physical scale is **recorded, never inferred** — the rule figure_builder
already stated, now shared by the viewer. Three states, and one field decides
which:

- **`pixel_size_source: "metadata"`** — the file states its own `PhysicalSizeX`
  (or Aperio MPP, or an NGFF axis scale, or a DICOM optical path). Read fresh
  off the file on every load and never copied into the project, so re-importing
  a corrected image picks the correction up.
- **`"manual"`** — somebody typed it. Stored on `ImageSpec.pixel_size` as
  `{value, unit, source}` under the entry key `pixelSize`, written only when
  set and **removed entirely** when cleared, so every project predating this
  round-trips byte for byte.
- absent — nothing knows, and the scale bar counts **pixels** rather than
  showing a length nobody stands behind.

`data_model._with_pixel_size` lays the manual value over the file's on the way
out of `/get_ome_metadata` and stamps the source, so the endpoint is the single
answer. That matters because three readers consume it: the viewer's scale bar,
the calibration control beside the channel list, and figure_builder's
`readPixelSize` — which now reports `source: "manual"` for a typed value rather
than claiming the file said so on its provenance page.

`POST /set_pixel_size` writes it (`datasource.set_pixel_size`); an empty or
non-positive value clears it. The control re-reads the metadata endpoint after
every write rather than trusting its own number, because the scale bar reads
the same payload and two readers updating independently are two readers that
can disagree.

**The bar has two modes, one builder.** `imageViewer.scalebarScaleOptions()` is
the only thing that decides, and both `scalebar()` call sites plus every later
refresh go through it. `pixelsPerMeter: 1` is what keeps an uncalibrated bar
visible at all — the OSD plugin hides itself on a falsy one — and makes "one
meter" mean "one image pixel", which module-level `pixelScaleSizeAndText` then
labels in px. Its rounding reimplements the plugin's private
`normalize`/`roundSignificand`; the metric ladder cannot be reused because it
would render "2 kpx".

**The control has two faces and one controller.** `views/scaleCalibration.js`
drives both, finding its parts by `data-role` INSIDE its own root rather than by
document id, so neither face has to know the other exists.

- The **viewer** face (`_scale_calibration_float.html`) is a 22px pencil beside
  the scale bar, with a popup behind it. It is **docked into
  `#openseadragon_wrapper`, not OpenSeadragon's container**: the channel legend
  is an absolutely-positioned sibling of `#openseadragon` at z-index 220, so
  anything parked inside the OSD container sits in a nested stacking context
  that no z-index can lift above it — the popup opened underneath the legend
  and lost its Set button to it. `followScalebar()` repositions it on the
  plugin's own three events (`open`/`animation`/`resize`) using bounding rects,
  because the bar is placed at four fifths of the free width and neither of its
  edges is something CSS can anchor to.
- The **project edit** face (`_scale_calibration.html`) is an inline field in a
  list of settings, constructed with `alwaysShow: true`.

**It hides itself when the file states the size** — in the viewer only. A
control offering to contradict the file invites exactly the
scale-bar-disagrees-with-its-source failure the bar exists to prevent; the edit
page shows it anyway, because a stated size *can* be wrong and that is where it
is meant to be fixable without re-importing. Clearing is offered only for
`"manual"`: clearing a file's value would mean nothing, since the next load
reads it straight back off the file.

A 404 from `/set_pixel_size` is reported as "running an older server", because
a long-running Plexora fixed its route table at import while it re-reads
templates from disk — so after an upgrade the control appears and nothing
behind it does, and a generic failure sends somebody hunting the wrong bug.

Its CSS lives in `main.css`, not `viewer.css`: the edit page loads `import.css`
and would otherwise draw an unstyled form.

**The same reader is also consulted at import, unasked.**
`datasource._channel_names_from_sidecar` looks for `channelNames.txt` (or
`channel_names.txt`) in the image's own directory and then its parent, which is
where an Akoya/CODEX export leaves the panel while the stacks sit in a
subdirectory — the reason QuPath opened those files with markers named and
Plexora did not. It is the **last** tier in
`_channel_names_from_image_metadata`, reached only where the format's own
answer was already None, so no project that already resolved names can have
them change. It accepts only what `channel_file.autodetect` accepts — one
column, accounting for every channel — and anything needing a question asked
falls through to generic names rather than being guessed at; the modal above is
where that question gets asked.

The per-column `nonempty` counts in the description are what let the "File
contains column headers" checkbox re-label the select and re-count instantly,
without asking the server again — `nameCount()` mirrors `channel_file.names()`
exactly, and the two are pinned against each other by
`tests/test_channel_names_upload.py` and `tests/js/channel_names_probe.mjs`.

The dialog is a native `<dialog>` + `showModal()`, like `requirementsModal.js`
and unlike `segmentationWait.js`: a modal dialog is promoted to the top layer,
*above* the fullscreen element and its opaque `::backdrop`, so it does not
need `PopoverPortal`. An ordinary positioned element on `<body>` would.

### A rename lands in place — and names are keys

**A rename moves no index.** The image is the same file and `imageData` keeps
its order, so every tile URL, `rangeConnector`, `colorConnector` and
`currentChannels` entry — all keyed by index — is still correct. That is the
whole reason `main.js`'s `adoptChannelNames(names)` can take the new names on
without a reload, and it is the same argument `adoptSegmentation` makes.

What *does* move is every container keyed by **name**, and they have to move
together:

| where | what |
| --- | --- |
| `config.imageData[i]` | `name` / `fullname`, mutated in place |
| `imageChannels`, `imageChannelsIdx`, `columns` | rebuilt in place |
| `dd` | old channel keys deleted, description re-fetched, image-side stats carried across by index |
| `ChannelList` | `columns`, `channelIDs`, `image_channels`, `hasChannelGMM`, `sel`, `sliders`, `selections`, the row label, the swatch datum |
| `ViewerSidebar` | `columns`, `markerRangeOverrides`, each slot's `name`, each marker select's options |
| the DB | `data_model.rename_saved_channels` — the saved channel list holds **names** |

**Mutate, never replace.** `config`, `imageChannels` and `dd` are held by
reference by things that outlive the call — including every plugin's
`ctx.dataset`, which reads all three *live* through getters
(`services/datasetContext.js`). Handing anyone a fresh object leaves them on the
old names. Same rule `refreshDataset` records.

**The saved channel list is the one that used to bite.** It is what
`ViewerSidebar.applySavedChannels` rebuilds slots from on *every* page load, so
a reload did not fix the stale name — it restored it. That slot then asked for
stats under a name the server no longer had, and `next(...)` with no default
raised `StopIteration`. Channel lookups now go through
`data_model.real_channel_index`, which raises `UnknownChannelError`;
`/get_image_channel_stats` and `/get_channel_gmm` turn that into a 404 with a
sentence, and the background warm-up pass skips the channel rather than
abandoning the rest.

`dd` holds two different things under one key — the feature table's stats for a
column of that name, and the image-side stats `ensureChannelStats` fetched
lazily. The image side belongs to the *channel* and is carried across (the
pixels did not change, and re-fetching would blank every open slider); the table
side belongs to the *column* and is re-read, because which marker each channel
now matches is the whole point of the rename.

Tests: `tests/test_channel_rename_state.py` + `tests/js/channel_rename_probe.mjs`
(which drives both `renameChannels` methods through
`Object.create(...prototype)`, including the two-channels-swap-names case).

## The Rendering Pipeline

This is the part most worth understanding before touching anything visual.

**Two TiledImages per active channel.** `viewerManager.js`'s `channel_add`
calls `addTiledImage` twice per channel — a cover blit
(`destination-out`, below) and a paint blit (`lighter`, above) — the same
cover/paint pair a registered layer's `addLayerChannelSet` uses, and for the
same reason: composited against a black ground the pair is pixel-identical
to the single opaque `lighter` blit this used to be, but it is what lets the
reference image sit on something other than black and be dragged above a
registered layer without washing it out additively. Segmentation is a
further layer with `tileFormat: 32`.

**Tile request.** `getTileUrl` →
`/generated/data/<datasource>/<channel>/<level>/<x>_<y>.png` (`?q=hd` for the
16-bit path). Server side: `data_routes.generate_png` → `_get_tile_png_bytes`
(1500-entry LRU keyed on `load_generation`) → `data_model.encode_tile` →
`generate_zarr_png` slices the zarr pyramid level → quantize → encode.

A registered layer that is not the reference image goes through a sibling
route instead: `GET /generated/layer/<datasource>/<layer>/<channel>/<level>/
<tile>` → `layer_sources.layer_tile`. Core, not a plugin — it is the layer
MODEL, not any one modality, so it serves a second registered slide or a
transcript density raster (a uint16 channel like any other) the same way.
Its ETag carries `project.config_generation()`, not `load_generation`: this
tile is a function of the project record and the file it names, not of which
datasource the viewer happens to have open. Two sibling routes beside it,
`GET /generated/layer/<datasource>/<layer>/<channel>/stats` and `.../gmm`
(`data_routes.generate_layer_channel_stats`/`generate_layer_channel_gmm` →
`layer_sources.layer_channel_stats`/`layer_channel_gmm`), are what let the
Layers panel mount the same channel controls on a registered layer that the
reference image has — a 404 for a layer with no channel planes.
`route_count` 115 → 117 for these two.

**Tile decode.** `tile-loaded` reads the raw bytes off `e.tileRequest.response`,
and both channel paths decode in the worker pool. The default 8-bit WebP path
uses `createImageBitmap` + a canvas readback; the HD 16-bit path parses the PNG
directly and inflates with the browser's native `DecompressionStream`, never
touching UPNG.js. Only segmentation tiles still decode inline via UPNG (RGBA8,
and a single layer rather than one per channel). Result lands on
`e.tile._array` as a `Uint8Array`. A registered layer's channel is drawn by
TWO world items over the same tile url (`addLayerChannelSet`'s cover/paint
pair, above), so OpenSeadragon gives the pair one shared cache record and
raises `tile-loaded` with a request for only whichever item asked first — the
other's `Tile` never passes through the decode and would have no `_array` of
its own. `shareDecoded(tile)`, called from a `finally` in the tile-loaded
handler (so a scaled tile borrowed from a neighbour is shared too, not only a
freshly decoded one), leaves a copy on the cache record itself
(`cache._plexoraArray`/`cache._plexoraFormat`) rather than on either `Tile`,
because the pixels are freed when the record is evicted and belong to it;
`tileColorize.js` reads it back for the item that has no `_array`.

The HD PNG is written with **stored (uncompressed) deflate** on purpose — see
the measured facts below. The worker's PNG parser only handles what
`fast_png.py` emits (non-interlaced, filter type 0) and returns `unsupported`
otherwise so the caller falls back to UPNG.

**Colorize.** `tile-drawing` runs per tile. It uploads the tile to a texture and
runs a one-channel fragment shader that multiplies the scalar by the channel
colour, then blits the WebGL canvas into the tile's own 2D canvas. OSD then
composites that canvas onto the sketch canvas, and the sketch onto the display.
`tileColorize.js` reads `source.channel` first, before falling back to the
reference image's own channel-by-URL lookup, and HARD-RETURNS (draws nothing)
for a world item that carries a `layerId` but no `channel` record — a layer's
file can share a channel key (`<stem>_<N>`) with the reference image's, and
silently borrowing the reference's colour/window would be wrong rather than
absent. `tileColorize.js` also calls `renderer.updateShape(w, h)` GROW-ONLY —
only when a tile is LARGER than the current GL canvas, growing it to the
largest size seen and leaving it there — when a tile's size differs from the
GL canvas's, because a layer can carry its own tile grid; a tile smaller than
the canvas is upscaled and downscaled back as every reference-image edge tile
has always been, and reallocating for every one of those would be the cost
this avoids.

**Every channel drawn as a cover/paint pair carries coverage in its alpha —
the reference image's channels included, now that they are a pair too.**
`alphaMode` is `source.coverageAlpha ? 1 : 0`, asked of the tile source
rather than inferred from `source.channel` being present — it used to be
`source.channel ? 1 : 0`, on the reasoning that only a registered layer's
channel carried its own record; the moment the reference image's channels
became a pair too that inference silently went wrong for them, which is
exactly the failure mode of an inference that has to be revisited every time
a second thing becomes true. `channel_add` and `addLayerChannelSet` both set
`coverageAlpha: true` on their tile sources. `alphaMode` rides along in
`u_alpha_mode` and in the per-tile `sig`. `u_alpha_mode` is plumbed as
`alpha_mode_1i` through `renderer.gl_arguments` into `glInit.js`'s
`gl-drawing`/`gl-loaded` uniform upload, and `frag.glsl`'s `float
tile_alpha(float opaque, float coverage)` reads it: mode 0 is byte-identical
to the old constant alpha (nothing composites this way any more, but the
code path is unchanged); mode 1 — every cover/paint channel there is now —
returns the channel's own windowed intensity, so the tile's alpha carries
COVERAGE, what the cover blit reads. The colour side of the shader is
unchanged either way. `clearOrBlack(rendered, alphaMode, w, h)` prepares the
tile's own canvas: CLEARED for mode 1 (every channel today), because a black
backing is opaque and the cover blit would read it as full coverage and erase
the tile's whole footprint, including where the channel has no pixels yet;
black (opaque) survives only for mode 0, which nothing reaches. The
missing-array path clears rather than black-fills for the same reason.

**The critical optimization.** OSD re-raises `tile-drawing` for every visible
tile of every channel on *every frame*, and the pixels are almost always
identical to the previous frame's. `e.rendered` is the tile's own persistent 2D
context (OSD resolves it via `DrawerBase.getDataToDraw()` → the tile cache) and
OSD blits it immediately after the handler returns — so if it already holds the
right pixels, the handler returns early. A signature is stored **on
`e.rendered`**, not on the tile, so it travels with the canvas that actually
holds the pixels:

```
`${tile.cacheKey}|${tileFmt}|${alphaMode}|${floatColor}|${range}|${modes.edge},${modes.or}`
```

Anything that changes what should be drawn must be in that signature or the
viewer will show stale pixels.

### Changing tile quality without blanking the canvas

The **HD toggle** ("Reloads the image in high resolution") is the one control
that changes every tile address at once: `?q=hd` swaps the fast 8-bit WebP path
for the full-precision 16-bit PNG one. OpenSeadragon cannot re-point a
TiledImage at a new address space — its per-address `tilesMatrix` has to be
thrown away rather than invalidated in place, or stale canvases stay on screen
until each tile happens to be refetched — so every item is rebuilt.

**The ORDER of that rebuild is the whole of whether the user sees the slide go
black.** It used to be `channel_remove` then `channel_add` (and, for a
registered layer, `drop(); add();`), which emptied the world before anything
had asked for a tile. Measured in Chromium against a real 92-channel slide with
6 channels on, sampling the OSD canvas every animation frame across the toggle:
the lit fraction went **1.0000 → 0.0000 for five frames**, then filled back in
at 0.0164, 0.1495 … — a black flash followed by a checkerboard, in both
directions.

It is now add-then-remove, held:

1. The replacement items are added at **`opacity: 0` with `preload: true`**.
   `TiledImage.getDrawArea()` answers `false` for a zero-opacity item *unless*
   preload is set, so preload is what makes an invisible item load at all; and
   OSD's drawer skips a zero-opacity item entirely, so the held pair costs the
   fetch and the decode but not the colorize pass.
2. `ViewerManager.handOverWhenReady(items, commit)` waits for every one of them
   to report OpenSeadragon's `fully-loaded`. That flag means more than its name
   suggests here: `tile-loaded` is an **awaiting** event, so Plexora's own
   decode (tileDecode.js) has finished and every tile in view is holding a
   decoded plane by the time it flips.
3. `commit` then fades the new items up, claims them into the layer stack and
   removes the old ones — in one synchronous step, so no frame has both
   qualities and no frame has neither.

Same measurement after: the lit fraction **never drops below the value it held
when the toggle was clicked** (0.6138 worst case, against a 0.0000 floor
before). The picture does change brightness across the swap, because HD moves
the contrast domain from bytes to raw 16-bit — that is the feature, not a gap.

Four things this depends on, each with its own silent failure:

- **One `commit` for the whole group.** A channel is a cover blit
  (`destination-out`) and a paint blit (`lighter`). Reveal the new cover before
  the new paint and the base is taken away twice and the colour added once —
  the channel's own shape punched out as a dark patch, which is a worse
  artifact than the gap, not a smaller one. `addChannelItems` gates on both
  halves; `addLayerChannelSet.setStyle` prepares every handle through
  `addTiledLayer`'s new `prepareStyle(next)` and commits them together.
- **The quality is PINNED on the tile source** (`hd: Boolean(tileQuality.hd)`),
  and `getTileUrl` reads `this.hd ?? tileQuality.hd`. Reading the live flag was
  safe only while the flip also tore every item down in the same tick: with the
  outgoing items still drawing, they would fetch the *incoming* quality for
  anything panned onto mid-swap.
- **A held item is not claimed into the layer stack until it is revealed.**
  `applyReferenceState` pushes the base card's opacity onto every item the
  stack holds for the reference layer, so an item claimed early would be faded
  up by any stack change that landed mid-swap — two qualities compositing with
  `lighter`, which is a doubly-bright flash rather than a black one. For the
  same reason `hideReference` now sweeps the WORLD by `source.layerId` as well
  as the stack's own list: an eye closed mid-swap must take the unclaimed
  replacement with it, or it preloads a viewport of tiles forever and is then
  revealed under a closed eye. `addTiledLayer` keeps the same thing in
  `pending`, dropped by `drop()`.
- **The wait is bounded** (`QUALITY_SWAP_TIMEOUT_MS`, 20 s) and an
  `addTiledImage` **`error`** closes the swap too. A tile route that 404s would
  otherwise leave the toggle looking permanently dead; a pair where only one
  half added drops the half that arrived and keeps the pair already on screen,
  which is still the old quality and still correct.

`rememberView`/`restoreView` are no longer called from `setHdMode`. They were
there because emptying the world makes the next add look like a first open
(`Viewer.processReadyItems` calls `goHome` when the world reaches one item); a
world that never empties never goes home. Both are still used by
`hideReference` and `main.js`'s `rebuildTileLayers`.

`setHdMode` returns a promise that settles when every layer is back on screen,
and reports the wait through `PlexoraStatus.begin("HD tiles"/"Fast tiles")` —
the swap is no longer instant, and the navbar chip is what says the click was
heard.

### Brightfield draws none of that

A project with `image_kind == "brightfield"` (H&E and every other transmitted
-light slide) is the one image Plexora does not colorize. Its tiles are already
the picture, so `viewerManager.load_brightfield_base()` adds **one** TiledImage
with `tileFormat: 24`, `compositeOperation: "source-over"` and `index: 0`, and
both handlers above early-return on that format: `handleTileLoaded` leaves the
bytes to OSD (which turned the WebP into an image already) and
`tileDrawingCustom` leaves `e.rendered` alone, which is what draws it. There is
no `_array`, no shader pass and no contrast window anywhere on this path.

Four things follow from the ground being white rather than black, and each was
a visible bug before it was handled:

- **`compositeOperation`** goes on the `addTiledImage` options, not inside the
  tileSource, where OSD never reads it (the copy `channel_add`/
  `load_label_image` carry there is inert; the viewer-wide default is
  `lighter`). The *label* layer needs it too — a coloured outline added to
  near-white tissue saturates to white and the mask vanishes exactly when it is
  switched on.
- **The layer is found by tile key, not by position.** `imageData[0]` is the
  "Area" mask placeholder whenever the project has a segmentation, so taking
  the first entry drew the mask as the slide: a blank viewer with every tile
  fetched successfully.
- **`imageSmoothingEnabled` is on** for brightfield only. Interpolating a
  fluorescence channel invents intensities between measured pixels; a
  transmitted-light slide is a photograph and nearest-neighbour makes it blocky
  between pyramid levels.
- **`subPixelRoundingForTransparency: ALWAYS`**, brightfield only. A tile edge
  landing on a fractional device pixel is drawn antialiased and the next tile
  is drawn over it with `source-over`, so the two partial coverages do not sum
  to one and the canvas's transparency shows through as a pale hairline down
  every tile boundary — invisible on black, a grid over pink tissue. The tile
  *bytes* join exactly (measured against the source: the step across a boundary
  is the same in the served WebP as in the file), so this is drawing, not data.

`main.js` still runs the full `init()` for brightfield — only `image_kind ==
"rgb"` (the flat untiled PNG/JPEG quick view) hands off to `RgbImageViewer`.
That is what makes masks, tables, gating, ROI and Figure Builder work on an
H&E slide with no per-kind gating anywhere.

## Navigation and the App Shell

Plexora is server-rendered and multi-page: every destination is its own Flask
document. Walking from a slide to the Figures page and back therefore used to
destroy the OpenSeadragon viewer, its WebGL2 context, every decoded tile and
every piece of session state not written to the server — the **viewport above
all, which nothing persisted at all** — and rebuild it cold on return.
`services/appRouter.js` makes that one class of navigation happen inside the
document that is already open, under one rule:

> The viewer is rebuilt when, and only when, the PROJECT changes.

**How it works.** `base.html` renders `<body data-plexora-datasource="...">` and
an empty `#plexora_page_host` after the content block. A click on an internal
link is intercepted, the destination is fetched with `X-Plexora-Fragment: 1`,
and the response — the page's own `{% block style %}` and `{% block content %}`,
nothing else — is mounted into the page host while `#container` is hidden. The
stylesheets are lifted into `<head>`, the scripts are re-created (markup
inserted as innerHTML never runs), and `history.pushState` keeps the URL honest.

**Server side is one context processor and one line per template.**
`page_routes.inject_layout` picks `_fragment.html` over `base.html` per request,
and every page template says `{% extends layout|default('base.html', true) %}`.
No route knows the router exists, and a request without the header — a
bookmark, a hard reload, JavaScript off, every other test — gets the whole
document exactly as before. `tests/test_app_shell.py` pins that a fragment is
byte-for-byte the same content the full page renders.

**`_fragment.html` also emits `data.active_tool_styles`/`_scripts`**, which on a
full page base.html puts in `<head>`. Not optional: Figure Builder's library,
canvas and captures pages are whole pages whose controllers live in the plugin's
script list, so a fragment without them arrives as static markup and **looks completely correct** —
heading, tabs, search box, all in the template — while nothing ever loads and no
button does anything. Empty on every core page, so a core-only build pays
nothing and no template names a plugin.

**Three limits, each buying a large amount of safety.** They are why there is no
teardown code in this file to get wrong:

1. **Only a document that booted AS a viewer routes at all.** Landing on
   `/open_project` and clicking a project is a full navigation. There is no
   viewer to preserve yet, and booting one client-side would mean re-entering
   `main.js`, which has document-scoped top-level bindings (`const
   eventHandler`, `const datasource`) and can only run once.
2. **A link to a DIFFERENT project is a full navigation.** The server holds one
   loaded datasource (`data_model._loaded_source`) and `ImageViewer` has no
   destroy path. Client-side, "different project" is checked against
   `flaskVariables.datasources` — a snapshot frozen at this page's own render —
   which cannot know about a project registered since; the server-side half
   catches what that snapshot misses. See "a link to a project this page has
   never heard of" below.
3. **The viewer is hidden with `visibility`, never `display`.** OSD's autoResize
   compares its container's `clientWidth`/`clientHeight` every frame;
   `display: none` reports 0×0, resizing the viewport to nothing and taking the
   zoom with it — the exact state this exists to protect. See
   `#container.plexora-view-hidden` in viewer.css.

Anything it cannot do, it declines to do: an unroutable link, a fragment that
will not fetch or parse, all fall through to `window.location`.

**A page controller registers with `PlexoraPage.register(fn)`, not
`DOMContentLoaded`** — that event fires once per document, so a second visit to
a page would never get one. `register` mounts `fn` on the initial load and after
every swap; `fn` keeps its existing `if (!root) return` guard, which is what
makes running every controller on every page safe. It may return a **function**
to tear down anything that outlives the markup (settingsPage's migration poll,
figureWorkspace's window listeners); anything else returned is ignored, since
several of these are one-liners around a `boot()` that answers with its
instance.

**Two things a change here must not break.** A script already in the document is
never re-executed — these are classic scripts and several declare a top-level
`class`, whose re-declaration is a `SyntaxError`, and `columnClassifier` and
`coordinateField` are both loaded by `base.html` AND named again by the pages
that use them. And a navigation asked for while another
is in flight is **queued, not dropped**: for a `popstate` the browser has already
moved the address bar, so ignoring it leaves the URL describing a page that is
not on screen — which is what holding Back down did before the queue existed.

**Deliberately still full navigations**, both marked at the call site: saving on
the project edit page, and `importSample.js`'s `finishScoped` reloading after
"+ Add Layer" attached a mask. Each has just
changed what the project IS, and a running viewer holds the config, the column
statistics and a loaded datasource from before it. The edit page's save takes
that reload even when the mask pyramid is still converting — it used to hold the
form behind a blocking overlay until the job finished, and now hands the wait to
the viewer (`segmentationWait.js`) instead.

**The viewer leaving and returning is `onHide()`/`onShow()`.** `toolLoader.js`
turns `plexora:viewer-hidden` / `plexora:viewer-shown` into the hook a plugin
already implements, so ROI's pen and document-level keys stand down under a
routed page without any plugin learning a second lifecycle. Figure Builder's
`onShow` is also where it reads the pending-edit note the canvas leaves in
`sessionStorage` — `applyOrDefault` only ever ran at tool boot, which used to be
the only way back into the viewer.

**A link to a project this page has never heard of is also a hand-off**, not
just a redirect to one. `flaskVariables.datasources` — what `canRoute` checks —
is a snapshot frozen at this page's own render, so a project registered since
(a Quick View of a new file, a project added from Jupyter or another tab) isn't
in it; the router would otherwise fragment-mount the viewer template over the
live viewer with `main.js` already marked as run, an inert shell until a manual
refresh. The server's own answer is the only one that can't be stale: every
`image_viewer` response carries `X-Plexora-Datasource` (`page_routes.py`), and
`showPage()` reads it after the `response.ok` check — if it names a project
other than the one being fetched, the browser gets the navigation instead of
the fragment. `go()` sets a call-local `handedOff` flag (not a lasting one, so
a navigation the user cancels at a `beforeunload` prompt leaves a working
router) and drops a queued route once it's set, because mounting that queue
entry would hide the viewer and boot a controller in the seconds before the new
document actually arrives.

Covered by `tests/js/app_router_probe.mjs` (18 checks, driven from
`tests/test_app_router.py`), `tests/test_app_shell.py` (which also covers the
`X-Plexora-Datasource` header, present only on the viewer route), and the
viewer-visibility half of `tests/js/tool_switch_probe.mjs`.

## Status Indicator (`PlexoraStatus`)

One indicator for the whole app, far right of the navbar on every page that
extends `base.html`. Built by `services/appStatus.js`, styled in `main.css`.

```js
const task = PlexoraStatus.begin("Auto-contrast");
task.done();                       // or task.fail("reason"), task.relabel("...")
await PlexoraStatus.track("Saving", promise);
```

Three states, using the `--accent-success` / `--accent-warning` /
`--accent-danger` tokens: green "Live", orange plus a 1–2 word label with a
morphing glyph and a shimmer sweep, red plus a short reason. Tasks are
refcounted so overlapping features compose; the most recently begun label wins.
Debounced 150 ms before showing and held 400 ms, so warm sub-150 ms work never
flashes.

**Add new indication by calling `begin()`, not by inventing another affordance.**
Three inputs are already wired automatically:

- **`window.fetch` is wrapped once** in `appStatus.js` — every one of the ~40
  call sites (26 in `dataLayer.js`, each with its own swallowing try/catch)
  reports transport failures with no per-site change. It deliberately does *not*
  mark requests busy: a generic label is worse than the specific ones features
  supply.
- **`GET /health`** (`system_routes.py`, returns 204, does no work) polled every
  5 s while the tab is visible; two consecutive failures → red. Required because
  an idle page issues no other requests, so nothing else notices a dead server.
- **Tiles still loading**, tracked **per TiledImage** in `watchViewer()`. Not via
  the Viewer's aggregate `fully-loaded-change`: that only recomputes when some
  TiledImage raises its own event, and a newly added image doesn't raise one on
  the way in, so the viewer's cached `_fullyLoaded` stays `true` through the
  whole load and matches again at the end. Verified: **zero** viewer-level events
  across a channel toggle. Per-image tracking via `world`'s `add-item` /
  `remove-item` is the working hook.

**The centre spinner over the image is a separate indicator, `PlexoraViewerLoader`
(`services/viewerLoader.js`), not `PlexoraStatus`.** The navbar chip answers "is
work outstanding"; the centre spinner answers "is there anything to look at
yet", and the two disagree on purpose — a project with every channel switched
off is live and idle, and also showing nothing. Visible iff `holds > 0 ||
(!painted && (world.getItemCount() > 0 || !booted))`: `holds` is
`ImageViewer.setLoading()`'s explicit ref-counted claims (a stack now, not a
`display` write, because two overlapping true/false pairs used to cancel each
other and hide the spinner mid-load); `painted` latches true on the viewer's
first `tile-drawn` since the world was last empty (not `fully-loaded-change` —
see the per-TiledImage note above, and besides "fully loaded" arrives far later
than "something to look at"); `booted` is set one macrotask after
`window.__plexoraReady` settles (`main.js`'s `.finally()` calls
`PlexoraViewerLoader.settle()`), deferred because OSD queues tile-source
construction in its own 0 ms timer and settling synchronously would see an
empty world and blink the spinner before those adds land. The markup
(`#openseadragon_loader` in `index.html`) is visible by default in CSS, so the
spinner is up from first paint before any script has run; `viewerLoader.js` is
loaded before `main.js` for exactly that reason — see the Repository Map entry.
`rgbImageViewer.js` takes a hold before constructing OSD and releases it on
`open`/`open-failed`, because its drawer is WebGL and raises no `tile-drawn`.
`ImageViewer.init()` no longer awaits `waitForGLReady()`: that wait could never
be satisfied (`glReady` resolves on OSD's `open`, which needs a channel added,
which only happens after `init()` returns), so it always burned its full 5000 ms
timeout for nothing — removing it took first tiled layer from ~5.4 s to ~0.4 s.

## Staged Progress (long jobs)

Two jobs report through the same shape, and both fixed the same complaint: a
bar that sat at one number for minutes and read as a hang.

`data_model._staged_reporter(stages, on_change)` returns `(stage, report)`.
Each stage owns a **band** of the bar; `stage(key)` moves to the foot of its
band and names itself, `report(done, total)` maps a countable fraction into it.
Output is monotone by construction and only fires on a real change — the tile
loop calls back once per written tile.

- **`SEGMENTATION_STAGES`** — `loading → inspecting → preparing → building →
  writing`. The old first callback was the tile loop, so everything before it
  ran at 0%: the servable/adoptable checks (which read sampled pixel windows in
  outline mode) and above all the full-plane read, 60 s locally and 179 s
  streaming for the Orion mask. `stage_callback` is threaded
  `start_segmentation_job → convertOmeTiff → resolve_outline_segmentation →
  pyramidize_segmentation_mask`, kept **separate** from `progress_callback`
  because that one's `(done, total)` contract is pinned by tests.
- **`TABLE_STAGES`** — `opening → metadata → preparing → loading → finalizing`,
  served by `GET /get_table_status`. The load stays **synchronous inside the
  save request** on purpose: that request also validates the user's answer and
  restores the previous project when it is unreadable. The browser polls
  alongside the outstanding POST — waitress is multi-threaded.

Client: `views/jobProgress.js` is the one panel (bar + `.connect-steps` stage
rail); `views/tableProgress.js` is the poller over it now that the deleted
`segmentationProgress.js`'s import-page bar is gone — `views/segmentationWait.js`
watches a mask's build instead, from inside the viewer, as a listener on
`main.js`'s own poll rather than a second one over `jobProgress.js`. **Not**
`PlexoraStatus` — that is busy/live/error only, and `main.css` argues the case
where the rule lives.

## Performance: Measured Facts

These were established by profiling, not inspection. Several contradict the
obvious guess — read before optimizing.

**Server, 42 tiles (7 channels × 6), real whole-slide data:**

| | Before | After |
|---|---|---|
| First viewport | 63.8 s | 1.9 s |
| Re-pan over same tiles | 65.0 s | 0.005 s |

The dominant bug: `load_datasource()` ran on **every tile request** for
image-only projects. Its early return required `datasource is not None`, but
a project with no feature table legitimately sets that to `None`, so it could never
short-circuit; and `generate_zarr_png` treated `seg is None` as "not loaded".
Every tile reopened the OME-TIFF, re-parsed the OME-XML, wiped the derived
caches and bumped `load_generation` — which, being part of the tile cache key,
pinned that cache at a permanent 0% hit rate.

Loadedness is now tracked by an explicit `_loaded_source` global, set last inside
`load_datasource()`. **Never add a guard that infers loadedness from a global
that can legitimately be `None`.** Use `ensure_loaded()`, and call it *before*
sampling `load_generation` for a cache key — loading is what bumps it.

**Client, mid-zoom pan, median / p90 / frames over 16 ms of 98:**

| channels | before | after |
|---|---|---|
| 5 | 8.3 / 9.3 / — | 8.3 / 8.5 / 4 |
| 7 | 8.4 / 25.1 / 18 | 8.3 / 9.3 / 3 |
| 11 | 8.4 / 50.1 / 45 | 8.3 / 8.9 / 6 |
| 15 | 33.4 / 90.9 / 73 | 8.3 / 9.3 / 7 |

Two changes got there, and the profile pointed at a different culprit at each
channel count:

- **At 7 channels**, 82% of wall time was one call: the WebGL-canvas → 2D-canvas
  blit, ~103 times per frame at 2.29 ms each. Fixed by the signature cache above
  (median 258 → 8.4 ms).
- **At 15 channels**, ~60% was tile decode (`handleTileLoaded` 24.6%,
  `getImageData` 23.5%, plus Blob/bitmap/GC). Fixed by the worker pool. It scales
  with tiles streaming in, i.e. with channel count — at 7 channels the same work
  was ~2%, which is why it did not show up first.

**HD mode was a separate, worse problem.** With 7 channels panning into fresh
territory, HD sat at **466.6 ms** median while the default path was 8.3 ms — 56x
slower — because the signature cache helps a *stationary* HD view but every
newly-arrived tile still paid for decode. A CPU profile put ~81% of all HD time
in pako's JavaScript inflate (`inflate_fast` alone 71%) and another 13% in
UPNG's unfiltering. Two changes, measured in sequence:

| | median |
|---|---|
| 16-bit PNG at zlib level 6, UPNG on the main thread | 466.6 ms |
| + stored (uncompressed) deflate | 125.0 ms |
| + PNG parsed in the worker with `DecompressionStream` | **8.4 ms** |

Uncompressed costs 1.15x the bytes (2.10 MB vs 1.82 MB per 1024² tile) and drops
server-side encode from 36.9 ms to 1.5 ms. Output verified byte-identical.

An earlier attempt to skip PNG entirely and send raw `uint16` **failed**: OSD
wraps the tile response in a Blob and needs a decodable image to build the
tile's canvas (which is what `e.rendered` is), so a non-image response leaves
the tile permanently unloaded and the canvas black. The payload has to stay a
valid image; the win comes from making it trivially cheap to decode, not from
dropping the container.

**Switching a channel on was slow for a completely different reason.** Not
tiles — those arrive in ~200 ms, and tile latency barely moves under load
(44 → 54 ms). The auto-level `GaussianMixture(3, max_iter=1000, tol=1e-6)` fit
costs 0.2–1.9 s per channel (17.1 s for all 19), and everything waited on it:

| | before | after |
|---|---|---|
| newly enabled channel becomes visible (cold) | 6.6 s | ~0.12 s |
| …reaches its final contrast | 6.6 s | 1.6 s |
| restoring 3 saved channels (cold) | 5.8 s | 1.3 s |

Three distinct causes, all fixed:

1. A new slot starts at `[0, 255]`, the whole byte domain. Against a
   quantization ceiling of the channel's full-plane max that renders the tissue
   near-black, so the channel was *drawn* in 200 ms but *invisible* until the
   fit landed. `get_image_channel_stats` now also returns `vmin_hint`/`vmax_hint`
   (percentiles of the log-intensity distribution it already computes);
   `autoChannel` applies those immediately and the real fit replaces them.
2. `qmin`/`qmax` — needed to convert a stored raw-16-bit range into the byte
   domain — were reachable *only* as two extra fields on the GMM packet. So
   restoring saved channels ran a ~1 s fit per channel purely to read them, even
   though the saved range already *is* the auto-level result. They now ride on
   the stats response; `ViewerSidebar.quantWindow()` is the single accessor.
3. The restore loop `await`ed that fit **per iteration**, so channels restored
   strictly serially — each GMM started within 3 ms of the previous one ending.

The hint percentiles (p50 / p99.5, `_HINT_PERCENTILES`) were chosen by sweeping
7×5 candidates against the real GMM for all 19 channels and scoring the error in
the **byte** domain — worst case 15 byte-levels, mean 6.6, versus ~234 for the
`[0, 255]` default. Do not retune by eye.

**Do not loosen the GMM's `tol` to make it faster.** At `tol=1e-4` the total
drops 17.1 → 5.9 s but 12 of 19 channels shift vmin/vmax by >2% and CD45 moves
155 → 475. The fit is also non-deterministic run to run (SMA varies 554–569 /
2812–2965) because `random_state` is unset — so nothing downstream may assume a
stable auto-level across sessions.

**Things measured and rejected — do not redo these without new evidence:**

- *Single-pass WebGL drawer* to eliminate the `compositeOperation: "lighter"`
  sketch canvas. Forcing `source-over` to isolate the cost made frame times
  **worse** (15 ch median 41.6 → 91.9 ms). The sketch canvas is not the
  bottleneck.
- *Per-frame colorize budget* spreading work across frames. The extra
  `forceRedraw` passes cost more than the spike they spread (11 ch median
  8.4 → 16.6 ms). Built, measured, reverted.
- *Never-blank rendering* (thumbnail underlay, `immediateRender`). Measured blank
  pixel fraction when panning into fresh territory: worst 0.002 on zoom-in,
  exactly 0 on a hard jump. OSD's coarser pyramid levels already cover it.
  **This is about moving the VIEWPORT and says nothing about changing tile
  QUALITY** — the coarse levels that cover a zoom belong to the same TiledImage
  and the HD toggle replaces the TiledImage. Measured separately at 0.0000 lit
  pixels for five frames; see "Changing tile quality without blanking the
  canvas" above.
- *Fixing the GL texture cache, hoisting `gl.getParameter`, removing the
  O(tiles²) `tile-drawn` handler.* All real bugs, all worth keeping, but together
  they moved the median from 283.3 → 291.6 ms — nothing. The evictor fix matters
  for **memory**, not speed: the old one was written against OSD 2.x's
  `_tilesLoaded` shape (`{tile: ...}` records), so it freed nothing while still
  tearing tiles out of OSD's LRU, and the cache grew unbounded.

**`initGL` registers its handlers once.** `createGLInit` is an `open` handler,
and `open` is re-raised by every `channel_add` and by `load_label_image` — but
OpenSeadragon's `addHandler` does not dedupe, so a 7-channel project used to
hang seven copies of `tile-loaded` and `tile-drawing` on the viewer, and
`tile-drawing` is re-raised for every visible tile of every channel on every
frame. The duplicates were invisible because both handlers are idempotent (the
decode guards on `tile._array`, the colorize pass returns early on its
signature), so all they ever did was multiply the per-frame bookkeeping by the
channel count. A `wired` flag in the closure fixes it; the redraw at the end of
the handler is what the re-raise is actually for. `renderer.init()` still
refetches and recompiles both shaders per raise — cheap next to a tile load,
and left alone.

**Server per-tile cost breakdown** (1024² tile, after the fixes): zarr read
7.9 ms, LUT quantization 1.1 ms, WebP encode ~21 ms. Encode dominates. The
`method=` table is in a comment at the encode site — `method=0` is 21 ms/64410 B
versus `method=6` at 97 ms/58884 B.

**Concurrency ceiling.** `threads=8` buys less than it looks like: zarr 3 funnels
every read through a single global `zarr_io` event-loop thread, and tifffile
takes a per-file re-entrant read lock. All tile I/O is globally serialized; only
decode escapes to a pool. Caching is the lever, not thread count.

**Mini-map (overview lens).** `data_model.generate_channel_overview` builds a
lossless mode-`L` WebP from the already-resident `zarray` (the downsampled
~200-400px per-channel array `load_datasource` keeps), quantized with the same
`get_channel_quantization_window()` the tile path uses — which is what makes
the lens's contrast match the viewer for free. Measured on a real 298x357
array: lossless is 50408 B / 3.0 ms and byte-exact, versus quality=90 at
23514 B / 3.4 ms with max error 11 grey levels — cheap enough that there is no
reason to take the tile path's lossy tradeoff here, and a narrow contrast
window would multiply that byte error into a visibly wrong lens. Warm
server-side generation measured at 3.3 ms/channel for 19 channels (the first
call per channel additionally pays the one-time full-res `.max()` for the
quantization window, which the tile path pays anyway and caches). Client side,
Playwright against a real datasource (3 channels, chromium/ANGLE) measured pan
median 8.3 ms both with the lens collapsed and expanded — identical to the
documented pan baseline above — with zero `_draw()` calls across 100 pan
frames, zero network requests until the lens is first opened, and no request
on a colour change or a re-expand. `tests/js/mini_map_probe.mjs` +
`tests/test_mini_map.py` cover the client geometry/shader/guard logic (61
probe checks, 19 pytest tests, 15 of them mutation tests) and
`tests/test_channel_overview.py` covers the route and the quantization
contract, including that the Area placeholder does not shift the zarray
channel index.

If the lens opens to a black circle, check the server's age before the code —
see "A Python change needs a restart" under Key Invariants. `MiniMap._updateNote`
now says so on screen: a 404 on every active channel prints "restart the Plexora
server", any other total failure prints a generic message, and a partial failure
(one channel of several) stays silent because that draws a perfectly good map
with one colour missing. Failures are recorded per channel (`_failed`, srcIdx ->
status) rather than in one last-error field, because the fetches drain
concurrently and a scalar is won by whichever request happens to finish last.

**Visium HD, measured on the real run** (`/Users/aj/Downloads/visium HD`,
Space Ranger 4.1, pancreas, 2 µm grid 5524² = **19.9M bins, 869M nnz**):
building the bin store takes **~6 min, 8 GB on disk**; converting the 8 µm
level to an `.h5ad` (`tenx_matrix.convert`) is **165 s, 1.5 GB peak RSS, a
4.4 GB file**; a bin tile computes in **9–56 ms at every level** (a
whole-slide tile in **18 ms**); a lazy gene column read
(`AnnDataAdapter.read_feature_column`) is **5–11 ms**; a whole-transcriptome
`describe_features()` answer is **0.7 s, 20 MB of JSON, 1.1 MB gzipped**
(see `data_routes.serialize_and_submit_json`); and the 838,000-polygon
`cell_segmentations.geojson` reads (`boundary_mask.read_geojson_polygons`)
in **5.6 s**.

## Key Invariants

- **`datasets.json` is a sibling of config.json and never a key in it.** Every
  top-level key of config.json is a project — `Project.load_all` enumerates it
  that way and so does everything downstream — so a `"datasets"` key would
  become a phantom project on the Open Project page with no image and no way
  to delete it.
- **Dataset membership is pruned in the view and never written.** An unmounted
  shared root must not permanently erase a grouping: its projects are simply
  absent from what `load_all(known=...)` returns for as long as the root is
  gone. Nothing prunes on WRITE either -- every mutation (`create`, `rename`,
  `describe`, `assign`, `forget_project`) re-reads the file unpruned, so
  renaming a dataset while a drive is unmounted cannot drop the members that
  live on it. A dangling name only leaves the file when the project is deleted
  (`forget_project`) or explicitly reassigned.
- **One assign verb for every gesture that moves a project.** Drag-and-drop
  onto a folder, drag-and-drop onto the root crumb (unassign), and the "Move
  to…" picker all post `POST /projects/assign {projects, dataset: id|null}` —
  `dataset_routes.py`'s only mutation besides create/rename/delete. A second
  endpoint for the same effect is exactly the drift that let two surfaces
  disagree about what moving a project means.
- **`manifest.answered()` is the single truth about whether a project has
  something.** Nothing else — a route, a plugin, a template — may reimplement
  "does this project have a table"; `api/plugin.py`'s `_answered` delegates to
  it, and `GET /projects`' badges and `manifest.summary()` read the same
  function the requirements machinery does, so the Open Project page and the
  progressive-requirements modal cannot disagree about what a project has.
- **The word `dataset` still means two different things, on purpose, and
  neither renaming fixes it.** `plexora.datasets`/`api.dataset()` (the folder
  a cohort of projects lives in) and `Project.dataset`/`ctx.dataset`/
  `datasetName` (a project's own feature table) are unrelated concepts that
  happen to share a name from before the folder concept existed. The API
  rename (`api.dataset` → `api.project_data`, aliases kept) resolves the
  collision only where it was actually ambiguous — a plugin author holding a
  `Dataset` object; the config key and every internal reference to "this
  project's data source" were deliberately left alone, because renaming them
  would touch every plugin and template for a collision that, read in
  context, confuses no one.
- **Forgetting a project's dataset membership is a route concern, not a
  `Project.delete()` concern.** `project_routes.delete_project` calls
  `datasets.forget_project(name)` itself, after `Project.delete()` returns,
  rather than `delete()` doing it internally — `Project.delete()` is also what
  import rollback calls to undo a half-finished registration, which is not a
  human deleting a project and must not touch `datasets.json` at all.
- **A carried restore runs strictly after this sample's own saved state, never
  before or beside it.** `viewerSidebar.whenModulesApplied()` is the seam:
  `init()` fires every sidebar module's `applyOrDefault` without awaiting any
  of them (right for the sidebar itself, since none blocks the others), but
  `Promise.allSettled` over that set is kept as `this._modulesApplied` for a
  caller that has to run strictly after. `main.js`'s `restoreCarriedState()`
  awaits it before calling any plugin's `applyCarryState` — re-imposing a
  carried marker before this sample's own gates have loaded would read the
  previous sample's numbers, which is the one thing carrying an arrangement
  across a dataset walk must never do. The same ranking applies to channels:
  `viewerSidebar.carriedChannels(savedRows)` sits BELOW a launch state (an
  explicit notebook request about this page) and ABOVE this project's own
  saved list in `init()`'s selection, and only the colour travels — the
  window comes from this sample's own saved row when it has one, and is
  otherwise left out so `applyLaunchChannels` auto-levels against this
  image's own data.
- **`[tool.setuptools.packages.find]` namespace discovery must stay ON** (the
  default -- do not add `namespaces = false`). `plexora/server` and its
  `models/`, `routes/`, `utils/` subpackages have no `__init__.py`, so turning
  discovery off silently ships a wheel with no server in it while the build
  still looks successful. The `exclude = ["plexora.client*",
  "plexora.plugins.*.tests*"]` list exists because leaving discovery on also
  sweeps up `plexora/client/node_modules/flatted/python/flatted.py` and every
  plugin's `tests/` directory. Check with
  `python -m zipfile -l <whl>` and confirm it lists
  `plexora/server/models/data_model.py`.
- **`cli.py` and `connect.py` must stay importable without the `plexora`
  package.** `tests/test_cli.py` and `tests/test_connect.py` load them straight
  off disk with `spec_from_file_location` -- a test harness that had to install
  the package first to check its argument parsing would be paying a needless
  price, and the desktop app's own embedded interpreter is not a reason this
  rule exists any more (it ships a real interpreter and a real `plexora`
  package; the old PyInstaller onefile build, which put the package somewhere
  an importlib file loader could not reach, is gone). Anything from the
  package goes in a lazy import INSIDE a function (`_run_where`,
  `_run_config`, `_run_connect`, `_run_node_connect` are the pattern) --
  `_spawn_flags()` (`plexora._subprocess.popen_kwargs()`, or `{}` when the
  import fails) is the same pattern for every `Popen`/`run` in `cli.py`. This
  is why `cli.py` keeps its own copy of `_clean_base_url` rather than
  importing `plexora._url`; `tests/test_url_helpers.py` pins the two against
  each other.
- **No token ever goes on a command line.** Everything in a remote command is
  visible in `ps` to every other account on a shared login node. `plexora node
  serve` generates its own token and prints it on stdout (`[plexora-node]`,
  inside the ssh channel); the registration that uses it is POSTed through the
  tunnel to the far viewer's own `/settings/nodes`. Ports on argv are fine and
  are chosen locally up front, because an `-L`/`-R` forward is fixed when the
  connection opens. `remote_sessions.redact()` keeps the same token out of any
  log tail a page can show.
- **A Windows remote is never asked for a pty.** `direct_ssh_argv(...,
  tty=False)` drops `-t` for `remote_os="windows"`: Windows sshd answers `-t`
  with a ConPTY, a terminal emulator that renders and hard-wraps what the far
  side prints at the console width, and the node's announce carries a 32-hex
  token well past 80 columns -- so with a pty a perfectly working connection
  prints an announce `NODE_ANNOUNCE_RE` can never match. This is genuinely
  invariant to the machine, not a preference: asking would not merely be
  uglier, it would make the connection fail to register.
- **Closing the held stdin pipe must happen BEFORE terminating ssh, never
  after.** The pty a POSIX remote gets for free ties a node's life to its ssh
  channel (a SIGHUP on disconnect); a Windows remote has none, so
  `--exit-on-stdin-close` has the node watch its own stdin instead, and
  `_Watched(hold_stdin=True)` holds the local end of that pipe open so an
  ALREADY-inherited EOF (Plexora as a service, `< /dev/null`) does not read as
  "the connection is over" the moment ssh forwards it. `_Watched.stop()`
  closes the pipe first and terminates ssh second: reversed, the channel the
  close message needs to cross is gone before it can cross it, and the node
  is left running with no ssh reaching it -- the exact orphan this exists to
  prevent.
- **A saved remote profile stores no secret.** `remotes.py` has no field for a
  password; credentials reach ssh through `askpass.py` and live in memory for
  the seconds between the user typing one and ssh consuming it -- or, when
  a connection has more hops to authenticate, until it is open or has
  failed (`_forget_secrets_locked`). Pinned by
  `tests/test_remote_connect.py`, including that the answer appears in no
  status payload and that a one-time code is never replayed. A Google Cloud
  profile's `extra["gcloud"]` holds the same rule under a different name: it
  describes a connection (project, bucket, machine type) and is never a way
  into one, because the credential lives in `gcloud`'s own store and Plexora
  never reads it.
- **`plexora/gcloud.py` has no storage-deletion verb, and must never gain
  one.** `delete_instance`'s argv cannot mention the bucket at all, so
  "deleting the VM never deletes the bucket" is structural rather than a
  promise anybody has to keep by being careful.
- **Plexora cannot delete a VM it did not create.** `delete_instance()`
  describes the instance first and refuses unless it carries the
  `created-by=plexora` label `create_instance()` wrote on it -- read off the
  machine itself, not off the saved profile, so a hand-edited record, a stale
  `vm_source`, or a mistaken button all fail the same way.
- **A rented VM is put back on every exit path, and the profile says how.**
  `RemoteSession._release_compute()` runs from both `stop()` and
  `_tidy_after_end()`, so the Disconnect button, a failed connect, a
  connection dying on its own, an atexit handler and the VM's own idle-shutdown
  timer all consult `gcloud.exit_action(record)` -- previously only the HTTP
  disconnect route consulted anything at all. Three endings since schema v4
  (`on_exit`: `leave`/`stop`/`delete`, default **stop**); a v3 record's
  `stop_vm_on_disconnect` boolean is read as the two-valued version of the same
  question, so no profile saved before this silently switches to "leave
  running". **A failed connection is always stopped and never deleted**, even
  when the profile says Delete: the reason it failed is in
  `/var/log/plexora-startup.log` and `/tmp/plexora-gcsfuse-install.log` on that
  disk, and the next connection prints both -- deleting it would destroy the
  account of the bug that had just been hit. Never deletes a machine the user
  already runs: `exit_action` refuses to return Delete for one, `profile()`
  will not store it, `delete_instance` checks the instance's own label, and the
  form greys the row. The idle-shutdown systemd timer is likewise never
  installed on a `vm_source="existing"` machine; stopping one IS allowed,
  because that is a person answering a question about their own server.
- **Nothing on the internet can reach a Plexora VM, whether or not it has an
  address.** Since the egress fix a rented VM has a public IP by default (it
  cannot install gcsfuse or Plexora without one -- see the gcloud section), so
  the invariant is carried by `plexora-deny-public-ingress` instead: deny all
  ingress from `0.0.0.0/0` to `--target-tags plexora` at priority 65000, which
  beats the default VPC's world-open `default-allow-ssh` at 65534. Three
  orderings hold it up and each is pinned by a test: the rules are written
  before any instance exists; a reused VM is **tagged before it is addressed**;
  and a `vm_source="existing"` VM is never tagged, addressed or denied, because
  its network is not Plexora's to change. Turning `external_ip` off is allowed
  but `ensure_instance` then refuses to create the VM unless the subnet has
  Cloud NAT or Private Google Access.
- **A `recovery` key is only ever attached by the raiser, never inferred from
  an error's text.** `GcloudError.recovery` is set once, at the one place in
  `_create_failure` that knows the fix is a single unambiguous edit (a Spot
  zone-capacity refusal); `RemoteSession._fail` copies it with `getattr(exc,
  "recovery", "")` and nothing downstream re-derives one by matching a
  substring, because a button offered on a guess can change a saved profile
  for the wrong reason.
- **A VM's state is fetched on demand, never on the 1 Hz poll.** Settings'
  `vmStatus` check runs once per cloud-profile card plus a re-check after any
  VM action or a disconnect, and is deliberately kept off the page's poll
  loop, because that loop already ticks every second and one `gcloud`
  subprocess per cloud profile per second is not a cost anybody chose. A
  connected session is itself treated as proof the VM is running, so no round
  trip is needed while one is live. `POST …/vm/start` is the one VM verb that
  does not end live sessions first, because a stopped VM has none to end.
- **Environment detection only ever fills in flags the user did not type.**
  `should_detect` returns False for any of `--ood/--remote/-r/--bind-node/
  --base-url/--host/--login-host`, for `PLEXORA_HOST` in the environment (the
  Docker image sets it and means it), and for `--no-detect`. Every failure
  inside it means "we learned nothing", never a traceback in front of somebody
  who wanted a local viewer.
- **Subcommands are split off `argv[0]`, not by an argparse subparsers
  action.** A subparsers action is itself a positional, so on one parser with
  the optional `datasource` positional it takes first refusal on the only
  argument (`plexora tonsil` → "invalid choice") and, when a subcommand DOES
  match, the trailing positional then resets `datasource` to None afterwards,
  discarding what the subparser just read. `cli.split_command` +
  `cli.build_parser(command)` is the fix; do not merge them back.
- **A browser cannot serve a file by path.** Reading a file in place needs a
  process on that machine, and that process is the data node `plexora connect`
  starts on the user's laptop by default (`--no-local-node` to opt out). Without
  it, choosing "Local" on a data field degrades to a CSV upload plus a sentence
  naming the command to fix it — for an image, a mask or an .h5ad there is
  nothing else a browser alone can do.
- **A resource id is derived from the file's own path, never generated.**
  `nodes.resource_id_for` hashes the path; a project's binding and a node's
  manifest only meet again because both were computed from the same filename,
  with nothing exchanged between sessions to reconcile them — which is what
  lets a project reopen in a later session with no reconfiguration.
- **Where the primary image LIVES can change; which image it IS cannot.**
  `nodes._same_image` runs before `attach_image`, `attach_segmentation` and
  `detach("image", ...)`, and compares width/height/`num_channels`. Every ROI
  outline, figure panel and cell coordinate a project holds is expressed in
  that image's pixel space, and nothing downstream would notice a swap to a
  same-sized-but-different image — it would render, and mean something else.
- **A missing local mask or table degrades to the resource-status banner; a
  missing image still fails loudly.** The image is the floor of the contract
  (see `api/dataset.py`'s `Dataset.image`); a mask or table a node has stopped
  serving is something `data_routes.resource_status` can name and the viewer
  can keep working around.
- **A node manifest holds kinds, ids and paths only — never a project, a role
  or a read spec.** `--manifest PATH` lets a `--dynamic` node come back
  serving the same resources under the same ids; what those resources MEAN is
  recorded only on the primary, in config.json, same as everything else a node
  is not trusted with.
- **A data node's address is not a mount path.** `nodes.json` holds absolute
  endpoints (and optionally a browser-side address that may be portal-relative);
  none of them goes through `clean_prefix`, which is about where THIS app is
  mounted. Likewise `node://<node>/<resource>` is written where a filesystem
  path would go in config.json, so anything that stats, resolves or migrates a
  stored path must test `providers.is_node_locator()` first --
  `Path("node://hpc/cells")` is a perfectly valid relative path that exists
  nowhere, and on Windows it silently becomes `node:\hpc\cells`.
- **A tile URL has exactly one `?`.** The HD flag used to be written as a bare
  `"?q=hd"` in `getTileUrl`, which is a second `?` the moment a tile is fetched
  from a node and carries its token too. Anything added to that query joins
  with `&` through the same list; `tests/js/tile_url_probe.mjs` pins it,
  because a URL with two `?` fetches successfully and returns the wrong thing.
- **Whether the browser can reach a node is the browser's question.** The
  server offers a candidate (`/resource_routing`) and the browser probes the
  node's own health endpoint before using it, falling back to the proxy on
  anything short of a clean answer. A server-side guess would be wrong in
  exactly the deployments this exists for -- a cluster node reachable from a
  laptop through a tunnel and from nowhere else, a portal that rewrites
  addresses. Direct routing additionally requires the node to have been started
  with `--allow-origin <viewer origin>`; without it the probe fails and
  everything silently proxies, which is correct but worth knowing when
  measuring. **Only a remembered `true` verdict is reused** from
  `resourceRouting.js`'s sessionStorage cache — a remembered `false`
  (unreachable) always re-probes. A node mid-restart or a tunnel not yet up is
  a fact about a moment, and caching it pinned the whole tab to the proxy hop
  silently for as long as the tab stayed open, even long after the node came
  back; re-probing costs at most `PROBE_TIMEOUT_MS` once per load. One
  exception is decided server-side, before the browser ever gets a candidate
  to probe: `/resource_routing` skips a node whose `Node.browser_reachable` is
  False. A kernel node's address is the notebook process's own loopback, which
  reaches somewhere different from a hosted browser's point of view than it
  does from this server's -- offering it as a candidate would have the
  browser probe its own laptop for a machine that is actually the remote
  kernel, and would carry the node's token there in the attempt.
- **A read that is proportional to the table never crosses a node boundary.**
  The primary keeps a compact copy (the cell id, the coordinates, and the
  columns filling a role) so the spatial index, the centroid layers and the
  hover lookup answer locally; everything else is a column at a time or a
  bounded result. `TableHandle.frame()` therefore REFUSES for a node-backed
  table -- a frame missing every marker but answering `frame["id"]` perfectly
  well is the shape of bug that passes every test. Use `geometry()` when ids
  and positions are what you meant.
- **Work that reads the file and the loaded frame together runs where the file
  is.** Every scientific write-back checks the file's row count against the
  loaded table before touching anything, and that check means nothing across a
  network. Those are `@table_operation`s, not provider reads, and their
  refusals travel as data (`{"ok": false, "reason": …}`) so a "column already
  exists" stays something a user acts on.
- **A background job captures the registry it was started against.** The
  segmentation job outlives a delete, a project switch and a data-directory
  change; resolving `Project.config_path_for` when it FINISHES answers a
  different question by then. `start_segmentation_job` resolves the config path
  up front and passes it down, and declines to reload if the registry moved.
- **A label tile for a level the mask does not have is a stride, never a
  full-resolution read.** `data_model._label_region` is what `read_tile` calls
  for a mask (a bare `zarr.Array`, or a pyramid shorter than the image):
  it takes every `2**(level - b)`-th pixel of the finest level `b` at or below
  the one asked for, nearest-neighbour, rather than reading level 0 and
  calling it every zoomed-out level — which used to draw cells from one corner
  of the slide, stretched, over the whole zoomed-out tile. This is also what
  lets `seg_tile` (above) serve a mask still `preparing` or `error`: the raw
  file has no pyramid at all, so every level goes through this stride.
- **A derived label pyramid is located from the mask's path, never the
  project's.** `segmentation_pyramid.resolve_derived_mask` is the single answer
  to "where is it, and where would a new one go" — beside the source by
  default, the project's `derived_root` as fallback, BOTH always searched.
  Beside-the-source is what lets a second project and a data node (which has no
  project at all, so nothing to look under) reuse one conversion; the fallback
  is what keeps a read-only source directory working and what finds every mask
  built before this convention. `paths.mask_output_preference()`
  (`plexora config set mask-output beside|project`) swaps the order for both
  halves together — a preference that moved writes but not lookups would
  disagree with itself the moment a pyramid existed in both places. A data
  node calls it with `use_preference=False` and its own per-resource-id folder
  under the node's data root (`node/app._node_mask_dir`,
  `<paths.data_root()>/node-masks/<resource_id>/` — one folder per id because
  every mcmicro mask is `cell.ome.tif`, so a shared stem would collide two
  samples' pyramids into one file) as `data_directory`, since a node's own
  preference setting is not the question and beside-the-mask must win when
  it's writable. `_is_adoptable` also now checks that a candidate's level-0
  plane size matches the source's, not just its mtime — a stale-by-mtime rule
  alone would adopt another sample's pyramid that happened to share a folder
  or stem. Callers with a recorded `segmentationSourceKey` still use it
  (`refresh_segmentation_ mapping`); the ones without — a fresh import, a node
  — fall back to "ours, of this mode, not older than the source".
- **A remote mask is never served directly — always converted into the
  project.** `resolve_derived_mask` for a web address (`is_remote_locator`)
  skips beside-the-source entirely: there is no folder beside a URL to write
  into, and `Path()` of one would create a pyramid under the working
  directory (`https:/host/...` after `Path` folds the double slash). It
  requires `data_directory` and names the target under it
  (`_remote_derived_path`, e.g. `labels/cells` inside `sample.zarr` becomes
  `sample_cells`). Staleness has no mtime to compare, so `_is_adoptable`
  trusts the recorded ETag/Last-Modified instead (`source_fingerprint` via
  `remote_store.fingerprint_key`, checked on every load) and answers "still
  good" outright. `_open_level_zero`'s remote branch reads through
  `ome_zarr._open_group`/the chunk cache, and `_Plane2D` pins any leading
  axes (t=0, c=0, z=middle) to the one (rows, cols) plane the pyramidizer
  wants — an NGFF label image can be anything from (y, x) to (t, c, z, y, x).
- **`segmentationMode` missing is read as "outlines", not as "unknown".** Both
  `viewerControls.canDrawFilled()` and `imageViewer.renderLabelTile()` test it
  against `"filled"`, so an absent key greys Filled out ("stored as outlines,
  nothing to fill") and paints a filled label pyramid as solid blobs with
  Outlines selected — wrong picture, no error anywhere. Every path that records
  a mask must therefore record a mode. Locally `refresh_segmentation_mapping`
  backfills it; for a mask on a NODE that refresh is skipped wholesale (nothing
  here to fingerprint or convert), so the mode comes from the node's
  `/hello` `mask_mode`, which is `Resource.mask_mode` — decided once by
  `app._convert_mask_if_needed`, which returns the mode of whatever is left
  being served in every branch. It cannot be re-derived from the file: the two
  branches that skip conversion because the user's own mask is already fine (a
  servable label pyramid; a mask that already looks like outlines) leave no OME
  marker for `generated_mask_kind` to read. `load_config` backfills node-backed
  entries that predate this, and `nodes.attach_segmentation` falls back to
  `DEFAULT_MODE` when an older node reports nothing.
- **A segmentation mask need not be a raster.** `boundary_mask.is_boundary_source`
  (the parquet-or-geojson check) is asked before `resolve_outline_segmentation`
  in `data_model.convertOmeTiff`'s `isLabelImg` branch and before
  `describe_segmentation_work`'s own raster checks, because a table of
  boundary polygons opened as a TIFF fails inside
  the reader with nothing useful to say. Drawing one needs a frame it does not
  carry — width, height, and a transform or pixel size — so
  `convertOmeTiff(..., label_geometry=)` and `start_segmentation_job`'s
  `_label_geometry_for` resolve it from the project on the request thread and
  pass it down; a boundary table with no way to place it raises the mask's own
  error rather than a 500 on whichever request happened to attach it.
  `import_proposal._detect_parquet` tests `is_boundary_table` **before** the
  cell-table test, because a boundary table's `vertex_x`/`vertex_y` columns are
  exactly what `guess_roles` reads as centroids, and a segmentation left to
  that test alone would register as a cell table with one row per polygon
  vertex.
- **An image on a node has its KIND decided by the node, for the same reason.**
  The primary cannot open a `node://` address, so `brightfield.detect_image_type`
  cannot run there. `Registry.add` runs it once per image resource
  (`providers.local.detect_image_type`, the DICOM-vs-TIFF dispatch lifted out of
  `convertOmeTiff`), stores it as `Resource.image_type`/`image_type_reason`, and
  reports it in `/hello`'s `describe()` **and** in `/image/<id>/geometry`.
  `nodes.attach_image` reads it off the geometry response and records
  `image_kind='brightfield'` with the single `rgb` tile key and the display name
  `Image` — the same shape `_convert_brightfield_image` records locally. Without
  it every image reached through a node registered as a channel stack: an H&E
  slide came out as three markers composited additively on black, with nothing
  in a position to report an error. The detection also decides how the NODE
  reads the file (`_provider_for(..., rgb=)`), which matters only for a
  brightfield file whose planes are stored separately — an interleaved one is
  found by `is_rgb_layout` inside the provider regardless. Both keys are
  additive with no `API_VERSION` bump: an older node omits them and
  `_node_image_kind` leaves the project's own kind alone.
- **The node never stores the OVERRIDE.** `attach_image(image_type=...)` is the
  user's Auto/H&E/Fluorescence choice and it lives on the primary, in
  `ImageSpec.image_type_choice`, where every other project fact lives — the node
  manifest deliberately holds nothing but kind/id/path. It needs no cooperation
  from the node: a node's pyramid presents `(channel, y, x)` whichever way it
  opened the file, and `brightfield.rgb_region` stacks three planes when the
  level has no `.rgb` of its own. `attach_image` re-reads the stored choice on
  every attach, so a repoint or a reconnect keeps it. Guarded on `num_channels
  >= 3`, the same guard `_with_enough_planes` applies locally. The edit page's
  Image type control is `reregister_image` for a local file and
  `project_routes._reread_on_node` — one more `attach_image` — for a node one;
  before that it recorded the choice and rebuilt nothing, which looked exactly
  like a control that did not work.
- **A full origin never passes through `clean_prefix`.** `PLEXORA_BASE_URL` and
  `app.config['PLEXORA_BASE_URL']` hold a MOUNT PATH. Colab's proxy is a whole
  origin (`https://….googleusercontent.com`), and prefixing that with "/" gives
  `/https:/…` — a valid-looking path that fails nowhere near the mistake.
  `clean_prefix` raises ValueError on one; origins belong in the DISPLAY url
  via `join_display`.
- **`--plugins ""` is not `--plugins` unset.** Unset means "activate everything
  installed"; `""` is a deliberate core-only build. Anything forwarding the
  setting to a child must omit the flag entirely when it is unset — passing
  `""` is what silently disabled every plugin behind the jupyter-server-proxy
  launcher tile. And **the value cannot cross a process boundary in an
  environment variable on Windows**: `SetEnvironmentVariable(name, "")` deletes
  the variable, so a child launched with `PLEXORA_PLUGINS=""` reads "unset" and
  activates everything — the exact opposite. Pass it in **argv** and let the
  child write it into its own `os.environ` before `import plexora`
  (`server_cli.main()` does exactly this, which is what makes the notebook
  sidecar and the proxy tile correct).
- **No entry point can set `PLEXORA_PLUGINS` in time by itself.** Blueprints
  are registered during the first `import plexora`, and reaching `cli.main` at
  all requires that import — the console script is generated as
  `from plexora.cli import main`, and `python -m plexora` imports the package
  to find `__main__`. Writing the variable inside `main()` therefore lands
  after the decision it is meant to make, and `--plugins` did nothing from
  either command. `cli.maybe_reexec_for_plugins` re-execs once into
  `cli.bootstrap_program(...)` — a `python -c` program that sets the variable
  before importing anything — and only when `--plugins` was passed and the
  environment disagrees. Do not "simplify" it back into `main()`.
- **Open OnDemand is reached through `/rnode/`, never `/node/`.** The portal
  offers both doors (`node_uri` / `rnode_uri` in `ood_portal.yml`): `/node/`
  forwards the request path UNSTRIPPED, which suits Jupyter because Jupyter is
  started with a matching `base_url`, and guarantees a 404 for Plexora, which
  always serves at root and uses its base URL only to generate links.
  `/rnode/` strips the prefix. jupyter-server-proxy is irrelevant on OOD
  either way — it would have to be in the Jupyter SERVER's environment, which
  on a typical site is an admin-controlled module. `notebook_env.OOD_NODE_RE`
  matches the discovered prefix; do not re-run `discover_jupyter_prefix` to
  test it, because that function prints when several servers are running.
- **The auth guard activates on a token, never on a bind address.** It is
  registered unconditionally in `create_app()` (which runs once per
  interpreter, so a conditional registration could never be corrected later)
  and reads `app.config['PLEXORA_AUTH_TOKEN']` per request. The Docker image
  binds 0.0.0.0 deliberately and shares one server deliberately; it sets no
  token and must stay open. Nothing is exempt from the guard, health probes
  included — which is why `jupyter._wait_until_ready` takes a `token` and
  `cli.main` writes the token onto `app.config` as well as the environment
  (create_app already ran by then, exactly as for `PLEXORA_BASE_URL`).
- **`resolve_display` decides the bind host too.** It returns a `Resolved`
  NamedTuple rather than a pair so that the URL and the bind cannot disagree:
  the OOD route is unreachable on loopback, and every other route depends on
  staying there. A separate "what should I bind" helper is the shape to avoid.
- **A Python change needs a full server restart; a client change does not.**
  `server_cli` hands the app to `waitress.serve`, which has no reloader, and
  Flask binds routes at import — while Jinja templates and everything under
  `client/src/` are read from disk per request. So a live process serves the
  NEW frontend against the OLD backend: a newly added route 404s while the
  feature that calls it looks fully deployed. This cost real debugging time on
  the mini-map, whose lens, circle, viewport indicator and drag all worked
  against a server that had never heard of `/generated/overview`. Before
  suspecting the code, compare `ps -eo pid,lstart | grep plexora` against the
  source mtime.
- **Tile size comes from the zarr chunk shape**, so HTTP tiles map 1:1 to TIFF
  tiles. `data_model.convertOmeTiff` currently hardcodes `chunks = (1, 1024, 1024)`
  for multiscale files instead of reading the real shape — fine for 1024-tiled
  sources, silently wrong (4× or 16× read amplification) for others.
- **There is no tile level that reliably holds the whole image.** `convertOmeTiff`
  does not build a pyramid — it reads `maxLevel = len(channels)` from whatever
  wrote the OME-TIFF — while `tileWidth` is hardcoded 1024, so the coarsest
  level is a 1x1 tile grid for some files and 4x4 for others. This is why the
  mini-map (`generate_channel_overview`, `GET /generated/overview/<datasource>/<channel>`)
  has its own route instead of reusing the tile route.
- **The pyramid is real and used.** `_zarr_level(channels, level)` indexes the
  level group. When the source is a bare `zarr.Array` (non-pyramidal), `level` is
  ignored and every tile reads full resolution.
- **`qmin`/`qmax` must come from full-resolution data.** The downsampled `zarray`
  overview is mean-pooled, which dilutes single-pixel peaks and causes whole
  channels to saturate. `get_channel_quantization_window()` is deliberately split
  out of `get_channel_gmm()` so the tile path does not pay for the ~1 s
  GaussianMixture fit it does not need. The full-resolution read itself
  (`quantization_window_of`) is slabbed (`_WINDOW_SCAN_SLAB_BYTES`) and
  bounded to two concurrent scans process-wide (`_WINDOW_SCAN_GATE`): as one
  whole-plane `np.asarray(...).max()` it was gigabytes in a single numpy
  call, and a node whose startup warm-up walks every channel
  (`node/app.warm_resources`) spent minutes answering nothing at all -- not
  even `/node/v1/health` -- right after registering. Observed live against
  two clusters; the globe said "Not answering" over a machine that was
  merely busy. The mini-map honours this by quantizing
  pooled `zarray` pixels against the full-res window rather than deriving the
  ceiling from `zarray` itself, which is the mistake this invariant warns
  against.
- **The black `fillRect` before the GL blit is load-bearing.** The shader emits
  alpha 0.9, so the output composites over whatever is already in the reused tile
  canvas.
- **Polars, not pandas**, in the data layer.
- Tile responses carry `ETag` + `Cache-Control`; the ETag embeds
  `load_generation` so a reload invalidates without rewriting tile URLs.
- **`config` and the database description are each one shared object.** Both are
  fetched once at boot and handed out by reference — `config` to ImageViewer,
  ChannelList and ViewerControls, and the description (`dd`) to
  `channelList.init(dd)`, `viewerSidebar.init(dd)` and every plugin's
  `init(dd)`. Anything that refreshes them mid-session (`__plexora.refreshDataset`,
  after the requirements modal changes which matrix is read) must **mutate them in
  place**; assigning a new object updates only its own reference and leaves every
  holder on the old one. This is not theoretical: rebinding
  `__plexora.databaseDescription` shipped a Thresholding panel whose slider
  readout was in log units while its histogram axis and slider domain were still
  in raw counts, because the gating panel reads the *sidebar's* reference.
  Merge per column rather than replacing entries — `image_min`/`image_max`/
  `image_histogram` and the quantization window are fetched lazily per channel
  (`ChannelList.ensureChannelStats`) and live in those same entries.
- **Loaded tools are cards, and cards are layers.** `toolLoader.js` gives each
  tool its own mount (`[data-tool-panel="<name>"]`) inside a card
  (`[data-tool-card="<name>"]`) in `#tool_panel_slot`, rather than writing a
  whole slot's `innerHTML` — the earlier version destroyed a second tool's DOM
  and left its controller wired to nodes no longer on the page. **The card
  header is the only header a tool gets.** Every sidebar plugin's panel used to
  open with a `.section-heading` of its own — an icon, the tool's name and an X
  — directly under a card header carrying the same name and an X of its own,
  and the two X's did different things (the panel's folded through
  `hideToolPanel`, the card's unloads through `removeTool`). The headings are
  gone from `cell_explorer`, `roi` and `gating`, and with them
  `#cell_explorer_close`, `#roi_panel_close` and `#gate_marker_close`; folding
  is now the chevron, the Tools row and the chord, which all still reach
  `hideToolPanel`. Header actions that were NOT duplicates come up into the
  card instead: a panel stages them in a `[data-tool-extras]` div and
  `liftExtras(toolName, mount)` MOVES that node into `.tool-card-extras` in the
  header, before the eye — the same bargain `data-layer-extras` strikes for a
  layer card, and moved rather than rebuilt for the same reason (a controller's
  handles survive a change of parent, not a re-render). It runs after the
  fragment lands, on both open paths (`openTool`'s `innerHTML` write and
  `adopt`), because the card exists before its contents do. Gating's CSV pair
  is the only user left — ROI's Import/Export/Save/Map to cells/? row moved
  into the panel body as `#roi_actions`, a pill row of ROI-owned
  `.roi-action` classes copying `.layer-card-action`'s values, because a
  hierarchy of categories and regions needed its own header space more than
  its four actions needed the card's. `toolLabel()` reads the
  menu row's `.nav-item-label`, not the row, and `toolShortcut()` reads the
  `.nav-item-key` that `PlexoraShortcuts.register(link)` prints into it —
  registering first because the scan is deferred and a boot-path card can be
  built before it runs.
  Three states,
  kept apart: **loaded** (record, panel and cached data exist, nothing drawn),
  **visible** (contributes a layer; several at once, stacked in card order, top
  card on top), **active** (the shared Cells control, opacity slider, picking
  and gate flows act on it, and its panel is expanded — exactly one, or none,
  with one sanctioned exception below). Opening a tool makes it all three and
  stands the previous one down to loaded; its card's eye turns it back on and
  PINS it, and a pinned layer is exempt from the stand-down (the default is
  for the first switch, not a rule that keeps dismantling a stack). The other
  exception is `openToolAlongside(toolName, anchorToolName)`, which opens a
  tool WITHOUT standing the anchor down, forming a COEXISTING PAIR: both cards
  stay expanded and both layers stay drawn while the selection moves freely
  between them, and `isCoexisting(name)`/`coexistPartner(name)` let a
  controller ask whether it is one half of one. Cell Explorer's
  `#cell_explorer_open_roi` button is the only caller, because its ROI
  composition card only means anything while the ROI overlay it summarises is
  still drawn underneath — opening a tool from the Tools menu is unaffected.
  Opening a third tool folds both halves and clears the pair; closing or
  removing either half promotes the survivor to sole active tool;
  `tests/js/tool_coexist_probe.mjs` + `tests/test_tool_coexistence.py` pin all
  of that. **One card open at a time, across both of the sidebar's lists.**
  Opening a tool card (`setToolCollapsed`, `activateTool`) calls
  `collapseOthersFor(toolName)`, which folds every other tool card (coexisting
  pairs excepted) and then `window.PlexoraLayerManager.collapseAll()`; opening
  a layer card does the mirror through `layerManager.openOnly`, which folds
  every other layer card and then `window.PlexoraToolLoader.collapseAllCards()`
  (both exported on their module's api). Neither module ever calls back into
  the far list from inside the call it just received — `collapseAll`/
  `collapseAllCards` only ever touch their own cards — or the two would fold
  each other back and forth forever. Cards drag to restack (`window.Sortable`,
  same vendored library as `columnClassifier.js`); the DOM order is reversed on
  the way to `setCellLayerOrder`, which stacks bottom-first.
  Switching tools calls the outgoing controller's `onHide()` before painting,
  and the incoming one's `onShow()` after. A controller that only touches
  widgets inside its own panel can ignore both — collapsing is a class, the DOM
  survives, and it comes back instantly. One that reaches outside its panel
  (viewer-canvas pointer handlers, document keyboard shortcuts) must stand those
  down in `onHide()` and re-arm in `onShow()`, or a hidden panel keeps eating
  input meant for the visible one. `onVisibilityChange(on)` is the separate hook
  for the eye: core switches a *cell* layer off by itself, but a plugin drawing
  its own overlay (ROI) has to be told.
- **`loadTool()` is `openTool()` minus the one `show()` at the end.** Extracted
  so a walk to the next dataset sample (`services/carryOver.js`,
  `views/datasetNav.js`) can restore several tools and arrange them once —
  going through `openTool` per tool would mean a `show()` each, and `show()`
  stands the previous tool down, folds every other card and folds the Layers
  list, N times over, ending in a state the public setters cannot even
  express (`setToolVisible` also pins; `setToolCollapsed(name, false)`
  re-folds the rest). `toolLoader.snapshot()` reads card order (not
  registration order -- the cards ARE the layer order) and
  `{name, visible, collapsed, pinned}` per tool; `restore(state, {started})`
  loads whatever is missing with `{quiet: true}` (a missing requirement is
  reported rather than opening the modal that asks for it -- the user asked
  to change sample, not fill in a column) and `started: true`, the load-
  bearing flag: `loadTool`'s lazy path normally awaits `window.__plexoraReady`
  before activating a plugin, and `restoreCarriedState()` in `main.js` runs
  FROM that promise's own `.then()` continuation, never inside `init()`, so
  awaiting it there would be a promise waiting on itself and the boot would
  never finish. Every entry is written onto the loaded-tools map directly and
  one `show()` runs last, for the tool that should end up active.
- **One decoded label tile, one canvas per drawn layer.** `handleTileLoaded`
  fills `tile._layerContexts` (name → 2D context) and `tileDrawingCustom` blits
  them in `maskDrawList()` order with each layer's opacity — so restacking and
  opacity are redraws, and only a colour/gate/mode change re-renders, for one
  layer at a time (`rerenderSegmentationTiles(name)`). Hiding a layer drops its
  canvases and keeps its lookup table, which is why loaded-but-off is cheap
  enough to need no cache limit. `tile-unloaded` frees both — it used to free
  only `_array`, leaking a canvas per evicted tile
  (`tests/test_label_tile_lifecycle.py` pins it).
- **Two stacks, not one.** Card order restacks the mask layers among themselves.
  Centroid-mode layers draw on core's `CanvasOverlayHd`, which is above every
  mask tile whatever the cards say. ROI no longer builds its own
  `CanvasOverlayHd` — it draws through core's shared overlay
  (`ctx.layers.addOverlay`, see `views/layerStack.js`'s `OverlayHost`) instead,
  which is what fixed a latent bug: `CanvasOverlayHd` calls `onRedraw` once per
  world item (i.e. once per active channel), and nothing used to guard against
  that, so centroids were filled once per channel. The guard is
  `stack.anchorIndex()` — explicitly NOT `opts.index !== 0`, because the first
  world item is not reliably the anchor once a scene can hold more than one
  image layer. Which centroid POINTS exist is also not per layer: the gate is applied server-side
  when the tiles are fetched, so a visible-but-inactive gating layer colours the
  active layer's point set.
  `PlexoraToolLoader.activeTool()` is how a controller checks this for itself.
- **A plugin can announce a hover without knowing who is listening.** ROI
  (`plugins/roi/static/roiTools.js`) tracks pointer hover on the ROI overlay
  with an `OpenSeadragon.MouseTracker` on `viewer.canvas` (rAF-throttled,
  suppressed mid-gesture) and dispatches `plexora:roi-hover` /
  `plexora:roi-unhover` on `window` — a plain DOM CustomEvent, not a plugin API
  call, because ROI has no reason to know Cell Explorer exists. The hover
  detail's `anchorRect`/`viewportRect` are computed once on hover-enter, not
  per pointer move, and are in CLIENT pixels (the canvas bounding rect already
  folded in) so a listener never has to know about OpenSeadragon coordinate
  spaces. A client-pixel anchor is stale the moment the picture moves, so ROI
  re-announces the standing hover on `viewport-change` (`viewportMoved` /
  `reanchorHover`, one dispatch per frame, re-testing what is under the pointer
  because a zoom can carry a shape out from under it). Do NOT make listeners
  close on `viewport-change` instead: no pointer event follows one, so the
  region has to be left and re-entered before anything can be seen again, which
  reads as a hover the tool missed. Cell Explorer's `cellExplorerRoiBridge.js`
  (`plugins/cell_explorer/static/`, listed in `__init__.py`'s `scripts` before
  `cellExplorerSidebarController.js`) is the one listener today: it renders a
  floating composition card, fetching every cell centre once per session via
  core's `viewer.numericData.loadCells()` and using them UNSCALED — raw
  full-resolution image pixels — because that is the space ROI geometry is
  stored in. That fetch is warmed when the user asks for the ROI tool and the
  card shows a pending state if a hover beats it; every later hover is
  synchronous. It tallies membership by the active categorical variable only; a
  continuous column is gated out, since a composition card has nothing to
  count. **Hidden categories are excluded, counts and all** — the legend is how
  somebody narrows the question being asked of the slide, so the card answers
  the narrowed one, and the total is summed from the shown rows so the fixed
  0–100% bars stay comparable between regions. `recolor()` is the single funnel
  that keeps an open card truthful (hide, show, All/None, colour change).
  Because ROI geometry objects are replaced rather than mutated on edit, the
  bridge revalidates on every `store.onChange` by identity rather than a deep
  compare. `tests/js/roi_hover_probe.mjs` covers the announcing half (what is
  dispatched, when, in which coordinate space, and the pan re-anchor) and
  `tests/js/cell_explorer_roi_bridge_probe.mjs` the answering half (membership
  checked against a brute-force count, the tally, hidden categories, the
  ranking and `Other`), wired up by `test_roi_client_js.py` and
  `test_cell_explorer_roi_bridge.py`.
- **Changing a client file means bumping its `?v=` tag** in the template that
  loads it (and `plugins/<name>/__init__.py`'s `VERSION` for plugin assets, which
  stamps every URL `asset_urls` builds). Sources are served straight from
  `client/src/`, so a stale tag means the browser keeps running the old file and
  the fix looks like it did nothing. `viewerManager.js` and `glRenderer.js` are
  the exceptions: they are webpacked into `client/dist/vendor_bundle.js`, which
  has to be rebuilt *and* re-tagged.
- **A page template extends `layout`, not `'base.html'`.** Hardcoding the base
  back in makes that page unroutable: it would come back from a fragment fetch
  as a whole second document — navbar, `<head>`, another `<body>` — to be
  inserted next to the live viewer. `tests/test_app_shell.py` walks every page
  in both shapes.
- **A new page controller mounts through `PlexoraPage.register`.** A
  `DOMContentLoaded` listener works exactly once, so the page would be correct
  the first time it is opened and inert on every visit after that — with nothing
  in the console to say why. See "Navigation and the App Shell".
- **Asset URLs in templates start with `{{ data.base_url }}/client/...`**, never
  `../client/...`. A relative URL resolves against the page's own path, so it
  works only for a page exactly one segment deep at the site root and silently
  404s everywhere else — no server-side error, just a page with no CSS and no
  JS. It broke `/project/<name>/columns` (three segments) outright, and every
  page under the Jupyter proxy, which adds a prefix. `tests/test_page_assets.py`
  fetches each page's assets to keep it fixed.
- **The marker/metadata split is the project's answer, never re-derived.** A
  plugin asks `ctx.dataset.table.markers` (core's
  `datasetContext.js`); the server side is `spec.columns.markers`, which
  `CsvAdapter` reads for `feature_columns`. Deriving it from the column
  statistics — "everything numeric with a histogram that is not id/x/y" — cannot
  tell a stain from a measurement, and a CSV puts both in one header, which is
  the entire reason the import screen asks. Gating did derive its own, so every
  CSV project got a threshold slider for `Area` and `Eccentricity`.
  `tests/test_marker_split.py` pins the rule and drives the real getter.
- **A screen asks only for what it is a checkpoint for.** The CSV import screen
  confirms `IMPORT_ROLES` (`cell_id`, `x`, `y`, `image_id`) — the roles that
  decide how the table is *read*. `celltype` is not among them: nothing in core
  reads it, and a plugin that wants an annotation column declares it
  (`Requires(roles=("celltype",))`) and is asked through the requirements modal
  at the moment it matters. Both halves matter — a role echoed back unasked is
  stored *and* marked confirmed, which retires a question nobody saw.
- **`is_transformed` is honoured by every adapter, and asked for on every
  format.** The log1p switch is a separate question from which matrix to read:
  a CSV has nothing to pick between and is still the format most likely to
  arrive as raw counts. It was skipped for CSV in `plugin.py`'s
  `_never_confirmed` and in the edit page's `has.features`, and `CsvAdapter`
  ignored the flag anyway — so the transform was unreachable, and would have
  been a lie if reached.
- **Anything that fits a distribution to marker values fits it on a log
  scale**, and reads `dataset.table.log_transformed` to know whether it has to
  apply the log itself. Marker intensities are log-normal; a mixture of
  *normals* fitted to raw counts chases the skew instead of the populations,
  and a mixture fitted to values that were logged twice sees a separation that
  has been compressed away. Both `get_channel_gmm` (image) and gating's
  `auto_gate` (feature table) do this, and the result is that the same data
  gates identically whether or not the user ticked log1p.
- **A GMM threshold is a density crossover, not the midpoint of two means**,
  and the fit needs three components rather than two. A marker's background is
  a broad distribution, near-symmetric once logged, so a two-component fit
  splits *it* instead of separating it from the positives — and the midpoint of
  the resulting centres sits inside the negative population. That shipped:
  gating called 27-46% of cells positive on markers whose real fraction was
  3-12%. `plexora/plugins/gating/tests/test_auto_gate.py` measures against
  populations whose true membership is known, and keeps the old estimator
  alongside as the baseline.
- **Every slider in Plexora is a `PlexoraSlider`.** No stylesheet outside
  main.css's slider and gradient blocks may style an `input[type="range"]`,
  and `accent-color` survives only on checkboxes and radios — pinned by
  `tests/test_slider_css.py`, because the way this drifts back is not
  somebody rewriting `.plx-slider`, it is somebody adding a one-off rule
  beside a new control and never reaching for the one it already had.
- **`views/slider.js` loads before `gradientRange.js` and before
  `toolLoader.js` in `base.html`**, because both build with it.
- **`onInput` fires per tick of a drag, `onChange` fires once on release, and
  the primitive never coalesces them.** Every consumer's own throttling — a
  rAF-coalesced repaint, a 500ms debounced PATCH, a 400ms save, an 800ms
  save, one undo entry per drag — hangs off that split, which is what let
  each of them keep its throttling exactly as it was when it moved onto the
  shared control.
- **A log slider stores the exact value and derives only the thumb
  position, never the reverse.** A channel window runs 1..65535 over a
  1000-step grid; reading the value back off that grid instead of keeping
  it would turn a window displayed as 1234 into one saved as 1231.7.
- **WebKit and Gecko thumb pseudo-element rules are never comma-joined.** A
  selector list containing a pseudo-element an engine does not recognise is
  invalid and the whole rule is dropped — one comma between
  `::-webkit-slider-thumb` and `::-moz-range-thumb` is a slider with no
  thumb in Firefox, on every page, with nothing in the console to say so.
- **The WebKit thumb twin carries `margin-top: calc(var(--plx-thumb) / -2)`
  and the Gecko twin carries none.** With the input and the runnable track
  both at `--plx-hit`, Blink and WebKit leave the thumb's centre exactly half
  a thumb BELOW the rail -- independent of `--plx-hit`, which is the part no
  reading of the spec suggests and which is why this has to be measured
  rather than derived. Two plausible expressions shipped here before the
  right one: `(hit - thumb) / 2` left every thumb 12px under its line, and
  removing the margin altogether still left 7px. Verified in headless Chrome
  at thumb/hit of 14/24, 18/18, 10/24 and 20/32 -- centred to 0.01px in all
  four. Gecko centres the thumb itself, so a margin on that twin breaks
  Firefox alone, where nobody is looking. Pinned by
  `test_the_thumb_is_pulled_up_onto_the_line`.
- **Adoption is a loan, and `destroy()` repays it.** Eleven sliders adopt an
  `<input type="range">` a template staged; adoption MOVES that element into
  the slider's root, so a destroy that only drops the root deletes markup the
  page owns — the id a `<label for>` points at, that a golden records, and
  that the panel looks up on its next bind. `destroy()` therefore puts the
  adopted element back where the root stood, first. A consumer must also not
  destroy a slider it did not build: `paintTree()` in the transcripts
  controller destroyed all four on every gene-list rebuild, which runs on
  load, and three rows of that panel lost their controls within a moment of
  opening — permanently, because `bindSlider` returns early once the element
  is gone, and silently, because nothing throws. Pinned by
  `test_destroy_gives_back_what_adoption_borrowed`.
- **Number spinner arrows are suppressed globally in main.css.** A
  component must not add its own copy of that reset.
- **A points layer with `render.pointKind == "bin"` is served by
  `bin_tiles` in GRID UNITS; its registration is the `LayerSpec.transform`
  the viewer draws, never baked into the store.** A tile's pixel coordinates
  are the bin grid times `supersample`; how that grid sits on the reference
  (for Visium HD, a 180-degree turn and a mirror) is drawn by OSD off the
  layer's affine, exactly like every other registered layer. Re-registering a
  layer therefore never rebuilds the store, and the store never has to know
  what it is registered against.
- **A Visium HD sample's table is an ordinary AnnData — the converted
  `.h5ad` — and nothing downstream may special-case a 10x matrix.** By the
  time gating, the cell explorer, ROI write-back, the notebook API or SCIMAP
  looks at it, the file already is one; the conversion
  (`server/utils/tenx_matrix.py`) is where "this came from Space Ranger"
  stops mattering. **Wide tables never hold their features in the frame:**
  read a gene through the provider (`read_feature_column`/
  `get_filter_columns`/`TableHandle.columns`), never `frame()[gene]` — the
  frame does not have the column, and reaching for it directly is a `KeyError`
  a narrow table would never raise, which is what makes the mistake easy to
  miss until someone opens a whole-transcriptome run.
- **Space Ranger states every position and every polygon in the run's
  FULL-RESOLUTION microscope pixels; the hires PNG reference is
  `tissue_hires_scalef` of that frame, never the frame itself.** A bin's
  centre, a cell polygon's vertices and the bin-grid registration are all
  written in that one frame, and every one of them is scaled by the
  reference's own `render.frameScale` exactly once (`LayerProposal.frame ==
  "fullres"`, composed in `_align`) — composing it twice, or on the wrong
  layer, draws bins or cells at the wrong size on an otherwise correct-looking
  slide.
- **Anything that draws or picks in screen space goes through
  `PlexoraViewTransform`, never OSD's viewport directly.** OSD's own
  `pointFromPixel`/`pixelFromPoint` ignore the rotate/flip core's Rotate &
  Flip tool applies (`services/viewTransform.js`; state `{degrees, flipH,
  flipV}`, composed as `Fh^h·Fv^v·R(d)` and mapped onto OSD's single
  built-in horizontal flip as `V = flip + 180°`), so a caller that reaches
  OSD's own methods draws or hit-tests as if the image were upright even when
  it is not. `pointFromPixel`/`pixelFromPoint`/`orientContext`/
  `screenBoxOfImageRect`/`imageToScreen`/`screenToImage` are the helpers;
  `canvas-overlay-hd.js`'s `_updateCanvas` orients the 2D context before
  anything draws into it, and `viewerManager`'s `getImagePixel`, the
  right-click picker in `imageViewer.js`, and the ROI/Figure Builder/Visium
  HD pickers all go through them rather than through OSD.
  `viewportImageBounds` and centroid culling call
  `getBounds(true).getBoundingBox()` for the same reason: OSD's own bounds
  are for an upright image.
- **A Figure Builder panel's `viewport.x/y/w/h` is always the axis-aligned
  image box, never the turned frame.** Captured on a view turned or mirrored
  by core's Rotate & Flip, the panel also carries `viewport.orientation =
  {degrees, flip_h, flip_v, frame_w, frame_h}` (`schema.normalize_orientation`
  in `plexora/plugins/figure_builder/server/schema.py`, mirrored by
  `figureSchema.js`): turn the image clockwise by `degrees`, then mirror on
  the screen's own axes, same convention as `PlexoraViewTransform` above but
  named `flip_h`/`flip_v` on the wire instead of `flipH`/`flipV`. `frame_w`/
  `frame_h` are the panel's size in image pixels along the screen axes, not
  the box's -- the box is only ever equal to the frame at a right angle.
  `orientation` is absent for an upright panel, so every figure saved before
  this existed reads back byte-identical. Anything that means "the panel's
  width/aspect" -- the scale bar span, tray sizing, Quick Edit's aspect,
  provenance's field size, effective DPI -- reads `frameSize`/`frame_size`,
  never `viewport.w`/`viewport.h` directly, or it sizes a turned panel as if
  it still had the box's proportions. All three renderers (server
  `render._render_oriented`, `FigurePanelCompositor.renderPreview`, Quick
  Edit's mini view/commit) composite the box padded to size, then
  `orient_raster` and crop the frame from its own true centre, so a panel
  looks the same whichever one drew it.

## Validation

Python environment is the conda env `plexora`. The path differs per machine --
`C:/Users/aj/.conda/envs/plexora/python.exe` on Windows,
`/Users/aj/miniconda3/envs/plexora/bin/python` on macOS. Plain `python` is the
miniforge base env and has no Flask, so it is not a fallback.

```bash
# Test suite -- from the repo root, with NO path argument
python -m pytest -q -p no:randomly
```

**The desktop app has its own validation, outside pytest:**

```bash
# The release script's own checks -- toolchain present, versions in sync
python scripts/release.py doctor
python scripts/release.py propagate --check   # tauri.conf.json/Cargo.toml/both package.json vs pyproject.toml
# desktopBridge.js against a probe, not a real Tauri window
node tests/js/desktop_bridge_probe.mjs
# The Rust side, from desktop/src-tauri -- on this Windows machine, with no VS
# C++ workload installed, this means the llvm-mingw toolchain (host
# stable-x86_64-pc-windows-gnullvm); CI uses MSVC instead
cargo test
# An end-to-end installer build, signed if configured, then validated
python scripts/release.py --target x86_64-pc-windows-gnullvm --yes all
```

**`pytest tests/` is not the suite.** Every plugin carries its own tests under
`plexora/plugins/<name>/tests/`, and they are about a quarter of the total:
3079 collected under `tests/` against 4036 repo-wide. Narrowing to `tests/`
runs none of the gating, roi, transcripts, cell_explorer or figure_builder
server tests, which is exactly the blind spot when the change being validated
is a plugin's. Do not run two pytest processes in this tree at once either --
they race on the golden files (see Sharp Edges).

The bounded-memory table read (the `plan()`/`stream()` split, `_LazyObs`,
`_node_take`, blocked matrix streaming) and the staged progress for both long
jobs added `tests/test_staged_progress.py` (7) and
`tests/test_table_progress.py` (8), and six tests to
`tests/test_anndata_adapter.py` -- of which
`test_plan_never_opens_the_matrix` is the one that matters: it sabotages
`_read_adata` and asserts `plan()` is undisturbed, so a full read put back on
the loading path fails here instead of on a user's machine. It also removed
`NormalizedDatasource.source_obs_ids` and `obs_metadata`,
`data_model.get_cells_phenotype` and `get_row` (with `GET /get_database_row`
and `dataLayer.getRow`) -- all unreferenced -- so `tests/test_csv_adapter.py`,
`test_spatialdata_adapter.py` and `test_anndata_adapter.py` assert against the
table's own `obs_id` column instead of a field nothing read. On macOS/conda
after it: **3061 passed, 2 failed** (the two baseline failures below).

The per-field Local/Remote data-location work added four test files
(`tests/test_node_dynamic.py`, `tests/test_data_location.py`,
`tests/test_reconnect.py`, `tests/js/data_location_probe.mjs`); the macOS line
below was reverified against a real run after it, and the Windows one was not.
The follow-up that made the switch appear on every launch and made Remote a
saved SSH connection chosen mid-form added no new files -- it extended those,
`tests/test_connect.py` (`NodeSession`, `as_node_kwargs`) and
`tests/test_remote_connect.py` (the two session kinds).

Registering a cohort whose files are on a node from a script or the command
line (`datasets.py`'s `node` key, `--node`) added
`tests/test_datasets_api_on_a_node.py` (14, against a real second process) and
extended `tests/test_cli.py` and `tests/test_datasets_api.py`.

The listing-picker completion (`dir_listing.py`'s rewrite, `/picker_prefs`,
the `pathPicker.js` rebuild) added `tests/js/path_picker_probe.mjs` (73
checks, run with `node tests/js/path_picker_probe.mjs`) and
`tests/test_path_picker.py` (the pytest wrapper plus wiring assertions), and
extended `tests/test_browse_routes.py` and `tests/test_node_dynamic.py`.

Current healthy state on Windows/conda: **1921 passed, 1 failed, 0 skipped**
(2026-08-27, after the multi-source data-node work and the move of derived
segmentation masks to beside their source; not reverified against the
Figure Builder image-toolbar rebuild below). With
`plexora/plugins` on the path -- `testpaths` includes it. The one failure fails
on a clean tree:
`test_quick_view_routes.py::test_quick_view_dedupes_name_on_repeat_registration`.
`test_register_image_datasource.py::test_derive_dataset_name_from_path` is a
Windows path assertion, so it fails on macOS and passes here -- expect **2
failed** on macOS.

The unified connection architecture (`services/remoteState.js`, one owner of
remote-connection polling; `services/connectionModal.js`, the one dialog for
connecting from anywhere; `server/models/recipes.py`, the "Add a server"
preset catalogue; `services/remoteGlobe.js`, the navbar globe) added
`tests/test_remote_state.py`, `tests/test_connection_modal.py`,
`tests/test_recipes.py`, `tests/test_remote_globe.py`, and probes
`tests/js/remote_state_probe.mjs`, `connection_modal_probe.mjs`,
`remote_globe_probe.mjs`; it extended `tests/test_connect.py`,
`tests/test_remote_connect.py` and `tests/js/data_location_probe.mjs`.

The follow-up pass (one connection concept, `services/logTerminal.js`, the
globe's two-line rows, `GET /remote_health`) added
`tests/test_settings_remotes_page.py`, `tests/test_remote_health.py` and
`tests/js/settings_remotes_probe.mjs`; it rewrote `tests/js/remote_globe_probe
.mjs` and extended `connection_modal_probe.mjs` and `test_connection_modal.py`.

Making a disconnect quiet (`nodes._disconnected`, `http.speculative()`, and
the cache warm-up's calm exit) added `tests/test_node_disconnect.py` (9 tests);
it extended `tests/test_remote_health.py` -- whose `json_request` stub has to
carry `allow_disconnected` -- and the root `conftest.py`, which clears the
`_disconnected` set between tests because it is process state and the suite
reuses node names and loopback ports across tmp roots.

Making a RECONNECT visible to an already-loaded project
(`nodes.address_generation`, `_NodeBacked.node`'s re-resolve,
`ProviderSet.held_addresses` → `data_model.held_node_addresses()`, and
`/remote_health`'s `stale`) added `tests/test_node_reconnect.py` (8 tests) and
one check to `tests/js/remote_globe_probe.mjs`; `conftest.py` clears
`nodes._addresses` alongside `_disconnected`. This is the counterpart to the
paragraph above and the two must stay distinct: a node REMOVED from the map is
a disconnect and keeps its own sentence, while a node still on the map at a new
address is a reconnect and is picked up silently. The two meet in
disconnect-then-reconnect, which is a MOVE even though it writes over an
absence -- `test_disconnecting_and_connecting_again_is_a_move` is the one that
catches getting that backwards.

Note that three separate readers resolve a node's address and only one of them
was ever stale, which is what made this so hard to see from the symptoms:
`/resource_routing` (the browser's DIRECT tile path) and `/remote_health` both
`node_registry.find()` freshly per request, so tiles kept arriving and the
globe stayed green while every value the SERVER computed -- GMM, stats, a
proxied tile -- was refused. "Manual contrast works, Auto 503s" is that split.

Two features on top of that pass -- the countdown on a scheduled job
(`recipes.walltime_seconds`, `RemoteSession`'s clock, `nodes.json`'s
`expires_at`, `services/sessionExpiry.js`) and the connect offer for a project
whose node is not up (`node_for`/`NodeImageProvider.open`/
`NodeSegmentationProvider.open` no longer swallowing `ResourceUnavailable`,
`/resource_status`'s `profiles`, `POST /reload_datasource`,
`services/resourceStatus.js`) -- added `tests/test_session_walltime.py` (33),
`tests/test_unreachable_node.py` (20), `tests/test_session_expiry.py` and
`tests/js/session_expiry_probe.mjs`; extended `remote_globe_probe.mjs`,
`remote_state_probe.mjs`, `settings_remotes_probe.mjs` and
`resource_status_probe.mjs` with their wrappers, and
`tests/test_node_table.py`, whose `/resource_status` assertion is
shape-exact. Both probe stubs of `PlexoraRemotes` now have to carry
`remaining`/`duration`/`WARN_SECONDS`, and the resource-status probe's fake
element needed `append`/`showModal`/`close`.

Four follow-up bugs from using the two: a countdown that outlived its own
connection (`RemoteSession.expires_at`'s liveness test, `remaining()`'s, and
the expiry dialog closing when its clock goes), a project whose node had
been disconnected opening silently onto cached tiles (`/resource_status`'s
`ensure_loaded` and `_nodes_that_have_gone`, and `resourceStatus.forget()`),
and every tile that project then asked for arriving as an unhandled 500 with
a full traceback (`create_app`'s `ResourceUnavailable` handler).
Both probe stubs of `remaining()` -- in `session_expiry_probe.mjs` and in
`remote_state_probe.mjs`'s expectations -- mirror the liveness guard, so a
change to it has to be made in both. `resource_status_probe.mjs` no longer
asserts that a dismissed project skips the request: it must ask, because "it
is fine now" is the answer that clears the memory.

Thumbnails for a node-backed image and the globe's viewer-attachment match
(`data_model._node_thumbnail_plane`, `/data_places`'s `registered_node`,
`remoteGlobe.nodeNameOf`) added four tests to `tests/test_node_image.py` and
two to `tests/test_remote_health.py`, extended `tests/js/remote_globe_probe
.mjs` and `remote_state_probe.mjs` (with their wrappers in
`test_remote_globe.py` and `test_remote_state.py`), and regenerated
`tests/golden/boundary_*.json` -- the goldens record every page's `?v=` asset
tags, so bumping one in `base.html` requires
`PLEXORA_UPDATE_GOLDEN=1 pytest tests/test_plugin_boundary.py`.

The shared Local/Remote file-location layer (`utils/file_transfer.py`,
`routes/transfer_routes.py`'s `/fetch_file`/`/put_file`, `node/api.py`'s
`/node/v1/read_file`/`write_file`, `services/fileLocation.js`) added
`tests/test_file_transfer_node.py` (13), `tests/test_transfer_routes.py` (11),
`tests/test_file_location.py` (20) and `tests/js/file_location_probe.mjs` (30
checks, run with `node tests/js/file_location_probe.mjs`); it extended
`providers/http.py`'s `request()` (`raw_body`/`allow_status`) and
`tests/node_harness.py` (`post_bytes`), and touched `gatingApi.js`, `roiApi.js`,
`csvGatingList.js`, `dataLocation.js` and `channelNamesUpload.js` without
adding files for any of them.

Making a data-node death or a reconnect propagate without a reload
(`RemoteSession._tidy_after_end` and its `unregister` callback,
`settings_routes._forget_node_entry`, `services/resourceRouting.js`'s
`held`/`refresh`, `remoteGlobe.staleNodes`, `remoteState.js`'s `nodeChanges`
and the `plexora:remote-nodes-changed` event, `main.js`'s `applyRouting`/
`rebuildTileLayers`/`repairRouting`) added 7 tests to
`tests/test_remote_connect.py` (self-exit unregisters the node, a sibling
tunnel is stopped, a deliberate disconnect still leaves the forgetting to the
route, a failed connection releases its `connect._ACTIVE` entries, starting
over a dead session reaps it first, the connect route wires `unregister`
through, and `_forget_node_entry`'s own `managed_by` guard); it extended
`remote_state_probe.mjs` (a dispatched-events collector, +2 checks),
`remote_globe_probe.mjs` (+3 checks, including an integration section stubbing
`PlexoraRouting.held`) and `resource_routing_probe.mjs` (+2 sections), and
regenerated `tests/golden/boundary_*.json` for the `?v=` bumps on
`resourceRouting.js`, `remoteState.js`, `remoteGlobe.js` and `main.js`. **A
probe context that loads `remoteState.js` or `remoteGlobe.js` now needs a
`window` stub with `dispatchEvent` and `CustomEvent`**, since both files
dispatch `plexora:remote-nodes-changed` directly rather than through a
subscriber callback.

On macOS/conda, after that pass (2026-08-30): **2579 passed, 3 failed, 2
skipped**, with `python -m pytest -q -p no:randomly`. The 3 failures are the
same three as the file-location-layer baseline below — the quick-view dedupe
test, the Windows-path assertion, and
`test_connection_modal.py::test_one_connection_concept_reaches_the_page_that_explains_it`,
none of them caused by this pass. That third one had been read as an artifact
of an in-flight uncommitted `settings.html` rewrite in this tree; it is not —
it still fails on a clean checkout, so treat it as a standing known failure
alongside the other two rather than something a rebase clears. All JS
probes pass.

The Google Cloud (Compute + GCS FUSE) connection preset (`plexora/gcloud.py`;
`server/routes/gcloud_routes.py`; the `preparing_compute`/`mounting_data`
session states; `connect.py`'s `gcloud_ssh_argv`/mount chain;
`recipes.py`'s `gcloud` preset and `_compose_gcloud`) added
`tests/test_gcloud.py` (~40 tests, all driven through the `_RUNNER`
monkeypatch seam plus Flask route tests — no real `gcloud` CLI or Google
account touches the suite) and extended `tests/test_connect.py`,
`tests/test_recipes.py` and `tests/test_remote_connect.py`; goldens were
regenerated (`route_count` 84 → 93 for `boundary_core.json` — the nine new
`/settings/gcloud/*` and `/settings/remotes/<name>/vm*` routes), and
`tests/js/remote_state_probe.mjs`/`connection_modal_probe.mjs` were amended
(recipe cards 3 → 4, badges 1 → 2, new gcloud fetch stubs). No fresh full-suite
count against this addition has been verified here — the two platform
failures and the `test_connection_modal.py` one above are still expected to
be the only three.

The ownership follow-up (`vm_source="plexora"`/`"existing"`, the
`plexora-idle-shutdown.timer` startup script, `made_by_plexora`/
`can_reach_storage`/`instances()`/`valid_instance_name()` on `gcloud.py`,
`RemoteSession._release_compute`/`_other_live_session` covering all five
session-ending paths, and `GET /settings/gcloud/instances` for the
bring-your-own picker) pushed `route_count` on to 94 (93 → 94, the one new
route) and moved `ensure_instance()`'s return value from a bool to
`"created"`/`"started"`/`"reused"`, which every caller and test had to follow.
On macOS/conda, after this pass: **2675 passed, 3 failed**. The 3 failures are
the same standing three named above (the quick-view dedupe test, the
Windows-path assertion, and the `test_connection_modal.py` one), unchanged by
this work.

The VM-buttons follow-up (`POST /settings/remotes/<name>/vm/start`,
`gcloud.start_instance`'s new `block=` matching `stop_instance`'s,
`gcloud.zone_of_instance`, `_compose_gcloud` resolving `vm_source` before
region/zone, and Settings' per-card VM state with Start/Stop/Delete offered
accordingly) pushed `route_count` on to 95 (94 → 95, `vm/start`), and gave
`tests/js/settings_remotes_probe.mjs` — which previously had NO Google Cloud
coverage at all — a `CLOUD` profile fixture, vmStatus/vmStart/vmStop/vmDelete
stubs, an `offered()` helper distinguishing a button being present from being
shown, and 14 checks, mirrored in `tests/test_settings_remotes_page.py`.
`settingsPage.js`, `remoteState.js` and `connectionModal.js` are all on
`?v=20260831_gcloud_vm_buttons`. On macOS: **2692 passed, 3 failed**, the same
standing three named above, unchanged by this work.

The bucket/VM picker follow-up replaced both `<datalist>` fields on the Google
Cloud form with `formFields().pickField` — a `<select>` of what the account has
plus a `PICK_OTHER` option that swaps in a text box. **A `<datalist>` is not a
dropdown**: browsers draw no affordance for it, so a list that had been
fetched, parsed and filled was indistinguishable from an empty box, and the
only way to reach it was to type a name you had opened the field to look up.
`pickField` writes a `{ get value, set disabled }` object into `boxes`, so the
one collection loop and `setControlsEnabled` are both unchanged. Two other
things came out of it: listing the buckets got its own `bucketListToken`
(sharing `bucketToken` with the single-bucket check meant changing project
cancelled the very list it had just requested), and `loadBuckets` now calls
`checkBucket()` *after* the fill rather than `projectSelect.onchange` calling
it before. `main.css` gained `.connect-field[hidden], .connect-field >
[hidden] { display: none !important }` — `.form-control`'s own `display` beats
the browser's `[hidden]`, and this field shows one of two controls.
`connectionModal.js` and `main.css` are on `?v=20260831_gcloud_bucket_dropdown`.
`route_count` is unchanged at 95, but **all five boundary goldens still had to
be regenerated**: `pages` records each script and stylesheet URL with its `?v=`
query, so any asset-tag bump invalidates them even when no route moved. Three
new tests in `tests/test_connection_modal.py`; the modal probe's one datalist
check became four dropdown checks. On macOS: **2695 passed, 3 failed**, the
same standing three.

The drawn-dropdown follow-up replaced **every** native `<select>` on the
Google Cloud form with `menuSelect()` in `connectionModal.js`. A `<select>` can
be styled shut and not open: its menu is drawn by the operating system, so on
this dark dialog it was a white rectangle no stylesheet reached. Four things
about it are load-bearing:

1. **The menu stays inside the `<dialog>`.** The top layer carries the
   dialog's whole subtree; anything portalled to `document.body` lands behind
   it. This is the same constraint that rules out `searchableSelect.js`.
2. **It is `position: fixed`, placed from the trigger's rect** — both
   `.connect-modal` and `.connect-modal-body` clip overflow, so an absolute
   menu is cut off near the bottom of a scrolled form. `place()` also flips
   above the trigger when there is more room there, sets `min-width` from the
   trigger (the menu is `width: max-content`, because a 14rem grid cell would
   ellipsise every machine type), and clamps `left` against the window.
3. **A field holding a dropdown is a `<div>`, not a `<label>`.** A label
   forwards clicks to the first labelable element inside it; the trigger is a
   `<button>` and the menu is in the same field, so a row click would choose,
   close, and then be re-opened by the label. `selectField`/`pickField` use
   `aria-labelledby` instead. `field()` and `switchField()` keep their labels.
4. **Escape is swallowed while the menu is open** (`stopPropagation`), or it
   closes the whole dialog.

Focus never leaves the trigger — the listbox pattern, with
`aria-activedescendant` — which is what makes closing on `blur` safe; the
menu's `mousedown` is prevented so a row click cannot take focus either. The
control exposes `{value, disabled, options, has(), setOptions(), onchange}`
and is hung on its root as `plexoraSelect`, so `boxes[key]` and
`setControlsEnabled` are unchanged, but **every `replaceChildren`/`append` of
`<option>` had to become `setOptions`**. Machine type became a `pickField`,
`MACHINE_TYPES` grew to 16 entries spanning `e2-micro`→`n2-highmem-32` with
`SHARED_CORE` said in the label, and `gcloud.valid_machine_type` checks the
*shape* (so `custom-4-8192` and GPU types pass) rather than membership.
`?v=20260831_drawn_dropdowns`; goldens regenerated for the tag again. On
macOS: **2703 passed, 3 failed, 2 skipped** in ~5:22.

`DEFAULT_BOOT_DISK_GB` then went **200 → 50**. Almost nothing of the user's is
on that disk — the images are in the bucket and a `kind=node` session's
databases are on the user's own machine (`as_node_kwargs` deliberately does not
pass `data_dir`). What is on it is the Debian image (~2 GB), `~/plexora-venv`
(~1.3 GB across ~30k files, measured), pip's cache, and **gcsfuse's write
staging** — which is why `prepare_command_line` now passes an explicit
`--temp-dir "$HOME/.plexora-gcsfuse-tmp"`: left to itself gcsfuse stages into
`/tmp`, and on an image where that is a tmpfs a large write is an OOM rather
than a full disk. The disk is the only thing that keeps billing after the idle
timer stops the VM, which is what made 200 GB (~$20/mo, indefinitely) the wrong
default. `?v=20260831_boot_disk_50gb`; goldens regenerated again. On macOS:
**2705 passed, 3 failed, 2 skipped**.

**`DEFAULT_BOOT_DISK_GB` then went 50 → 20, and `MIN_BOOT_DISK_GB` 30 → 10** --
the IOPS argument above (`pd-balanced` sells 6 IOPS/GB, so 50 GB bought 300
against 180 at the old floor) does not survive being honest about what is
actually on the disk: ~5 GB. 20 GB is the small end of what fits, because the
disk is the one thing that keeps billing after the VM stops. The cost is a
slower FIRST connection to a new VM (120 IOPS rather than 300); every later
connection is unaffected, because the venv is already there and there is
nothing left to unpack. The floor is no longer one Plexora imposed on top of
Google's -- it is Google's own: a boot disk may not be smaller than the image
it is built from, and the Debian cloud image is 10 GB.
`tests/test_gcloud.py::test_the_boot_disk_is_sized_for_what_is_actually_on_it`
and `::test_the_floor_is_low_enough_to_accept_the_default` pin both numbers.

**The Google Cloud form became four pages, and two of its options became real.**
It had grown to nineteen controls on one screen, six of which only meant
anything for one of the two kinds of VM -- and the single most consequential
question on it, what happens to the machine when the session ends, was a switch
inside a collapsed `<details>`. A question nobody scrolls to is a question
answered by its default, and that default was worth money every hour. The pages
are **Google Cloud -> Data -> Compute -> When Plexora exits**, which is the
order the answers depend on each other in; every control on all four is built
once and only ever hidden, so Back loses nothing and no answer has to be
remembered separately from the control holding it. `blocker(index)` returns why
a page cannot be left, and the sentence is rendered beside the disabled button
rather than left to be inferred.

Three things behind it changed with it. **Spot is the default provisioning
model** (`--provisioning-model=SPOT --instance-termination-action=STOP`): the
same hardware at 60-91% off, and defensible as a default only because STOP
keeps the disk -- the data is in the bucket, so preemption costs a reconnect
rather than a rebuild, and the reuse ladder starts the same machine again.
`_create_failure` reads a zone-capacity refusal differently on a spot request,
because "try another zone" is the wrong advice when the fix is Standard.
**`stop_vm_on_disconnect` became `on_exit`** (`leave`/`stop`/`delete`, schema
v3 -> **v4**), read in exactly one place, `gcloud.exit_action(record)`, by all
four teardown paths; the v3 boolean is still understood as the two-valued
version of the question, so nothing saved earlier silently switches to "leave
running". Delete was a UI option nothing acted on and now genuinely deletes --
except after a *failed* connection, which is always stopped, because that disk
holds the two logs the next connection prints to explain the failure. And
**`bucket()` gained a public fallback**: a world-readable bucket grants objects,
not metadata, so `buckets describe` 403s on exactly the published atlas somebody
is here to mount; a `storage objects list --limit=1` that succeeds makes it
usable, marked `public` with no location, which is why the form then says the
region could not be read instead of leaving that field mysteriously blank.
`MACHINE_TYPES` also went 16 -> 8 (`e2-micro`/`e2-small` and four middle rows
gone, `e2-medium` now the smallest and the only `SHARED_CORE` member): a
shortlist that has to be read to the end is not much better than the catalogue
it stands in for, and the Custom box is what makes shortening it safe.

**"Install or update Plexora" is ON for the gcloud preset and OFF for every
other one**, and the asymmetry is deliberate — both halves are pinned by
tests, so do not "fix" either into agreeing with the other. Off elsewhere
because no starting point gets to install software into somebody's account on
a machine Plexora has only read the documentation for; on here because
`prepare_command_line` pip-installs only when `$venv/bin/plexora` is *absent*,
so without the switch every connection after the first would run whatever
version that first boot happened to get, on a machine Plexora created.
`?v=20260831_gcloud_install_on`.

**The gcloud VM gets a public IP address, and that is the security-preserving
choice rather than a relaxation of one.** The preset was written `--no-address`,
IAP-only — which is correct about *ingress* and forgets *egress* entirely. A
Compute Engine VM with no address has no outbound route at all unless the
network gives it one, and a default VPC subnet does not (`privateIpGoogleAccess`
false, no Cloud Router). Observed, not theorised: the serial console of a real
`plexora-gcloud` showed `curl: (28) … packages.cloud.google.com … Timeout was
reached` after 300 s, `Network is unreachable` for every Debian mirror, and
`E: Unable to locate package gcsfuse`. IAP reaches *in* over a separate path,
so the machine booted, answered the tunnel and looked healthy while nothing
could be installed on it. The two real fixes are Cloud NAT (~$32/mo for the
gateway — not a default for a preset whose smallest offer is an `e2-micro`) and
an address on the instance; Private Google Access is **not** a third, because
PyPI is not a Google domain. So `create_instance` drops `--no-address` unless
`external_ip` is off, tags every VM `plexora`, and `ensure_public_deny` writes
`plexora-deny-public-ingress` (deny all ingress, `0.0.0.0/0`, `--target-tags
plexora`, priority **65000** — under the default VPC's own `default-allow-ssh`
at 65534, far above the 1000 a deliberate rule gets). Four orderings are
load-bearing and pinned: rules before any instance; tag before address on a
reused VM (`repair_egress`); `wants_external_ip` reads a **missing** key as
True, so every pre-v3 profile repairs itself; and a `vm_source=existing` VM is
never tagged, addressed or denied — its network is the user's.

`ensure_instance` refuses to create a private VM whose subnet has neither NAT
nor PGA (`network_egress` → `_no_egress_error`, which prints the two `routers
… create` commands). Cheapest possible failure point: after the create there is
a VM and a disk being billed for.

**The repository key is used as published — never `gpg --dearmor`ed.** Google
serves `apt-key.gpg` as an ASCII-armoured block, and apt takes an armoured key
in `signed-by` directly, so the dearmor step converted a file that already
worked into a dependency on a program **the Debian 13 image does not ship**.
The whole visible symptom was `E: Unable to locate package gcsfuse`, four
steps downstream of the actual line: `sh: 1: gpg: not found` → no keyring →
`E: The repository … is not signed` → package invisible. Both the startup
script and `_GCSFUSE_INSTALL` now `curl -o /usr/share/keyrings/cloud.google.asc`
and reference that path. This was found only because the failure had started
printing apt's log — see below.

**`ConnectError(..., diagnosed=True)` beats `RemoteSession._diagnose`'s
substring guessing, and the reason is a bug that fix caused.**
`MISSING_COMMAND_MARKERS` contains bare `"not found"` and
`"No such file or directory"`, and `_diagnose` scans *all* watcher output for
them. The moment the mount step began tailing apt's log, that log's
`gpg: not found` matched — so a connection whose VM was missing a package
started telling people their remote PATH was wrong and to edit “Plexora
command or environment”, which was correct as it stood. `_await_mount` now
raises with `diagnosed=True` and `_diagnose` returns such an error's own text
before it reads a single line. The heuristic is unchanged; only its precedence
is. Any future step that works out a cause with the output in front of it
should set the same flag.

**The VM keeps its own logs, because the serial console stops answering.**
`gcloud compute instances get-serial-port-output` works only on a **running**
instance — and a VM whose connection failed is one Plexora has just stopped,
so the evidence is gone by the time anybody looks for it. Twice during this
preset's development a diagnosis was lost exactly that way. So the startup
script `tee`s everything to `STARTUP_LOG` (`/var/log/plexora-startup.log`),
the mount chain's fallback writes to `INSTALL_LOG`
(`/tmp/plexora-gcsfuse-install.log`) instead of `/dev/null`, and the
"could not install gcsfuse" branch tails both to stderr. **The failure text in
the app is now the primary diagnostic**, not gcloud.

Two things that look like nits and are not. The tails are spelled
`tail … >&2 2>/dev/null`, and **the order is load-bearing**: redirections are
applied left to right, so `2>/dev/null >&2` points stdout at whatever fd2
already is — /dev/null — and prints a header, a footer and nothing between
them. And `_NO_GCSFUSE` no longer names a cause: it offers the commonest one
and says to read the log first, because the version that confidently asserted
"the VM cannot reach the internet" was right once and then wrong, and sent
somebody to check a network that was fine.

**`IMAGE_FAMILY` is `debian-13`, and it is about Python.** Plexora's
`requires-python` is `>=3.12,<3.14`; Debian 12 ships 3.11, so a bookworm VM
mounts the bucket, verifies access, builds the venv and *then* fails with
`No matching distribution found for plexora` — which reads as "no such
package". Trixie ships 3.13. `MIN_PYTHON = (3, 12)` is written here because
this module may not import `plexora`, and a test pins it to pyproject.toml's
declared floor. `_python_check()` runs on the install path only (inside the
`[ -x $venv/bin/plexora ]` guard, so a VM that already has Plexora is never
refused by it) and names the fix, which differs by owner: a rented VM gets
deleted and rebuilt, somebody's own machine is theirs to decide about.
**An image family only applies at create**, so an existing VM has to be
deleted to move images.

Two related things stopped being written down and are now read off the
machine. The gcsfuse **apt suite** comes from `VERSION_CODENAME` in
`/etc/os-release` in both the startup script and `_GCSFUSE_INSTALL` (it was a
hardcoded `gcsfuse-bookworm`, which would have silently pointed apt at the
wrong Debian the moment the family changed; `/etc/os-release` rather than
`lsb_release`, which is a package and not a guarantee). And the mount's
**write check is `: >` rather than `touch`** — `touch` creates the object and
then calls `utimensat`, which a bucket mount need not implement, so a `touch`
failure could mean "read-only bucket" or merely "cannot set mtime", and
reporting the second as the first sends somebody to the IAM page to fix a
permission they already have.

**A VM that answers ssh is not a VM that is ready**, and `ensure_instance`
asks both questions now. sshd answers within seconds of boot; the startup
script is still running `apt-get` minutes later, and on an `e2-micro` (1 GB
RAM, a *fraction* of a vCPU) that apt install is most of what the machine has
— so the session's own ssh arriving mid-install came back `Connection closed
by UNKNOWN port 65535`, exit 255, seconds after a probe had succeeded. This
only started happening once the egress fix made the startup script actually do
its job; before that it failed instantly and used nothing. `await_startup()`
runs after the ssh readiness loop and probes `test -f STARTUP_MARK` (`test -f`,
**not** `command -v gcsfuse` — the question is whether the script finished, and
one that finished having installed nothing still needs the mount chain to get a
connection through). Never fatal: it times out at `STARTUP_READY_TIMEOUT` (600)
with a line saying so. Rented VMs only — a BYO machine has no startup script
and waiting ten minutes for its marker would be ten minutes of nothing. The
mount chain still waits for the same marker; the difference is that **this**
wait is on the side of the door that retries.

If a `runner` fixture ever hangs in `test_gcloud.py`, this is why: the startup
probe is also a `compute ssh`, so `_signed_in`'s general ssh answer will
answer it with the wrong marker and the loop spins for the full 600 s of real
time (`_sleep` is patched, `_now` is not). The fixture keys
`"PLEXORA_STARTUP_DONE"` **before** `"compute ssh"` for that reason.

Two more repairs from the same incident. The startup script's tail was
`touch /var/run/plexora-startup-done` — the script has no `set -e`, so it
marked itself done having installed nothing, and the mount chain then waited
its full 5 minutes for a package that was never coming. `STARTUP_DONE` now
writes `ok`/`no-gcsfuse` into `STARTUP_MARK`, and `_gcsfuse_step`'s wait loop
watches for the file rather than only the clock. The rented fallback was a bare
`apt-get install gcsfuse`, which cannot work on the machine that needs it (the
repo is listed with a `signed-by` keyring the failed `curl` never wrote, so
apt's list has never mentioned the package) — both branches now run the whole
`_GCSFUSE_INSTALL` recipe, and the failure sentence names the cause instead of
printing 600 characters of shell. The startup script also makes a 2 GB
swapfile: `e2-micro` is 1 GB of RAM, a Debian cloud image has no swap, and pip
unpacking scipy/scikit-image/pyarrow on that is an OOM kill with an empty log.
Profile schema **v2 → v3** (`external_ip`). `?v=20260831_gcloud_egress`;
goldens regenerated.

The modal probe's fake DOM gained three things worth keeping, all of which
close real coverage gaps: **`<label>` click forwarding** (a browser sends a
label click to the first labelable descendant — that is the only way a switch
on this form is ever operated, and it is also the hazard `selectField` avoids
by not being a label), and **`getBoundingClientRect`/`innerHeight`**, without
which `menuSelect.place()` took its early return and was never executed by any
test at all.

On macOS/conda, at the four-page Google Cloud form: **2757 passed, 3 failed,
2 skipped**, with `python -m pytest -q -p no:randomly`. The third failure is
`test_connection_modal.py::test_one_connection_concept_reaches_the_page_that_explains_it`,
and it **is** a baseline now -- the `settings.html` rewrite that dropped the
string it asserts has been committed, so `git show HEAD:...settings.html` no
longer has it either. Either the sentence goes back into the template or the
test's expectation is retired; until one of those happens it fails on a clean
checkout alongside the two below. Before this pass: 2481 passed at the gcloud
egress work, 2360 at the countdown/thumbnail pass, 2318 before that.

**A zone-capacity refusal on a Spot request now carries a one-press fix.**
`GcloudError` gained a third field, `recovery` -- a short `RECOVERY_*` key
(today only `gcloud.RECOVERY_STANDARD`) rather than free text a surface would
have to parse, attached by the raiser and never inferred downstream from the
sentence: `RemoteSession._fail` reads `getattr(exc, "recovery", "")` and
nothing reads the error text to guess at one, because a recovery offered on a
substring match is a button that changes a saved profile on a hunch.
`_create_failure(cfg, err)` now returns `(sentence, recovery)`, and the only
non-empty recovery it hands out is for "no spare capacity" on a **Spot**
request -- Standard's own capacity refusal gets the same sentence with an
empty recovery, because there is no single-field fix for that one. The key
rides `RemoteSession.status()` as `"recovery"`, `_remote_view` seeds it `""`
for every profile with no session, and `/data_places` forwards it alongside
`error`, never instead of it. `connectionModal.js`'s `paintActions` compares
`shape + ":" + recovery` (not `shape` alone) so a second failed attempt with a
different recovery repaints its buttons, and a failed row with
`recovery === "standard"` gains a **"Reconnect with Standard"** primary
button -- wired to `useStandard()`, which calls the new `POST
/settings/remotes/<name>/vm/standard` then retries `begin()`. That route
(`gcloud_routes.gcloud_vm_standard`, the routes module's fifth VM verb) flips
the saved profile's `provisioning_model` to Standard via `dataclasses.replace`
(`Remote` is frozen) and touches no machine -- the next connect does that --
refused with 400 for `vm_source == "existing"`, where Plexora does not choose
how somebody else's VM is bought. `remoteState.js` carries `recovery` on both
halves of `merge()` and exports the `vmStandard(name)` verb.
`plexora/client/templates/base.html` moved `remoteState.js` and
`connectionModal.js` to `?v=20260831_gcloud_recovery`; goldens regenerated,
`route_count` **95 → 96**. Tests: `tests/test_gcloud.py` (the recovery key
attached only for Spot, withheld for Standard, and three route tests for
`/vm/standard`), `tests/test_remote_connect.py` (`recovery` reaching
`status()`), `tests/test_connection_modal.py` (two mirror tests) and
`tests/js/connection_modal_probe.mjs`.

The gcloud wizard's copy was also cut down: the last page no longer renders
`recipe.notes` at all -- rather than seven paragraphs about billing, IAP roles
and Spot repeated at the end of the form. (At the time this also left the
"untested" badge as what survived of those notes on the preset card; that
badge is gone now -- the preset is `tested=True`.) The mount, region,
external-IP and idle-shutdown hints were shortened, and the machine-type,
boot-disk and install-Plexora hints were removed entirely; the zone hint is
empty in create-a-new-VM mode and appears only in existing-VM mode.

On macOS/conda, after the recovery button and the boot-disk correction:
**2766 passed, 3 failed, 2 skipped**. The 3 failures are the same baseline
named above.

**A scheduler that refuses a job is diagnosed by its own error line, not the
login banner.** `connect.py` gained `SRUN_ERROR_RE` (matches only `srun:
error:` / `srun: fatal:`, so srun narrating a queued job does not match) and
`SCHEDULER_REFUSALS`, an ORDERED table of (marker substring, advice) for seven
Slurm refusals -- missing `-p`, an invalid partition name, node/partition
configuration unavailable, invalid account/partition, a QOS violation, an
invalid time limit -- plus `SCHEDULER_GENERIC` as the fallback when srun
refused for a reason not in the table. Order at the top is load-bearing: a
site with no default partition refuses TWICE (missing `-p`, then invalid empty
partition name) and only the first refusal has the fix. `scheduler_lines(lines)`
is the load-bearing function, not the table: a cluster's login banner is the
LAST thing in the output, so `watched.tail()` shows the banner rather than the
reason -- on HMS O2 the user saw four lines about lower-case usernames and a
password-reset URL wrapped around `srun: error: Job not submitted: please
specify partition with -p`, and `scheduler_lines` filters to srun's own error
lines before anything reads them; `scheduler_refusal(lines)` returns the whole
message, or None when srun said nothing. It is wired in BEFORE
`looks_like_missing_command` at three job-failure sites -- `_wait_for_announce`
(raised instead of `_Retriable`, because a scheduler that refused a job will
refuse the identical job again), the data-node-in-a-job path in `Session`, and
`connect_node` -- because a job that was never allocated never ran `plexora`,
so a PATH is not the problem; all three raise `ConnectError(refusal,
diagnosed=True)`. `RemoteSession._diagnose` gained the same check, before
`looks_like_missing_command`, for any path that reaches it undiagnosed; its
docstring now names four failures rather than three. The generic Slurm recipe
in `server/models/recipes.py` gained a third note about sites with no default
partition. Tests: seven new cases in `tests/test_connect.py`, one in
`tests/test_remote_connect.py`, one in `tests/test_recipes.py`. On macOS/conda:
**2775 passed, 3 failed, 2 skipped** (was 2766/3/2). The 3 failures are the
same baseline named above.

**One `Browse…` button, not a pair.** Every import/picker field used to offer
`File…`/`Store…` (or, on the quick-view landing, `Browse…`/`Folder…`) because
no single native dialog could return either a file or a directory. Mode
`"any"` (see "An image may be a folder" above and `native_dialog.py` in the
Repository Map) replaced the pair with one button on every import/picker field.
(The home page later went the other way on purpose — the pair IS its primary
action there, and it has no Browse button at all. See "The home page is one
vertical run".) `tests/test_browse_routes.py`
grew to 34 tests (hybrid-mode route tests plus native-dialog unit tests,
including `test_the_import_fields_ask_with_one_button_that_takes_either_kind`,
which asserts `upload.html` contains exactly three `data-browse-mode="any"`
and nothing single-kind), and `tests/js/path_picker_probe.mjs` gained a
section 11 for the hybrid listing UX. On macOS/conda: **2853 passed, 3 failed,
2 skipped**. The 3 failures are the same standing baseline named above, plus
one detail worth keeping straight: the third of the three,
`test_connection_modal.py::test_one_connection_concept_reaches_the_page_that_explains_it`,
regressed in commit `f98ab6cb`, before this work — not caused by it.

**H&E and brightfield.** Four new test files —
`tests/test_brightfield_detection.py` (one case per rung of the detection
ladder, plus the two mistakes that would matter most: a three-plex panel called
H&E, and a `photometric=RGB` tag tifffile wrote by default flipping an ordinary
8-bit stack), `tests/test_brightfield_reader.py` (the pyramid contract, the
virtual halving chain, the derived levels a flat slide needs),
`tests/test_brightfield_routes.py` (registration, tiles, the override both
ways, and that a fluorescence tile is byte-for-byte what it always was) and
`tests/test_brightfield_pipeline.py` (a mask, a table and a Figure Builder
panel on a brightfield project) — plus `tests/brightfield_fixtures.py`. Client
assets were re-tagged to `20260831_brightfield` (`viewer.css`, `import.css`,
`main.js`, `vendor_bundle.js`, `channelList.js`, `imageViewer.js`, `miniMap.js`,
`pathPicker.js`, `projectEdit.js`, `importFormValidation.js`) and all five
boundary goldens regenerated with them; `vendor_bundle.js` was rebuilt, since
`viewerManager.js` gained `load_brightfield_base` and `RGB_TILE_FORMAT`
(exported to `window` through `vendor.js`, because `imageViewer.js` is not in
the bundle). Verified against a real 312 MB TCGA `.svs` end to end in a headless
Chrome — tiles, scale bar, mini-map, adjustment sliders, mask outlines and the
edit-page override — as well as by the suite. On macOS/conda: **2927 passed, 3
failed**. The 3 failures are the same standing baseline named above.

**DICOM whole-slide images.** Six new files —
`tests/dicom_fixtures.py` (hand-written `pydicom` instances, deliberately
never `wsidicom`'s own writer, so the fixtures exercise the same header
reading a real export would) plus `tests/test_dicom_detection.py`,
`tests/test_dicom_assembly.py`, `tests/test_dicom_pyramid.py`,
`tests/test_dicom_registration.py` and `tests/test_dicom_routes.py`. 83
tests, all passing, each module guarded by
`pytest.importorskip("wsidicom")` so a checkout without the `[wsi]` extra
still collects the rest of the suite. `wsidicom>=0.22` and `pydicom>=3` were
added to the EXISTING `[wsi]` extra rather than a new one, so `pip install
'plexora[wsi]'` remains the one install line for both OpenSlide formats and
DICOM; installed versions on this checkout are wsidicom 0.35.0, pydicom
3.0.2. Cache tags were bumped to `?v=20260901_dicom` for `pathPicker.js` (in
`base.html`) and `importFormValidation.js` (in `upload.html`). Run with
`C:/Users/aj/.conda/envs/plexora/python.exe -m pytest -q -p no:randomly`.

**The workstation recipe.** No new test files -- it extended six existing
ones: `tests/test_connect.py` (a "Windows machine on the far side" section of
~20 checks, announce-platform parsing, `NodeSession` Windows behaviour and the
stdin-tie tests; both `FakeProcess` fakes gained `stdin = None`, since a real
`Popen` always has one, and the `rig` fixture now records `spawn_stdin`),
`tests/test_recipes.py` ("the workstation preset" section, 8 tests),
`tests/test_remote_connect.py` (an unreachable/wrong-OS diagnosis section
behind a `_diagnosis()` helper, plus three end-to-end `RemoteSession` tests),
`tests/test_settings_remotes_page.py` (4 persistence/view tests),
`tests/test_node_dynamic.py` (`/hello`'s machine facts, that `machine_facts`
never raises, and its caching) and `tests/test_prime_hot_code.py` (the
announce names the platform, parsed with the real `parse_node_announce` so
the two ends cannot drift apart). `tests/js/connection_modal_probe.mjs`
appended a workstation fixture recipe LAST (the gcloud checks reach their
card by index, so adding a row above it would move a card every one of them
names) plus its own section; the two `length === 4` card-count assertions
became 5. Client cache tags were bumped to `?v=20260901_workstation_os`
(`main.css`, `remoteState.js`, `connectionModal.js`, `remoteGlobe.js` in
`base.html`; `settingsPage.js` in `settings.html`); goldens regenerated, but
`route_count` is unchanged (98/103/107/108/122 across the five boundary
goldens) -- that this preset adds no new route is itself the check. Live
on this machine: `machine_facts()` reports 24 cores / 127.7 GB / an RTX 3080
/ its disk free, and `--exit-on-stdin-close` was proven against a real node
subprocess (stays up and answers `/health` while stdin is held; exits 0 the
moment it closes). **Not** verified end to end over a real SSH connection to
a Windows box -- OpenSSH Server is not installed on this machine and
installing it needs elevation this session does not have; the Windows-shell
code paths above are exercised only through the unit tests, not through a
live `cmd.exe`/PowerShell on the far end. The suite's full-count baseline was
not reverified after this work; the last confirmed Windows/conda number
remains **1921 passed, 1 failed, 0 skipped** (2026-08-27, quoted near the top
of this section) -- read as a floor, not as this work's own result.

**Saved connections became a boxed grid of equal cards.** A refactor of the
pass below rather than a polish of it, in the same three files plus the
template. (1) `settings.html` wraps the grid in a `.settings-remotes-box` --
its own surface, border and shadow, titled "Saved connections" with a one-line
helper -- so the saved half and the catalogue are two objects instead of one
run of cards on one background; the panel lead that explains Local vs Remote
came back above it. (2) `.settings-remotes` went `align-items: start` ->
`stretch` and `.settings-remote-card` to `--surface-2`, `.settings-remote-
description` is clamped to two lines with a two-line `min-height`, and
`.settings-icon-button.is-danger` turns the bin red on hover only. (3)
`settingsPage.js` lost the `serving` row, the `vmLine` row, `detailLine()`,
`VM_EXIT_WORDS` and `gcloudExit()`; the description is now `entry.description
|| entry.detail` alone, its `title` repeats it, the clock lost its trailing
clause, and `paintVmState` writes "This machine is running." to the VM
buttons' `title` instead of drawing a row. Rationale in the Settings section
above: a hover that shows configuration is still configuration on the card.
Tests: `test_settings_remotes_page` gained `test_the_saved_servers_sit_in_a_
box_of_their_own` and `test_a_long_description_cannot_make_one_card_taller_
than_its_row`, and four existing card tests moved their subject;
`settings_remotes_probe.mjs` has six rewritten checks (address/tooltip,
settings-off-the-card, the connected card's dot, and three VM-state checks now
reading button `title`s). Cache tags `?v=20260901_saved_connections`; goldens
regenerated.

**The saved servers became cards, and the catalogue split in two.** Follow-on
to the pass below, all of it in the same three files. (1) `.settings-remotes`
is a three-column grid and a card shows the name, a plain-words description,
a status DOT and Connect -- see the Settings section above for what left the
card's face for `detailLine()`'s tooltip and which two facts stayed. Edit and
Forget became `iconButton()` pencil/bin controls in the head, Forget is now
captioned Delete and confirms for EVERY profile rather than only a rented one,
and `settingsPage.js` gained `buildHead()`/`iconButton()`/`detailLine()` while
`paintCard`'s address block went. (2) `Recipe` gained `summary` (the card's
words) and `institution` (hms-o2, mgb-eris), `RECIPES` was reordered to ssh,
slurm, workstation, gcloud, aws, hms-o2, mgb-eris, the gcloud label became
"Google Cloud (GCP)" and mgb-eris's "MGB / BWH ERIS"; `_remote_view` reports
`description`, and `remoteState.js`'s merge carries it per entry.
`recipeGrid()` now returns a `.connect-catalogue` holding two `.connect-
recipes` grids with a `.connect-recipes-more` disclosure between them -- both
surfaces that draw preset cards split them identically because they call the
same function. (3) The catalogue's card TITLES were invisible on the Settings
page and legible in the dialog from the same markup; `.settings-recipes` now
names `color: var(--text-primary)` -- see the Settings section for the
`color: inherit` chain that caused it. New tests:
`test_recipes.test_the_named_institutions_are_the_ones_the_first_screen_holds_
back`, `test_a_named_institution_is_exactly_a_preset_that_fixes_the_address`
(the invariant tying `institution` to a `{host}`-less template),
`test_every_preset_can_name_itself_on_a_saved_card`;
`test_settings_remotes_page`'s four card tests plus
`test_a_saved_card_is_told_what_kind_of_machine_it_is_showing`;
`test_connection_modal.test_the_first_screen_is_the_machines_anyone_could_
have`. Both probes were reworked -- `settings_remotes_probe.mjs` gained an
`iconSaying()` helper and a `window.confirm` stub, and
`connection_modal_probe.mjs` gained `recipeNamed()` so no check reaches a
preset card by index any more. Cache tags `?v=20260901_server_cards`; goldens
regenerated.

**The presets became the way in.** Settings' hand-written "add a server"
form -- `settings.html`'s `.remote-card`, `RemotesSection`'s `REMOTE_FIELDS`/
`JOB_FIELDS`/`fillDefaults`/`setValue`/`addPort`/`renderForwards`/`revealJob`/
`clearForm`/`save`/`addFromPreset`, and the ~460-line `.remote-*` block in
`settings.css` -- is gone; Settings now draws the identical recipe catalogue
`connectionModal.js` uses everywhere else (`drawCatalogue()` +
`recipeGrid()`) and edits through one preset's prefilled form
(`openRecipe(id, remote)`). `services/remoteState.js` lost `save()` with it,
since it was the only browser caller of `POST /settings/remotes`; every
profile write now goes through `POST /settings/recipes/<id>`. Server side,
`recipes.compose()` gained `data_dir`, `forwards` (read off the raw answers,
not the trimmed ones, so a list survives) and `bind_node`, plus
`body["recipe"] = recipe.id`; new `split_target()` and `for_remote()`
(`remotes.py` gained the `Remote.recipe` property they read) answer which
preset's form edits a saved profile. **The bug fix riding along**:
`settings_routes._remote_payload` used to read `data_dir`/`forwards`/
`bind_node` straight off the payload instead of through `kept()`, which
silently erased a saved data directory and port list on the first edit --
invisible while Settings had its own form (which always sent all three) and
live the moment editing went entirely through presets (whose forms only ever
send the fields they ask). No new test files -- `tests/test_recipes.py` and
`tests/test_settings_remotes_page.py` (new
`test_editing_through_a_preset_does_not_drop_what_the_preset_never_asked`,
`test_a_saved_profile_says_which_form_edits_it`, and a module-scoped
`probe_modal` fixture that runs the connection-modal probe) were extended,
and `tests/js/connection_modal_probe.mjs` grew to 187 checks (a ports-control
section and a whole "editing a saved server" one); `tests/js/
settings_remotes_probe.mjs`'s stubs swapped `PlexoraConnectionModal.open` for
`recipeGrid` and dropped `PlexoraRemotes.save`. Client cache tags were bumped
to `?v=20260901_preset_catalogue` (`main.css`, `settings.css`,
`connectionModal.js`, `settingsPage.js`, `remoteState.js`); goldens
regenerated, `route_count` unchanged -- no route was added or removed, only
which UI reaches the existing ones. Not reverified against a full suite run
in this session (one was already in progress in this tree); read the counts
above as the last confirmed baseline, not as covering this pass.

**Ephemeral launch state and in-memory (kernel-as-node) datasources.**
`plexora.view(..., tool=, overlay=, channels=)` carries a one-shot launch
state into the entry URL (`jupyter.py`'s `_launch_state()`/`_entry_query()`,
`page_routes._parse_launch()`, `viewerSidebar.js`'s `applyLaunchChannels()`);
`plexora.view(name, image=..., adata=..., table=..., sdata=...)` serves a
notebook kernel's own objects to the sidecar over the existing node API with
no disk write (new `plexora/memory.py`'s `KernelNode`, new
`server/providers/memory.py`, new `server/models/adapters/memory_adapter.py`,
new `server/utils/memory_pyramid.py`). Added `tests/test_memory_datasource.py`
(30), `tests/test_memory_adapter.py` (14), `tests/test_memory_pyramid.py`
(13), `tests/test_launch_state.py` (7) and `tests/js/launch_state_probe.mjs`;
extended `tests/test_jupyter_viewer.py` and `tests/test_page_routes.py`. All
five boundary goldens regenerated for the new `launch` key in
`page_routes.template_data` and cache tag `?v=20260902_launch_state`. On
macOS/conda, `python -m pytest -q -p no:randomly`: **3145 passed, 4 failed, 5
skipped**. The 4th failure is new alongside the standing three (the
quick-view dedupe test, the Windows-path assertion in
`test_register_image_datasource.py`, and the `test_connection_modal.py` one
above): `test_path_picker.py::test_the_home_panel_is_a_control_rather_than_a_
drop_target`, unrelated to this pass. The standing baseline at the time was
four failures on a clean macOS checkout.

> **Superseded, 2026-09-19.** It is ONE failure now, not four:
> `test_register_image_datasource.py::test_derive_dataset_name_from_path`,
> which asserts a Windows path. `test_quick_view_routes.py` no longer
> exists, the `test_path_picker.py` case named just above was renamed or
> retired, and the `test_connection_modal.py` one now passes. Every count in
> this section is the count on the day it was written; the current one is at
> the bottom of it. Check the failure LIST against the tree before treating
> any of them as exempt.

The viewer's centre spinner moving off a direct `display` write and onto
`PlexoraViewerLoader` (`services/viewerLoader.js`), and the app-shell router's
new server-header hand-off for a project this page has never heard of
(`page_routes.DATASOURCE_HEADER`, `appRouter.js`), added
`tests/js/viewer_loader_probe.mjs` (13 checks) and its wrapper
`tests/test_viewer_loader.py`, and extended `tests/js/app_router_probe.mjs`
(now 18 checks, up from 16), `tests/test_app_router.py` and
`tests/test_app_shell.py` (two new datasource-header tests). All five boundary
goldens were regenerated for the asset-tag bumps this pass carries (no page-id
changes). Verified on Windows/conda, `python -m pytest -q -p no:randomly`:
**3278 passed, 40 failed, 2 errors, 2 skipped**. Read that failure count against
the platform, not the pass: 38 of the failures and both errors are the flaky
Windows zarr `PermissionError: [WinError 5]` on an atomic rename, which come and
go between runs on the same tree. The four that are not zarr are the standing
ones named above (quick-view dedupe, `test_path_picker.py`,
`test_connection_modal.py`) plus `test_browse_routes.py::test_the_listing_puts_
folders_first_and_hands_back_no_bytes`; all four were confirmed to fail
identically with this pass's server change backed out. Diff the failure LIST
against this one, never the counts.

On macOS/conda, at the disconnect pass: **2318 passed, 2 failed, 2 skipped**, with
`python -m pytest -q -p no:randomly`. The 2 failures are the same two named
above (the quick-view dedupe test and the Windows-path assertion in
`test_register_image_datasource.py`); the 2 skips are the same Font Awesome
icon-name pair, which skip when `plexora/client/node_modules` has no
`@fortawesome` package. Before it: 2303 passed at the unified connection
architecture.

Before that, on macOS/conda, after the browser-based file explorer's
completion (2026-08-28, `dir_listing.py`'s rewrite, `/picker_prefs`, and the
`pathPicker.js` rebuild): **2199 passed, 2 failed, 2 skipped**. The 2 failures
are the same two named above. The line before this one read 2156 passed, at the
location-chosen-when-data-is-added work (`placePicker.js`,
`connect.NodeSession`, `/data_places`, node `list_dir`, the Settings cleanup,
the per-field mount fix below, and node-backed quick view), in ~3:23 with
`-p no:randomly`. Before that, 2129 passed, at the per-modality Local/Remote
data-location work (`dataLocation.js`, dynamic node resources, the CSV upload
and the reconnect degradation). The
line here before that read 1927 passed as of the Figure Builder image-toolbar rebuild
(2026-08-27, `figureChoiceField.js`, the panel/legend/scalebar schema and
rendering changes). The 2 skips are `tests/test_icon_names.py`'s pair
(`test_every_icon_name_is_one_font_awesome_ships`,
`test_the_scan_would_notice_a_dead_name`): its module-scoped `shipped` fixture
skips both when `plexora/client/node_modules/@fortawesome` is absent, and on
this checkout `node_modules` is a partially-synced Dropbox copy holding only
`cross-spawn` and `mockttp`. Orthogonal to the reportlab story above -- see
Sharp Edges.

`plexora/plugins/figure_builder/tests/test_figure_builder_routes.py::test_downloading_before_the_job_finishes_is_409`
is order-dependent and flaky under `-p no:randomly` when only the figure_builder
subset is run -- it is not part of the two known failures above and its result
depends on what ran before it.

The capture-bin rework (the persistent server-side bin at
`plexora/plugins/figure_builder/server/captures.py`, the destination picker,
and the bin grid) verified on Windows/conda with `python -m pytest -q
-p no:randomly plexora/plugins/figure_builder tests/test_plugin_css_boundary.py
tests/test_page_assets.py`: **502 passed** (472 before -- the 30 new ones are
the capture-bin repository tests plus the new structural/probe-backed
assertions it added to the existing capture and boot tests).

The follow-up pass -- the destination modal's fixed four-slot layout, the
viewing/export pyramid split, and numbered placeholder titles -- takes the
figure_builder subset plus `tests/test_plugin_css_boundary.py` to **501
passed** with the one flake above.

**`tests/golden/boundary_*.json` records every page's script and stylesheet
list**, so adding a `<script>` to base.html fails five tests until the goldens
are regenerated with `PLEXORA_UPDATE_GOLDEN=1 python -m pytest
tests/test_plugin_boundary.py`. Read the diff before accepting it -- that is the
whole point of the file.

**A test that writes `os.environ` directly must claim the variable with
`monkeypatch.setenv` first.** `cli.main()` writes `PLEXORA_BASE_URL` and
`PLEXORA_AUTH_TOKEN` into the real environment, and monkeypatch cannot undo a
write it did not make -- a leaked `PLEXORA_BASE_URL` then prefixes every route
in `tests/test_plugin_boundary.py`'s golden inventory, which runs in a
subprocess and inherits it. `tests/test_cli.py::_inside_a_job` is the pattern.

`pytest-randomly` is installed; the suite is order-stable, but pass
`-p no:randomly` anyway for a comparable baseline when counting failures.

**The data-node tests start a real second process.** `tests/node_harness.py`
spawns `python -m plexora node serve` on a free port and talks to it over a
socket; `tests/test_node_table.py`, `test_node_image.py`, `test_node_routes.py`
and `test_node_dynamic.py` (the `--dynamic` add/status/remove/browse endpoints)
run against it. That is deliberate -- the failures this architecture actually
has are failures of the seam between two processes (a header not exposed to a
browser, a body decoded twice, a float32 cast eating a text column, a lock held
across a stream), and a stub is a seam with nothing on the far side. It costs
~2 minutes of the suite's ~4:40. The node gets a data root of its own outside
`tmp_path`: a node must never be able to reach the primary's registry, and on
Windows a directory the child touched breaks pytest's cleanup in a later test.
`tests/test_data_location.py` and `tests/js/data_location_probe.mjs` pin the
Local/Remote switch itself (which shape each choice produces, per field), and
`tests/test_reconnect.py` pins `data_routes._reconnect_hint` -- that a node a
saved connection manages names the `plexora connect` command rather than
pointing at Settings, which cannot fix a tunnel that is gone.

**`spatialdata` is installed in the conda env** (0.8.0, verified 2026-08-24 on
Windows), so `tests/test_spatialdata_adapter.py` and the SpatialData cases in
`plexora/plugins/gating/tests/test_anndata_gates.py` run there. Do not pass
`--ignore=tests/test_spatialdata_adapter.py` unless an import actually fails --
the counts above are from a run that DID ignore it, so a full run reports more.
`.venv/` is a partially-synced Dropbox checkout (empty `pip list`, missing
`click`); ignore it.

**Pointing a test at its own data directory.** The repo-root `conftest.py` has
an autouse `plexora_data_root` fixture that sets `PLEXORA_DATA_PATH` to
`tmp_path` and calls `paths.reset()`. That is all a test needs — nothing binds
the root at import any more, so one env var reaches every module. Use
`tests.helpers.use_data_root(monkeypatch, root)` to point at a *different*
directory and `use_shared_roots(monkeypatch, *roots)` to add shared ones.
(This replaced ~108 per-module `monkeypatch.setattr(module, "data_path", ...)`
calls across 31 files, which had to name each module that had imported the
constant and silently missed any added later.)

**`data_model`'s module globals leak across test files.** It keeps the loaded
datasource in globals (`ball_tree, source, config, seg, zarray, channels,
metadata, _loaded_source, datasource`); the loadedness guards compare
`_loaded_source` against `loaded_scope(name)`, which is the bare name unless
shared roots are configured. Many test files register a datasource named
`"proj"`, so a test that loads a project and does not reset these leaves the
next file silently served the previous test's table — own all of them via
`monkeypatch` in any fixture that loads real data (see the ROI plugin's
`isolate_data_model`). Separately, a synthetic test image must be
`(2, 256, 256)`: a single-channel write comes back 2D and `data_model` indexes
`shape[2]`, and the pyramid walk needs every dimension >= 200.

**`load_datasource()` spawns a daemon thread**
(`_warm_datasource_caches`, `data_model.py` ~line 429) that outlives the test
that started it. Everything it calls goes through `_ensure_loaded()`, which
can reassign `data_model.config` wholesale and bump `load_generation` — against
whatever data root the *next* test set up. The symptom is a KeyError
in an unrelated file, and which file depends on wall-clock timing, so it used
to move whenever anything was added to the suite (this is what previously made
`test_segmentation_mapping.py::test_a_user_supplied_label_pyramid_is_served_without_conversion`
pass alone but fail in a full run). The repo-root `conftest.py` disables the
thread suite-wide with an autouse fixture; nothing asserts warming happens, and
disabling it changes no test result, only speed.

The dataset/manifest/progressive-registration work (`server/models/
datasets.py`, `server/models/manifest.py`, `server/routes/dataset_routes.py`,
`plexora/datasets.py`, `DataSpec.unresolved`/`deferred_spec`, the Open Project
page rewrite) added `tests/test_datasets_registry.py`, `tests/test_manifest.py`,
`tests/test_dataset_routes.py`, `tests/test_datasets_api.py` and
`tests/test_open_project.py`, plus the probe `tests/js/open_project_probe.mjs`;
it extended `tests/test_api_dataset.py`, `tests/test_cell_mode_control.py`,
`tests/test_cli.py` (`dataset`/`project` subcommands) and
`tests/test_requirements_routes.py` (including the end-to-end
`test_a_tool_opens_a_deferred_project_by_asking_once`, which is the whole
progressive story through a real plugin) and `tests/test_project_edit_routes.py`
(the unresolved-source edit path), and regenerated `tests/golden/boundary_*.json`
for the asset-tag and route-count changes this pass carries.

Full suite on 2026-09-17 after this work: **3483 passed / 25 failed / 2 skipped
/ 4 errors**, 15m28s. Every failure was accounted for: 23 are the Windows
zarr-write `PermissionError [WinError 5]` flake (see the note above -- the set
shifts run to run and each passes alone), 5 are long-standing assertion
failures confirmed identically in a clean `git worktree` at HEAD
(`test_brightfield_routes::test_a_flat_slide_derives_its_coarse_levels`,
`test_path_picker::test_the_home_panel_is_a_control_rather_than_a_drop_target`,
`test_browse_routes::…folders_first…`, `test_connection_modal::…one_connection…`,
`test_quick_view_routes::…dedupes_name…`), and
`test_remote_connect::…short_tail…` is load-sensitive and passes alone. The
count is LOWER than the ~57 recorded for 2026-09-10 because that flake's
count varies with machine load, not because anything was fixed -- diff the
failure LISTS, never the counts.

```bash
# Syntax gate for the unbundled viewer
node --check plexora/client/src/js/views/imageViewer.js

# Frontend build -- `npm run build`, NOT `npx webpack`. The config's own mode is
# 'development', so a bare `npx webpack` emits an 8 MB bundle under a different
# set of chunk names than the one that is committed; `build` passes
# `--mode production` and the diff is then only what you changed.
cd plexora/client && npm run build

# Local server
python -m plexora.server_cli --port 8848 --host 127.0.0.1
```

The baseline smoke test takes a datasource via `PLEXORA_BASELINE_DATASOURCE`
(default `orion2`). On macOS `orion2`'s files live only on the Windows side of
this Dropbox-synced repo, so use a locally-populated datasource instead.

### Validating rendering changes

Syntax checks and unit tests prove nothing about pixels. Playwright is available
under `plexora/client/node_modules` (run with
`NODE_PATH="$PWD/node_modules"` if the script lives elsewhere). The pattern that
works:

1. Launch `chromium` with `channel: 'chrome'`, `args: ['--use-gl=angle',
   '--ignore-gpu-blocklist']`.
2. Load `http://127.0.0.1:8848/<datasource>`, wait, then activate channels via
   `window.__plexora.seaDragonViewer.updateActiveChannels(name, 'add')`.
3. Set a deterministic viewport with `zoomTo` + `panTo` + `applyConstraints`.
4. For **speed**: sample `requestAnimationFrame` intervals while driving
   `viewport.panBy` in a loop; report median/p90, not mean.
5. For **correctness**: hash the `#openseadragon canvas` pixels, then re-run the
   identical script against stashed pre-change code and compare. Cover base,
   pan-away-and-back, colour change, range change, HD toggle.
6. To attribute cost, monkey-patch `CanvasRenderingContext2D.prototype.drawImage`
   and bucket by source argument, or take a CDP `Profiler` trace.

Caution: `git stash push` only isolates a change if HEAD does **not** already
contain it. After a commit lands, that comparison silently becomes a no-op
against itself.

**Pixel hashes are not sufficient for a uniform rename.** Proven by mutation:
pointing the JS at `u_gating_shape` while the shader declared `u_cell_range_shape`
left all 14 captured pixel hashes identical, with no console error and
`gl.getError() == 0`. `getUniformLocation` returns `null` for an unknown name and
`gl.uniform2iv(null, ...)` is a silent no-op, and the colour-coded path that
would have shown it is unreachable from the UI — `u32_rgba_map` returns white
before consulting the range table unless `or_mode`, and `eval_mode` is always
`'and'`. Add a `getUniform` readback of the range-table shape, which tracks
`[5,2]`/`[5,1]`/`[5,0]` as gates change and goes `null` the instant the names
disagree.

Two setup requirements for anything touching the range table:

- **Use a datasource with segmentation.** That code lives under `u_tile_fmt == 32`,
  so a maskless datasource exercises none of it. Nothing in `plexora/data/` or
  `tests/` has one — build it. Any mask size works;
  `segmentation_pyramid.pyramidize_segmentation_mask` writes its own OME metadata
  and stops adding levels once the image fits one tile.
- **Know which mask kind the datasource stores.** `segmentationMode` is
  `"filled"` (the default since `sp.DEFAULT_MODE` changed: labels stored whole,
  boundaries derived client-side) or `"outlines"` (boundaries baked into the
  file; nothing in the UI selects it any more). Both are handled in
  `renderLabelTile()` (`views/labelTile.js`, extracted from `imageViewer.js`'s
  constructor; `segmentationMode` is its last argument now rather than read off
  `this.config`) — **not** in the shader. That trips
  people up: frag.glsl has a `u_tile_fmt == 32` branch (`u32_rgba_map`) that
  looks like it draws the label layer, but `handleTileLoaded` renders every
  label tile — once per drawn layer — into `tile._layerContexts` and the
  tile-drawing handler blits those canvases, so the GL branch is unreachable for
  tileFormat 32. Editing the shader to change how cells are drawn will appear to
  do nothing. `tests/js/label_outline_probe.mjs` runs the real function against
  synthetic tiles, `cell_color_probe.mjs` pins its pixels byte-for-byte with and
  without a colour table, `cell_layer_registry_probe.mjs` the layer registry and
  `label_tile_lifecycle_probe.mjs` what a tile holds and when it lets go;
  `frag.glsl`'s `near_cell_edge`/`in_diff` are dead code inherited from
  minerva_analysis and were never called there either.
- **Give gate ranges in data units, not 0–1.** Normalized values match no cell,
  and every gate state then hashes identically.

Hash `page.locator('#openseadragon').screenshot()`, not `canvas.toDataURL()` —
the latter returned stale content for the WebGL layer partway through a long
run. Screenshots reproduced bit-for-bit across runs.

Absolute frame times from headless ANGLE are pessimistic versus a real GPU. Trust
the before/after ratio, not the number.

### The suite is green, and the "standing failures" above are history

**2026-09-17: `3530 passed, 2 skipped` on Windows/conda** with
`python -m pytest -q -p no:randomly` -- no failures, no errors. Every
per-pass note above that names a tolerated failure predates this, and none of
them should be tolerated again:

- **The standing three are fixed, not excused.** The quick-view dedupe test
  asserted a second registration of the same image becomes `sample_2`;
  `/quick_view` had been changed to reopen the existing project instead (see
  `_find_existing_datasource_for_image`) and the test was never updated, so it
  now pins the reopen, with a second test for the case the `_2` suffix is still
  for. The Windows-path assertion in `test_path_picker.py` wanted
  `.browse-kind-split.is-panel` to be a solid border and main.css said
  `dashed`, directly under the comment explaining why it must not -- the CSS
  was wrong, not the test. `test_connection_modal.py`'s
  `test_one_connection_concept_reaches_the_page_that_explains_it` wanted
  `plexora connect you@login.cluster.edu` on the Settings page, which the
  page rewrite had dropped while keeping the Jinja comment claiming the panel
  still said it; the paragraph is back.
- **`test_browse_routes.py`'s size assertion was a platform bug in the test.**
  It compared a listed file's size against `len("a,b" + chr(92) + "n...")`
  while creating the file with `write_text`, which translates newlines -- 10
  bytes on Windows, 8 on Linux. It writes bytes now. Anything asserting a byte
  count must not create the file with `write_text`.
- **Forty of them were one Windows fact.** See the zarr entry under Sharp
  Edges: they were all `PermissionError: [WinError 5] ... .partial ->
  zarr.json`, across `test_ome_zarr_reader.py`,
  `test_register_zarr_image_datasource.py`, `test_spatialdata_*.py`,
  `test_project_edit_routes.py`, `test_requirements_routes.py`,
  gating's `test_anndata_gates.py` and roi's `test_roi_adapters.py`, and the
  set shifted from run to run. Nothing was wrong with any of them.

The spatial-layer work (the seven-commit `LayerSpec`/`spatial_layers`,
`ngff_transform.py`, `spatial_scene.py`, `layer_sources.py`,
`transcript_tiles.py`, the `layerStack.js`/`cardList.js`/`layerManager.js`
client split, and the bundled `transcripts` plugin) added
`tests/test_layer_spec.py`, `test_layer_transform.py`, `test_layer_sources.py`,
`test_transcript_tiles.py`, `test_spatial_scene.py`, `tests/spatial_fixtures.py`,
`tests/golden/transform_cases.json` (one table read by BOTH
`test_layer_transform.py` and `tests/js/layer_transform_probe.mjs`, so a case
added to one is checked by both runtimes) and
`plexora/plugins/transcripts/tests/`, plus the JS probes
`layer_stack_probe.mjs`, `layer_transform_probe.mjs`, `layer_manager_probe.mjs`,
`overlay_api_probe.mjs` and `transcripts_boot_probe.mjs`. A measured run on
2026-09-18 gave **3629 passed / 7 failed** — higher than the 2026-09-17 green
baseline above because of these additions, and the count has only grown since;
treat the number above as history, not as today's target, and diff failure
LISTS as the suite-is-green note already says.

The `*_boot_probe.mjs` probes (`transcripts_boot_probe.mjs` today) take their
script list as command-line ARGUMENTS rather than discovering it themselves —
they are driven from a pytest wrapper that reads the plugin's own `scripts`
descriptor (see `test_transcripts_boot.py`) and hands it the file list to run.
Running one bare loads nothing and always exits 1; do not add a loop like
`for f in tests/js/*_probe.mjs; do node "$f"; done` — it silently fails every
`*_boot_probe.mjs` while every other probe in the loop passes.

**The home page went back to its own controls (2026-09-18).** The import
redesign had replaced the quick-view landing with one card holding an "Import
Sample…" button, which meant an empty install's only action was to open a modal
that then asked the same first question the page could have asked itself. So
`views/quickViewLanding.js` and `css/quickView.css` are restored, `index.html`'s
`{% else %}` branch is the landing card again, and `main.css`'s `.home-landing*`
rules are gone.

**Nothing under it was reverted, and that is the point.** The restored page
POSTs to `/import/sample` rather than the deleted `POST /quick_view`, so it is
an entry point on the one engine instead of the second importer it used to be —
see "The home page is not one of the dialog's surfaces" above. The dialog keeps
all five of its surfaces (the home page's is now the footer link), and no route,
model or plugin seam was touched: `route_count` is unchanged at 113.

Added `tests/test_home_landing.py` (11 tests: the markup, the wiring, the
one-importer guard, the library link the redesign added and this kept, and the
two capability floors — repeat picks reopening, and a folder arriving with its
mask and table). `tests/test_path_picker.py` and
`tests/test_data_location.py` assert the landing *and* the dialog where they
had been narrowed to the dialog alone; `tests/test_single_image_import.py`'s
docstring no longer says the landing page is gone. Asset tags
`?v=20260918_one_importer` on `main.css`, `importSample.js`, `navbarControls.js`
and the two restored files; **all six boundary goldens regenerated** for those
tags and for `quickView.css` rejoining `index.html`'s style block —
`route_count` unchanged, which is itself the check that this was a UI change.
On macOS/conda: **3781 passed, 1 failed, 8 skipped** — the one failure is
`test_register_image_datasource.py::test_derive_dataset_name_from_path`, a
Windows-path assertion that fails on macOS on a clean tree (verified by
stashing).

**A cell table may be a parquet (2026-09-18).** Selecting a Xenium run got as
far as proposing the whole sample, wrote the image, then refused the table with
"Cannot read cells.parquet: expected a .csv, .h5ad or .zarr file" —
`detect_data_type` had a suffix table with no `.parquet` in it, and every
platform shipping today writes its per-cell table as one. `.parquet` is now a
`DATA_TYPE` of its own and `adapters/flat_table.py` holds the seam (see the
`models/adapters/` entry above): the dozen `data_type == "csv"` branches that
meant "the file IS the table" now ask `is_flat_table()`, which is what keeps
the next flat format from being a dozen edits with one silently missed.

Touched, all in that one sense: `detect_data_type`/`_ADAPTERS`/`DATA_TYPES`,
`inspect_csv` → `inspect_flat_table(path, data_type)`, `replace_project_data`
and `/inspect_data`, `register_datasource`'s schema read and
`flat_table_spec(..., data_type=)`, the node's `/table/<id>/inspect`, the edit
page's `has.columns`, `source_layers`/`source_obsm`, and the ROI plugin's
write-back (which now writes a parquet back out AS a parquet — writing it as
CSV would have silently changed the format of somebody's own file). Also
`import_proposal`: the Xenium and loose-parquet cell-table proposals no longer
put the string `"parquet"` in `LayerProposal.table`, which names a table INSIDE
a container and was landing in `DataSpec.table`; and a cell parquet from any
pipeline is now recognised by `guess_roles` finding a centroid, not only by
Xenium's spelling. Browser upload accepts `.parquet` (flat tables are copied
into the project anyway), and the Data field, path picker and file dialogs name
it. Asset tags `?v=20260918_parquet_tables` on `pathPicker.js`,
`browsePicker.js`, `dataLocation.js`, `dataSourceField.js`; all six boundary
goldens regenerated for those tags, `route_count` unchanged at 113.
`tests/test_parquet_tables.py` (18 tests) is the guard. Verified against the
real 247,636-cell `cells.parquet` from a Xenium ovarian run: registered in
0.02s, loaded in 0.02s.

**And the suite stopped writing into the developer's own install.**
`layer_jobs.start` builds a layer on a daemon thread, and a Xenium run's
transcripts outlive the test that imported them; once teardown had undone
`PLEXORA_DATA_PATH`, the thread's `Project.mutate` and tile cache resolved
`paths.data_root()` to `~/Library/Application Support/plexora` and wrote there.
Found the hard way — a run of the new tests left 23 `run_0042*` projects in a
real install, written from tmp_paths that no longer existed, and the `run`
project `test_import_sample_routes.py` creates had been leaking the same way
for longer. `conftest.py`'s `_finish_layer_builds` joins every `layer-*` thread
before `plexora_data_root` tears down. It is declared against that fixture on
purpose: teardown runs in reverse dependency order, so the join happens while
the environment still points at the test's own root.

After both: **3799 passed, 1 failed, 8 skipped** (the same macOS baseline
failure).

**A Xenium image is read right, its cell table is normalised at import, and
the transcripts plugin became a LAYER SECTION (2026-09-18).**
`tiff_series.channel_series` now collapses a single-channel Z-stack to its
middle focal plane (`focal_planes`/`single_plane_series`, keeping the
pyramid) rather than reading a Xenium `morphology.ome.tif`'s fourteen focal
depths as fourteen channels; a `morphology_focus/` folder of several
single-channel files is read as one multi-channel image
(`server/utils/xenium_focus.py`, `FocusPyramid`) dispatched first, before
the zarr/DICOM/brightfield tests, everywhere an image file is opened.
`spatial_scene.XENIUM_FILES` now prefers `morphology_focus/` over the raw
Z-stack. A Xenium `cells.parquet` is rewritten in the project's own copy at
import (`server/utils/xenium_cells.py`) — a numeric `cell_index` and
centroids rescaled into reference pixels — so `roles.cell_id` needs no
question asked. The `transcripts` plugin's `PLUGIN.panels` moved to
`Plugin.LAYER_SECTION_SLOT`: it is now on from page load rather than opened
from the Tools menu, `plugins.tools_for`/`ready_tools` exclude it, and
`layerManager.js` no longer cards `points`/`shapes` layers at all (see the
`views/layerManager.js` and "A LAYER SECTION is a different thing" entries
above). `transcript_tiles.CACHE_VERSION` bumped to 2 for an 11-byte point
record. New tests: `tests/test_xenium_focus.py`, `tests/test_xenium_cells.py`,
`tests/js/transcript_points_probe.mjs`,
`plexora/plugins/transcripts/tests/test_transcripts_client.py`, plus
additions to `test_tiff_hyperstack.py`, `test_spatial_scene.py`,
`test_centroid_tiles.py`, `test_parquet_tables.py`, `test_transcript_tiles.py`,
`test_layer_sources.py`, `test_plugins.py` and `test_plugin_boundary.py`'s
probe (`PROBE_CONFIG` now carries a `spatialLayers` transcripts entry so the
layer-section slot is pinned in the boundary goldens — all six regenerated).
Verified: **3866 passed**, the one known pre-existing macOS failure
(`test_register_image_datasource.py::test_derive_dataset_name_from_path`).

**A Xenium run's boundary polygons become a real segmentation mask.**
New `server/utils/boundary_mask.py` rasterizes `cell_boundaries.parquet`
into the same tiled pyramidal label OME-TIFF `segmentation_pyramid` writes
for a converted raster mask, so Outlines, Filled and colour-by-gating now
work on a run that used to register its segmentation as a `shapes` layer
nothing drew. `segmentation_pyramid.pyramidize_segmentation_mask`'s tile-loop
and level-factor math were extracted into two reusable pieces —
`pyramid_factors` and `write_label_pyramid` — so `boundary_mask.rasterize` is
the second producer of a Plexora-generated mask rather than a second answer
to what one looks like on disk; `write_label_pyramid` gained `min_levels`,
because the mask is served at the reference IMAGE's level count and a
polygon-drawn pyramid a level short raised a KeyError on the first
whole-slide tile. `data_model.convertOmeTiff` dispatches
`boundary_mask.is_boundary_table` before `resolve_outline_segmentation` in
its `isLabelImg` branch (a parquet opened as a raster fails inside the TIFF
reader), taking a new `label_geometry=` a boundary table needs and an
ordinary raster mask does not; `start_segmentation_job`'s
`_label_geometry_for` resolves it from the project on the request thread.
`import_proposal` gained `CELL_BOUNDARY_ELEMENT`: a Xenium bundle's
`cell_boundaries` element claims `role="mask"` when it carries a `label_id`,
and a standalone boundary parquet is recognised the same way in
`_detect_parquet`, tested before the cell-table branch so its
`vertex_x`/`vertex_y` columns are not read as centroids first.
`import_sample._preferred_mask` lets a raster mask the user supplied outrank
the run's own polygons (the loser demoted to `role="layer"`, not dropped),
and `register_sample` now attaches the mask **last**, after `Project.mutate`
has had the chance to backfill `ImageSpec.pixel_size` from the run — attaching
it earlier drew a run's cells at one pixel per micron, a fifth-scale mask in
the corner of its own slide. `opencv-python-headless` became a core dependency for it, added with
`uv add`. New `tests/test_boundary_mask.py` (21 tests),
plus additions to `tests/test_segmentation_pyramid.py`,
`tests/test_import_proposal.py` and `tests/test_import_sample_routes.py`. No
client-side file changed, so no asset tag moved and the boundary goldens are
untouched. Verified: **3896 passed, 1 failed, 8 skipped** on macOS/conda --
the one failure the known pre-existing `test_derive_dataset_name_from_path`
Windows-path assertion.

**The density tile grew a bin-size, ramp and threshold vocabulary, and a
plugin-drawn points layer can now earn a Layers card (2026-09-19).** New
`server/utils/colormaps.py` names the four ramps (`viridis`, `magma`,
`cividis`, `coolwarm`) a density tile can be read off, anchored to the digit
against the client's `cellExplorerColors.js` so a density map and a coloured
cell overlay share colours — `tests/js/transcript_points_probe.mjs` reads the
Python file to check the client's copy still matches. `layer_sources.parse_style`
gained `bin` (bin size in image pixels), `ramp` and `dlo`/`dhi` (the contrast
window as fractions of the automatic one) alongside the existing
`genes`/`colors`/`minq`; `transcript_tiles.bins_for` is now the one place bin
geometry is decided, `density_tile`/`density_rgb_tile`'s `bin_size=` kwarg
became `bin_pixels=` (absolute image pixels, so a 40-micron bin stays 40
microns at every zoom instead of doubling with the level), and a new
`density_ramp_tile` draws one summed field through a named ramp. Separately,
`views/layerStack.js` gained `claim(id, by)`/a `drawnBy` field, and
`layerManager.js`'s `isCarded` became `CARDED_KINDS.has(kind) ||
Boolean(drawnBy)` — the seam that lets the transcripts plugin's points layer
take an ordinary Layers card (`main.js`'s `pluginLayerApi.claim`/`release`)
instead of a switch of its own; `__centroids__` is deliberately never
claimed. The transcripts panel's own heading checkbox is gone along with it.
New `plexora/client/src/js/views/fileSourceRow.js` (a Local/Remote/Upload row
lifted out of `channelNamesUpload.js`, which keeps its own copy) and
`plexora/plugins/transcripts/static/transcriptGroupModal.js` (CSV/remote
gene-group import). New routes `GET`/`POST /plugins/transcripts/state` (the
panel's own gene/colour/group selection, kept per project) and
`POST /plugins/transcripts/groups` (the CSV/remote import behind the modal
above); `/manifest` also returns `pixel_size` and `density_stretch`. Asset
tag `?v=20260919_transcript_controls` on `main.css`, `vendor_bundle.js`,
`layerStack.js`, `layerManager.js` and `main.js`; all six boundary goldens
regenerated (`boundary_transcripts.json`'s `route_count` 113 -> 115, for the
two new routes; the other five unchanged in count). New
`tests/test_colormaps.py`; additions to
`tests/test_transcript_tiles.py`, `tests/test_layer_sources.py`,
`plexora/plugins/transcripts/tests/test_transcripts_routes.py`,
`tests/js/transcript_points_probe.mjs` and `tests/js/layer_manager_probe.mjs`.
Verified: **3933 passed, 1 failed, 8 skipped** on macOS/conda — the same
pre-existing `test_derive_dataset_name_from_path` failure, and the only one
of that name and shape; earlier baselines in this file with more failures are
history superseded by this one, not a widening of the standing set.

**The sidebar became one visual layer stack (2026-09-19).** Every data
modality is a layer card and the card is where that modality is configured.
What this replaced: three unrelated surfaces over one scene — an Image
Channels section, a plugin's own layer section, and a "Layers" list whose eye,
opacity slider and X wrote the client model and nothing else, so they moved
nothing and forgot everything on reload. The model was already one ordered
list; only the sidebar was not.

Five things carry it, and each has an entry above: the mask is `pinned` in
`layerStack.js` rather than carded, because the Cells footer already owns how
cells are drawn and a card with no card above it cannot be dragged back;
`viewerManager.js` subscribes to the stack and is the one place the model
reaches the renderers; `index.html`'s `#layer_section_slot` became hidden
STAGING whose `data-layer-body`/`data-layer-extras` contents `layerManager.js`
MOVES into cards; `layerManager.js` reconciles rather than rebuilds, because a
card now holds markup other controllers own handles into; and two new routes
(`PATCH /project/<name>/layers/<id>`, `PUT /project/<name>/layers/order`) make
a card's choices statements about the sample rather than about the tab.

DOM ids GONE: `image_channel_section`/`_body`/`_collapse`,
`image_adjust_section`/`_body`/`_collapse`, `transcripts_panel_section`,
`transcripts_collapse`. Ids that SURVIVED, because the markup was moved and
not rebuilt: `channel_slot_list`, `add_channel_button`, `channels_upload_icon`,
`num-selected-channels`, `max-channels`, `image_adjust_reset`, every
`adjust_*`, and `transcripts_body` (its class is now
`layer-card-plugin-body`). Also gone:
`viewerSidebar.setupChannelSectionCollapse` (its `setChannelSectionCollapsed`
now delegates to `PlexoraLayerManager.setCollapsed("__image__", …)`),
`brightfieldAdjust`'s `slideLayer()` and its `add-item` handler (its opacity
slider writes the stack now, which is what makes it and the base card one
control), and `layerSections`' `bindCollapse`/`bindVisibility`/`isVisible`/
`setVisible` along with `TranscriptsSidebarController.setLayerVisible`.

One constraint kept rather than fought: points draw on their own canvas above
all rasters, so the stack shows ONE labelled divider ("Points and shapes draw
over images") and refuses drags across it. Interleaving would mean rendering
points through the tile pipeline.

Asset tag `?v=20260919_layer_cards` on `main.css`, `viewer.css`,
`vendor_bundle.js`, `cardList.js`, `layerStack.js`, `imageViewer.js`,
`viewerSidebar.js`, `layerManager.js`, `layerSections.js`, `toolLoader.js`,
`brightfieldAdjust.js`, `main.js` and figure_builder's `workspace.html` copy
of `viewer.css`; transcripts `VERSION` likewise. All six boundary goldens
regenerated: `route_count` +2 in every one, for the two new routes.
New `tests/js/layer_state_probe.mjs` + `tests/test_layer_state.py` (the base
image layer's eye and opacity — that hiding it REMOVES its channel items but
leaves the slots standing, and that the mask is never touched) and
`tests/test_project_layer_routes.py`; `tests/js/layer_manager_probe.mjs`
rewritten (58 checks); additions to `tests/test_layer_visibility.py`,
`tests/js/layer_visibility_probe.mjs`, `tests/js/layer_stack_probe.mjs`,
`tests/test_layer_spec.py` and `tests/test_plugin_layers.py`.
Verified: **3984 passed, 1 failed, 8 skipped** on macOS/conda — the same
pre-existing `test_derive_dataset_name_from_path`, and nothing else.
Superseded by the 4049 below.

**The colour bar and its palettes moved out of Cell Explorer into core, and
the transcripts panel was reordered around Display first (2026-09-19).** New
`views/gradientRange.js` defines `PlexoraColorRamps` (the four continuous
ramps plus `custom`, `PALETTE_LABELS`, `CUSTOM_LOW`/`CUSTOM_HIGH`, `STOPS`,
`parseHex`/`toHex`/`anchors`/`ramp`/`rampStop`/`gradientCss`) and
`PlexoraGradientRange` (the bar with two range handles, the palette
disclosure, an optional Auto button, an `extras` slot for a caller's own
controls, an optional `swatch(name)` for a palette entry with no ramp of its
own, and an optional `caption` between the two scale numbers) — the one
client-side definition of a ramp, because this started as Cell Explorer's
numeric-column control and the transcript density map wanted the same bar,
the same handles and the same palettes rather than a second implementation to
learn. `cellExplorerColors.js`'s own `RAMPS`, `PALETTE_LABELS`, `CUSTOM_LOW`/
`CUSTOM_HIGH`, `RAMP_STOPS`, `parseHex`, `toHex`, `ramp` and `rampStop` are
now thin delegations to it, and `cellExplorerContinuous.js` shrank from
~400 lines to ~140 — an adapter over `PlexoraGradientRange` keeping only what
core cannot know: a column's stats as the extent, the constant-column case,
`n_missing`, and its own per-column eye. The `.cex-ramp*`/`.cex-palette*`/
`.cex-custom*` rules moved out of `cell_explorer.css` into `main.css` as
`.gradient-range*`/`.gradient-palette*`/`.gradient-custom*`, plus new
`.gradient-auto` and `.gradient-range-caption`; only `.cex-ramp-visibility`
(the per-column eye button) stayed behind, because that one is Cell
Explorer's own. `gradientRange.js` is loaded from `base.html` before
`toolLoader.js`, like every other shared widget a plugin panel builds with.

The transcripts panel template was rewritten around the same control:
metadata line, Display (Points | Density map), then either Appearance (Style,
Point size) or Density (bin size and its tick ladder, Color map) for whichever
mode is chosen, then a shared Opacity row, then Filtering (Min Q-score), and
Genes (search plus tree) LAST — because the gene list is the one part with no
bounded height, and controls placed under a forty-gene list are a scroll away
that moves every time someone adds a gene. Gone with it: the standalone
`#transcripts_colormap` select, `#transcripts_dlo`/`#transcripts_dhi` number
boxes, `#transcripts_ramp_low`/`#transcripts_ramp_high`,
`#transcripts_opacity_label`, and the `.transcripts-controls`/
`.transcripts-label`/`.transcripts-summary`/`.transcripts-tree-heading`/
`.transcripts-range-pair`/`.transcripts-ramp-legend` classes. New
`.transcripts-block`/`.transcripts-block-title` wrap each section (not to be
confused with `.transcripts-group`, still the gene-group box in the tree),
plus `.transcripts-meta`/`.transcripts-value`/`.transcripts-unit` and the
`--tx-label`/`--tx-value`/`--tx-gap` custom properties on `#transcripts_body`
that both the row grid and the density bin's tick ladder read.
`transcriptsSidebarController.js` lost `bindThreshold`, `paintThreshold` and
`fillColormaps`; `paintRamp` now drives a `PlexoraGradientRange` instance
whose handles move over 0..1 because `densityLow`/`densityHigh` already ARE
fractions, with the formatter converting to molecules-per-bin for display.
New statics `compact(value)` and `countLabel(value)`, and a new `geneSwatch()`
for the density map's "a colour per gene" option; `TranscriptLayer.RAMPS` and
`RAMP_LABELS` are now getters over `PlexoraColorRamps` rather than their own
copy.

Two bugs, found and fixed in the same pass. First, `viewerManager.addTiledLayer`
leaked world items: `addTiledImage` is asynchronous, so two `setStyle` calls
in quick succession give drop/add/drop/add, and the second drop has no `item`
to remove yet because the first add is still in flight. That first item then
arrives, finds `shown` true again — set by the second add — and is kept, then
is immediately overwritten in `item` by the second; nothing can ever remove it
again. Measured three orphaned density rasters live in the world at once. A
`generation` counter fixes it: every `add()` takes the next number, only the
holder of the current one may install its item, and `drop()` burns the
number. Second, `TranscriptLayer.decideSubstitution` returned `true` whenever
`viewAs === "density"`, so `substituting` (meaning "points were asked for and
there are too many to draw") was also true when the user simply chose density
— conflating a display choice with an LOD fallback. `decideSubstitution` now
answers only the LOD question, `refresh()` consults it only in points mode,
and a new `showMode(density)` puts one representation up and takes the other
down in one place.

> **Half superseded, same day.** `showMode` is still there and still the one
> place a representation goes up or down. `decideSubstitution`, `substituting`,
> `COVERAGE_LIMIT` and `HYSTERESIS` are gone entirely — see *Points stay
> points* below. There is no density fallback left to disentangle.

Asset tag `?v=20260919_gradient_range` on `main.css`, `viewer.css`,
`vendor_bundle.js` and the new `gradientRange.js`; the cell_explorer plugin
`VERSION` is `20260919_gradient_range` and the transcripts plugin `VERSION`
is `20260919_transcript_panel`. All six boundary goldens regenerated with
`route_count` unchanged (a script/stylesheet swap, not a route change);
`tests/js/cell_explorer_boot_probe.mjs`, `transcripts_boot_probe.mjs` and
`transcript_points_probe.mjs` all preload `gradientRange.js` in their core
widgets now, because `TranscriptLayer.RAMPS` reads from it. On macOS/conda,
run directly rather than through an agent: **3984 passed, 1 failed, 8
skipped**, ~6m26s — the same `test_derive_dataset_name_from_path` and nothing
else; the total is unchanged from the baseline above because this pass moved
and renamed code rather than adding or removing tests.

### Points stay points, the level of detail is a merge, and hover picks a gene out

Zooming out in Points mode used to hand the view over to the density raster
once the dots would have covered the screen. That answered the cost question
and got the meaning wrong: the user asked for molecules and was shown a heat
map, so the picture changed REPRESENTATION without anybody choosing it. What
changes with the zoom now is only how many molecules one dot stands for.

Server: `transcript_tiles.aggregate_tile` (documented with the rest of that
module above) and `level` + `minq` on `/points`.

Client, all in `transcriptLayer.js` unless said otherwise:

- `showsDensity()` is now `state.viewAs === "density"` and nothing else.
  `substituting`, `decideSubstitution`, `COVERAGE_LIMIT` and `HYSTERESIS` are
  deleted; `MAX_POINTS_ON_SCREEN` survives as the frame budget, but it is now
  a reason to merge HARDER rather than to draw something else.
- `pickLevel(bounds, screenWidth?)` picks the level: the first at which a bin
  (`binPixelsAt(level)` image pixels) reaches `dotSize(zoom) *
  AGGREGATE_SPREAD` on screen — the widest a dot can be AT THAT ZOOM, times
  2.5, with a 4-pixel floor (`AGGREGATE_SPACING`) — then coarsened while
  `estimateAggregates(bounds, level)` is over the budget. There is always a
  level that fits, because the coarsest is one tile. `estimateAggregates` is
  NOT `estimateInView`: a bin contributes at most one dot PER GENE, so the
  count is capped by bins-in-view times genes.

  **The spacing is an ink budget and can be read as one.** A dot a 2.5th of
  the way to its neighbour covers (π/4)/2.5² of the ground where every bin
  is full — about a twentieth for ordinary dots. At 1.6 that was an eighth,
  and a dot 62% of the way to its neighbour reads as a disc laid over the
  tissue however small the disc is. The floor is not an independent number:
  it is the same rule applied to the smallest dot there is, `MIN_DOT *
  MAX_GROWTH * AGGREGATE_SPREAD = 4`, and the probe asserts they agree.

  **It is not, however, the control over HOW MANY dots there are.** A level
  is a quadtree step, so raising the spread quarters the count or leaves it
  alone depending on where the view happens to sit inside a band: asked for
  half the dots, 2.5 → 2.5√2 gave 69% fewer at whole slide and none at ×4,
  ×16 or ×64. That control is the server's `AGGREGATE_BINS`, which moves
  continuously and leaves the level, the dot size and the ink budget where
  they were.
- **Tiles are tagged, and the tag is the cache key.** `tagFor(level)` is
  `"0"` at level 0 — those tiles hold every gene and every score, so one is
  good for any selection and any threshold, which is what keeps toggling a
  gene free at the zoom where somebody is comparing genes molecule by
  molecule. Above level 0 it is `` `${level}|${genes}|${minQ}` ``, because an
  aggregate is a count OF those. `settle(keys, level, tag)` switches
  `renderer.setActive(tag)` only once every tile the view needs has arrived,
  so a level change uploads the new tiles underneath the old ones instead of
  blinking; `evict()` then drops what can never be drawn again.
- `evict` trims by BYTES (`MAX_TILE_BYTES = 64 MB`) as well as by count.
  A tile count was never a memory budget: level-0 tiles run from 15k to 50k
  molecules on one slide, and the cache now holds several levels at once.
- `transcriptPoints.js`: the GPU stride is **16**, not 12 — x f4, y f4, gene
  u2, q u1, pad, count f4 — and `repack(buffer, aggregated)` reads 11-byte
  molecules or 14-byte aggregates into it, giving a molecule `count = 1` and
  an aggregate `q = 255`. One vertex layout and one shader for both. The
  vertex shader sizes by `min(1 + AGGREGATE_GROWTH * log2(count),
  MAX_GROWTH)` — a tenth per doubling, stopping at 1.6.

  **Size is deliberately a weak channel, and getting that wrong is what the
  first version did.** It sized by `sqrt(count)` so that AREA carried the
  count, which is correct as an encoding and, at whole-slide zoom on an
  abundant gene, turns the section into a mat of overlapping bubbles with
  the tissue invisible under it (measured: ACTB at 54 µm bins wanted a 37 px
  dot in an 11.7 px bin). What the picture is FOR at that zoom is the
  spatial distribution, and a dot that covers its neighbours destroys
  exactly that; quantity, when it is the question, is what Density map
  answers. A tenth per doubling also makes the zoom transitions calm — a
  level boundary quadruples the count, which under sqrt DOUBLED every
  radius, a visible step at every stage of a zoom, and is now a fifth.
  `u_binPixels * u_scale` still caps a dot at the patch it stands for, now
  a backstop rather than the main restraint, and it is applied to the DOT
  the glyph is equal in area to rather than to the sprite, or a triangle
  and a circle would stop covering the same pixels.

- **The resting dot is a function of the ZOOM, not a constant screen size,
  and this is what makes a zoomed-out view read as tissue.** `u_pointSize`
  arrives already faded: `TranscriptPointRenderer.restingSize(pointSize,
  cssZoom, fullZoom)` = `clamp(size * (cssZoom/fullZoom) ** ZOOM_FADE,
  MIN_DOT, size)`, recomputed in `draw()` every frame off `place.scale /
  devicePixelRatio`, so the shrink is continuous through a zoom rather than
  a step at each level change. `TranscriptLayer.restingSize` /
  `dotSize(zoom)` mirror it, because `pickLevel` has to choose a level for
  the size that will actually be drawn; `fullZoom()` is the anchor and is
  DEFINED as the zoom at which `pickLevel` reaches level 0, so "the dots are
  full size" and "a dot is one molecule" are the same moment and the fade
  and the merge cannot argue. `ZOOM_FADE = 0.5`: proportional shrinking hits
  `MIN_DOT = 1` after about one screenful of zooming out and stays there,
  which throws away the whole progression from a cell to a section; a half
  power halves the dot per four-fold zoom out. The layer pushes `fullZoom`
  through `applyStyle` because the renderer cannot ask the manifest how big
  a level-0 bin is.

  **Merging alone cannot make a zoomed-out view restrained, and that is the
  mistake the version before this made.** Dots held at a fixed screen size
  and spaced just far enough to clear each other cover the same fraction of
  the screen at EVERY zoom, so no choice of level helps: a whole section
  came out as a lattice of discs with the morphology invisible under it
  (measured: 7,200 twelve-pixel dots for three genes). With the fade the
  same view is level 3 — 27 µm bins, 4.4 CSS pixels apart — and 67,000
  one-pixel dots, which reads as a tint of gene colour over visible tissue.
  That is also why `AGGREGATE_SPACING` went 8 → 4: once the dots shrink,
  merging as hard as before throws away the distribution instead of
  protecting it. Halving the dot count again on top of that (2026-09-19) is
  `AGGREGATE_BINS` 64 → 45 and nothing else — see that entry in the
  repository map for why the level rule cannot do it.

- **Hovering a gene in the list picks it out on the slide**, Xenium-style.
  The set of emphasised genes is a byte in the gene table the shader
  already reads (`meta.g`); how far they are lifted is one uniform,
  `u_emphasis` (a FRACTION 0→1 of the way from the resting size to the
  lifted one, not a multiplier), eased exponentially by
  `TranscriptPointRenderer.ease()` off the draw loop (`EMPHASIS_TAU =
  55 ms`). So a hover is a few hundred bytes of texture and a number: **no
  tile is refetched, no aggregation recomputed, no geometry rebuilt** —
  measured at eight hovers in 203 ms with zero requests.

  The lifted size is `min(pointSize * EMPHASIS_SCALE, binPixels * scale *
  emphasisFill)` — **off the size the SLIDER says, not off the faded one**,
  which is the part that matters now that resting is a pixel or two at low
  zoom: 1.8× of one pixel is two pixels and nobody could find it. Hovering
  lifts a gene OUT of the fade rather than scaling within it. The bin is
  still the ceiling, so a lift can never produce the overlap the level was
  chosen to avoid.

  `emphasisFill` (`EMPHASIS_CROWDING = 0.55`) is the gene-aware part: **a
  lift that fills every bin is not a highlight, it is a flood fill.** ACTB
  on this run has a dot in every bin of the section at whole-slide zoom, so
  lifting all of them to the full bin painted the tissue solid red (49% ink)
  and answered the question by erasing the picture. The layer estimates how
  many of the bins in view the hovered gene occupies — from the manifest's
  per-gene counts, never from the tiles, because a hover must not read a
  megabyte of vertex data — and gives up to 55% of the bin back in
  proportion. A sparse gene is untouched (fill 0.98 measured); ACTB comes
  back at 0.45 and 22% ink, a dense stipple with the morphology and its
  holes visible through it.
  While a gene is lifted the draw runs in TWO passes (`u_pass` 0 then 1,
  −1 meaning "everything" for the ordinary single-pass case) so the
  emphasised points land on top of the field they are being picked out of;
  an enlarged dot with a neighbour's small one punched out of its middle
  reads as a ring. `TranscriptLayer.emphasize(names)` owns the mask;
  clearing it deliberately LEAVES the mask and only eases the amount back,
  so letting go of a row shrinks rather than snaps. The controller
  delegates `mouseover`/`mouseleave` on the tree once (`bindHover`) rather
  than binding per row — a 480-gene panel is a thousand listeners per
  repaint, and a row replaced under the pointer never fires its own
  `mouseout`. `data-gene` beats the `data-group` box around it, so a
  group's heading lifts all of it and one of its rows lifts just that one.
- **The panel says nothing about the level any more.** `#transcripts_level_note`
  (which had been `#transcripts_truncated`) and its `paintLevelNote()`, its
  `.transcripts-note` rule and `TranscriptLayer.onLevelChange` — the hook
  that existed only to feed it — are all gone, at the user's request: the
  picture is meant to read as a distribution without a caption explaining
  the binning. Nothing else subscribed to `onLevelChange`.

Verified in Chromium on the Xenium run at the default point size of 6, three
genes (ACTB, EPCAM, APOBEC3A), tissue at full opacity — level / resting dot /
bin on screen / dots drawn / share of the overlay's pixels with any ink in
them. The "before" column is the same measurement at `AGGREGATE_BINS = 64`:

| view | level | dot | bin | dots (was) | ink (was) |
|---|---|---|---|---|---|
| whole slide (1 mm bar) | 3 | 1.08 px | 6.2 px | 36,885 (66,886) | 11.8% (15.5%) |
| ×4 (200 µm) | 2 | 2.16 px | 12.4 px | 70,214 (126,981) | 14.7% (17.6%) |
| ×16 (50 µm) | 1 | 4.32 px | 24.9 px | 49,746 (81,854) | 5.7% (6.2%) |
| ×64 (10 µm) | 0 | 6.00 px | — | molecules | 0.05% |

−45% dots at every aggregated zoom (the grid asks for −50.6%; sparse bins
merge without losing a dot, which is the difference), every level unchanged,
and the molecule view untouched because level 0 is not binned. The dot grows
a little at the middle zooms because `fullZoom` is `spacing / binPixelsAt(0)`
and the level-0 bin got wider — molecules now resolve at ~1.05 CSS px per
image px rather than 1.5, i.e. sooner.

The lattice the coarser grid exposed is `_priority`'s, not the grid's: a
row-sum DFT across the overlay at whole slide had its strongest period at
6.25 CSS px (magnitude 545) against a 6.22 px bin — the grid drawn as data.
After the avalanche fix the peak is 4.5 px at magnitude 80, off the bin
frequency and down 6.8×.

Zero density world items throughout; panning at a coarse level and hovering
both cost zero requests. The slider deliberately has little effect at
whole-slide zoom — `fullZoom` scales with it, so the fade takes a square root
of the change — and full effect once the view is in far enough to resolve
molecules, which is what "point size still influences it, but the low-zoom
view stays restrained" has to mean. Transcripts plugin `VERSION` is
`20260919_transcript_grain_slider4` (a concurrent session appended its own
slug to the `transcript_grain` bump this work needed);
`tests/golden/boundary_transcripts.json`
regenerated (no route change; `transcripts_level_note` is out of the element
list).

**Every slider became one shared primitive (2026-09-19).** Before this there
were nineteen sliders built five different ways — Jinja markup with an
`<output>`, `createElement` with a span, an innerHTML string with nothing at
all, two stacked range inputs, and d3-simple-slider's SVG — and only two of
the fifteen native ones set `accent-color`, so the app had no slider design,
it had thirteen of the platform's. New `views/slider.js` (`class
PlexoraSlider`, documented in the Repository Map above) replaces all of them
except the two d3-simple-slider lists that render off-screen into
`#legacy_controls_mount` (`views/channelList.js`'s `addSlider` and
`plugins/gating/static/csvGatingList.js`'s), which nobody sees and so were
left alone. Migrated: `gradientRange.js` (range mode, `minGap` one step,
`fields:false`, overlaid on the colour bar; its two scale-row numbers are
`PlexoraSlider.numberField`, and `buildHandle`/`drag` are gone),
`layerManager.js` (layer opacity), `brightfieldAdjust.js` (brightness/
contrast/gamma/opacity — `index.html`'s four `<output>`s are gone, the
slider's number box keeps their ids), `viewerControls.js` (cell point size,
which gained a readout it never had, and cell layer opacity),
`viewerSidebar.js` (`redrawChannelSlider` → `syncChannelSlider`, log scale,
`sliderDirty` and its resize listener deleted), `gatingSidebarController.js`
(`redrawGateSlider` → `syncGateSlider`, resize listener deleted;
`normalizeGateRange` gained a ±1e-9 guard because its rounding grid is now
the slider's own step), `transcriptsSidebarController.js` (size/opacity/
minQ, and the bin ladder with `field:false`), and `figure_builder`'s
`figureShapePanel.js`/`figureLinePanel.js` (a new `upgradeSliders()` call
after each innerHTML render).

CSS deleted from main.css: `.gradient-range-handle*`, the `.slider .handle`/
`.slider text` duplicate (viewer.css keeps its own copy for the two legacy
lists), `.layer-card-row output`, `.layer-card-number`'s own spinner rules
and box styling, `.adjust-row input[type=range]`, `.adjust-row output`,
`.cell-layer-opacity-value`, `.range-readout`, `.slot-range-readout`,
`.slot-detail-header .range-readout`, `.transcripts-value`,
`.transcripts-unit`, `.transcripts-number`, `.fb-range`, `.fb-range-value`;
`gating.css` lost `line.track`. In their place: a `.plx-slider`/`.plx-range`/
`.plx-number` block in main.css, plus a global `input[type="number"]`
spinner reset near its top (see Key Invariants above for both).

Asset tag `20260919_plx_slider` on `base.html`, `main.css`,
`viewerControls.js`, `viewerSidebar.js`, `layerManager.js`,
`gradientRange.js`, the new `slider.js`, `index.html`'s `viewer.css`/
`brightfieldAdjust.js`, figure_builder's `workspace.html` copy of
`viewer.css`, and the transcripts/figure_builder/gating plugin `VERSION`
constants; all six `tests/golden/boundary_*.json` regenerated. New
`tests/test_slider.py` (18 tests) + `tests/js/slider_probe.mjs` (~70 probe
checks) and `tests/test_slider_css.py` (10 tests, including the "nothing else
styles a range input", "no comma between WebKit and Gecko thumb rules" and
"no margin-top on a thumb" guards). Updated: `tests/test_cell_mode_control.py` and
`tests/js/cell_mode_control_probe.mjs` (stubs `PlexoraSlider`),
`tests/js/layer_manager_probe.mjs` (now loads the real `slider.js`; its
`makeNode` grew `style.setProperty`/`append`/`insertBefore`/`remove`),
`tests/js/figure_line_panel_probe.mjs` (stubs `PlexoraSlider`), and the
three `gradientRange.js` probe preloads (`cell_explorer_boot_probe.mjs`,
`transcripts_boot_probe.mjs`, `transcript_points_probe.mjs`), which now load
`views/slider.js` first.
### What a screenshot found that the arithmetic could not (2026-09-19)

The first pass shipped without a browser — Playwright is not installed under
`plexora/client/node_modules` on this machine and there was no populated
datasource to open — so the geometry was argued from the CSS. A screenshot of
the running channel slider disproved three parts of that argument at once, and
all three are worth remembering because none of them is visible in a diff:

1. **The thumbs sat below the rail, not on it.** See the `margin-top`
   invariant above. This took three attempts and was only settled by
   rendering it: headless Chrome (`/Applications/Google Chrome.app/.../Google
   Chrome --headless=new --screenshot --force-device-scale-factor=4`) against
   a harness page built from the served `tokens.css`/`main.css`/`slider.js`,
   with the thumb and rail centroids measured out of the PNG in Pillow. That
   loop costs about a minute and is the only way to answer a question of this
   kind; do not try to reason it out. Note that headless Chrome colour-manages
   its screenshots, so `#38bdf8` comes back as `(99, 187, 243)` -- sample the
   rendering to calibrate before matching on a colour.
2. **Two inline number boxes eat a 300px sidebar.** Risk 9 of the plan,
   confirmed: the upper box was clipping its own text. The boxes moved to the
   line above via the `fieldsSlot` option, which also put the channel
   slot's Auto button back on the line it had always shared with the numbers
   — the previous alignment, which the first pass had quietly changed. (A
   later pass moved the window to a single row again -- `fieldsSlot` is no
   longer how the channel window's numbers are placed; see the contrast
   window redesign further down.) The
   contrast window also formats as whole numbers now (`decimals: 0`); it is
   an integer photon count and `formatValue`'s two decimals were what pushed
   `65535.00` out of the box.
3. **Every transcript slider vanished** — and the first two explanations for
   it were both wrong. A stale `VERSION` was real but incidental; once it was
   fixed and the server restarted, the sliders were still gone. The actual
   cause needed the running app to find: `paintTree()` destroyed all four
   sliders on every gene-list rebuild, and because each had ADOPTED the
   template's `<input type="range">`, `destroy()` took that input out of the
   page with it. See the adoption invariant above. Both defects are fixed and
   both are pinned by tests.

   The method that found it, when reading the source and a green suite could
   not: start a server on a spare port, point headless Chrome at a real
   datasource with `--use-gl=angle --use-angle=swiftshader
   --enable-unsafe-swiftshader` — without software WebGL the viewer aborts at
   `createTexture` and the panel never binds, which looks like a different
   bug entirely — and `--dump-dom`. The dump showed
   `<label for="transcripts_size">` with no input beside it and no
   `.plx-slider` anywhere, which named the defect in one step. Real
   datasources for this are under `~/Library/Application Support/plexora/`.


Asset tag is now `20260919_plx_slider4` everywhere the list above names,
except the transcripts `VERSION`, which a concurrent session had already
moved to its own new value (any new value busts the cache equally); goldens regenerated
(gating swapped the markup ids `gate_min_value`/`gate_max_value` for the
container `gate_threshold_fields`, the boxes being runtime now; `route_count`
unchanged at 110/115/117/119/120/142).

Verified: **4049 passed, 1 failed, 8 skipped** on macOS/conda, run repo-wide
— the same pre-existing `test_derive_dataset_name_from_path` and nothing else.
This is the current baseline; the 4035 and the 4030 above are history
superseded by it. The
count also moved because a concurrent session was adding transcripts tests in
the same tree at the time, so treat it as approximate. Note the scope: the first
runs of this pass were `pytest tests/`, which never touched the gating plugin's
own tests although gating was the plugin most changed — see the warning at the
top of this section.

Still NOT verified in a browser: the halo growth on hover and drag, the
keyboard-only focus ring, the drag halo against `.channel-slot-detail`'s
`overflow: hidden` (it overhangs the track by 6px vertically and 11px past
each end, which `.sidebar-slider`'s padding is sized for), and Firefox
honouring `pointer-events: auto` on `::-moz-range-thumb` under an input set
to `none`.

### The contrast window collapses to one row, and Auto gets a way back

The channel contrast window went from two rows (a `.slot-detail-header`
carrying the slider's `fieldsSlot` number boxes and a bordered "Auto" text
button, with the track below) to one, `.slot-range-row`: the low number, the
track, the high number, and a 20px icon-only Auto/Revert button, in that
order. `fieldsSlot` is no longer passed by this consumer and
`.slot-detail-header` is gone from both the JS and the CSS; the slider is
built with `className: "channel-range-slider"` (see `.channel-range-slider
.plx-number` in `viewer.css`), and its two `<input type="number">`s are drawn
borderless and transparent until focused, with a fixed `--plx-number-width`
(`sizeRangeFields`, sized from the domain's digit count so the track cannot
resize mid-drag) and a blur-on-Enter built into `numberField` itself (Enter
commits, then blurs) so a committed value goes back to reading as text.

Auto is now a two-state icon rather than a one-way button. `onSlotAutoClick`
captures `slot.preAutoRange = {range, userRangeChanged, autoLeveled}` before
calling `autoChannel(i, {force: true})`, disables the icon while the GMM fit
runs, and keeps the capture only if the range actually moved; `revertSlotRange`
restores exactly that via `setSlotRange(..., false)` (so
`markerRangeOverrides` is untouched) and returns the icon to Auto;
`syncSlotAutoButton` sets its icon, tooltip and disabled state. `fa-wand-magic-
sparkles` ("Auto contrast") and `fa-rotate-left` ("Restore previous range") are
the two icons. The new slot field `preAutoRange` is cleared on a marker change
and on slot removal, and is remapped alongside `slot.range` in
`onHdModeChanged` when the HD toggle switches the domain under it. New
`tests/test_channel_auto_revert.py` (4 tests) drives
`tests/js/channel_auto_revert_probe.mjs` (9 checks; run directly with `node
tests/js/channel_auto_revert_probe.mjs`).

### Card folding animates, and the transcripts kebab moves onto the gene list (2026-09-19)

`.layer-card-body`/`.tool-card-body` no longer go `display: none` when their
card carries `.is-collapsed` — a fold the eye can watch answers, before it
finishes, the question a click on the sidebar's one-card-open-at-a-time
header just asked. Both are now `display: grid; grid-template-rows: 1fr`,
transitioning to `grid-template-rows: 0fr` plus `padding-bottom: 0` over
`--duration-base` (0ms under `prefers-reduced-motion`, so the whole thing is
one token from off) — a one-row `fr` track interpolates where `height: auto`
cannot. `overflow: hidden` sits on the wrapper and deliberately not on its
child, because for half the cards in the sidebar that child is a PLUGIN's own
root element and not core's to turn into a scroll container; the child only
gets `min-height: 0` so it can be squeezed below its own content. The child's
`visibility` still flips to `hidden` on collapse — so a folded card's controls
leave the tab order the way `display: none` used to — but the flip is DELAYED
by `--duration-base` on the way out (`transition: visibility 0s linear
var(--duration-base)`), or the body would blink out on the fold's first frame
and there would be nothing left to watch fold.

The transcripts layer section's header lost its kebab: the `data-layer-extras`
wrapper and `transcripts_menu_button` are gone from `panel.html`, and its
three actions (Create gene groups…, Reset all icons and colours to default,
Clear all genes) moved onto `transcripts_add`, the button beside the gene
search — which used to be "add the gene in the box" and could never fire,
since `addGene` clears the box on every pick. `bindStaticControls()` binds
`transcripts_add` to `openMenu` now; `bind()` no longer binds a click on it
for the old purpose. `openMenu`'s local `add()` helper takes a third
`className` argument, and the two undo-ish items carry `is-sectioned`/
`is-destructive`. The "Display" and "Appearance" block titles described just
above are gone too — a bare uppercase word over "Points | Density map" said
nothing the control itself didn't — and "Density", "Color map" and "Filtering"
now wrap their text in a `<span>` so `.transcripts-block-title::after` (a
hairline rule from the end of the heading to the panel edge) has something to
sit `order: 1` after, with the gene counter at `order: 2` past it.
`#transcripts_view_label` is gone with the "Display" heading; the display
control now carries `aria-label` instead of `aria-labelledby`. In
`transcripts.css`, the block/heading gap moved from the block to the heading:
`.transcripts-block` is 6px top (8px for the first block, under the molecule
count), `.transcripts-block-title` is 8px top and 4px bottom, and a heading
that is not its block's first child makes up the difference itself at 14px.

`transcripts_menu_button` and `transcripts_view_label` are out of the element
list in every one of the six boundary goldens. Asset tag `?v=20260919_card_fold`
on `viewer.css` in both `index.html` and figure_builder's `workspace.html`
copy; the transcripts plugin `VERSION` is `20260919_transcript_gene_menu`.

### A Z-stack collapses even without OME-XML, and re-importing rewrites in place (2026-09-19)

`tiff_series.focal_planes` now reads a new `plane_sizes(tiff)` instead of
`_ome_sizes` directly: OME-XML first, and — new — an ImageJ header's
`channels`/`slices`/`frames` second, so a single-channel Z-stack that went
through Fiji and carries no OME document at all also collapses to its middle
plane. `OME_MARKER_WINDOW` (4096 bytes, up from 512 chars) widens where the
OME marker is looked for, and `<OME` counts alongside the `openmicroscopy`
namespace URL — a long `<?xml?>` prolog or UUID attribute used to push the
marker past where this looked. The conservative rule is unchanged: a stack
naming more than one channel still reads as channels, because an Akoya/CODEX
export puts its CYCLES on the Z axis. `spatial_scene` gained
`xenium_image_note(path)` and `MORPHOLOGY_NOTES`, saying in words which of a
run's three morphology outputs a path is ("focus composite" / "maximum
projection" / "focus stack"); `import_proposal._focal_note(path)` opens the
header and says "middle of 14 focal planes" instead, on both the Xenium
morphology row and a loose image row, so which picture was chosen and which
plane was kept is visible on the import screen rather than reading as a file
Plexora half-opened. `_detect_directory` also gained a single-bundle descent:
a picked plain folder with no readable loose files now looks one level down
for a lone Xenium/Visium/SpatialData bundle (`_lone_bundle`, capped at
`BUNDLE_SCAN_LIMIT` = 200 entries) — the wrapper directory a Xenium zip
unpacks into, or a `<sample>/outs/` layout — and descends into it. EXACTLY
one: two bundles side by side are left alone, since picked paths carrying
bundles assemble into one sample and guessing between two runs would silently
merge two slides.

The "Already imported as X" banner in `importSample.js` gained a **Re-import**
button beside "Open it". `submit(replace)` now sends `replace` *and* `name` as
that sample's name, so the record is rewritten in place instead of copied
under a deduplicated name. This exists because `_find_existing` matches by
bundle root: without a way back in, a project written by an older detection
pass was the only sample those files could ever open, and a detection fix
(the four paragraphs above, or any future one) looked inert on a project that
already existed.

Asset tags bumped for `main.css` and `importSample.js`. Verified on
macOS/conda: **4049 passed, 1 failed, 8 skipped** — the one failure is the
standing `test_derive_dataset_name_from_path` Windows-path assertion. New
tests: `tests/test_tiff_hyperstack.py` (ImageJ z-stack, ImageJ hyperstack
still flattened, OME-XML past a long prolog), `tests/test_import_proposal.py`
(the morphology row's note, a z-stack row's plane count, a loose focus stack,
the wrapper folder, two runs left alone), `tests/test_spatial_scene.py`
(`xenium_image_note`), `tests/test_import_sample_routes.py` (re-import
rewrites rather than copies). The boundary goldens were regenerated in this
pass and also folded in pre-existing uncommitted layer-sections work —
`route_count` 113 -> 115.

### The gene-group dialog is redesigned, and a modal learns to host its own popups (2026-09-19)

`transcripts/static/transcriptGroupModal.js` rebuilt: one action row in a
footer whose primary button's word and enabled state come from the open pane
("Create group" / "Add selected"), replacing a floating pane action plus a
separate Done row; a header close X; tabs instead of `.cell-mode-*`; labels
above fields; a footer status line, since the dialog stays open after each
group and used to say nothing about it; Enter in the name field creates the
group; All/None on the CSV group list; tabs and panels wired with
`aria-controls`/`aria-labelledby`. `transcripts.css`'s gene-group-dialog
section was rewritten to match, and the orphaned `.transcripts-warning` rule
was deleted with the last thing that used it. The dialog no longer touches a
Bootstrap class: `.form-control`/`.btn`/`.btn-primary`/`.btn-secondary` paint
straight from the vendor bundle (a white field, a #0d6efd button, a #6c757d
one) with nothing plugin-scoped to stop them, so every control it builds now
carries a plugin class, and core's shared file-source row is toned down by
two-class-deep scoped overrides under `.transcripts-modal` — the same
arithmetic `main.css` already uses for `.channel-names-modal`.

That surfaced a gap in `PopoverPortal`: a `<dialog>` opened with
`showModal()` sits on the top layer, above the whole ordinary document, so a
popup portaled onto `<body>` from inside one opens *behind* it and no
z-index reaches over — the gene dropdown was opening behind the Create gene
groups dialog it belongs to. `popoverPortal.js` gained `modalOnTop()`
(queries `dialog[open]`, filters on `:modal`), which makes an open modal
dialog the portal host instead of `<body>`, and a `close` listener in the
**capture** phase hands the popups back once it closes (`close` does not
bubble, so a bubbling listener would never fire). Nothing fires when a
dialog *opens*, so `PopoverPortal.reseat(el)` is new too, called by
`SearchableSelect.open()` and `ColorSwatchPicker.open()` on their way up. A
modal dialog outranks a fullscreen element for this purpose. The Sharp Edges
note on `PopoverPortal.root()` still holds for `<body>`-hosted popups; this
is the case where the host is a modal dialog instead.

`tests/js/popover_portal_probe.mjs` gained section 8 for this (a modal
dialog hosts the popups while open and releases them on close; a modal
dialog outranks a fullscreen element and falls back to it). Two probe
`document` stand-ins learned `querySelectorAll` because the portal now asks
for it: `tests/js/segmentation_wait_probe.mjs` (this one was actually
missing it — three `tests/test_segmentation_wait.py` errors before the fix)
and `tests/js/cell_explorer_roi_bridge_probe.mjs` (latent: it loads the real
`popoverPortal.js` and had only passed because its `createElement` returns
null).

Asset tag `?v=20260919_gene_groups_dialog` on `popoverPortal.js`,
`searchableSelect.js` and `colorSwatchPicker.js` in `base.html`, and the same
string as the transcripts plugin's `VERSION`. Boundary goldens regenerated
for it. Verified on macOS/conda with `python -m pytest -q -p no:randomly`:
**4056 passed, 1 failed, 8 skipped** — the one failure is the standing
`test_derive_dataset_name_from_path` Windows-path assertion; the count rose
from 4053 because the three segmentation-wait tests now run instead of
erroring.

### A registered image layer gets the reference image's own channel controls (2026-09-19)

`layer_sources.py` gained per-channel `stats`/`gmm` caches on `OpenLayer` and
`window_of` (see the Repository Map entry above), plus two new routes,
`GET /generated/layer/<datasource>/<layer>/<channel>/stats` and `.../gmm` —
`route_count` 115 → 117. On the client, `layerChannelPanel.js` (new) mounts a
second scoped `ViewerSidebar` in a layer's card; `layerManager.js` gates on
`hasChannelPanel`/`channelPanelFor`; `viewerManager.js` gained
`addLayerChannelSet`/`channelSetLayer`/`seedChannelsFor` and `addTiledLayer`
took `spec.channel`/`spec.srcIdx`; `tileColorize.js` reads `source.channel`
and hard-returns for an unrecorded layer item, and `frag.glsl`/`glInit.js`
gained a `u_alpha_mode` uniform for a windowed-intensity alpha (`source-over`
tinting rather than covering) — reverted the next day along with the per-card
Add/Over control, see "The per-layer blend control is retired" below, then
brought back later that same day for a different, correct reason — a layer
that composites as a GROUP over the base rather than tinting it — see "A
registered layer composites as a group" further below. Asset
tag `?v=20260919_layer_channels` on `vendor_bundle.js`,
`layerStack.js`, `glInit.js`, `tileColorize.js`, `imageViewer.js`,
`viewerSidebar.js`, `layerChannelPanel.js` and `layerManager.js` in
`base.html`, and on `viewer.css` in `index.html` and the figure_builder
workspace template. Boundary goldens regenerated for the tag bumps and the
route count. New tests: `tests/test_sidebar_scoping.py` +
`tests/js/sidebar_scoping_probe.mjs`, `tests/test_layer_channel_panel.py` +
`tests/js/layer_channel_panel_probe.mjs`; `tests/test_layer_sources.py`
extended (stats/gmm packets, the zero-`load_datasource` invariant covering
them too, the fit cached once per channel, the window scanned once per
channel rather than once per tile); `tests/test_project_layer_routes.py`
extended (`render.channels` stored whole and surviving a later `render`
patch -- written against `blend`, rewritten to `opacity` the next day);
`tests/js/layer_visibility_probe.mjs` + `tests/test_layer_visibility.py` (20
new checks); `tests/js/layer_manager_probe.mjs` extended (panel mount-once /
rgb-skip / destroy-on-unregister, a new `ColorSwatchPicker` sandbox stub);
`tests/js/channel_auto_revert_probe.mjs`'s stand-in sidebar now provides
`slotRow` instead of `q`; `test_plugin_boundary.py`'s
`test_the_transcript_tile_route_is_core_rather_than_the_plugins` now names
the three layer routes instead of counting one. Full suite after the change:
**4132 passed, 1 failed, 8 skipped** — the one failure is the macOS baseline
`test_register_image_datasource.py::test_derive_dataset_name_from_path`.

The bundle was first rebuilt with a bare `npx webpack`, which emits an 8 MB
eval-wrapped bundle and broke `test_view_menu.py`'s grep for a Font Awesome
icon name. `npm run build` is the command (see the Frontend build line above),
and that test is what notices when it is not.

### The per-layer blend control is retired, and channel rename reaches a registered layer (2026-09-20)

The Add/Over control the previous entry gave every raster card was a bug, not
a feature: `source-over` between a layer's OWN channel items meant the topmost
channel with signal won, so a multichannel layer showed one channel at a time
instead of blending its enabled channels the way the reference image does.
`layerStack.js`'s `defaultBlendFor` is deleted (it was the one place the
default was decided, so there was nowhere else to fix it); `layerManager.js`'s
`buildBlendRow` and `imageKind()` and `restyle`'s `change.blend` branch go with
it, along with the now-dead `.layer-card-row > .cell-mode-control` /
`.layer-card-row .cell-mode-option` rules in `viewer.css`. `render.blend` is no
longer written or read anywhere. `viewerManager.blendOperation` is now just
`channelSetLayer(spec) ? "lighter" : "source-over"` — a layer whose planes are
channels composites exactly as the reference image's channels do, an rgb layer
draws `source-over`; where a layer sits relative to what is under it is stack
order and opacity, not a composite operation, so there is no longer a user
choice here at all. `addTiledLayer`'s handle keeps `setBlend` (the transcripts
plugin's density raster still calls it); `LayerChannelSet`'s, added a day
earlier, is deleted -- a channel set has no blend to set.
The `u_alpha_mode` uniform the previous entry added is fully reverted along
with it: `frag.glsl` and `glInit.js` are byte-identical to HEAD again, and
`tileColorize.js`'s tile signature is back to
`` `${e.tile.cacheKey}|${tileFmt}|${floatColor}|${range}|${modes.edge},${modes.or}` ``
with no alpha-mode term. **All of this — `viewerManager.blendOperation`,
`u_alpha_mode`'s absence, and the tile signature above — held only for the
rest of that morning.** A `lighter` layer additively washing out into a
bright base was still visible and its sliders still read as dead, so later
the same day the layer was made to composite as a GROUP instead:
`blendOperation` is deleted outright (not left as `"lighter"`/`"source-over"`),
`u_alpha_mode` comes back for a different reason, and the signature gains an
`alphaMode` term again — see "A registered layer composites as a group"
further below, which supersedes the two paragraphs above.

Separately, "Upload channel names" now works for a registered layer rather
than only the reference image: `datasource.py` gained
`rename_layer_channels(name, layer_id, channel_names, data_dir=None)` beside
`rename_channels`, which rewrites each layer channel's `name`/`fullname` and
deliberately leaves `src` alone — a layer channel's tile address is
`/generated/layer/<sample>/<layer>/<key>/` and `data_model._parse_channel`
reads the plane number off the end of `key`, so the rename moves no address
and no index, which is what lets `render.channels` (index-resolved) survive
one untouched. `POST /upload_channels` takes an optional `layer` form field;
with it the channel count is the layer's, the rename goes through
`rename_layer_channels` instead of `rename_saved_channels`, and there is no
`load_datasource(reload=True)` on that path (404 for an unknown layer; no new
route, `route_count` unchanged). `channelNamesUpload.js`'s `open()` takes
`layer` and `label` now (a new `title()` puts the label in the dialog
heading), and `layerChannelPanel.js`'s `mount()` takes a `rename` callback
that gates a new `.layer-card-actions` upload-icon row in `buildMarkup`.
`layerManager.js` gained `renameChannels(id)` (opens the dialog) and
`adoptChannelNames(id, names)` (writes the names onto `layer.spec.channels` —
the same object as the `config.layers` entry — drops the panel and world
items, re-syncs, then `render()`); both resolve the layer through
`stack.get(id)` at call time. `main.js`'s own `adoptChannelNames` is unchanged
and still owns the reference-image case.

Asset tag `?v=20260920_layer_channels` on `vendor_bundle.js`, `layerStack.js`,
`tileColorize.js`, `imageViewer.js`, `viewerSidebar.js`, `layerChannelPanel.js`,
`layerManager.js` and `channelNamesUpload.js`. Bundle rebuilt with `npm run
build` (not `npx webpack`, see above). Boundary goldens regenerated and
diffed: only `?v=` tags changed, no route count change. New tests:
`tests/test_channel_names_upload.py` gained a layer section (6 tests, helpers
`_with_layer`/`_layer_channels`); `tests/js/layer_manager_probe.mjs` gained a
rename block; `tests/js/layer_visibility_probe.mjs`'s blend checks were
rewritten and `tests/test_layer_visibility.py`'s CHECKS list updated;
`tests/test_project_layer_routes.py`'s two blend-keyed tests now use
`opacity` instead. Full suite: **4139 passed, 1 failed, 8 skipped** — the one
failure is the same standing macOS baseline
`test_register_image_datasource.py::test_derive_dataset_name_from_path`.

### A registered layer composites as a group, not additively (2026-09-20, later the same day)

`lighter` on every channel of a registered layer meant a fluorescence layer
ADDED into a bright H&E and saturated to white — invisible, with its sliders
reading as dead — and stack order could not mean anything, because addition
is commutative. OpenSeadragon composites every world item straight onto one
canvas; there is no group. So the result is now said with TWO world items per
channel instead of one: a COVER blit (`destination-out`) that takes the
picture underneath away in proportion to that channel's coverage, and a PAINT
blit (`lighter`) that adds the channel's colour exactly as the reference
image's own channels do. Together: `base · Π(1 − vᵢ) + Σ cᵢ · vᵢ`. Channels
still mix among themselves (red + green still reads yellow); the layer as a
whole now occludes the base the way a group would. `viewerManager.js`'s
`COVER_OPERATION`/`PAINT_OPERATION`/`COVER_Z`/`PAINT_Z` name the pair;
`addLayerChannelSet` builds both items per channel (`entries` is now
`name -> {handles: [cover, paint], record}`) and `blendOperation` — the
helper the entry above gave a one-line body — is deleted outright, because
there is no longer one operation per layer to return: an rgb layer's
`"source-over"` is inline at the `addTiledLayer` call site in
`syncLayerImages`, and a channel set's pair is `addLayerChannelSet`'s alone.

Ordering the pair correctly needed a second axis inside the stack's existing
one: `views/layerStack.js` gained `ITEM_Z` (exported, `"_plexoraItemZ"`), a
property a world item may carry for where it sits WITHIN its layer, and
`applyWorldOrder` now sorts by `(rank, z, current index)` rather than
`(rank, index)` — insertion order alone cannot keep every cover blit below
every paint blit, because the HD toggle re-adds both halves of a pair
asynchronously and they can land either way round.
`ViewerManager.claimWorldItem(layerId, item, z)` takes the third argument
and sets it.

The cover blit reads coverage out of the tile's own alpha, which used to be a
constant. `frag.glsl` gained back `uniform int u_alpha_mode` (plumbed as
`alpha_mode_1i` through `gl_arguments` into `glInit.js`'s `gl-drawing`/
`gl-loaded` uniform upload) and a `float tile_alpha(float opaque, float
coverage)` helper: mode 0, the reference image, is byte-identical to before
(a constant 0.9 alpha over a black-filled tile canvas); mode 1, a registered
layer's channel, returns the channel's own windowed intensity as alpha.
**Superseded by "The reference image is no longer a special kind of picture"
further below: the reference image's own channels became a cover/paint pair
too, so mode 0 is now dead code and `alphaMode` no longer reads
`source.channel` at all.** This is the SAME uniform name the "channel
controls" entry above added on 2026-09-19 for the retired Add/Over control
and the entry directly above this one reverted that same morning — brought
back now for a different and correct reason, and not a user-facing choice
either time. `tileColorize.js` decided the mode with `const alphaMode =
source.channel ? 1 : 0;` at the time of this entry, folds it into the
per-tile `sig` (so a mode change is not mistaken for a repeat frame), and a
new module-level `clearOrBlack(rendered, alphaMode, w, h)`
clears a layer's tile canvas instead of filling it black — black is opaque,
and the cover blit would read an opaque tile as full coverage and erase the
whole tile footprint before any layer pixels exist. The missing-array
fallback path clears rather than black-fills for a layer tile for the same
reason.

Both items of a pair address the same tile url, so OpenSeadragon still gives
them one fetch, one decode and one shared cache record — the pair costs one
extra blit, nothing more. `tileDecode.js` gained `shareDecoded(tile)`,
called from a `finally` in the tile-loaded handler, which leaves the decoded
plane on the tile's OSD CACHE record (`cache._plexoraArray`/
`cache._plexoraFormat`) as well as on the `Tile`: OpenSeadragon raises
`tile-loaded` with a request for only whichever of the pair's two `Tile`s
asked first, so the second one never passes through the decoder and would
have no `_array` of its own without this. `tileColorize.js` reads the shared
copy back when a tile item's own `_array` is missing.

New `tests/js/tile_colorize_probe.mjs` + `tests/test_tile_colorize.py` (12
named checks, same wrapper pattern as `tests/test_layer_visibility.py` — a
Python fixture runs the probe under node and parametrizes one test per
printed check name, so a check quietly deleted from the probe fails in
Python rather than passing silently). `tests/js/layer_stack_probe.mjs`
gained three checks for `ITEM_Z` ordering, and `tests/test_layer_visibility.py`'s
CHECKS list was regenerated (52 entries).

### The reference image is no longer a special kind of picture

The single fact blocking both "let the reference image sit on something
other than black" and "let the reference image be dragged above a registered
layer" was the same one: `channel_add` drew each reference channel as ONE
opaque `lighter` blit, which only ever worked because black adds nothing and
because nothing was ever underneath it. `channel_add` now draws the
cover/paint pair `addLayerChannelSet` already drew for a registered layer's
channels (`coverageAlpha: true`, `layerId: REFERENCE_LAYER_ID`); against a
black ground the pair is pixel for pixel what it drew before.
`channel_remove` now removes every item a channel drew, not the first found
(it used to `break`, which with a pair left half the channel behind).
`tileColorize.js`'s `alphaMode` reads `source.coverageAlpha` off the tile
source instead of inferring it from `source.channel` being present, because
that inference silently stopped covering the reference image's own channels
the moment they became a pair too. See "The Rendering Pipeline" and the
`views/viewerManager.js`/`views/tileColorize.js` entries above for the
mechanics; this entry is the "why now."

That one change let two more land:

**Reordering.** `Project.image_depth` (`imageDepth`) is how many registered
layers `all_layers` draws beneath the reference image — 0, the ground,
unless the user has dragged its card up the stack. `with_layer_order` is the
one place a reserved id (`__image__`) is now accepted, once, keeping a depth
for it rather than a position since it is not one of `spatial_layers`.
`PUT /project/<name>/layers/order` returns the resulting `imageDepth`;
`PATCH /project/<name>/layers/<id>` accepts `__image__` for `render` only
(never `visible`) and stores it as `Project.image_render`. On the client,
`layerManager.js`'s base card lost its `lockFixed` padlock and its
sorted-to-the-foot/hoisted-to-the-front treatment (`cardedLayers`,
`syncOrder`, `ensureSortable`'s `onMove`) — it drags and locks like any other
card now — and `isUnstorable(id)` (mask and centroids only) replaces
`isReserved(id)` (image, mask and centroids) at the two places that decide
what gets written to the server, because two of the three synthesized layers
now have a stored fact of their own. `imageViewer.js`'s new `referenceItem()`
resolves the anchor through `layerStack.anchorIndex()` instead of assuming
`world.getItemAt(0)`, because item 0 is no longer guaranteed to be the
reference and a registered layer at that index carries its own affine.

**Per-layer background.** `Project.image_render` also carries the ground the
reference image's channels composite onto (merged into `reference_layer.render`
with `imageKind` last, so it cannot be overwritten). `viewerManager.js`'s
`applyViewerGround(hex)` sets `--plexora-viewer-ground` on `#openseadragon`
for the reference case, because the reference is the scene's frame and the
ground under it is the canvas's; `addLayerGround(spec)` draws a registered
layer's ground as its own one-pixel world item at `GROUND_Z` (below both
halves of every channel), because a layer's ground has to be the same shape
as the layer rather than the whole canvas. `viewer.css`'s
`#openseadragon`/`#openseadragon.brightfield-ground` read the variable with
the old per-kind literal (black / `#fbfbfc`) as the fallback. Every image
card grew a `.layer-card-ground` dot (`ColorSwatchPicker`), drawn as one of
the card header's own buttons rather than a bare swatch; a registered
layer's popover offers "None" (`NO_GROUND`, the default — "whatever is
underneath"), the reference image's offers "Default" (`DEFAULT_GROUND`)
instead, because the reference has no underneath to fall back to except
whatever `viewer.css` picks. Both are `hex: "transparent"` and both send
`null` to the PATCH route to remove the key; `setGround` re-reads
`groundValue(layer)` into the picker afterward so the dot keeps showing the
actual canvas colour rather than the slash. The palette itself is rebuilt
per open from `groundPresets(base)` — a slash, black, white, then
`ColorSwatchPicker.DEFAULT_PRESETS` filtered against those three so nothing
repeats — with `GROUND_COLUMNS` (4) handed to the popover as
`--ground-columns` so the grid comes out a whole number of rows; the
bespoke greys this used to carry are gone.

New/changed tests: `tests/test_layer_spec.py` (depth and `imageRender`),
`tests/test_project_layer_routes.py` (the `__image__` exception on both
routes), `tests/js/layer_visibility_probe.mjs` and
`tests/js/layer_manager_probe.mjs` (ground checks, the inverted base-card
drag/lock expectations, a real base-card reorder check, a document/canvas
stub for `groundTileUrl`), `tests/js/tile_colorize_probe.mjs` (coverage vs.
opaque alpha), and the CHECKS mirror lists in `tests/test_layer_visibility.py`
and `tests/test_tile_colorize.py` regenerated to match, plus
`tests/js/layer_state_probe.mjs` and `tests/test_layer_state.py` (the base
layer is a pair per channel, and the composite operation is asserted off the
`addTiledImage` OPTIONS — see the sharp edge). `tests/golden/boundary_*.json`
regenerated for the asset-tag bump.

Full suite after all of it: **4186 passed, 1 failed, 8 skipped** — the one
failure being the macOS baseline `test_derive_dataset_name_from_path`, which
asserts on a Windows path. Verified live in Chrome against a brightfield
project with a multiplex layer and a fluorescence project with an H&E layer;
the reference image's own channels are exercised by `channel_add`, which no
probe drives end to end.

### A registered layer's opacity line matches the reference card's, always (2026-09-20, later still)

`layerChannelPanel.js`'s `buildMarkup` now builds the panel's
`.layer-card-actions` row (marked `data-layer-opacity-slot`) as the panel's
first child unconditionally, not only when a `rename` callback is supplied —
the "Upload channel names" button still needs one, but now goes inside that
always-present row. `layerManager.js`'s `buildBody` mounts the channel panel
first and looks for that slot in `panel.node`; when found, the compact
`buildOpacityControl` goes first on it, exactly as `buildBaseBody` already
does for the reference card, so a multiplexed image imported as a layer now
carries the same opacity control and "Upload channel names" button on one
line as the reference card. Only a card with no such slot (an rgb layer, a
build without the panel module) still gets the plain `buildOpacityRow`. A new
module `opacities` Map (id -> `{trigger, destroy}`) beside `panels` and
`grounds` holds the control across rebuilds — `refreshBody` moves the
existing trigger back rather than rebuilding it — and `dropChannelPanel(id)`
calls its `destroy` (closes the popover, `PopoverPortal.detach`s it, drops the
`opacityReadouts` entry and the map entry) on a rename rebuild or a layer
removal, so the popover comes off the portal rather than being orphaned
there.

Asset tag `?v=20260920_layer_opacity_line` on `layerManager.js` and
`layerChannelPanel.js` in `base.html`; `viewer.css` went to the same tag and
then, in the same session, to `?v=20260920_alignment_muted` in `index.html`
and `figure_builder/templates/figure_builder/workspace.html`, when
`.layer-card-alignment.is-warn` ("Aligned by assumption") was changed from
`--accent-warning` to `--text-muted` -- the words carry the caution, and amber
on every freshly added layer read as a fault. The five boundary goldens were
regenerated for both bumps (`route_count` unchanged).
`tests/js/layer_manager_probe.mjs` gained a 6-check block (100 checks total)
covering the shared trigger, its position ahead of "Upload channel names",
that the card grows no opacity row of its own, that a rebuilt card keeps the
same control rather than a second one, and that a layer leaving the stack
takes its opacity popover off the portal; it had no pytest driver until
`tests/test_layer_manager.py` (2026-09-24, added for the `•••` menu below),
which pins the menu checks by name rather than the full count — run
`node tests/js/layer_manager_probe.mjs` directly for everything else in it.

### The import dialog's `pick` and `proposal` states are redesigned (2026-09-20)

`views/importSample.js`: the header gained a `?` help button and a per-phase
subtitle (`paintSubtitle()`); `pick`'s where-row gained a "Data location"
kicker and a "this computer" caption around the unchanged **L | R** switch,
which is the home page's own arrangement (`quick-view-where-label` /
`-caption`) reused so the one control reads the same on both surfaces that
give it a row; the split control's labels are now "Choose a file" /
"Choose a folder"; the old `.plx-import-drop-title` heading and the
duplicated 11px formats list are gone, replaced by one formats sentence below
the panel ending in an "All formats" link. `proposal` is now one CARD per
sample with a right-ranged role badge per row (`ROLE_BADGE`), a question as
an attached callout that gains `.is-unanswered` styling rather than changing
what blocks Import (`part("go").disabled` is still driven only by the picks),
and a `.plx-import-blocked` reason span beside the primary button. `importing`
now draws the shared `connect-steps` rail instead of bare "waiting…" text.
New module `views/importHelp.js` (`window.PlexoraImportHelp`) is a second
`<dialog>` with Overview/Formats/Examples/Good-to-know tabs built from
`FORMATS`/`QUESTIONS`/`EXAMPLES`/`NOTES`, which carry the server's own
modality/bundle/question strings so a new format needs an entry there and
nothing in `importSample.js`. It is hosted BESIDE the import dialog
(`hostFor(from)` → the opening button's own `<dialog>`'s parent) and is
deliberately **not** given to `PopoverPortal`: that service moves everything
it hosts into whichever modal is topmost on every `close` and every
fullscreen change, and with this dialog both portaled and on top, that rule
resolves to appending the import dialog into this one. Staying out of the
portal costs nothing — a sibling modal still enters the top layer above the
one already open — and it also puts this dialog outside the subtree
`importSample`'s `part(role)` searches. The same modal is reachable from the home page
(`#quick_view_help`, bound in `views/quickViewLanding.js`): that page asks the
dialog's own first question and answering it from one catalogue is what stops
the two from drifting. It is the third line of that page's footer paragraph
("Not sure what Plexora reads? ⓘ What's supported"), beside the two ways out
already there, and NOT an icon in the card's corner — the page is one centred
vertical run and a corner-anchored glyph is the only thing in it that belongs
to no line.
`dataLocation.js` itself is unchanged — an earlier draft of this work gave
`attach()` a words-labels option for the dialog's row and it was reverted, so
the switch has one render everywhere.

`main.css` gained `.plx-button-primary:disabled` (every dialog's disabled
primary had carried the accent fill and looked pressable) and the new
`.plx-import-*`/`.plx-help-*` rules; none of this went into `import.css`.
Asset tags on `main.css`, `dataLocation.js`, `importSample.js` and the new
`importHelp.js` moved to `?v=20260920_import_redesign` in `base.html`, and
`quickView.css`/`quickViewLanding.js` to the same in `index.html`; the
boundary goldens were regenerated for the bump.

New/changed tests: `tests/test_import_help.py` (new — the `FORMATS` catalogue
against `import_proposal.py`'s own vocabulary, the main.css-not-import.css
pairing, the `data-role` collision guard between the two dialogs, and that
each has its own `cancel` listener, that both surfaces can reach it, and that
`.quick-view-help` is styled by the page's own sheet rather than main.css).
`tests/js/data_location_probe.mjs` and `tests/test_data_location.py` are
unchanged, the words-labels option having been reverted.

### The ROI panel grows a tree, and regions get their own eye (2026-09-20)

New file `plugins/roi/static/roiTree.js` (`class RoiTree`), added to
`PLUGIN.scripts` between `roiTools.js` and `roiSidebarController.js`. It owns
the Category → ROI tree and is reconciled BY ID into two `Map`s, never
rebuilt: `RoiStore.setStatus()` calls `changed()`, so the 400 ms autosave
repaints the panel, and a rebuilt list would take an inline rename input out
from under the caret mid-word. `RoiTree.place()` inserts a node only when it
is out of position, because re-appending a node that is already in place
still moves it and blurs whatever is focused inside. Its constructor touches
no DOM, so the boot probe can build a controller against a bare context. It
also carries plugin-local `RoiTree.popup`/`menu`/`closePopup`
(`PopoverPortal` + fixed positioning + a document click and Escape listener
in the CAPTURE phase, so a menu's Escape does not also deselect) — there is
no core primitive for a positioned popup.

Regions gained a per-ROI `visible`, defaulted `True` by
`schema.normalize_feature` and added to `FEATURE_FIELDS`, so `normalize_state`
running on every load is the only migration a blob written before the flag
existed needs. `operations._roi_update_properties` accepts `visible` and is
NOT gated on the lock — the same footing as rename, because hiding is a
viewing aid and not a property of the region. GeoJSON export and import round
-trip `properties.visible`. On the client, `RoiStore.isVisible(feature)` is
now the region's own flag AND its category's, and `visibleFeatures()` remains
the ONE list feeding the renderer, hit test and hover. A hidden ROI is still
mapped to cells and still written to every export — hiding never changes what
exists, only what is drawn. The AnnData `uns` blob carries the flag; the
SpatialData shapes table does not get a column for it.

The panel itself: `[data-tool-extras]` is gone from ROI (see "Loaded tools
are cards, and cards are layers" above) — Import/Export/Save/Map to cells/?
moved into the panel body as an `#roi_actions` pill row, ROI-owned
`.roi-action` classes copying `.layer-card-action`'s values, gaining
`is-compact` when Map to cells is shown. Export now hides only when nothing
is drawn, not by a native destination. Ids removed: `roi_category_add`,
`roi_category_add_button`, `roi_category_empty`, `roi_export_download`, and
the whole `roi_selection_*` set. Ids added: `roi_help_button`,
`roi_category_new`, `roi_category_new_row`, `roi_actions`.

`RoiInteraction` now constructs with `tool="freehand"`,
`state="drawing.freehand"` — set in the constructor, not by `setTool` in
`onShow()`, because `setTool` would scold an empty project on every open.
`arm()` re-derives the state, since disarm leaves `"idle.select"` behind.
`nextName()` is max-existing-number + 1, not count + 1, so a default name
never reuses one freed by a deleted region. New `deleteFeature(feature)`;
`deleteSelected()` delegates to it.

A CSS gotcha worth keeping: an author `display` outranks the UA sheet's
`[hidden] { display: none }`. `.roi-banner`, `.roi-action`,
`.roi-new-category` and `.roi-children` each needed an explicit `[hidden]`
rule — the `.roi-banner` one was a live bug, both banners standing on every
panel until it was added.

`VERSION` is now `"20260920_roi_hierarchy"`;
`tests/golden/boundary_roi.json` was regenerated for it. New/changed tests:
the boot probe now expects
`{"tool": "freehand", "state": "drawing.freehand", ...}`; new interaction
probes ("the pen is already in hand", "default names take the next FREE
number"); a new state probe ("hiding a region"); new mutation partners in
`test_roi_client_js.py` (a panel that opens on Select, a name that reuses a
deleted number, a region's own eye being ignored); and new Python tests
across `test_roi_operations`, `test_roi_geojson`, `test_roi_mapping`,
`test_roi_routes`, `test_roi_repository` and `test_roi_adapters`. Scoped run,
verified: `pytest plexora/plugins/roi/tests` = **221 passed**.

### Walk a dataset without losing the viewer you arranged (2026-09-21)

New client files: `services/carryOver.js`, `views/datasetNav.js`,
`services/toast.js`, `views/viewerErrorState.js` -- see the Repository Map.
`main.js` gained `reportImageFailure()`, `restoreCarriedState()` (run from
`__plexoraReady`'s own `.then()` continuation, never inside `init()`),
`restoreCarriedCells`/`restoreCarriedLayers`, and the HD-carry block right
after `ImageViewer.init()`. `toolLoader.js` gained `loadTool()` (extracted
from `openTool`), `snapshot()` and `restore()`. `viewerManager.js` gained
`presetHdMode()`. `viewerSidebar.js` gained `carriedChannels()`,
`whenModulesApplied()`, and the `slot.autoSilent` fix for a pre-existing leak
where `applyAutoRange` persisted a launch/carried restore about a second
after boot. `pluginRegistry.js` documents the two optional hooks; gating,
cell_explorer and transcripts implement them. `data_model.py` gained
`image_status`/`classify_image_error` behind the new `GET /image_status`
(`data_routes.py`).

New tests: `tests/js/{carry_over,dataset_nav,toast,viewer_error_state}_probe.mjs`
(23/21/13/16 checks) with pytest wrappers
`tests/test_{carry_over,dataset_nav,toast,viewer_error_state}.py`, and
`tests/test_image_status.py` (22 passed, 1 skipped on Windows -- the file-lock
sharp edge below). `main.css` and `viewer.css` asset tags were bumped for the
toast host and the nav chip/error card, and the boundary goldens regenerated.

Full suite on Windows/conda, `python -m pytest -q -p no:randomly`:
**4441 passed, 7 failed, 3 skipped**. All 7 failures are
`tests/test_boundary_mask.py` on `ModuleNotFoundError: No module named
'cv2'` -- `opencv-python-headless` is a declared core dependency this conda
env predates, a stale env rather than a regression (see the Python-env note
in Validation above).

### A node-served mask retries itself, and a wrong-level tile is a stride not a full-res read

`node/api.py`'s `POST /resources/<id>/prepare`, `_label_region`'s
strided-sample fix, `seg_tile` no longer behind `_ready`, and
`/resource_status`'s `masks` (see the node API and `/resource_status` rows
above) added `tests/test_node_mask_status.py` and `tests/test_label_region.py`.
Not reverified against a fresh full-suite run at the time of writing --
the 4441/7/3 baseline above is the last confirmed number.

Core's Rotate and Flip (`server/core_tools.py`, `plugins.tools`,
`services/viewTransform.js`, `views/viewTransformTools.js`, the
`/view_transform/<datasource>` route and `database_model.ViewTransform`)
added `tests/test_core_tools.py`, `tests/test_view_transform.py`,
`tests/test_view_transform_routes.py` and the probes
`tests/js/view_transform_probe.mjs`, `view_transform_tools_probe.mjs` and
`overlay_transform_probe.mjs`; `tests/test_view_menu.py` was rewritten for the
View menu that now holds Rotate/Flip/Scalebar instead of Sidebar/Cells/HD, and
the boundary goldens were regenerated for the new asset tags. Full suite on
Windows/conda: **4846 passed, 1 failed, 3 skipped** — the 1 failure has since
been fixed.

Figure Builder capturing a panel on a turned or mirrored view (`viewport.
orientation`, `schema.normalize_viewport`/`normalize_orientation`/
`frame_size`, the oriented render path in `render.py`, and the capture
tool/compositor/Quick Edit/scene-snapshot changes that read `frameSize`
instead of `viewport.w`/`h` -- see Key Invariants) added
`plexora/plugins/figure_builder/tests/test_figure_builder_orientation.py`
and `tests/js/figure_orientation_probe.mjs`, bumped the plugin `VERSION` to
`20260926_figure_orientation`, and regenerated
`tests/golden/boundary_figure_builder.json` for it. Full suite:
**4873 passed, 0 failed, 3 skipped** -- zero known failures, not just this
change's tests passing.

Opening an OME-Zarr image, a label image, or an AnnData/SpatialData table
directly from a web address (`server/utils/remote_store.py`'s chunk cache,
`server/models/remote_sources.py`'s address book and warm/pin jobs,
`providers/remote.py`, `client/src/js/views/locators.js`) added
`tests/remote_fixtures.py` (a real `ThreadingHTTPServer` with gateway,
forbidden and listing modes, a request log, and an `outage()` context manager
that flips it unreachable mid-test), `tests/test_remote_chunk_cache.py`,
`tests/test_remote_discovery.py`, `tests/test_remote_image_provider.py`,
`tests/test_remote_image_status.py`, `tests/test_remote_jobs.py`,
`tests/test_remote_labels.py`, `tests/test_remote_locator.py`,
`tests/test_remote_tables.py` and `tests/test_settings_webdata_page.py`; the
root `conftest.py` gained an autouse `_no_remote_warm` (stubs
`remote_sources.start_warm` so an unrelated test opening a remote fixture does
not spawn a background fetch) and `_forget_remote_stores` (clears the
in-process store cache between tests, the same reason `_disconnected`/
`_addresses` are cleared for nodes). Asset tag `20260926_remote_import`
(`locators.js`, `importSample.js`, `main.css`) and the boundary goldens were
regenerated for it. Not reverified against a fresh full-suite run at the time
of writing -- **4873 passed, 0 failed, 3 skipped** above is the last confirmed
number. **A git worktree has no `client/node_modules`** (gitignored) -- the
JS layer probes (`tests/js/*_probe.mjs`) that `require()` a client dependency
need it; symlinking it in from a tree where `npm install` has run is the
fix, not a fresh install per worktree.

**The gene tree and its group dialog become core's, so Visium HD gets one
too, and its heatmap learns to aggregate (feature/visium-hd-sidebar).** New
`views/geneList.js` (`PlexoraGeneGroups`, `PlexoraGeneTree`,
`PlexoraGeneVocabulary`) and `views/geneGroupModal.js` (moved here from
`transcripts/static/transcriptGroupModal.js`) hold the selected-genes tree,
its groups and its CSV/remote group-file dialog for BOTH the Transcripts
layer and the Visium HD bin layer -- see the Repository Map entries above.
New `server/utils/gene_groups.py` backs both plugins' `POST
/plugins/<name>/groups`, each still supplying its own vocabulary.
`SearchableSelect` gained a `match:` option (`PlexoraGeneVocabulary.match`,
capped at 50) that replaces its substring filter for an 18,000-gene Visium
HD run; `PlexoraMenu` items gained `checked`/`title` (a radio row,
`.is-checkable`/`.is-checked`), and `.plx-menu-item.is-destructive` moved
from `transcripts.css` into `main.css` since Visium HD's list menu needed it
too. The Visium HD panel dropped its own legend, "All genes (UMI)" row,
helper texts and hover readout -- the gene tree already says the same things
-- and `BinLayer` gained `groups`/`collapsed` state, `agg` (default
`"mean"`), `AGGREGATIONS`, `aggregates()` and `setAggregation()` (restyles
the same tiled world item immediately, no debounce, since a menu pick is one
click and not a drag to coalesce). The heatmap's choice is a labelled
**Combine** row under Scale (`#vhd_agg_row`, four `data-vhd-agg` buttons
built by the controller's `paintAggregation`), shown whenever the ramp is,
and disabled -- not hidden -- with fewer than two genes drawn. (It was first
an unlabelled `fa-layer-group` glyph in the gradient bar's `extras` that only
appeared with two genes, and read as the feature having been removed.) The
colour bar's extent is in COUNTS (0 .. the `/stats` ceiling), converted back
to the `dlo`/`dhi` fractions on release: core's gradient control prints its
two typeable ends in the extent's own units and never the caller's `format`,
so an extent of 0..1 showed "0.000 / 1.000". `PlexoraGradientRange.render`
takes an optional `decimals` for those ends. Server-side, `bin_tiles.py` gained the matching
`AGGREGATIONS`/`aggregation`/`aggregate`, and `ramp_tile`'s new `how=`
aggregates raw counts before the window is applied and aggregates the genes'
own auto-windows the same way; `layer_sources.parse_style` reads the style's
new `agg` key and `_style_key` folds it into the tile ETag.

Separately, `data_model._local_thumbnail_plane` was fixed to pick a level by
SPATIAL dims rather than assuming `(channel, y, x)` -- the bug that drew a
Visium HD run's H&E card as one 56-byte row of pixels -- and gained `rgb=`
for a brightfield project's card to render in colour; the thumbnail cache
filename was bumped to `.thumbnail-v2.webp` (`_STALE_THUMBNAIL_NAMES` cleans
up the old one) since the change means every existing cached card is wrong.
New `tests/test_thumbnail_rgb.py`; aggregation tests added to
`tests/test_bin_tiles.py` and `tests/test_layer_sources.py`; the goldens and
probes above regenerated, and `tests/js/visium_hd_layer_probe.mjs` and
`transcript_points_probe.mjs` now preload `geneList.js`. A full-suite run on
macOS (test-runner, 2026-09-26) was 4801 passed, 2 failed, 8 skipped -- the
two failures both baseline. One test not caused by this change and not new
to it, `tests/test_import_entry_points.py::
test_a_mask_lands_the_same_wherever_it_was_attached`, fails on a clean
macOS/conda checkout of `main` as well as here -- add it to the standing
macOS baseline above when next confirming a count; it is not evidence this
change broke anything.

> **Continued on 2026-09-24: a Composition glyph mode, and the colour scale
> stops moving on zoom.** `bin_tiles.py` split `requested_pooling(requested)`
> (the square asked for, floored to a power of two) out of
> `effective_pooling`, and `rgb_tile`/`ramp_tile` both take `scale_pooling=`:
> the colour window is now measured at the REQUESTED square rather than
> whatever a coarse level happens to draw, `_scaled_fields` dividing pooled
> sums by `(pooling/scale_pooling)^2` to put them back on that scale first --
> the invariant is that the same count reads the same colour at every zoom,
> zoom changing only which squares get merged for drawing. `_alpha_grid`
> gained a matching rule for the gutter ink: once a square is one tile pixel
> and there is no strip left to draw, every pixel pays the strip's average
> cost (`GUTTER_MEAN_ALPHA`) instead of none, so a ramp colour over the H&E
> no longer changes shade between levels. `layer_sources._bins_tile` passes
> `scale_pooling=requested`, and `plugins/visium_hd/server/routes.py`'s
> `/stats` normalises its own `bin` argument through the same
> `requested_pooling`, so the legend's numbers are the window every zoom
> level stretches against.
>
> New drawing path, COMPOSITION: `bin_tiles.parse_components`/
> `composition_shares`/`_paint_glyphs`/`composition_tile` turn a `comp=`
> string (indices into `genes=`, `0|1|2:mean,3`) into one glyph per square --
> a two-level squarified treemap (`_squarify`): a cell per component sized by
> its share of raw counts, and a group's cell squarified again by its
> members' own counts regardless of `how` (first drawn as slice-and-dice
> strips; replaced at the user's request with a treemap like
> advsofteng.com's simpletreemap.png) --
> painted pixel-exact above `COMPOSITION_MIN_GLYPH_PX` and collapsed to the
> dominant gene's colour below it. It needs no window (shares are ratios of
> counts) and is always exact PNG (`data_model.encode_tile_array(...,
> lossless=True)`, new `lossless=` parameter -- lossy WebP would smear the
> glyph's hard edges into colours no gene has). `layer_sources.parse_style`
> gained the `comp` key and a `_composition_tile` helper that resolves
> `genes=` positionally against the store and drops anything the store does
> not hold; a malformed `comp` is a new `layer_sources.BadStyle(ValueError)`,
> which `data_routes.generate_layer_tile` now catches into an HTTP 400 (not a
> 404, which a layer card reads as "Preparing…", and not a cached-a-year
> fallback picture) -- and the same route now actually forwards `agg`, which
> had reached `parse_style` since the aggregation work above but nothing read
> back out, so Mean/Sum/Max/Min had had no effect on a heatmap until now.
>
> Client: `binLayer.js`'s old blended `"composite"` mode (each gene its own
> colour, additively) is GONE, replaced by the Composition glyph under the
> same mode id (kept so saved panel state still loads) -- `GENE_COLOURS` is
> gone with it. Groups now carry `agg` (`PlexoraGeneGroups.create(state,
> name, {agg})`, `groupAggregation`/`setGroupAggregation`), `composition()`
> orders the glyph's parts as the tree paints them -- every group in state
> order (visible members only), then ungrouped genes in selection order --
> and `compositionParam()` renders that as `comp=`. `COMPONENT_SOFT_CAP = 4`:
> past it `tooManyComponents()` is true and the panel shows a warning row
> (`#vhd_comp_warning`) rather than refusing to draw. The composite tile's
> `styleUrl` now names only what the picture depends on -- genes, colours,
> grouping, square size -- no ramp, window, log or panel aggregation, since a
> key that did not change the picture would still be a new url and a
> viewport refetched for nothing. `views/geneList.js`'s `PlexoraGeneTree`
> gained `options.groupExtras(group) -> [nodes]`, the group-heading twin of
> `rowExtras`, for Visium HD's per-group `vhd-agg-button--group`; `normalize`
> now keeps a group's existing extra fields (`{...group, name, genes}`)
> rather than rebuilding the object, so `agg` survives a reload.
>
> New test coverage: `tests/test_bin_tiles.py` and `tests/test_layer_sources.py`
> both grew composition/scale-pooling cases, `plugins/visium_hd/tests/
> test_visium_hd_routes.py` covers the normalised `/stats`, and
> `tests/js/visium_hd_layer_probe.mjs` grew substantially for the glyph mode.
> All six `tests/golden/boundary_*.json` regenerated for the asset-tag bump;
> `geneList.js` and the `visium_hd` plugin `VERSION` are both now
> `20260927_visium_composition`. No fresh full-suite count taken after this
> pass -- confirm one before relying on the pass/fail numbers above.

> **Continued on 2026-09-25: the below-glyph square is dithered, not
> flattened.** `bin_tiles.composition_tile` no longer collapses a
> sub-`COMPOSITION_MIN_GLYPH_PX` square to its single largest-share gene's
> colour -- on the real pancreas store that argmax gave a first-gene pixel
> fraction of 0.76-0.81 against 0.50 in the treemap levels, so a region read
> as turning solid red on zooming out. New `_dither_leaves(leaf_rgb, share,
> tile_size, grid)` and `_dither_threshold(tile_size, grid)` (interleaved
> gradient noise, indexed by the level's global pixel position so it is
> seamless across tiles) pick each pixel exactly one leaf's colour, chosen so
> that over a patch each colour's pixel fraction equals its share -- 0.50 at
> the coarsest level too. `_assemble` already had the `pixels=True` path this
> needed. Client: `binLayer.js`'s `usesRamp()` is now `mode === "heatmap" ||
> drawnGenes().length <= 1` (was `=== 0`) -- a one-gene Composition treemap is
> just one rectangle saying "present", so it now draws that gene's heatmap
> instead, ramp and scale controls included.

**The desktop app (`feature/desktop-app`): a Tauri v2 shell around
`plexora --desktop`.** New leaf modules `plexora/_lifetime.py` and
`plexora/_subprocess.py` (see the Repository Map rows above); `cli.py` gained
`--desktop`, `ready_line`/`DESKTOP_PROTOCOL`/`DESKTOP_PORT`, and `_run_desktop`;
`plexora/server/routes/desktop_routes.py` added `/desktop/open` and
`/desktop/info`; `plexora/client/src/js/services/desktopBridge.js` and
`pointerDrag.js` are new (`/desktop/open` detects through `import_sample`,
not `cli._wants_detection`, which is unchanged);
`/shutdown` no longer calls `os._exit()` (see the `system_routes.py` diff
under Key Invariants -- `_lifetime.request_shutdown` now, with atexit teardown
and a 10s backstop); `RULE_FROZEN` is gone from `paths.py`, since the app
ships a real interpreter and shares the platform-default data dir with the
CLI. `template_data` gained a `desktop` key (`page_routes.py`); `route_count`
in every `tests/golden/boundary_*.json` went 128 -> 130 for the two new
routes, and all seven were regenerated for the `desktop` key and the asset-tag
bumps (`main.css?v=20260925_desktop_app` and the same tag on `cardList.js`,
`imageViewer.js`, `rgbImageViewer.js`, `navbarControls.js`, `browsePicker.js`,
`columnClassifier.js`, `fileLocation.js`, `confirmDialog.js`,
`importSample.js`). New test files: `tests/test_desktop_cli.py`,
`tests/test_desktop_process.py` (a real subprocess), `tests/test_desktop_routes.py`,
`tests/test_lifetime.py`, `tests/test_subprocess_flags.py` (the static
spawn-site scan), `tests/test_release_script.py`, `tests/test_desktop_bridge.py`
(driving `tests/js/desktop_bridge_probe.mjs`); `tests/test_app_shell.py` and
`tests/test_paths.py` were extended (see their diffs for the two renamed/new
tests). New top-level `desktop/` (the Tauri shell) and `scripts/release.py`
(the release pipeline) -- see their Repository Map rows. No full-suite
pass/fail count has been confirmed against this branch yet; take one before
relying on any of the numbers earlier in this section for it.

## Sharp Edges

- **Windows menu accelerators never fire while WebView2 has focus.** A native
  menu's Ctrl+N/Ctrl+W/Ctrl+Q etc. is swallowed by the web content instead of
  reaching the shell, on Windows only (macOS's WKWebView does not have this
  problem). `desktopBridge.js`'s `SHELL_CHORDS` is what actually runs
  mod+N/W/Q, mod+shift+B and F11 (off macOS) -- a `keydown` listener inside the
  page, not the native menu's own accelerator. **WKWebView (the desktop app's
  macOS window) implements neither `window.confirm()` nor `window.prompt()`**
  -- both return immediately with the "no"/`null` answer, so code that still
  called them directly (a Remove button, say) would silently do nothing there.
  `views/confirmDialog.js`'s `PlexoraConfirm.fromText(message)` is the
  drop-in replacement, and every native-confirm/-prompt call site in the
  client is now gone in favour of `PlexoraConfirm`.
- **Windows will not rename a file over one that anything has open, and a file
  written a moment ago is exactly what Defender has open.** Zarr writes every
  key by renaming a temporary file over the target, so a burst of writes to one
  key fails with `PermissionError: [WinError 5] ... zarr.<hex>.partial ->
  zarr.json`. Measured here: 130 of 200 writes to one key refused, every one
  clearing on the next attempt. `plexora/_transient_locks.py` holds the retry
  (`past_transient_locks`, which `write_config` already used) and
  `install_zarr_retry()` wraps `zarr.storage._local._put` with it on Windows
  only; `plexora/__init__.py` installs it after `create_app()`, where zarr is
  already imported. **The shim is deliberately silent if zarr moves `_put`**,
  so `tests/test_zarr_write_retry.py::test_zarrs_local_store_is_actually_wrapped`
  is what notices when it stops taking effect -- do not delete it for looking
  tautological. What turned a rare failure into a certain one was our own
  `attrs.update({...})` in `build_extension`: that is MutableMapping's, one
  full rewrite of `zarr.json` per key, so four labels were four renames
  milliseconds apart. Use `Group.update_attributes(...)`, which is one.
- `data_model` module globals are mutated under `load_lock`, but
  `generate_zarr_png` reads them without it. A datasource switch mid-pan can race.
- `getTileKey` omits the HD flag while `getTileUrl` appends `?q=hd` — same key,
  different URL. That is why `setHdMode` removes and re-adds every channel
  instead of invalidating. The GL texture cache works around it by including the
  pixel format in its own key.
- **`getTileKey` interpolates `srcIdx`, and `glInit.js`'s `GLTileTextureCache`
  is keyed on that plus the pixel format.** `addTiledLayer`'s `spec.srcIdx`
  defaults to `` `${layerId}:${src}` `` for exactly this reason — anything
  adding a new kind of `tileFormat: 16` world item must give it its own
  distinct `srcIdx` or it will draw another plane's pixels out of a shared
  texture-cache slot.
- **A layer rebuild empties `world`, and an empty `world` costs the user their
  place.** OSD's `Viewer.processReadyItems` does `if (world.getItemCount() === 1
  && !preserveViewport) viewport.goHome(true)`, so the first channel re-added by
  `setHdMode` (or by `main.js`'s `rebuildTileLayers`) re-frames the whole slide —
  measured on a project with no segmentation, whose label layer would otherwise
  have kept the count above zero: zoom 9 → 1.14, centre back to home. Both
  callers now bracket the rebuild with `ViewerManager.rememberView`, and the
  restore rides the `success` callback of every layer this class adds, which is
  the first hook that runs *after* that goHome.
- **`setHdMode` is the wrong call for turning HD on before anything has been
  drawn.** `viewerManager.presetHdMode(enabled)` exists for the one caller that
  needs exactly that -- a sample opened by walking from a dataset sibling that
  had HD on (`main.js`, after `ImageViewer.init()`, before the channel slots
  build). `setHdMode` calls `rememberView()`, and a viewer with no world items
  has no meaningful centre or zoom to snapshot; that snapshot stays armed for
  its five-second window, the first channel then arrives, and `restoreView()`
  applies the nonsense over OpenSeadragon's own fit-the-whole-slide open -- the
  sample comes up framed on nothing. `presetHdMode` only sets the flag and
  fires `plexora:hd-mode-changed` (the HD checkbox, the mini-map and the
  channel sliders' domain all listen for it and have to agree with the flag
  whether or not a tile has been drawn yet); there is also nothing to rebuild,
  since `getTileUrl` reads the flag when it builds each address and every
  channel added from here on is already an HD address.
- **`DataLayer.getChannelNames` answers `undefined` rather than throwing when
  the image will not open**, and that used to reach `new ChannelList(config,
  undefined, ...)` and throw a TypeError on a spread -- a stack trace about a
  channel list, for a problem with a file. `init()` now checks
  `Array.isArray(columns)` right after that call (the first request in `init`
  that actually opens the image, and so the first that a missing, unreadable
  or corrupt file can fail) and returns early into `reportImageFailure()`
  instead. `reportImageFailure()` itself is best-effort and never throws --
  it runs on paths that are already failing -- and is the one function that
  asks `GET /image_status` and hands the answer to `viewerErrorState.js`;
  `window.__plexoraReady`'s own `.catch()` and two OpenSeadragon handlers
  (`tile-load-failed` past the routing-repair throttle, `add-item-failed`)
  call it too, each guarded so the question is asked once per page.
- **npm's dev server and its shipped bundle are not the same artifact, and a
  test can tell.** `npm run start` builds webpack with the config's own
  default `mode: 'development'`; the bundle every test and every deployment
  actually reads is `npm run build`'s. The difference is not only size (8 MB
  against 3.3 MB) -- Font Awesome stores its icon table differently between
  the two modes, and `tests/test_view_menu.py` greps the shipped bundle for
  whether an icon name exists, so a dev-mode bundle fails it even though
  nothing about the icon changed.
- **`tests/test_icon_names.py` greps source for `fa-<name>` and cannot tell
  code from comment.** An icon class assembled at runtime (`"fas fa-chevron-"
  + direction`) reads to it as an icon literally named `chevron-`, which does
  not exist; so does a comment that quotes the stem to explain why the class
  is not assembled that way. Both names have to be written out whole in the
  markup, and a comment discussing the trap must not spell the stem out
  either -- `views/datasetNav.js`'s own comment about this says so without
  doing it.
- **Windows keeps an image file locked for as long as its project is
  loaded**, so a test that registered a file cannot unlink it to simulate it
  going missing. Swap the path in the record with `dataclasses.replace`
  instead of touching the file on disk.
- The server tile LRU is capped by **count** (1500), not bytes: ~2.7 GB in HD
  mode.
- `maxImageCacheCount` is a **shared** budget — OSD 6 creates one `TileCache` on
  the viewer and hands the same instance to every TiledImage, so it must cover
  visible tiles × channel count.
- `tileDrawingCustom` (now in `views/tileColorize.js`'s `createTileDrawing`,
  extracted from `imageViewer.js`'s constructor) is declared `async` but has no
  `await` before its callback. It works only because it runs to completion
  synchronously — OSD raises `tile-drawing` with `raiseEvent`, which ignores
  the return value, rather than `raiseEventAwaiting` — so adding an `await`
  ahead of the draw would silently make tiles render a frame late or not at
  all.
- HD mode measurably darkens the image (mean pixel value ~506 → ~276). This is
  pre-existing and **still unexplained**. Two real defects in the 16-bit range
  handling have since been found; neither accounts for a 45% drop, so a third
  cause remains open:
  - **Fixed (2026-08-24).** `getRawImageRange` took the HD slider's ceiling from
    `image_max`, which `get_image_channel_stats` computes from the mean-pooled
    `zarray` — the same source `get_channel_quantization_window` documents as
    invalid for a max-based ceiling. On a channel whose pooled max was 1313 the
    slider could not be moved above 1313 and everything brighter clamped to full
    intensity. It now reads `qmax` (full-resolution, already in the same packet).
    `tests/test_hd_slider_domain.py` pins it. Note `image_max` is deliberately
    left pooled — it is the axis `image_histogram` is plotted against.
  - **Open.** `frag.glsl`'s `u16_rg_range` reconstructs the sample as
    `pixel.r * 255 + pixel.g`; the high byte's weight is 256, not 255. Values are
    under-read by up to `r/65535` (~0.39% of full scale) and raw 255/256 collide.
    Separately the shader normalizes by 65535 while `toImageConnectorRange`
    normalizes by 65536. Both are one-token fixes but shift HD rendering
    slightly brighter and move where existing saved thresholds bite, so they
    need a deliberate pixel-hash comparison, not a drive-by edit.
- `tests/baseline_orion2.py` depends on datasource files that may not exist on
  the current machine; those tests skip rather than fail.
- A floating popup on the viewer page must be portaled with
  `PopoverPortal.attach` (`client/src/js/views/popoverPortal.js`), never with
  `document.body.appendChild`. Two reasons, and only the first is always in
  play: a dimmed row has `opacity < 1` and traps a popup in its own stacking
  context; and the Fullscreen API paints an opaque `::backdrop` over everything
  that is not the fullscreen element or a descendant of it, so anything left on
  `<body>` opens where nobody can see or click it whenever something SMALLER
  than the document goes fullscreen. The full-screen button itself fullscreens
  `document.documentElement` (`imageViewer.js`, `pre-full-page`) precisely so
  the navbar -- a sibling of `#bodyDiv`, not a child -- stays on screen, and
  `PopoverPortal.root()` returns `<body>` whenever the fullscreen element
  contains it. Whatever is attached must be handed back with
  `PopoverPortal.detach` on teardown: a portal still holding a destroyed element
  re-attaches the orphan on the next fullscreen toggle. The three viewer popups
  (`searchableSelect.js`, `colorSwatchPicker.js`,
  `cell_explorer/static/cellExplorerRoiBridge.js`) all go through it, and
  `tests/test_popover_portal.py` keeps them there -- and so does
  `views/segmentationWait.js`, which is not a floating popup at all but a
  full-screen overlay, because the backdrop covers siblings of the fullscreen
  element whatever their size. The deleted `views/segmentationProgress.js` used
  to append to `<body>` for the same reason, and was right to -- it was loaded
  only by the deleted `project_columns.html`, which had no viewer and no way to
  go fullscreen. A native `<dialog>` opened with
  `showModal()` is the OTHER exemption, and the better shape for anything that
  is a modal rather than a popover: the top layer sits above the fullscreen
  element, so `requirementsModal.js` and `views/channelNamesUpload.js` are
  correct on `<body>`. That same top layer is why a popup opened FROM INSIDE
  a modal dialog cannot stay on `<body>` either — it would open behind the
  dialog it belongs to. `PopoverPortal.modalOnTop()` finds an open
  `dialog[open]:modal` and hosts the popup there instead, a capture-phase
  `close` listener hands it back (`close` does not bubble), and
  `PopoverPortal.reseat(el)` — called by `SearchableSelect.open()` and
  `ColorSwatchPicker.open()` — re-checks the host on every open, since
  nothing fires when a dialog opens. A modal dialog outranks a fullscreen
  element as a host.
- `data_model._parse_channel` must test the `rgb` sentinel **first**, before
  the `_<N>` regex. That regex's `AttributeError` fallback means "no trailing
  index, therefore a segmentation mask", and `"rgb"` has no trailing index — so
  without the special case ahead of it every brightfield tile request is routed
  to the label-mask reader, on a project that usually has no mask. The node's
  `_channel_index` calls the same function, so the one line covers both. It is
  also why `_windowed` on the node and `_channel_num_to_name` on the primary
  both have to skip the sentinel: neither a quantization window nor a channel
  name exists for a layer that names no index.
- A new `"Browse…"` filter needs an entry in BOTH tables in
  `server/utils/native_dialog.py` -- `_TK_FILTERS` (which `FILTER_NAMES`, and
  therefore `/browse_path`'s allowlist, is derived from) and
  `_APPLESCRIPT_EXTENSIONS`. A filter missing from the second is a KeyError-free
  `None`, which is merely unfiltered; one missing from the first is a 400 and a
  button that looks ordinary and does nothing. Prefer `None` on the AppleScript
  side for any set containing an extension macOS has no UTI for (`.tsv`,
  `.h5ad`): one unregistered extension greys out every file in the dialog,
  including the ones that would have matched. Mode `"any"`'s hybrid macOS
  dialog (`_browse_for_path_macos_hybrid`) hits the identical trap and so sets
  NO filter at all, on purpose — there is no UTI for `.zarr` either, and this
  is the same rule, not a new one.
- Scrollbar chrome is defined **once**, in `viewer.css`, as one selector list
  covering `.viewer-sidebar *`, the two portaled popups and
  `.channel-names-modal *`. Anything new that scrolls goes in that list rather
  than shipping its own `::-webkit-scrollbar` rules -- left to the platform they
  are a white trough on a near-black panel, and a second definition is a second
  thing to keep in step. Note both spellings are needed: `scrollbar-*` for
  Firefox, `::-webkit-scrollbar` for Chromium.
- Anything that stores a channel by NAME has to be listed in "A rename lands in
  place" above and moved by `adoptChannelNames`. The failure is quiet: the page
  shows the channel twice and asks the server for one it no longer has. The
  non-obvious member of that list is the **saved channel list in the DB**, which
  is server-side and is what the sidebar restores slots from on the next load --
  so it is not fixed by reloading the page.
- `tests/test_icon_names.py`'s `shipped` fixture skips both its tests when
  `plexora/client/node_modules/@fortawesome` is missing -- true on this
  checkout, where `node_modules` is a partially-synced Dropbox copy holding
  only `cross-spawn` and `mockttp`. That means the suite CANNOT catch a dead
  Font Awesome name here: a new `fa-*` class added on this machine is
  unverified until someone runs `npm install` in `plexora/client`.
- `dicom_wsi.is_dicom_path` must be checked **before** the colour/OpenSlide
  branch everywhere an image is dispatched (`LocalImageProvider.open`,
  `_missing_pyramid`, `image_geometry`, `convertOmeTiff`, `_sniff_quick_view_kind`)
  -- a DICOM H&E project carries `rgb=True`, and OpenSlide 4 can also open
  DICOM, so a colour-first check would silently hand the slide to OpenSlide,
  which flattens it to RGB, rather than to `dicom_wsi`, which reads its
  optical paths as channels.
- `wsidicom` reports a level's optical paths in a **different order at
  different levels**. `dicom_wsi._sorted_identifiers` imposes a fixed channel
  order across all levels; skipping it makes level 0's "CD45" become another
  level's "DAPI".
- `wsidicom.read_region()` takes coordinates in the **requested level's own**
  coordinate system, the opposite of OpenSlide (always level 0), and it
  **raises** `WsiDicomOutOfBoundsError` on an out-of-bounds region rather than
  returning a short one -- `dicom_wsi` clips before calling it. 16-bit
  monochrome regions come back from `wsidicom` as PIL mode `"I"` (int32) and
  must be cast to uint16.
- **Two level rules, and mixing them up is invisible until somebody times it.**
  `render.choose_level` is the EXPORT rule (never fewer pixels than asked for);
  `render.choose_view_level` is the VIEWING rule (the nearest level, so a
  region 1.5x the size of the view reads one step down the pyramid). Quick
  Edit's `server/pixels.py` must use the view one and `render.render_panel`
  must keep the export one: measured on a 17513x15569 deflate-tiled slide, a
  mini-view region read was 2.7s at level 0 and 0.22s at level 1, and the
  client asks coarser still (`FigureQuickEdit.DRAG_DETAIL`) while the view is
  being dragged. `test_figure_builder_pixels.py` pins both rules and both call
  sites against a three-level fixture.
- **A figure with no title gets a numbered one, and the number comes from the
  store.** `repository.create()` with no title calls `_next_untitled()`, which
  reads every figure's title and takes the lowest free `Untitled Figure N`;
  `schema.DEFAULT_TITLE` is still the bare fallback inside `new_document`. The
  canvas then selects that name on arrival (`FigureWorkspace.inviteRename`,
  guarded on `revision === 0` and on the name still matching
  `FigureWorkspace.UNTITLED`), so the first keystroke replaces it.
- `FigureCanvas.placePanels` must not end by calling `select()` on the panel it
  just placed. Selecting reached `FigureWorkspace.selectionChanged` ->
  `contextSidebar()` -> `showSidebar("image")`, which swapped the sidebar out
  from under the panel tray mid-assembly -- placing a panel hid the very tray
  it came from. It now calls `this.render()` then `this.onPlaced(panelIds)` (a
  constructor option), which `FigureWorkspace` uses only to prune
  `traySelection`.
- `remote_sessions.os_mismatch_advice`/`unreachable_advice` must be consulted
  **before** `connect.looks_like_missing_command` in `_diagnose`. The cmd.exe
  wording for "no such program" is itself one of `MISSING_COMMAND_MARKERS`,
  so a Linux box answering a Windows-quoted command line reads, to that
  regex, exactly like a PATH problem -- and checked in the other order every
  wrong-OS workstation would be told to fix its PATH instead of its saved
  operating system.

### The ROI panel's row menu toggles, and a tool survives a reload (2026-09-21)

Five fixes, four of them in `plugins/roi`, one in core's `toolLoader.js`.

- **`RoiTree.openMenuFor` now closes an already-open menu instead of
  reopening it.** The delegated row listener calls `event.stopPropagation()`
  before it opens anything, so the document-level click listener that
  `RoiTree.popup` installs never saw a SECOND click on the same dots -- the
  menu closed and reopened in one gesture and read as stuck open. Decided off
  the button's own `aria-expanded`, which `popup()`/`close()` already
  maintain, so it is the same mechanism `#roi_help_button` has always used.
  Clicking outside and Escape are untouched.
- **`#roi_panel_section` takes back `padding-top: var(--space-2)`.** Core
  zeroes a `.sidebar-section`'s top padding inside a tool card on the grounds
  that the card header holds its own -- but that header's 6px was the ONLY
  separation, so the toolbar was welded to the title while the list below it
  sat 12px clear. On the section rather than the toolbar, so a banner (which
  opens above the tools) is spaced off the header too.

  > **Taken back again, the other way, on 2026-09-23.** Core now pays this
  > 8px itself, on `.tool-card-body > .tool-panel-mount > .sidebar-section`
  > -- every tool panel and the layer-card plugin body gets it, not ROI
  > alone -- so `#roi_panel_section`'s own copy would have doubled it to
  > 16px. See "A plugin panel does not pay its own offset" below.
- **`.roi-item` indents to 40px, was 26px.** 26px put a region's name 3px to
  the LEFT of its category's -- the comment claimed alignment and the list
  read as flat. 40px puts it 11px in.
- **`.roi-row` gains `flex: 0 0 auto`, and `.roi-tree` caps at fifteen rows.**
  Rows are flex items of the scrolling tree, and the default `flex-shrink`
  squeezed the ones that are DIRECT children of it: with the list overflowing,
  category rows came out 22px while region rows nested in `.roi-children` kept
  their 24px -- two row heights in one list, appearing only once it was long
  enough to scroll. `max-height: calc(15 * 24px + 14 * 1px)`, was a flat 300px.
- **The resting save indicator is gone** (`#roi_status` and every
  `.roi-status*` rule). It reported "Saved" for the whole of every session.
  `renderStatus()` survives as an EDGE-triggered announcement of the one state
  that had no other voice -- a failed autosave -- routed through the existing
  `#roi_message` box. Edge-triggered because `render()` runs on every store
  change and a stuck retry would otherwise re-raise itself every few hundred
  ms. Conflict and blocked already have banners. The three ids left
  `tests/golden/boundary_roi.json`.
- **`toolLoader.rememberTool()` writes `?tool=` back out**, from `paint()`.
  The parameter was always honoured on the way IN (`page_routes.py` renders
  that tool's panel server-side, `registerLoaded()` adopts it) but nothing
  ever wrote it, so a tool opened from the Tools menu lived only in the
  module's memory and a reload silently dropped it. `replaceState`, not
  `pushState` -- opening a panel is not a place to go Back to -- and guarded
  on `location.pathname === homePath`, the path read at load, because
  appRouter puts `/settings` and the rest over the live viewer with pushState
  and a `?tool=` written onto one of those would survive the Back.

Asset tags `?v=20260921_roi_menu_toggle` on `toolLoader.js` and the roi
plugin's `VERSION`. All six `tests/golden/boundary_*.json` regenerated -- note
that they were ALREADY stale on this branch, so that regeneration also swept in
pending tag changes from the layer-card work in progress alongside this.

**The Thresholding panel and the overlay toggle (2026-09-21).** Four changes,
all of them the same one: a plugin had grown its own answers to questions core
had already answered, and a control the canvas owns was only offered while a
plugin was open.

- **The gate is one line.** `value — slider — value — icon`, exactly what the
  image channel's contrast window is. `gate_threshold_fields` and its
  `fieldsSlot` are gone (the boxes are inline, text-until-focused — see the
  2026-09-24 note below), the
  full-width "Auto Threshold" button is the muted `.slider-auto-button` glyph
  at the end of the track, and it carries a REVERT of the last fit, held per
  marker in `preAutoGates` -- a single pending revert would offer marker A's
  gate back while marker B is on screen. Precision follows the marker
  (`gateDecimals`), read live through `format` rather than captured, because
  `setBounds` has no `decimals` and a gate on raw counts wants whole numbers
  where one on a log-transformed copy wants two. Accent is
  `--accent-channel`; `--accent-gate` is the orange DESIGN.md retires by name.
- **Load/Download Gates moved into the panel.** `data-tool-extras` is gone from
  `gating/panel.html`, so nothing is lifted into the card header; they are
  `.layer-card-action` buttons on a `.layer-card-actions` line at the top of
  the body, where the image card puts Opacity and "Upload channel names". Ids
  unchanged -- `csvGatingList.js` binds both by id.
- **Three class names became shared** rather than channel-slot-specific:
  `.slot-range-row` -> `.slider-auto-row`, `.slot-auto-button` ->
  `.slider-auto-button`, and `.channel-range-slider`'s plain-number rules ->
  `.plx-slider.is-plain-numbers` in **main.css**, beside `.plx-number` itself.
  `ViewerSidebar.blurFieldsOnEnter` became `PlexoraSlider#blurFieldsOnEnter()`
  for the same reason. Pinned by `tests/test_channel_auto_revert.py`,
  `tests/js/slider_probe.mjs` and the new
  `plugins/gating/tests/test_gating_panel.py`.

  > **Both superseded on 2026-09-24.** `is-plain-numbers` is gone —
  > `.plx-slider .plx-number` reads as text until hovered or focused on EVERY
  > slider, not just this row's — and `PlexoraSlider#blurFieldsOnEnter()` is
  > gone with it: `numberField`'s own Enter handler blurs the field itself.
  > See the slider entry in the Repository Map above.
- **Opacity and the overlay key are the canvas's** -- see "Opacity belongs to
  the canvas, not to a tool" and "Hiding the cells is a redraw" under the Cells
  control above. `tests/js/cell_mode_control_probe.mjs` and
  `tests/js/cell_layer_registry_probe.mjs` grew the checks; the fake viewer in
  the first now carries `setOverlayMuted`/`setCellDisplayOpacity` and its
  `document` stub an `activeElement` and a `keydown` listener.

Asset tags `?v=20260921_overlay_controls` on `main.css`, `viewer.css`,
`slider.js`, `tileColorize.js`, `imageViewer.js` and `viewerSidebar.js`;
`viewerControls.js` has since moved to `?v=20260922_hint_follows_drawing` (the
hint's visibility rule above); gating `VERSION = "20260921_threshold_line"`. All
six `tests/golden/boundary_*.json` regenerated -- the only non-tag change is
`gate_threshold_fields` leaving gating's element list.

**A tile-quality change no longer blanks the canvas (2026-09-22).** The HD
toggle's rebuild is now add-then-remove, held until the replacements can draw --
see "Changing tile quality without blanking the canvas" under The Rendering
Pipeline for the mechanism and the measurement. `viewerManager.js` gains
`handOverWhenReady`, `addChannelItems`, `referenceItemsFor`, `dropWorldItems`
and the module helper `whenItemCanDraw`; `addTiledLayer`'s handle gains
`prepareStyle`; `setHdMode` and both `setStyle`s return promises.
`glInit.js` stops re-registering `tile-loaded`/`tile-drawing` on every `open`.
Asset tags `?v=20260922_seamless_quality_swap` on `vendor_bundle.js` and
`glInit.js`; the production bundle was rebuilt (`npm run build`).
`tests/js/layer_state_probe.mjs` gains 11 checks and
`tests/js/layer_visibility_probe.mjs` 13, with the wrappers
`tests/test_layer_state.py` / `tests/test_layer_visibility.py` naming each --
including a `loading: true` harness whose items answer `getFullyLoaded` /
`fully-loaded-change`, which is the only way a probe can look at the world
while a swap is half done. The invariant both files pin is `world.fewest`, the
smallest item count seen since `mark()`: a rebuild that removes before it adds
ends up exactly where it started, so nothing about the world afterwards can
catch it.

**The marker line, and whether the three inputs agree (2026-09-21).** Two
changes to Thresholding, one of them core's.

- **MARKER and its picker share a line.** The caption stacked above the
  combobox, spending a row of a 300px sidebar on six characters while
  everything below it was a one-line control. `.control-row` in `viewer.css` is
  now the one label-beside-control shape: `display:flex`, the `.control-label`
  at `flex: 0 0 auto` with its bottom margin taken back, everything else at
  `flex: 1`. `.cell-point-size` and `.cell-layer-opacity` were byte-for-byte
  copies of it and are now `padding-top` only (index.html carries both classes
  on those rows). ROI's heading rows are a different shape
  (`justify-content: space-between`) and were left alone.
- **The panel says where the table, mask and image disagree**, under the
  distribution plot — see `models/consistency.py` above for what is decided
  and where. The panel adds the one finding core cannot have (this marker is
  not an image channel), and suppresses it on a project whose table and image
  simply use different names for everything, where it would be true of every
  marker. `#gate_consistency` is rebuilt on every paint rather than appended
  to. Styled as muted text behind a 2px `--accent-warning` edge: the soft amber
  FILL DESIGN.md also offers was tried and is wrong at this size — two filled
  blocks are the loudest thing on a 300px panel, which is not what a heuristic
  that can be wrong about a legitimate crop should look like.
- **The distribution's threshold lines are `--accent-channel`.** They were a
  raw `#ff3131`, which is not a token, says something failed where nothing has,
  and made one control two colours: cyan handles on the track, red lines twelve
  pixels below them marking the same two numbers.

New `tests/test_consistency.py` (the findings, plus two through the route),
`tests/js/gating_consistency_probe.mjs` with
`plugins/gating/tests/test_gating_consistency_notes.py` driving it, and two
cases in `tests/test_segmentation_pyramid.py` for `plane_size`. Asset tags
`dataLayer.js?v=20260921_consistency`, `viewer.css?v=20260921_router_visibility`,
gating `VERSION = "20260921_consistency_notes"`; the boundary goldens gained
`GET /get_consistency_report`.

**A stale slider stub in `tests/js/sidebar_scoping_probe.mjs`** was what the
previous entry's rename left behind: its hand-rolled `PlexoraSlider` had no
`blurFieldsOnEnter` and it still queried `.slot-auto-button`. Worth knowing
that probe stubs are a second place every slider method and shared class name
has to be kept in step, and that they only fail in a full run.

**An open card kept painting over every routed page (2026-09-21).** Reported as
"a plugin stays in view when I go to Samples", and it was not gating's, nor any
plugin's, nor the router's.

`appRouter.js` hides the viewer by putting `.plexora-view-hidden` on
`#container`, which sets `visibility: hidden`; every descendant INHERITS it, and
that inheritance IS the mechanism (`display: none` was rejected because OSD's
autoResize would take the viewport down with it — see the comment at the top of
viewer.css). Inheritance is also the one thing that can be defeated from below:
`.layer-card-body > *, .tool-card-body > *` declared `visibility: visible` for
itself, so it never inherited. An OPEN tool card's plugin panel and an open
layer card's controls went on painting over Samples, Settings and the figure
library, in a sidebar whose own background had correctly disappeared. Collapsed
cards were unaffected, which is exactly why the report named the open plugin.

The declaration only existed to undo the shut state's `visibility: hidden`, and
**not declaring anything does that already** — an open card inherits `visible`
from the page and `hidden` from the router, which is the whole point. Deleting
it fixes the bug and keeps the fold: a transition fires on a computed-value
change whether the new value was inherited or declared, so closing still holds
the body visible for `var(--duration-base)` and opening still reveals it on the
first frame (both re-verified in a browser).

`tests/test_app_router.py` now refuses **any** bare `visibility: visible` in
viewer.css, rather than pinning these two selectors: the file already carried a
comment warning against exactly this (the viewer spinner's, ~L2213) and it did
not stop the rule being written. An open state never needs to say `visible`;
if a rule must reveal something inside a subtree IT hid, scope it so it cannot
match while the router's class is on.

### A plugin panel does not pay its own offset, and Import Sample gets contextual actions (2026-09-23)

Two unrelated fixes landed together.

**A plugin panel does not pay its own offset off the card header.** Core zeroed
a `.sidebar-section`'s `padding-top` inside a tool card so the header's own
rule and padding would be the only separation -- but the header held just 6px,
so a panel started welded to the title, and every plugin then answered the
question for itself: ROI paid 8px in its own stylesheet (see the 2026-09-21
entry above), Thresholding got 8px by accident because its first row happened
to carry a margin, Cell Explorer paid nothing. Three panels in one sidebar
started at three different heights. `.tool-card-body > .tool-panel-mount >
.sidebar-section` now pays `padding-top: var(--space-2)` itself -- the same
8px `#tool_panel_slot`'s cards are stacked with -- and `.layer-card-plugin-body`
(a plugin panel drawn as a layer card's body rather than a tool card's) gets
the same 8px. `roi.css` gave its copy back, `cell_explorer.css`'s
`.cex-roi-launch`/`.sidebar-action-fancy` overrides are gone along with
`sidebar-action-fancy`'s own `margin-top: 14px` (it was compensating for two
panels that stacked their rows with no gap of their own), `gating.css`'s
`#gate_marker_section` became a flex column with `gap: var(--space-2)`
(Thresholding's rows had no spacing between them at all before this), and
`transcripts.css`'s `.transcripts-meta` lost the top padding it used to hold
itself off the header rule with. The invariant: **a tool panel or a layer-card
plugin body never states its own top offset** -- core gives every one of them
8px, and a plugin adding its own would double it. Asset tag
`?v=20260923_card_header_gap` on `viewer.css` (index.html, figure_builder's
workspace.html) and on the roi/cell_explorer/gating/transcripts plugin
`VERSION` constants. All six `tests/golden/boundary_*.json` regenerated.

**Import Sample's proposal document now says which pick a row came from and
what a sample still lacks**, and the dialog uses both. `LayerProposal.pick` is
the index into the CALLER's own `paths` array -- not `layer.src`, which is
where the data will be READ from and differs from what was picked every time
something is resolved on the way (a node rewrites a browsed path into
`node://<node>/<derived id>`, a local pick is expanded and normalised).
Comparing `src` against the pick list is what made "Remove" silently do
nothing for a remote image; the dialog now removes a pick by that index
(`pickOf()`/`removePick()`), resolved against `state.picked`, and forgets every
answer keyed to that file's name (`added-as:`, `sample-for:`, `mask-or-image:`)
so a re-picked file does not inherit a role or a sample that no longer exists.
`inspect_paths` enumerates `paths` and skips blanks INSIDE the loop, and
`import_routes._picked` appends `''` for a blank entry instead of dropping it,
for the same reason: a filter that shortens the list renumbers every pick
after the first blank, and a Remove-by-index would then take out the wrong
file.

`SampleProposal.key` is what a sample IS, derived from its contents
(`stem:<stem>`, `bundle:<root>`, `frame:<stem>`) rather than its position in a
list that gets rebuilt on every inspection, and `SampleProposal.missing` is
which of `("mask", "table")` it lacks. The card draws one "+ Add segmentation
mask" / "+ Add data" / "+ Add layer" action per missing role
(`renderCardActions`), opening a picker INSIDE the card (`renderInlinePick`)
rather than a modal over it, because which sample the file joins is the one
thing the user must not have to remember. Answering it rides two new keys in
`answers`, read by `_declared()`/`_attached()`: `added-as:<basename>` (mask/
table/layer -- what the user said the file is) and `sample-for:<basename>`
(the `SampleProposal.key` it was added to). Both ride in `answers` rather than
a second array, so they survive re-inspection and reach the Python API/CLI
unchanged. **A declared role never overrules the pixels** -- a file added as a
mask that reads as a three-channel image is still proposed as an image, with a
warning, because the alternative is a cell-id lookup over a photograph.
`_split_samples` holds attached picks back from anchoring a sample, and an
ambiguous image (an unanswered `mask-or-image:`) no longer anchors one while a
certain image stands beside it -- this is what turned "a slide and its own
mask" into one card instead of two. `_place()` then attaches held/orphan rows
by key, then by a unique ≥4-character filename prefix, then asks a new
`sample-for:<name>` question rather than guessing; a second `role="table"` in
one sample produces a warning instead of silently discarding it.
`import_sample()` gained `key=None` and refuses (`ImportError_`) when it
disagrees with `proposal.samples[index].key`, because `index` alone is a
position in a list this call has just rebuilt and would happily register
whatever now sits there; `/import/sample` passes it.

The name/dataset row at the foot of a card is gone; `renderCardHead` draws
name and dataset as a muted inline-editable line at the top instead
(`editName`, `chooseDataset`), because both values arrive already filled in
and a form is the wrong weight for a correction. And the dialog now posts
`/import/sample` ONCE PER SAMPLE (`submit()` loops `state.proposal.samples`,
each POST carrying its own `index` and `key`) -- it used to post once no
matter how many sample cards were on screen, so "Import 3 samples" silently
created one project and dropped the other two.

New `tests/test_import_dialog.py` (source-grep, companion to
`test_import_help.py`: no DOM in this suite, so what is pinned is the shape of
the file -- that Remove never compares `src`, that the contextual actions send
`sample-for:`/`added-as:`, that the dialog draws from `main.css` and not the
project-edit page's `import.css`). Additions to `tests/test_import_proposal.py`,
`tests/test_import_sample_routes.py` and `tests/test_import_from_a_node.py`;
`CORE_QUESTIONS` in `test_import_help.py` gained `sample-for`. Asset tag
`?v=20260923_import_guided` on `main.css`, `importHelp.js`, `importSample.js`
in `base.html`.

**Removing a pick from the dialog still does not `unshare_path` it** -- see
"A file on a data node is SERVED during inspection" above. That gap is
narrower now, not gone: the client knows exactly which pick it removed
(`LayerProposal.pick`), it just does not act on a node's copy of it yet.

**A help `?`, a marker/dataset keyboard, and the Image card's copy/paste
(2026-09-24).** Three additions sharing one asset tag,
`?v=20260924_shortcuts_clipboard`, and gating `VERSION =
"20260924_shortcuts_clipboard"`.

- **A tool card can carry its own help.** `pluginRegistry.js`'s
  `Plexora.registerPlugin` gains an optional `help: {summary, notes?,
  shortcuts?: [{keys, label}]}`; `toolLoader.js`'s new `attachHelp()` draws the
  `?` (`.tool-card-help`) in the card header for a plugin that has one, and
  `views/pluginHelp.js` opens it through `PlexoraConfirm.tell`'s new `content`
  node — text only, and the open/close chord row is read off the tool's own
  Tools-menu binding rather than restated by the plugin. Gating is the first
  adopter, on Thresholding: a summary, two notes, and Z/X. Its CSS namespace
  is `.plx-tool-help*`, deliberately NOT `.plx-help*` — that belongs to
  `importHelp.js` — because the two collided once and made the modal 720px
  wide.
- **Bare-letter keyboards for the marker and the dataset.** Gating's
  `gatingSidebarController.js` binds Z/X to step `gateMarker` one place either
  way along the dropdown's own list (`stepMarker`), armed in `onShow`,
  disarmed in `onHide` and on cleanup, and live only while gating is the
  ACTIVE tool (`PlexoraToolLoader.activeTool()`), the way ROI's own bare
  letters already gate. `views/datasetNav.js`'s Previous/Next chips gain B/N
  alongside the existing bare PageUp/PageDown, printed as a `<kbd>` cap inside
  each button (outermost: B before the left chevron, N after the right one);
  both keyboards lower-case a single-character `event.key` before matching, so
  Caps Lock still works, and both stand down through `isTyping()` — now
  exported from `services/keyboardShortcuts.js` for exactly this, so a
  bare-letter keyboard living outside that service still follows its one rule
  for "somebody is typing" — and while a `<dialog>` owns the window. See the
  "Bare letters" line under Hiding the cells is a redraw, above, for the full
  set and why OpenSeadragon's W/A/S/D bounds all of them.
- **The Image card's `•••` copies and pastes channel names and rendering.**
  New `services/renderClipboard.js` (`sessionStorage` key `plexora:clipboard`,
  `names` and `rendering` slots, pure planners `mergeNames`/`resolveSlots`) and
  `views/popoverMenu.js` (`window.PlexoraMenu`, portal-based, modelled on
  `plugins/roi/static/roiTree.js`'s popup) back a new `.layer-card-menu` button
  on the reference image's card only (`layerManager.js`'s `hasRenderMenu`/
  `renderMenuFor`), last among the header extras. Pasting channel names goes
  through the new `POST /rename_channels` (JSON; `data_routes.py`, shares
  `_reference_channels`/`_finish_reference_rename` with `/upload_channels`)
  and `main.js`'s `adoptChannelNames`, falling back to a full reload if the
  page and server disagree on channel count afterwards. Pasting rendering
  reuses `ViewerSidebar.applyLaunchChannels(entries, {silent: false})` — an off
  slot stays off and its auto-level is saved, unlike a launch's, because a
  paste is an edit — off the new `ViewerSidebar.snapshotSlots()`. A
  module-level `transferring` flag mutes both Paste items for the length of
  one request.

  > **Redrawn the same day, later on 2026-09-24.** Four text menu items became
  > two rows — "Channel names" and "Rendering" — each carrying a copy
  > (`fas fa-copy`) and paste (`fas fa-paste`) icon button, the old sentences
  > kept as each button's tooltip and `aria-label`. This needed a new
  > `PlexoraMenu` item shape; see `views/popoverMenu.js` in the Repository Map
  > above.

New tests: `tests/test_render_clipboard.py` /
`tests/js/render_clipboard_probe.mjs`, `tests/test_popover_menu.py` /
`tests/js/popover_menu_probe.mjs` (covers both `popoverMenu.js` and
`pluginHelp.js`), `plugins/gating/tests/test_gating_marker_keys.py` /
`tests/js/gating_marker_keys_probe.mjs`, `tests/test_rename_channels_json.py`,
and `tests/test_layer_manager.py` — the first pytest wrapper for the
pre-existing `tests/js/layer_manager_probe.mjs` (see the note where that probe
is introduced, above). `tests/test_dataset_nav.py` and
`tests/test_launch_state.py` each gained cases for the new checks.
`route_count` +1 in every boundary golden for `/rename_channels`; all six regenerated.

**The dataset counter opens every sample, not just the next one.** New
`views/datasetStrip.js` (`window.PlexoraDatasetStrip`, see the Repository Map
entry above); the "2 / 12" chip is now a button that drops a thumbnail grid
under it, and a pick goes through `datasetNav.js`'s own `go()` so carry-over is
identical to Previous/Next. New convention on `pluginRegistry.js`:
`data-viewer-furniture` on plugin chrome appended to `#openseadragon_wrapper`,
so core's canvas popups can measure and avoid it without naming a plugin's
class — Figure Builder's `.fb-dock` is the first to set it. It works both
ways: core marks the dataset chip too, and `FigureCaptureDock.topFor()` stacks
the dock 6px below any marked box in its top-right corner (re-measured by a
`MutationObserver` on the wrapper, since the chip mounts after its own fetch),
with `roomFor(hostHeight, legendTop, top)` taking the lower ceiling. The strip
treats a box level with its first row as beside it, so it steps left of the
stacked dock instead of losing rows. z-index 250 for the
strip, above the plugin dock and scale float at 240, below tooltips at 400.
`FigureCaptureDock.mount()` also calls `PlexoraDatasetStrip.close()` directly,
once, the moment the dock appears — a click already dismisses the strip as an
outside press, but a keyboard shortcut opening the builder does not pass
through that, and the two must not share the corner.
New `tests/js/dataset_strip_probe.mjs` / `tests/test_dataset_strip.py` (34
checks, run the same source-grep way as the other `_probe.mjs`/wrapper pairs);
`tests/js/dataset_nav_probe.mjs` grew to 33 checks for the counter-as-button
and the close-on-walk. Asset tag `?v=20260924_dataset_strip` on `viewer.css`
and both `views/datasetNav.js` and the new `views/datasetStrip.js`, loaded
from `index.html` in that order (the strip module before the chip that opens
it).

## Agent Operating Notes

- **Profile before optimizing.** Every intuition about this codebase's hot spots
  was wrong at least once; the numbers above were the only reliable guide, and
  the answer changed with channel count.
- Prefer measurements on the real slide over synthetic data — the costs are
  dominated by tile size and channel count.
- `data_model.py` and `imageViewer.js` are both large and load-bearing. Read the
  surrounding comments; several encode hard-won reasons (the `qmax` full-res
  requirement, the WebP-vs-PNG alpha corruption, the black fill).
- Keep `requires-python = ">=3.12,<3.14"` unless a dependency forces otherwise.
