"""Run the gating engines side by side on real images: decision gate 1's campaign.

    python tools/bench_gates/run_arms.py --campaign 2026-10-21-gate1-dev \
        --images lsp11385_2,PCA_dev_1 --arms classic,candidates \
        --data-root ~/plexora-bench-root [--runs 3] [--markers CD3e,CD8a] \
        [--dev --model claude-opus-5-5] [--truth dev_gates.json] [--dry-run]

Each (image, arm, run) is one gating session answered by Plexora's own
harness -- `plexora.ai.harness.decision.GatingRun`, what `plexora ai run
gating` drives -- through the AI gateway (`--gateway` to put a proxy in
front; `--dev` for the dev route, which alone may name a `--model`). An arm
is a `plexora.ai.bench.VARIANTS` entry, `classic` being its `baseline`:
`classic`, `candidates`, `candidates_2`, `tiles`, ... Every session runs in
propose mode with `reuse_answers: false`, the contrast check off and
`on_limit: extend` by default (nobody is there to answer), so it writes no
gate and replays no answer; the stored gates are hashed before and after
each one, and a session that changed them stops the campaign. Run k of every
arm has the seed `--seed + k - 1` -- the cells sampled and shown differ
between runs, not between arms -- and the order of the arms turns round
from one run to the next.

`--images` is project names, comma-separated, or a file of them (one a line,
`#` comments). `--data-root` is required and is never the live install's:
the data directory the settings file names, the connection's suggestion
(PLEXORA_DATA_PATH_DEFAULT), the platform default and every shared root are
refused, and so is a folder inside or around one. Benches run on a copy of
the projects or a scratch root: the sessions, the harness's trace and any
set-up a session writes land there. The driver points PLEXORA_DATA_PATH at
it for the campaign and puts it back afterwards.

Records, in docs/internal/bench/campaigns/<campaign>/ (`--records-root` for
another place):

    manifest.json                the plan (arms and their options, images,
                                 runs, seeds, markers, the options common to
                                 every session), the code it ran on, start
                                 and end, and per session its status, seed,
                                 the reader models the harness's trace names,
                                 cost and record
    <arm>/<image>__r<run>.json   the session record the engine stored,
                                 copied; one over 1 MB is trimmed
                                 (`trim_record`) and the manifest says so
    incomplete/...               the record of a session that did not finish

A campaign that stops -- a refusal, a session that failed or paused -- goes
on when the same command is run again: a session with a record is kept, the
rest run. A different plan under the same name, or other code, is refused.
At the end the `score_gap.py` command comparing the arms is printed, and run
when `--truth` names a person's gates (`{project: {marker: gate}}`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGNS = ROOT / "docs" / "internal" / "bench" / "campaigns"

#: A stored session record larger than this is kept trimmed (`trim_record`).
MAX_BYTES = 1_000_000
#: Arm names that are not `bench.VARIANTS` keys themselves.
ALIASES = {"classic": "baseline"}
#: Nothing is written until a commit, and the harness only ever closes.
MODE = "propose"
#: A string at least this long with no space in it is an encoded payload (a
#: rendered picture, an artifact's bytes), not text anyone reads.
PAYLOAD_CHARS = 2000
#: What a trimmed record keeps of each unit: everything `score_gap.py` and
#: `calibrate.py` read (the hypotheses whole, with their observations and
#: labels) and what says how the marker was decided.
UNIT_KEEP = ("project", "marker", "state", "final", "proposed", "high", "start", "lattice",
             "hypotheses", "packets", "confidence", "tier", "method", "reason", "candidate",
             "needs_review", "class", "flags", "gmm", "used", "classifier")
#: What a campaign is: run again under the same name, these must not change.
PLAN_KEYS = ("data_root", "images", "markers", "arms", "common", "runs", "seeds", "model", "dev")
#: Seconds a session's own jobs (its classifier layers) get to finish before
#: its record is copied.
DRAIN_S = 600


# -- the plan ----------------------------------------------------------------------------


def arm_options(name) -> dict:
    """The session options of arm `name`: its `bench.VARIANTS` entry, with
    the engine said outright (a default could change under a campaign)."""
    from plexora.ai import bench

    variant = ALIASES.get(name, name)
    bench.check_variants((variant,))
    return {"engine": "classic", **bench.VARIANTS[variant]}


def parse_arms(text) -> dict:
    names = [n.strip() for n in str(text).split(",") if n.strip()]
    if len(set(names)) != len(names):
        raise ValueError(f"an arm is named twice in {text!r}")
    if not names:
        raise ValueError("no arm named")
    return {name: arm_options(name) for name in names}


def read_images(value) -> list:
    """Project names: a file of them (one a line, `#` comments) or a
    comma-separated list."""
    path = Path(str(value)).expanduser()
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        names = [line.split("#", 1)[0].strip() for line in lines]
    else:
        names = [n.strip() for n in str(value).split(",")]
    names = [n for n in names if n]
    if not names:
        raise ValueError(f"no image named in {value!r}")
    if len(set(names)) != len(names):
        raise ValueError(f"an image is named twice in {value!r}")
    return names


def safe_name(image) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(image))


def plan(images, arms, runs, seed, common) -> list:
    """One entry per session, in the order they run: run by run, image by
    image, the arms' order turned round one place each run."""
    if runs < 1:
        raise ValueError("--runs must be at least 1")
    files = {}
    for image in images:
        if safe_name(image) in files:
            raise ValueError(f"{image!r} and {files[safe_name(image)]!r} share a record name")
        files[safe_name(image)] = image
    names = list(arms)
    entries = []
    for run in range(1, runs + 1):
        turn = (run - 1) % len(names)
        for image in images:
            for arm in names[turn:] + names[:turn]:
                entries.append({
                    "order": len(entries) + 1, "key": f"{arm}/{image}/r{run}", "arm": arm,
                    "image": image, "run": run, "seed": seed + run - 1,
                    "options": {**arms[arm], **common, "seed": seed + run - 1},
                    "record": f"{arm}/{safe_name(image)}__r{run}.json"})
    return entries


# -- the live install --------------------------------------------------------------------


def live_roots() -> list:
    """[(why, path)] of every data directory a live Plexora on this account
    uses or may use: the one the settings file names (`plexora config set
    data-dir`), the connection's suggestion, the platform default, and the
    shared roots. Read without resolving `paths.data_root()`, which would
    create the winner. PLEXORA_DATA_PATH is not among them: it is a choice
    for one process, which this driver makes for itself."""
    from platformdirs import user_data_dir

    from plexora import paths

    settings = paths.read_settings()
    found = []

    def add(raw, why):
        if isinstance(raw, str) and raw.strip():
            found.append((why, Path(raw).expanduser().resolve()))

    add(settings.get("data_dir"), f"data_dir in {paths.settings_path()}")
    add(os.environ.get(paths.ENV_DATA_PATH_DEFAULT),
        f"{paths.ENV_DATA_PATH_DEFAULT}, the connection's suggestion")
    add(user_data_dir(paths.APP_NAME, appauthor=False), "the platform default")
    for entry in (os.environ.get(paths.ENV_SHARED_PATH) or "").split(os.pathsep):
        add(entry, f"a shared root in {paths.ENV_SHARED_PATH}")
    shared = settings.get("shared_dirs")
    for entry in shared if isinstance(shared, (list, tuple)) else ():
        add(entry, f"a shared root in {paths.settings_path()}")
    return found


def check_data_root(value) -> Path:
    """`value` resolved, or ValueError when it is, holds or sits inside a
    live data directory (`live_roots`)."""
    root = Path(str(value)).expanduser().resolve()
    for why, live in live_roots():
        if root == live:
            where = "is"
        elif live in root.parents:
            where = "is inside"
        elif root in live.parents:
            where = "holds"
        else:
            continue
        raise ValueError(f"--data-root {root} {where} the live data directory {live} ({why}). "
                         "Benches run on a copy of the projects or a scratch root only.")
    return root


def registered(root) -> set:
    """The project names `root`'s registry (config.json) lists."""
    from plexora import paths
    from plexora.server.models.project import read_config

    return set(read_config(paths.config_path(root)))


# -- records -------------------------------------------------------------------------------


def _size(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))


def _is_payload(value) -> bool:
    return isinstance(value, str) and len(value) >= PAYLOAD_CHARS and (
        value.startswith("data:") or " " not in value[:PAYLOAD_CHARS])


def _strip_payloads(value, dropped, where):
    """`value` without its encoded payloads; each dropped place is added to
    `dropped` with unit ids as `*` and list positions as `[]`."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            at = where + ("*" if where == ("units",) else str(key),)
            if _is_payload(item):
                dropped.add(".".join(at))
            else:
                out[key] = _strip_payloads(item, dropped, at)
        return out
    if isinstance(value, list):
        kept = []
        for item in value:
            if _is_payload(item):
                dropped.add(".".join(where + ("[]",)))
            else:
                kept.append(_strip_payloads(item, dropped, where + ("[]",)))
        return kept
    return value


def trim_record(record) -> tuple:
    """(a copy of `record` under MAX_BYTES where it can be, the places
    dropped). First every encoded payload goes; if that is not enough, each
    unit keeps only UNIT_KEEP. The copy says what was dropped (`_trimmed`)."""
    dropped = set()
    trimmed = _strip_payloads(record, dropped, ())
    if _size(trimmed) > MAX_BYTES:
        units = {}
        for uid, unit in (trimmed.get("units") or {}).items():
            units[uid] = {k: v for k, v in unit.items() if k in UNIT_KEEP}
            dropped.update(f"units.*.{k}" for k in unit if k not in UNIT_KEEP)
        trimmed["units"] = units
    trimmed["_trimmed"] = {"from_bytes": _size(record), "dropped": sorted(dropped),
                           "rule": f"records over {MAX_BYTES} bytes lose their encoded payloads, "
                                   "then each unit keeps only " + ", ".join(UNIT_KEEP)}
    return trimmed, sorted(dropped)


def store_record(source, dest) -> dict:
    """Copy a session record to `dest`, trimmed when over MAX_BYTES;
    returns what the manifest says of it."""
    source, dest = Path(source), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = source.stat().st_size
    if size <= MAX_BYTES:
        shutil.copyfile(source, dest)
        return {"bytes": size, "trimmed": False}
    trimmed, dropped = trim_record(json.loads(source.read_text(encoding="utf-8")))
    text = json.dumps(trimmed, separators=(",", ":"), default=str)
    dest.write_text(text, encoding="utf-8")
    out = {"bytes": len(text.encode("utf-8")), "source_bytes": size, "trimmed": True,
           "dropped": dropped}
    if out["bytes"] > MAX_BYTES:
        out["over_limit"] = True
    return out


def started_as_planned(record, entry) -> list:
    """The planned options the stored session does not hold, as
    `name: planned != stored` lines (empty when it ran as planned)."""
    stored = record.get("options") or {}
    planned = {**entry["options"], "mode": MODE}
    return [f"{k}: {v!r} != {stored.get(k)!r}" for k, v in planned.items() if stored.get(k) != v]


def gate_state(project) -> str:
    """sha1 of the gates stored for `project` (the gating plugin's own
    state, as the Thresholding panel reads it)."""
    from plexora.plugins.gating.server import model

    saved = model.get_saved_gating_list(project)
    return hashlib.sha1(json.dumps(saved, sort_keys=True, default=str).encode()).hexdigest()


def reader_models(trace, run_id) -> tuple:
    """(every model the gateway says served the run, {task: models}), from
    the harness's trace."""
    by_task = {}
    for call in trace.calls(run_id):
        if call.get("model"):
            by_task.setdefault(call.get("task") or "-", set()).add(call["model"])
    models = sorted(set().union(*by_task.values())) if by_task else []
    return models, {task: sorted(found) for task, found in sorted(by_task.items())}


# -- manifest ------------------------------------------------------------------------------


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_manifest(path) -> dict | None:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_manifest(path, manifest) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated"] = _now()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def plan_differences(manifest, fresh) -> list:
    return [key for key in PLAN_KEYS if manifest.get(key) != fresh.get(key)]


def _shown(path) -> str:
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def score_argv(campaign_dir, arms, truth) -> list:
    """score_gap.py's arguments comparing the arms (the first two are the
    paired comparison: the second minus the first)."""
    return ["--truth", str(truth or "PERSONS_GATES.json")] + \
        [f"{arm}={_shown(Path(campaign_dir) / arm)}/*.json" for arm in arms]


def score_command(campaign_dir, arms, truth) -> str:
    return "python tools/bench_gates/score_gap.py " + " ".join(
        shlex.quote(a) for a in score_argv(campaign_dir, arms, truth))


# -- running -------------------------------------------------------------------------------


def run_one(entry, args, *, session, gateway, trace, campaign_dir, emit) -> dict:
    """One planned session, start to finish; returns its manifest entry."""
    from plexora.agent import jobs
    from plexora.ai import bench
    from plexora.ai.harness import cli
    from plexora.ai.harness.decision import GatingOptions, GatingRun
    from plexora.plugins.gating.server.autogate import engine as engines

    before = gate_state(entry["image"])
    started = time.time()
    options = GatingOptions(project=entry["image"], mode=MODE, markers=args.markers,
                            model=args.model, start_options=dict(entry["options"]),
                            on_setup="fail")
    summary = GatingRun(options, gateway=gateway, trace=trace, session=session,
                        on_event=cli._progress).run()
    running = jobs.drain(DRAIN_S)
    ended = time.time()
    models, by_task = reader_models(trace, summary["run_id"])
    out = {"status": summary["status"], "reason": summary.get("reason"),
           "session_id": summary.get("session_id"), "harness_run_id": summary["run_id"],
           "arm": entry["arm"], "image": entry["image"], "run": entry["run"],
           "seed": entry["seed"], "model_requested": args.model, "reader_models": models,
           "models_by_task": by_task, "packets": summary.get("packets"),
           "model_calls": summary.get("model_calls"),
           "invalid_answers": summary.get("invalid_answers"),
           "charged_micro": summary.get("charged_micro"),
           "started": dt.datetime.fromtimestamp(started, dt.timezone.utc).isoformat(
               timespec="seconds"),
           "seconds": round(ended - started, 1), "code": bench.code_state(),
           "gates_unchanged": gate_state(entry["image"]) == before, "mismatched": [],
           "jobs_still_running": len(running)}
    source = engines.store().folder(out["session_id"]) / "session.json" \
        if out["session_id"] else None
    if source is not None and source.is_file():
        record = json.loads(source.read_text(encoding="utf-8"))
        out["mismatched"] = started_as_planned(record, entry)
        where = entry["record"] if out["status"] == "done" else \
            f"incomplete/{entry['record'][:-5]}.{int(time.time())}.json"
        out["record"] = where
        out.update(store_record(source, Path(campaign_dir) / where))
        if out.get("trimmed"):
            emit(f"  record trimmed: {out['source_bytes']} -> {out['bytes']} bytes"
                 + (" (STILL OVER THE LIMIT)" if out.get("over_limit") else ""))
    return out


def _problem(result) -> str | None:
    if result["status"] != "done":
        return f"ended {result['status']}: {result.get('reason') or 'no reason given'}"
    if not result.get("record"):
        return "finished, but its session record was not found"
    if result["jobs_still_running"]:
        return f"left {result['jobs_still_running']} job(s) running after {DRAIN_S} s"
    if not result["gates_unchanged"]:
        return "changed the stored gates although it ran in propose mode"
    if result["mismatched"]:
        return "did not start with the planned options (" + "; ".join(result["mismatched"]) + ")"
    return None


def _describe(fresh, entries, manifest, emit) -> None:
    recorded = {k for k, s in ((manifest or {}).get("sessions") or {}).items()
                if s.get("status") == "done" and s.get("record")}
    emit(f"Campaign {fresh['campaign']}: {len(fresh['images'])} image(s) x "
         f"{len(fresh['arms'])} arm(s) x {fresh['runs']} run(s) = {len(entries)} sessions "
         f"({len(entries) - len(recorded & {e['key'] for e in entries})} to run)")
    emit(f"data root: {fresh['data_root']}")
    every = ", ".join(f"{k} {json.dumps(v)}" for k, v in fresh["common"].items())
    emit(f"every session: {every}; " + (f"markers {', '.join(fresh['markers'])}" if fresh["markers"]
                                        else "every marker the session gates by default"))
    emit("reader model: " + (f"{fresh['model']} on the dev route" if fresh["model"] else
                             "whatever the gateway's routes serve (recorded per session)"))
    for arm, options in fresh["arms"].items():
        emit(f"  arm {arm:<14} {json.dumps(options)}")
    for e in entries:
        mark = "recorded" if e["key"] in recorded else ""
        emit(f"  {e['order']:>3}  run {e['run']} seed {e['seed']:<4} {e['arm']:<14} "
             f"{e['image']:<28} -> {e['record']} {mark}".rstrip())


def run_campaign(args, *, gateway=None, emit=print) -> int:
    from plexora import paths
    from plexora.ai import bench

    if args.model and not args.dev and gateway is None:
        emit("--model is accepted only with --dev (the dev route names a model; the "
             "production routes choose their own).")
        return 2
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.campaign):
        emit(f"--campaign {args.campaign!r}: letters, digits, '.', '_' and '-' only (it names "
             "a folder)")
        return 2
    try:
        root = check_data_root(args.data_root)
        arms = parse_arms(args.arms)
        images = read_images(args.images)
        args.markers = [m.strip() for m in args.markers.split(",") if m.strip()] \
            if args.markers else None
        common = {"reuse_answers": False, "display_check": args.display_check,
                  "on_limit": args.on_limit}
        entries = plan(images, arms, args.runs, args.seed, common)
    except ValueError as exc:
        emit(str(exc))
        return 2
    if args.truth and not Path(args.truth).expanduser().is_file():
        emit(f"--truth {args.truth}: no such file")
        return 2
    campaign_dir = Path(args.records_root).expanduser().resolve() / args.campaign
    manifest_path = campaign_dir / "manifest.json"
    fresh = {"campaign": args.campaign, "data_root": str(root), "images": images,
             "markers": args.markers, "arms": arms, "common": {"mode": MODE, **common},
             "runs": args.runs, "seeds": [args.seed + k for k in range(args.runs)],
             "model": args.model, "dev": bool(args.dev)}
    manifest = load_manifest(manifest_path)
    if manifest is not None and plan_differences(manifest, fresh):
        emit(f"{manifest_path} holds another plan (differs in "
             f"{', '.join(plan_differences(manifest, fresh))}): name a new --campaign.")
        return 2
    missing = [image for image in images if image not in registered(root)]
    _describe(fresh, entries, manifest, emit)
    emit(f"score with: {score_command(campaign_dir, list(arms), args.truth)}")
    if missing:
        emit(f"not in {paths.config_path(root)}: {', '.join(missing)} (copy the projects "
             "into the data root first)")
        return 2
    if args.dry_run:
        return 0
    code = bench.code_state()
    if manifest is not None and (manifest.get("code") or {}).get("git_sha") != code["git_sha"]:
        emit(f"{args.campaign} was started on {manifest['code'].get('git_sha')}, the code is "
             f"now {code['git_sha']}: arms run on other code are not comparable; name a new "
             "--campaign.")
        return 2
    if manifest is None:
        manifest = {**fresh, "code": code, "plan": entries, "started": _now(), "ended": None,
                    "sessions": {}, "attempts": [], "records": {
                        "max_bytes": MAX_BYTES, "trimmed": []}}
    previous = os.environ.get(paths.ENV_DATA_PATH)
    if previous and Path(previous).expanduser().resolve() != root:
        emit(f"PLEXORA_DATA_PATH {previous} is set aside for the campaign: {root}")
    os.environ[paths.ENV_DATA_PATH] = str(root)
    paths.reset()
    stopped = None
    try:
        from plexora.agent import AgentSession
        from plexora.ai.harness import cli
        from plexora.ai.harness.trace import TraceStore

        gateway = gateway or cli._client(args)
        trace = TraceStore()
        session = AgentSession(table_limit=4)
        try:
            for entry in entries:
                if (manifest["sessions"].get(entry["key"]) or {}).get("status") == "done":
                    continue
                emit(f"[{entry['order']}/{len(entries)}] {entry['arm']} {entry['image']} "
                     f"run {entry['run']} (seed {entry['seed']})")
                result = run_one(entry, args, session=session, gateway=gateway, trace=trace,
                                 campaign_dir=campaign_dir, emit=emit)
                problem = _problem(result)
                if problem is None:
                    manifest["sessions"][entry["key"]] = result
                    if result.get("trimmed"):
                        manifest["records"]["trimmed"].append(entry["key"])
                else:
                    manifest["attempts"].append({**result, "key": entry["key"],
                                                 "problem": problem})
                save_manifest(manifest_path, manifest)
                reader = ", ".join(result["reader_models"]) or "-"
                emit(f"  {result['status']}: {result['session_id']}, {result['packets']} packets, "
                     f"{result['model_calls']} model calls, reader {reader}, {result['seconds']} s")
                if problem is not None:
                    stopped = f"{entry['key']} {problem}"
                    break
        finally:
            session.close()
    finally:
        from plexora.agent import jobs

        # A session's jobs write under the data root: done before it is put back.
        jobs.drain(DRAIN_S)
        if previous is None:
            os.environ.pop(paths.ENV_DATA_PATH, None)
        else:
            os.environ[paths.ENV_DATA_PATH] = previous
        paths.reset()
    done = [manifest["sessions"].get(e["key"]) for e in entries]
    models = sorted({m for s in done if s for m in s.get("reader_models") or ()})
    manifest["reader_models"] = models
    manifest["one_reader_model"] = len(models) == 1
    manifest["score_command"] = score_command(campaign_dir, list(arms), args.truth)
    if stopped is None and all(done):
        manifest["ended"] = _now()
    save_manifest(manifest_path, manifest)
    emit(f"manifest: {_shown(manifest_path)}")
    if len(models) > 1:
        emit(f"note: {len(models)} models served this campaign ({', '.join(models)}); "
             "decision gate 1 asks for one (models_by_task per session says which task got which)")
    if stopped is not None:
        emit(f"stopped: {stopped}. Run the same command again to go on.")
        return 1
    emit(f"score with: {manifest['score_command']}")
    if args.truth:
        import score_gap

        return score_gap.main(score_argv(campaign_dir, list(arms), args.truth)
                              + ["--json", str(campaign_dir / "score_gap.json")])
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True, help="the campaign's name (its folder)")
    p.add_argument("--images", required=True,
                   help="project names, comma-separated, or a file of them")
    p.add_argument("--arms", default="classic,candidates",
                   help="bench.VARIANTS names (classic = baseline), comma-separated")
    p.add_argument("--runs", type=int, default=3, help="sessions per image and arm")
    p.add_argument("--markers", help="a subset of markers, comma-separated (default: the "
                                     "session's own choice)")
    p.add_argument("--data-root", required=True,
                   help="a copy of the projects or a scratch root; never the live install's")
    p.add_argument("--seed", type=int, default=0, help="run k's seed is SEED + k - 1")
    p.add_argument("--model", help="the reader model, on the dev route only (--dev)")
    p.add_argument("--dev", action="store_true", help="use the gateway's dev route")
    p.add_argument("--gateway", help="the gateway's address (a recording proxy, say)")
    p.add_argument("--display-check", default="off", choices=("off", "rule", "vision"),
                   help="the contrast check (off: vision's choice outlives a session, so the "
                        "first arm on an image would pay for it and change what later runs see)")
    p.add_argument("--on-limit", default="extend", choices=("extend", "stop"),
                   help="a marker at its allowance with nobody to ask")
    p.add_argument("--truth", help="a person's gates, {project: {marker: gate}}: score at the end")
    p.add_argument("--records-root", default=str(CAMPAIGNS),
                   help="where campaign folders go")
    p.add_argument("--dry-run", action="store_true",
                   help="print the planned sessions and their options; run nothing")
    return p


def main(argv=None, *, gateway=None, emit=print) -> int:
    """`gateway`: a client to use instead of `--gateway`/`--dev` (tests)."""
    return run_campaign(parser().parse_args(argv), gateway=gateway, emit=emit)


if __name__ == "__main__":
    sys.exit(main())
