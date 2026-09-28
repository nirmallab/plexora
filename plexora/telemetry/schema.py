"""What telemetry may say, as data. Anything not described here is not sent.

This is the allowlist, and it is the single source of truth for both ends: the
Cloudflare Worker validates uploads against `backend/vectors/plexora-vectors.json`,
which `scripts/sync_telemetry_vectors.py` writes from `vectors()` below and
checks for drift.

There is deliberately no free-text type. A value is an enum member, a band
label, a bounded integer, a boolean, a histogram of nine counts, or a string
matching a pattern that only code identifiers and hex digests can match -- so
a project name, a marker, a path or an exception message has nowhere to go.
An event with any key the schema does not name, or any value outside its
type, is rejected whole and counted (`telemetry.rejected`), never trimmed into
shape: a silently stripped event is how a denylist starts.

Two shapes of event:

- a **record** (`props`): one thing that happened, sent as it is -- a session
  summary, a project opened, an import;
- a **counter** event (`rows`): hourly aggregates, one row per
  `(key, dims)` carrying a count `n` and, for timings, a nine-bin histogram
  `h` plus its sum `s` and maximum `mx`.

Every field names the least mode it is sent in: `anonymous` fields are sent
in both modes, `diagnostics` fields only when the user chose diagnostics (or
nothing lowered it). Lowering the mode later strips them before upload.
"""

from __future__ import annotations

import bisect
import re

OFF = "off"
ANONYMOUS = "anonymous"
DIAGNOSTICS = "diagnostics"
MODES = (OFF, ANONYMOUS, DIAGNOSTICS)

A = ANONYMOUS
D = DIAGNOSTICS

SCHEMA_VERSION = 1

#: The plugins and tools that ship with Plexora. Anything else is somebody
#: else's code, and its name is theirs to publish, not ours: it is reported as
#: `ext:<8 hex>` of its name.
FIRST_PARTY = ("core", "cell_explorer", "figure_builder", "gating", "roi",
               "transcripts", "visium_hd", "rotate")

# -- bands ------------------------------------------------------------------

#: Millisecond histogram edges: nine bins, `<16`, `16-50`, ..., `>5000`.
MS_EDGES = (16, 50, 100, 250, 500, 1000, 2500, 5000)
MS_LABELS = ("<16", "16-50", "50-100", "100-250", "250-500", "500-1k",
             "1k-2.5k", "2.5k-5k", ">5k")
HIST_BINS = len(MS_LABELS)

#: log10 bands for counts and sizes: the lower edge of each decade.
BAND10_LABELS = ("0", "1", "10", "100", "1k", "10k", "100k", "1M", "10M",
                 "100M", "1G", "10G+")

#: Session length.
DURATION_EDGES = (60, 300, 900, 3600, 4 * 3600, 12 * 3600)
DURATION_LABELS = ("<1m", "1-5m", "5-15m", "15-60m", "1-4h", "4-12h", ">12h")

#: Small counts (CPUs, memory in GB, channels, zoom): powers of two.
POW2_EDGES = (1, 2, 4, 8, 16, 32, 64, 128, 256)
POW2_LABELS = ("0", "1", "2-3", "4-7", "8-15", "16-31", "32-63", "64-127",
               "128-255", "256+")


def ms_bin(ms) -> int:
    """The histogram bin a duration in milliseconds falls in (0..8)."""
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return 0
    if value != value:  # NaN
        return 0
    return bisect.bisect_left(MS_EDGES, value)


def band_ms(ms) -> str:
    return MS_LABELS[ms_bin(ms)]


def band10(n) -> str:
    """`0`, `1`, `10`, `100`, `1k`, ... -- the decade `n` is in."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        return "0"
    if value <= 0:
        return "0"
    index = min(len(str(value)), len(BAND10_LABELS) - 1)
    return BAND10_LABELS[index]


def band_duration(seconds) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return DURATION_LABELS[0]
    return DURATION_LABELS[bisect.bisect_right(DURATION_EDGES, value)]


def band_pow2(n) -> str:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return "0"
    if value <= 0:
        return "0"
    return POW2_LABELS[bisect.bisect_right(POW2_EDGES, value)]


# -- types ------------------------------------------------------------------


class Type:
    """A value's allowed shape. `check` never raises."""

    tag = "any"

    def check(self, value) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict:
        return {"t": self.tag}


class Enum(Type):
    tag = "enum"

    def __init__(self, *values):
        self.values = tuple(values)
        self._set = frozenset(values)

    def check(self, value):
        return isinstance(value, str) and value in self._set

    def describe(self):
        return {"t": "enum", "v": list(self.values)}


class Pattern(Type):
    """A string matching `regex` in full. Only for code identifiers and hex."""

    tag = "pattern"

    def __init__(self, regex, max_len=64, example=None):
        self.regex = regex
        self.max_len = max_len
        self.example = example
        self._compiled = re.compile(regex)

    def check(self, value):
        return (isinstance(value, str) and 0 < len(value) <= self.max_len
                and self._compiled.fullmatch(value) is not None)

    def describe(self):
        return {"t": "pattern", "re": self.regex, "max": self.max_len}


class Int(Type):
    tag = "int"

    def __init__(self, maximum):
        self.maximum = maximum

    def check(self, value):
        return (isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= self.maximum)

    def describe(self):
        return {"t": "int", "max": self.maximum}


class Bool(Type):
    tag = "bool"

    def check(self, value):
        return isinstance(value, bool)


class Hist(Type):
    """Nine non-negative counts, one per `MS_LABELS` bin."""

    tag = "hist"

    def check(self, value):
        return (isinstance(value, list) and len(value) == HIST_BINS
                and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10**12
                        for v in value))


class ListOf(Type):
    tag = "list"

    def __init__(self, of, max_items):
        self.of = of
        self.max_items = max_items

    def check(self, value):
        return (isinstance(value, list) and len(value) <= self.max_items
                and all(self.of.check(v) for v in value))

    def describe(self):
        return {"t": "list", "of": self.of.describe(), "max": self.max_items}


class MapOf(Type):
    tag = "map"

    def __init__(self, key, value, max_items):
        self.key = key
        self.value = value
        self.max_items = max_items

    def check(self, value):
        return (isinstance(value, dict) and len(value) <= self.max_items
                and all(self.key.check(k) and self.value.check(v) for k, v in value.items()))

    def describe(self):
        return {"t": "map", "k": self.key.describe(), "v": self.value.describe(),
                "max": self.max_items}


class Struct(Type):
    """A fixed set of optional fields; any other key rejects the value."""

    tag = "struct"

    def __init__(self, **fields):
        self.fields = fields

    def check(self, value):
        return (isinstance(value, dict)
                and all(k in self.fields and self.fields[k].check(v) for k, v in value.items()))

    def describe(self):
        return {"t": "struct", "f": {k: t.describe() for k, t in self.fields.items()}}


# Shared types.
COUNT = Int(10**12)
BAND10 = Enum(*BAND10_LABELS)
BAND_MS = Enum(*MS_LABELS)
POW2 = Enum(*POW2_LABELS)
OWNER = Pattern(r"(?:%s|ext:[0-9a-f]{8})" % "|".join(FIRST_PARTY), 16, "gating")
#: A Flask endpoint of Plexora's own (`generate_png`, `gating.static`,
#: `agent_v1.poll_commands`), `ext:<8 hex>` for somebody else's, or
#: `unmatched`. An identifier cannot carry a path or a name.
ROUTE = Pattern(r"(?:[a-z][A-Za-z0-9_]{0,47}(?:\.[a-z_][A-Za-z0-9_]{0,47})?|ext:[0-9a-f]{8}|unmatched)",
                96,
                "generate_png")
#: A capability name (`gating.auto`), or `ext:<8 hex>` for a plugin's own.
CAPABILITY = Pattern(r"(?:[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,47}|ext:[0-9a-f]{8})", 80,
                     "gating.auto")
#: A public Python API name or CLI subcommand.
FUNCTION = Pattern(r"[a-z][a-z0-9_]{0,47}(?:\.[a-z][a-z0-9_]{0,47})?|[A-Z][A-Za-z]{0,47}", 96,
                   "import_sample")
FINGERPRINT = Pattern(r"[0-9a-f]{8}|[0-9a-f]{16}", 16, "0123456789abcdef")
#: A builtin exception class, a Plexora-owned class name, or `ext`.
EXC_TYPE = Pattern(r"[A-Z][A-Za-z0-9]{0,63}|ext", 64, "KeyError")
#: The basename of one of Plexora's own client scripts, or `external`.
ASSET = Pattern(r"[A-Za-z0-9_-]{1,56}\.m?js|external|inline", 64, "toolLoader.js")

STATUS = Enum("2xx", "3xx", "4xx", "5xx")
OS_FAMILY = Enum("mac", "windows", "linux", "other")
BROWSER = Enum("chrome", "firefox", "safari", "edge", "other")
GPU_VENDOR = Enum("nvidia", "amd", "intel", "apple", "software", "other", "unknown")
LABEL_RENDERER = Enum("gpu", "cpu", "none")
TILE_PATH = Enum("local", "proxy", "direct")
TILE_KIND = Enum("channel", "hd", "label", "points", "rgb", "other")
CACHE = Enum("hit", "miss")
YESNO = Enum("yes", "no")
SERVE_MODE = Enum("terminal", "desktop", "notebook", "sidecar", "cli", "other")
SCHEDULER = Enum("slurm", "pbs", "lsf", "sge", "ssh", "none")
DETECTED = Enum("ood", "proxy", "colab", "origin", "remote", "none")
INSTALL_KIND = Enum("desktop", "editable", "readonly", "container", "no_pip", "pip", "unknown")
WHERE = Enum("server", "browser", "agent", "node")

#: The front-end components an error can be attributed to. The browser maps
#: anything else to `other` before it leaves the tab.
COMPONENTS = ("tool_loader", "plugin", "viewer", "tiles", "decode", "webgl", "labels",
              "routing", "settings", "import", "agent", "figure", "gating", "roi",
              "cell_explorer", "transcripts", "visium_hd", "window", "promise", "other")
COMPONENT = Enum(*COMPONENTS)
ACTIONS = ("load", "show", "hide", "activate", "deactivate", "render", "decode",
           "fetch", "init", "save", "export", "import", "commit", "other")
ACTION = Enum(*ACTIONS)

#: Things a user does, counted without what they did it to. A feature not on
#: this list is dropped in the tab and never reaches the server.
FEATURE_KEYS = ("gate.commit", "gate.brush", "autogate.run", "gates.export",
                "roi.create", "roi.edit", "roi.delete", "roi.export", "roi.import",
                "roi.save", "figure.keep", "figure.export", "column.select",
                "gene.add", "gene_group.add", "hd.gene.add", "channel.toggle",
                "layer.add", "rotate", "flip")
FEATURE = Enum(*FEATURE_KEYS)

AGENT_OUTCOMES = ("ok", "unknown_project", "unknown_capability", "invalid_input",
                  "precondition_missing", "resource_unavailable", "resource_not_local",
                  "viewer_not_available", "viewer_not_responding", "ambiguous_view",
                  "permission_required", "license_required", "conflict",
                  "capability_unavailable",
                  "unsupported_modality", "too_large", "internal_error", "other")

IMAGE_KINDS = ("ome_tiff", "brightfield", "rgb", "blank", "dicom", "other", "none")
MODALITIES = ("multiplex", "he", "xenium_morphology", "picture", "blank", "other", "none")
TABLE_KINDS = ("csv", "parquet", "anndata", "spatialdata", "other", "none")
BUNDLE_FORMATS = ("xenium", "spatialdata", "visium", "visium_hd", "other")
LOAD_OUTCOMES = ("ready", "missing", "inaccessible", "corrupt", "unavailable",
                 "offline", "error")
IMPORT_KINDS = ("xenium", "visium", "visium_hd", "spatialdata", "image", "table", "other")
IMPORT_OUTCOMES = ("registered", "name_taken", "declined", "error")
CONNECT_OUTCOMES = ("connected", "auth", "hostkey", "timeout", "refused", "dns",
                    "walltime", "node_announce", "cancelled", "other")


# -- field and event specs ---------------------------------------------------


class Field:
    __slots__ = ("type", "mode")

    def __init__(self, type_, mode=A):
        self.type = type_
        self.mode = mode


def F(type_, mode=A):
    return Field(type_, mode)


class Key:
    """One counter key: how its rows aggregate and which dims they carry."""

    __slots__ = ("agg", "dims", "mode")

    def __init__(self, agg, dims=None, mode=A):
        if agg not in ("count", "hist"):
            raise ValueError(agg)
        self.agg = agg
        self.dims = {name: (f if isinstance(f, Field) else Field(f))
                     for name, f in (dims or {}).items()}
        self.mode = mode


def C(**dims):
    return Key("count", dims)


def H(**dims):
    return Key("hist", dims)


class Record:
    kind = "record"

    def __init__(self, priority, **props):
        self.priority = priority
        self.props = {name: (f if isinstance(f, Field) else Field(f))
                      for name, f in props.items()}


class Counter:
    kind = "counter"

    def __init__(self, priority, **keys):
        self.priority = priority
        self.keys = keys


_BROWSER_DIMS = {"browser": F(BROWSER), "os": F(OS_FAMILY), "gpu": F(GPU_VENDOR, D),
                 "label_renderer": F(LABEL_RENDERER)}


def _bd(**dims):
    """Browser dims plus these."""
    return {**_BROWSER_DIMS, **dims}


EVENTS = {
    # ---- records ----
    "session.summary": Record(
        1,
        duration=F(Enum(*DURATION_LABELS)),
        ended=F(Enum("clean", "unknown")),
        container=F(Bool()),
        remote_env=F(Bool()),
        server_is_remote=F(Bool()),
        detected=F(DETECTED),
        cpus=F(POW2),
        memory_gb=F(POW2),
        gpu_present=F(Bool(), D),
        plugins_installed=F(ListOf(OWNER, 64)),
        viewers=F(POW2),
        projects_opened=F(POW2),
        errors=F(Struct(server=COUNT, browser=COUNT, agent=COUNT, node=COUNT)),
        telemetry=F(Struct(rejected=COUNT, dropped=COUNT, internal_failures=COUNT)),
    ),
    "dataset.opened": Record(
        2,
        image_kind=F(Enum(*IMAGE_KINDS)),
        modality=F(Enum(*MODALITIES)),
        image_type=F(Enum("brightfield", "fluorescence")),
        width=F(BAND10),
        height=F(BAND10),
        pixels=F(BAND10),
        levels=F(Int(32)),
        tile=F(Enum("256", "512", "1024", "other")),
        channels_band=F(POW2),
        channels=F(Int(4096), D),
        bit_depth=F(Enum("8", "16", "32", "float", "rgb", "unknown")),
        segmentation=F(Enum("none", "mask", "derived")),
        segmentation_status=F(Enum("ready", "pending", "building", "failed", "other")),
        table_kind=F(Enum(*TABLE_KINDS)),
        rows=F(BAND10),
        markers=F(POW2),
        metadata=F(POW2),
        layers=F(Struct(image=Int(256), labels=Int(256), points=Int(256), shapes=Int(256))),
        points=F(BAND10),
        transcripts_genes=F(BAND10),
        transcripts_points=F(BAND10),
        hd_bins=F(BAND10),
        hd_genes=F(BAND10),
        distributed=F(Bool()),
        bundle_formats=F(ListOf(Enum(*BUNDLE_FORMATS), 8)),
        node_backed=F(Bool()),
    ),
    "project.load": Record(
        3,
        outcome=F(Enum(*LOAD_OUTCOMES)),
        remote=F(Bool()),
        node_backed=F(Bool()),
        modality=F(Enum(*MODALITIES)),
        table_ms=F(BAND_MS),
        segmentation_ms=F(BAND_MS),
        image_ms=F(BAND_MS),
        total_ms=F(BAND_MS),
    ),
    "import.summary": Record(
        4,
        kind=F(Enum(*IMPORT_KINDS)),
        outcome=F(Enum(*IMPORT_OUTCOMES)),
        layers=F(Int(64)),
        replaced=F(Bool()),
        register_ms=F(BAND_MS),
    ),
    "remote.connect": Record(
        4,
        outcome=F(Enum(*CONNECT_OUTCOMES)),
        connect_ms=F(BAND_MS),
        scheduler=F(SCHEDULER),
        kind=F(Enum("node", "server")),
    ),
    # ---- counters ----
    "server.summary": Counter(
        5,
        route_ms=H(route=ROUTE, status=STATUS),
        family_ms=H(family=Enum("tile", "points", "page", "config", "data", "agent",
                                "tool", "import", "settings", "plugin", "other")),
        tile_ms=H(source=Enum("local", "node"), kind=TILE_KIND, cache=CACHE),
        tile_read_ms=H(source=Enum("local", "node"), kind=TILE_KIND),
        tile_lut_ms=H(kind=TILE_KIND),
        tile_encode_ms=H(kind=TILE_KIND),
        tile_cache=C(result=CACHE),
        node_forward=C(result=Enum("ok", "unavailable", "error")),
        unavailable_503=C(),
    ),
    "render.summary": Counter(
        6,
        tile_ms=H(**_bd(path=F(TILE_PATH), kind=F(TILE_KIND))),
        server_total_ms=H(**_bd(path=F(TILE_PATH))),
        server_read_ms=H(**_bd(path=F(TILE_PATH))),
        server_encode_ms=H(**_bd(path=F(TILE_PATH))),
        server_cache=C(**_bd(path=F(TILE_PATH), result=F(CACHE))),
        decode_ms=H(**_bd(format=F(Enum("webp", "gray16", "label", "png", "other")),
                          where=F(Enum("worker", "inline")))),
        decode_fallback=C(**_bd()),
        texture_cache=C(**_bd(result=F(CACHE))),
        frame_gap_ms=H(**_bd()),
        gestures=C(**_bd()),
        long_task_ms=H(**_bd()),
        first_paint_ms=H(**_bd()),
        boot_ms=H(**_bd()),
        gl_context_lost=C(**_bd(where=F(Enum("image", "label")))),
        tile_failures=C(**_bd(path=F(TILE_PATH))),
        tile_bytes=C(**_bd(band=F(BAND10))),
        peak_tiles_per_min=C(**_bd(band=F(BAND10))),
        active_channels=C(**_bd(band=F(POW2))),
        hw_concurrency=C(**_bd(band=F(POW2))),
        dpr=C(**_bd(band=F(Enum("1", "1.5", "2", "3+")))),
        viewers=C(),
    ),
    "node.summary": Counter(
        6,
        nodes=C(),
        hello_rtt_ms=H(),
        hello_failed=C(reason=Enum("timeout", "refused", "auth", "version", "other")),
        tiles=C(cache=CACHE),
        tile_total_ms=H(),
        tile_read_ms=H(),
        tile_encode_ms=H(),
        requests=C(),
        request_errors=C(),
    ),
    "error.fingerprint": Counter(
        3,
        n=C(where=WHERE, fp=FINGERPRINT, exc_type=EXC_TYPE, route=ROUTE,
            component=COMPONENT, action=ACTION, file=ASSET, plugin=OWNER),
    ),
    "capability.summary": Counter(
        5,
        n=C(name=CAPABILITY, owner=OWNER,
            permission=Enum("read", "reversible_write", "source_file_write", "destructive",
                            "other"),
            execution=Enum("immediate", "job"),
            transport=Enum("stdio", "web", "nested", "other"),
            outcome=Enum(*AGENT_OUTCOMES)),
        ms=H(name=CAPABILITY, owner=OWNER),
        job_ms=H(name=CAPABILITY, owner=OWNER, status=Enum("done", "failed", "cancelled")),
    ),
    "capability.transition": Counter(
        7,
        n=C(**{"from": F(CAPABILITY), "to": F(CAPABILITY)}),
    ),
    "tool.summary": Counter(
        5,
        open=C(tool=OWNER),
        close=C(tool=OWNER),
        fold=C(tool=OWNER),
        activate=C(tool=OWNER),
        load_ms=H(tool=OWNER),
        load_failed=C(tool=OWNER, why=Enum("script", "panel", "needs", "redirect", "other")),
    ),
    "feature.summary": Counter(
        5,
        n=C(feature=FEATURE, plugin=OWNER),
    ),
    "function.summary": Counter(
        5,
        n=C(source=Enum("ui", "python", "cli", "agent"), fn=FUNCTION),
        err=C(source=Enum("ui", "python", "cli", "agent"), fn=FUNCTION),
        ms=H(source=Enum("ui", "python", "cli", "agent"), fn=FUNCTION),
    ),
    "telemetry.health": Counter(
        8,
        rejected=C(event=Pattern(r"[a-z]+\.[a-z_]+", 40, "dataset.opened")),
        dropped=C(reason=Enum("queue_full", "redaction", "overflow", "server", "mode")),
    ),
}

#: What a browser tab may post to `/telemetry/ingest`. The tab is a producer
#: only; the server adds its rows to the current window.
BROWSER_EVENTS = {
    "render.summary": None,          # every key
    "tool.summary": None,
    "feature.summary": None,
    "error.fingerprint": {"where": "browser"},  # dims the server pins
}

#: The client block sent with every batch.
CLIENT_BLOCK = {
    "install_id": F(Pattern(r"[0-9a-f]{32}", 32, "0" * 32)),
    "session_id": F(Pattern(r"[0-9a-f]{32}", 32, "1" * 32)),
    "plexora_version": F(Pattern(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}(?:[a-z]+[0-9]*)?(?:\.dev[0-9]+)?(?:\+[0-9a-z.]{1,24})?", 48,
                                      "0.0.25")),
    "python": F(Enum("3.10", "3.11", "3.12", "3.13", "3.14", "other")),
    "os": F(OS_FAMILY),
    "arch": F(Enum("x86_64", "arm64", "other")),
    "launch_mode": F(SERVE_MODE),
    "deployment": F(Enum("local", "hpc", "ood", "jupyterhub", "colab", "container",
                         "remote", "other")),
    "scheduler": F(SCHEDULER),
    "install_kind": F(INSTALL_KIND),
    "mode": F(Enum(ANONYMOUS, DIAGNOSTICS)),
    "plugins": F(ListOf(Struct(id=OWNER, version=Pattern(r"[0-9A-Za-z.+-]{1,24}", 24, "0.0.25")),
                        64)),
    # Which plan this install is on, as a word: `free`, `paid` or `trial`.
    # Coarse on purpose -- a tier, never who holds the licence -- and read
    # from the licence already on this machine with no network call.
    # Telemetry OBSERVES licensing; nothing in licensing reads telemetry.
    "license_tier": F(Enum("free", "paid", "trial")),
    # Reserved for a future licensing service, which lives elsewhere. Allowed
    # when present, never set by this client, never required: they would tie
    # an anonymous install to a licence holder, and telemetry is anonymous.
    "license_account_id": F(Pattern(r"[0-9a-f]{16,64}", 64)),
    "license_id_hash": F(Pattern(r"[0-9a-f]{16,64}", 64)),
    "seat_id": F(Pattern(r"[0-9a-f]{16,64}", 64)),
    "machine_id_hash": F(Pattern(r"[0-9a-f]{16,64}", 64)),
    "activation_id": F(Pattern(r"[0-9a-f]{16,64}", 64)),
}
#: Reserved fields are allowed but never set by this client, so the sample
#: batch leaves them out.
RESERVED_LICENSE_FIELDS = ("license_account_id", "license_id_hash", "seat_id",
                           "machine_id_hash", "activation_id")

#: Caps both ends agree on.
MAX_EVENTS_PER_BATCH = 200
MAX_ROWS_PER_EVENT = 500
MAX_BATCH_GZIP_BYTES = 64 * 1024
WINDOW_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}"
_WINDOW = re.compile(WINDOW_PATTERN)


# -- validation --------------------------------------------------------------


def mode_allows(field_mode: str, mode: str) -> bool:
    """Whether a field whose least mode is `field_mode` is sent in `mode`."""
    if mode == OFF:
        return False
    return field_mode == A or mode == D


def validate_record(event: str, props, mode: str = D) -> bool:
    spec = EVENTS.get(event)
    if not isinstance(spec, Record) or not isinstance(props, dict):
        return False
    for name, value in props.items():
        field = spec.props.get(name)
        if field is None or not mode_allows(field.mode, mode) or not field.type.check(value):
            return False
    return True


def validate_row(event: str, key: str, dims, mode: str = D) -> bool:
    spec = EVENTS.get(event)
    if not isinstance(spec, Counter) or not isinstance(dims, dict):
        return False
    key_spec = spec.keys.get(key)
    if key_spec is None or not mode_allows(key_spec.mode, mode):
        return False
    for name, value in dims.items():
        field = key_spec.dims.get(name)
        if field is None or not mode_allows(field.mode, mode) or not field.type.check(value):
            return False
    return True


def key_agg(event: str, key: str) -> str | None:
    spec = EVENTS.get(event)
    if isinstance(spec, Counter) and key in spec.keys:
        return spec.keys[key].agg
    return None


def strip_record(event: str, props: dict, mode: str) -> dict:
    """`props` without the known fields `mode` does not send. Unknown keys
    are kept, so that validation rejects the event instead of this quietly
    trimming it into shape."""
    spec = EVENTS[event]
    return {k: v for k, v in props.items()
            if k not in spec.props or mode_allows(spec.props[k].mode, mode)}


def strip_dims(event: str, key: str, dims: dict, mode: str) -> dict | None:
    """`dims` without the dims `mode` does not send, or None if the key itself
    is not sent in `mode`."""
    spec = EVENTS[event].keys[key]
    if not mode_allows(spec.mode, mode):
        return None
    return {k: v for k, v in dims.items()
            if k not in spec.dims or mode_allows(spec.dims[k].mode, mode)}


def validate_client(block) -> bool:
    if not isinstance(block, dict):
        return False
    for name, value in block.items():
        field = CLIENT_BLOCK.get(name)
        if field is None or not field.type.check(value):
            return False
    return True


def valid_window(value) -> bool:
    return isinstance(value, str) and _WINDOW.fullmatch(value) is not None


def validate_upload_event(event: dict, mode: str = D) -> bool:
    """One event as it appears in an upload body (the Worker's view)."""
    if not isinstance(event, dict):
        return False
    name = event.get("type")
    spec = EVENTS.get(name)
    if spec is None or not valid_window(event.get("window")):
        return False
    if isinstance(spec, Record):
        return (set(event) <= {"type", "window", "props"}
                and validate_record(name, event.get("props"), mode))
    rows = event.get("rows")
    if set(event) - {"type", "window", "rows"} or not isinstance(rows, list):
        return False
    if not 0 < len(rows) <= MAX_ROWS_PER_EVENT:
        return False
    return all(validate_upload_row(name, row, mode) for row in rows)


def validate_upload_row(event: str, row, mode: str = D) -> bool:
    if not isinstance(row, dict) or set(row) - {"k", "d", "n", "h", "s", "mx"}:
        return False
    key = row.get("k")
    agg = key_agg(event, key)
    if agg is None or not validate_row(event, key, row.get("d", {}), mode):
        return False
    if not COUNT.check(row.get("n")):
        return False
    if agg == "hist":
        if not Hist().check(row.get("h")):
            return False
        for extra in ("s", "mx"):
            value = row.get(extra)
            if value is not None and not (isinstance(value, (int, float))
                                          and not isinstance(value, bool)
                                          and 0 <= value <= 1e12):
                return False
    elif any(k in row for k in ("h", "s", "mx")):
        return False
    return True


# -- the vectors file ---------------------------------------------------------


def vectors() -> dict:
    """Everything the Worker needs to validate an upload, as plain JSON."""
    events = {}
    for name, spec in EVENTS.items():
        if isinstance(spec, Record):
            events[name] = {
                "kind": "record", "priority": spec.priority,
                "props": {k: {**f.type.describe(), "m": f.mode} for k, f in spec.props.items()},
            }
        else:
            events[name] = {
                "kind": "counter", "priority": spec.priority,
                "keys": {k: {"agg": key.agg, "m": key.mode,
                             "dims": {d: {**f.type.describe(), "m": f.mode}
                                      for d, f in key.dims.items()}}
                         for k, key in spec.keys.items()},
            }
    return {
        "schema": SCHEMA_VERSION,
        "modes": list(MODES),
        "first_party": list(FIRST_PARTY),
        "hist_bins": HIST_BINS,
        "ms_edges": list(MS_EDGES),
        "ms_labels": list(MS_LABELS),
        "band10_labels": list(BAND10_LABELS),
        "pow2_labels": list(POW2_LABELS),
        "duration_labels": list(DURATION_LABELS),
        "feature_keys": list(FEATURE_KEYS),
        "window_pattern": WINDOW_PATTERN,
        "caps": {"max_events": MAX_EVENTS_PER_BATCH, "max_rows": MAX_ROWS_PER_EVENT,
                 "max_gzip_bytes": MAX_BATCH_GZIP_BYTES},
        "client": {k: {**f.type.describe(), "m": f.mode} for k, f in CLIENT_BLOCK.items()},
        "reserved_license_fields": list(RESERVED_LICENSE_FIELDS),
        "events": events,
    }
