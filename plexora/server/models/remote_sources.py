"""Remote data as the data root sees it: options, usage, and the fill jobs.

`server/utils/remote_store.py` knows how to read a web address through a
cache. This module knows the things that belong to a user's data directory:

**The address book** (`<data root>/remote_sources.json`). Options such as an
S3 endpoint or "use my AWS profile" belong to a host or a bucket, not to a
project: one IDR endpoint serves every IDR image, a store's image and its
labels need the same options, and the import dialog needs them before any
project exists. So they are kept once, per URL prefix, and a project records
the bare canonical URL. Never a key or a secret -- an endpoint, an anonymity
flag, a profile name, a region, an account name.

**Usage and budget**, for Settings.

**Two background jobs** over `layer_jobs.start_task`: *warm*, started when a
project with a remote image opens, which fetches the coarse levels (the ones
every zoomed-out view and the overview need) before anybody asks; and *pin*
("Make available offline"), which brings an entire store into the cache and
exempts it from eviction.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from plexora import paths
from plexora.server.providers.base import is_remote_locator

FILENAME = "remote_sources.json"

#: The only option names stored. Everything else a request sends is dropped,
#: which is what keeps a secret from ever reaching the file by accident.
OPTION_KEYS = ("endpoint_url", "anon", "profile", "region", "account_name", "label")

#: Task ids under `layer_jobs`.
REMOTE_WARM_ID = "__remote_warm__"
REMOTE_PIN_ID = "__remote_pin__"
#: The sample name pin tasks run under: a store can serve several projects,
#: or none yet.
PIN_SAMPLE = "__remote__"

#: What a warm fetch may bring in on open. The coarse levels of even a large
#: image fit comfortably; the finest levels are fetched as they are viewed.
WARM_BUDGET_BYTES = 256 * 1024 ** 2

#: ...and never more than this share of the cache budget, so warming one image
#: cannot evict another that was viewed yesterday.
WARM_BUDGET_SHARE = 0.05

WARM_STAGES = {
    "metadata": (0, 5, "Reading the store"),
    "coarse": (5, 97, "Fetching the zoomed-out levels"),
}

PIN_STAGES = {
    "metadata": (0, 5, "Measuring the store"),
    "fetching": (5, 99, "Downloading for offline use"),
}

_book_lock = threading.Lock()


# -- the address book ------------------------------------------------------


def book_path() -> Path:
    return paths.data_root() / FILENAME


def read_book() -> dict:
    """`{"version": 1, "sources": {prefix: options}}`; empty when absent or bad."""
    try:
        data = json.loads(book_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "sources": {}}
    sources = data.get("sources") if isinstance(data, dict) else None
    return {"version": 1, "sources": dict(sources) if isinstance(sources, dict) else {}}


def _write_book(book: dict) -> None:
    path = book_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(book, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _clean_options(raw: Mapping[str, Any]) -> dict:
    out: dict[str, Any] = {}
    for key in OPTION_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "anon":
            out[key] = value if isinstance(value, bool) else str(value).lower() in (
                "1", "true", "yes", "on")
        elif value not in (None, ""):
            out[key] = str(value).strip()
    return out


def options_for(url) -> dict:
    """The options of the longest address-book prefix `url` starts with."""
    from plexora.server.utils import remote_store

    url = remote_store.canonical_url(url) + "/"
    best, found = -1, {}
    for prefix, options in read_book()["sources"].items():
        if url.startswith(prefix) and len(prefix) > best and isinstance(options, dict):
            best, found = len(prefix), options
    return dict(found)


def set_options(prefix, options: Mapping[str, Any]) -> dict:
    """Store options for every URL under `prefix`. Returns what was stored."""
    from plexora.server.utils import remote_store

    if not is_remote_locator(prefix):
        raise ValueError("A prefix has to be a web address, e.g. s3://bucket/")
    # Always a directory: `s3://idr/` must not also match `s3://idr-backup/`.
    prefix = remote_store.canonical_url(prefix).rstrip("/") + "/"
    cleaned = _clean_options(options)
    if cleaned.get("endpoint_url") and not is_remote_locator(cleaned["endpoint_url"]):
        raise ValueError("The endpoint has to be a web address, e.g. https://s3.example.org")
    with _book_lock:
        book = read_book()
        book["sources"][prefix] = cleaned
        _write_book(book)
    remote_store.forget(prefix=prefix.rstrip("/"))
    return {"prefix": prefix, **cleaned}


def delete_options(prefix) -> bool:
    from plexora.server.utils import remote_store

    with _book_lock:
        book = read_book()
        removed = book["sources"].pop(prefix, None) is not None
        if removed:
            _write_book(book)
    remote_store.forget(prefix=str(prefix).rstrip("/"))
    return removed


def list_options() -> list[dict]:
    return [{"prefix": prefix, **(options if isinstance(options, dict) else {})}
            for prefix, options in sorted(read_book()["sources"].items())]


# -- usage -----------------------------------------------------------------


def _projects_by_root() -> dict:
    """{store root: [project names]} for every project that reads one."""
    from plexora.server.models.project import Project
    from plexora.server.utils import remote_store

    out: dict[str, list[str]] = {}
    try:
        config = Project.load_all()
    except Exception:  # noqa: BLE001 -- usage is shown without names then
        return out
    for name, entry in (config or {}).items():
        src = (entry or {}).get("channelFile") or ""
        if is_remote_locator(src):
            root, _ = remote_store.split_store_url(src)
            out.setdefault(root, []).append(name)
    return out


def usage() -> dict:
    """What Settings shows: where, how much, which stores, what is running."""
    from plexora.server.models import layer_jobs
    from plexora.server.utils import remote_store

    index = remote_store.cache_index()
    summary = index.usage()
    projects = _projects_by_root()
    jobs = layer_jobs.tasks(PIN_SAMPLE)
    for row in summary["stores"]:
        row["projects"] = projects.get(row["url"] or "", [])
        job = jobs.get(f"{REMOTE_PIN_ID}:{row['id']}")
        row["job"] = job
    summary.update({
        "cache_dir": str(remote_store.cache_root()),
        "env_override": bool(os.environ.get(paths.ENV_REMOTE_CACHE_BYTES)),
        "min_budget_bytes": paths.REMOTE_CACHE_MIN_BYTES,
        "options": list_options(),
        "support": remote_store.support(),
    })
    return summary


def set_budget(nbytes: int) -> int:
    """Record and apply a new cache budget; evicts at once if over."""
    from plexora.server.utils import remote_store

    nbytes = int(nbytes)
    if nbytes < paths.REMOTE_CACHE_MIN_BYTES:
        raise ValueError("The remote cache needs at least 1 GB.")
    settings = paths.read_settings()
    settings["remote_cache_bytes"] = nbytes
    paths.write_settings(settings)
    remote_store.cache_index().set_budget(None)
    return paths.remote_cache_budget()


def clear(store_id: Optional[str] = None) -> None:
    """Empty the cache (or one store), stopping whatever is filling it first."""
    from plexora.server.models import layer_jobs
    from plexora.server.utils import remote_store

    for sample, task_id in layer_jobs.running_tasks():
        if not str(task_id).startswith((REMOTE_WARM_ID, REMOTE_PIN_ID)):
            continue
        if store_id is None or str(task_id).endswith(store_id) \
                or str(task_id) == REMOTE_WARM_ID:
            layer_jobs.stop_task(sample, task_id)
    remote_store.cache_index().clear(store_id)


def remove_source(store_id: str) -> None:
    """Stop, unpin and evict one store."""
    clear(store_id)


# -- chunk enumeration -----------------------------------------------------


def _storage_grid(array) -> tuple:
    grid = getattr(array, "_shard_grid_shape", None)
    return tuple(grid) if grid is not None else tuple(array.cdata_shape)


def _storage_object_shape(array) -> tuple:
    shards = getattr(array, "shards", None)
    return tuple(shards) if shards else tuple(array.chunks)


def _object_nbytes(array) -> int:
    return int(math.prod(_storage_object_shape(array))) * int(array.dtype.itemsize)


def _chunk_keys(array, fixed: Optional[Mapping[int, int]] = None) -> list[str]:
    """Every stored object of `array`, as keys relative to the store root.

    `fixed` pins some dimensions to one chunk index (t=0, z=0 for a 5-D image,
    which is the plane the viewer shows). Sharded arrays enumerate shards --
    one object each -- which is what gets fetched and cached whole.
    """
    import itertools

    grid = _storage_grid(array)
    ranges = [range(1) if fixed and dim in fixed else range(n)
              for dim, n in enumerate(grid)]
    encode = array.metadata.encode_chunk_key
    base = array.path.strip("/")
    return [f"{base}/{encode(coords)}" if base else encode(coords)
            for coords in itertools.product(*ranges)]


def _metadata_keys(array) -> list[str]:
    base = array.path.strip("/")
    names = ("zarr.json",) if array.metadata.zarr_format == 3 else (".zarray", ".zattrs")
    return [f"{base}/{n}" if base else n for n in names]


def _image_levels(url):
    """(store, [(array, fixed dims)], finest first) for a remote image URL."""
    from plexora.server.utils import ome_zarr

    view = ome_zarr._RemoteView.of(url)
    multiscale = ome_zarr._remote_multiscale(view)
    if multiscale is None:
        return view.store, []
    group = view.group()
    arrays = [group[path] for path in ome_zarr._dataset_paths(multiscale)]
    axes = ome_zarr._axes_of(multiscale, arrays[0].ndim)
    fixed = {i: 0 for i, name in enumerate(axes) if name in ("t", "z")}
    return view.store, [(array, fixed) for array in arrays]


# -- warm on open ----------------------------------------------------------


def warm_plan(url, cache_budget: Optional[int] = None) -> tuple:
    """(store, keys) a warm fetch of `url` would bring in, coarsest first."""
    from plexora.server.utils import remote_store

    store, levels = _image_levels(url)
    budget = min(WARM_BUDGET_BYTES,
                 int((cache_budget or remote_store.cache_index().budget) * WARM_BUDGET_SHARE))
    keys: list[str] = []
    for array, _ in levels:
        keys += _metadata_keys(array)
    spent = 0
    for position, (array, fixed) in enumerate(reversed(levels)):
        chunk_keys = _chunk_keys(array, fixed)
        cost = len(chunk_keys) * _object_nbytes(array)
        # The coarsest level always, whatever it costs: it is the overview.
        if position and spent + cost > budget:
            break
        keys += chunk_keys
        spent += cost
    return store, keys


def start_warm(project) -> Optional[dict]:
    """Fetch a remote image's coarse levels in the background. Idempotent."""
    from plexora.server.models import layer_jobs

    src = str(getattr(getattr(project, "image", None), "src", "") or "")
    if not is_remote_locator(src):
        return None

    def work(stage, report, cancel):
        from plexora.server.providers.base import RemoteUnreachable

        stage("metadata")
        try:
            store, keys = warm_plan(src)
            stage("coarse")
            store.fetch_keys(keys, cancel=cancel, progress=report)
        except RemoteUnreachable as exc:
            print(f"{project.name}: remote image offline, not warming -- {exc}")

    return layer_jobs.start_task(project.name, REMOTE_WARM_ID, work,
                                 stages=WARM_STAGES)


def stop_warm(project_name) -> None:
    from plexora.server.models import layer_jobs

    if project_name:
        layer_jobs.stop_task(project_name, REMOTE_WARM_ID)


# -- make available offline ------------------------------------------------


def _walk_arrays(view, sub="", depth=0, seen=None):
    """Every array reachable from a node through metadata alone."""
    from plexora.server.utils import ome_zarr

    seen = seen if seen is not None else set()
    if depth > 6 or sub in seen:
        return []
    seen.add(sub)
    found = view.metadata(sub)
    if found is None:
        return []
    if found[1] == "array":
        return [sub]
    attrs = view.ome(sub)
    children: list[str] = []
    multiscale = ome_zarr._remote_multiscale(view, sub)
    if multiscale is not None:
        children += ome_zarr._dataset_paths(multiscale)
        children += [f"labels/{n}" for n in ome_zarr.label_names(view, sub)]
    plate = attrs.get("plate")
    if isinstance(plate, Mapping):
        children += [str(w.get("path", "")).strip("/") for w in plate.get("wells") or []
                     if isinstance(w, Mapping)]
    well = attrs.get("well")
    if isinstance(well, Mapping):
        children += [str(i.get("path", "")) for i in well.get("images") or []
                     if isinstance(i, Mapping)]
    if not children:
        listed = view.children(sub)
        if listed:
            children = listed
        elif sub == "" and attrs.get("bioformats2raw.layout") is not None:
            children = ome_zarr.series_paths(view)
    out = []
    for child in children:
        if not child:
            continue
        path = "/".join(p for p in (sub, child) if p)
        out += _walk_arrays(view, path, depth + 1, seen)
    return out


def pin_plan(url) -> tuple:
    """(store, keys, estimated bytes) for bringing a whole store offline."""
    from plexora.server.utils import ome_zarr, remote_store

    root, _ = remote_store.split_store_url(url)
    view = ome_zarr._RemoteView.of(root)
    group = view.group() if view.is_group() else None
    keys: list[str] = []
    estimate = 0
    for sub in _walk_arrays(view):
        array = group[sub] if group is not None else view.group(sub.rsplit("/", 1)[0])[
            sub.rsplit("/", 1)[-1]]
        chunk_keys = _chunk_keys(array)
        keys += _metadata_keys(array) + chunk_keys
        estimate += min(int(array.nbytes), len(chunk_keys) * _object_nbytes(array))
    return view.store, keys, estimate


class OverBudget(ValueError):
    """Pinning would need more room than the cache budget allows."""

    def __init__(self, estimate, pinned, budget):
        gb = 1024 ** 3
        super().__init__(
            f"This store needs about {estimate / gb:.1f} GB and "
            f"{pinned / gb:.1f} GB is already kept offline; the cache budget is "
            f"{budget / gb:.1f} GB. Raise the budget in Settings > Remote data.")
        self.estimate, self.pinned, self.budget = estimate, pinned, budget


def start_pin(url) -> dict:
    """Make the store `url` lives in available offline. Raises OverBudget."""
    from plexora.server.models import layer_jobs
    from plexora.server.utils import remote_store

    root, _ = remote_store.split_store_url(url)
    store_id = remote_store.store_id_of(root)
    index = remote_store.cache_index()
    store, keys, estimate = pin_plan(root)
    pinned = index.pinned_bytes()
    if not index.is_pinned(store_id) and estimate + pinned > index.budget:
        raise OverBudget(estimate, pinned, index.budget)
    # Registered and pinned first, so nothing fetched from here on can be
    # evicted by the fetch that follows it.
    _ = store.index
    index.pin(store_id, True)

    def work(stage, report, cancel):
        stage("metadata")
        stage("fetching")
        store.fetch_keys(keys, cancel=cancel, progress=report)

    return layer_jobs.start_task(PIN_SAMPLE, f"{REMOTE_PIN_ID}:{store_id}", work,
                                 stages=PIN_STAGES)


def unpin(url_or_id) -> None:
    from plexora.server.models import layer_jobs
    from plexora.server.utils import remote_store

    store_id = url_or_id if not is_remote_locator(url_or_id) else remote_store.store_id_of(
        remote_store.split_store_url(url_or_id)[0])
    layer_jobs.stop_task(PIN_SAMPLE, f"{REMOTE_PIN_ID}:{store_id}")
    remote_store.cache_index().pin(store_id, False)


def jobs(sample=None) -> dict:
    """Warm/pin records: one sample's, or every pin job when `sample` is None."""
    from plexora.server.models import layer_jobs

    if sample:
        return layer_jobs.tasks(sample, prefix=REMOTE_WARM_ID)
    return layer_jobs.tasks(PIN_SAMPLE)


def is_pinned(url) -> bool:
    from plexora.server.utils import remote_store

    root, _ = remote_store.split_store_url(url)
    return remote_store.cache_index().is_pinned(remote_store.store_id_of(root))


def cached_bytes(url) -> int:
    from plexora.server.utils import remote_store

    root, _ = remote_store.split_store_url(url)
    row = remote_store.cache_index().store_row(remote_store.store_id_of(root))
    return int(row["bytes"]) if row else 0
