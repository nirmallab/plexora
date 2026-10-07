"""The telemetry singleton: cheap to call, impossible to be hurt by.

A request thread that counts something does a tuple build, an uncontended
lock and a dict increment -- about a microsecond -- and never touches SQLite,
JSON, the settings file or the network. A daemon writer thread swaps the
in-memory aggregate out every thirty seconds, validates it against the
schema and upserts it into the queue; a separate uploader thread
(`uploader.py`) is the only thing that ever opens a socket.

`enabled` is one plain bool, False under pytest, in `off`, before `start()`
and after three internal failures, so the disabled cost of every hook is an
attribute read. Nothing here raises to a caller and nothing prints unless
`PLEXORA_TELEMETRY_DEBUG=1`.
"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
import json
import threading
import time

from plexora.telemetry import config, environment, identity, schema
from plexora.telemetry.queue import Queue, window_of

FLUSH_SECONDS = 30.0
MAX_KEYS = 5000
MAX_RECORDS = 500
TRIM_EVERY = 10
MAX_INTERNAL_FAILURES = 3
#: A session whose state row has not been refreshed for this long belonged
#: to a process that did not exit cleanly.
STALE_SESSION_SECONDS = 600


class Telemetry:
    def __init__(self):
        self.enabled = False
        self.mode = config.OFF
        self.resolved = config.Resolved(config.OFF, "default", None)
        self.serve_mode = None
        #: Set by the launch path before `start`: `detected`, and anything
        #: else the environment description wants that only the CLI knows.
        self.context = {}
        #: Which transport a capability call came in through (`stdio`, `web`).
        self.call_source = contextvars.ContextVar("plexora_telemetry_source", default=None)
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._reset_state()

    def _reset_state(self):
        self._agg = {}
        self._records = []
        self._deferred = []
        self._started = False
        self._library_checked = False
        self._upload = False
        self._app = None
        self._queue = None
        self._writer = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._flushed = threading.Event()
        self._ready = threading.Event()
        self._flush_lock = threading.Lock()
        self._failures = 0
        self._flushes = 0
        self._started_at = time.time()
        self._health = {"rejected": 0, "dropped": 0, "internal_failures": 0}
        self._errors = {"server": 0, "browser": 0, "agent": 0, "node": 0}
        self._viewers = set()
        self._projects = set()
        self._env = None
        self._valid = {}
        self._atexit = False
        self._session_written = False

    # -- the hot path ---------------------------------------------------------

    def count(self, event, key="n", n=1, /, **dims):
        if not self.enabled:
            return
        try:
            slot_key = (event, key, tuple(sorted(dims.items())))
            with self._lock:
                slot = self._agg.get(slot_key)
                if slot is None:
                    if len(self._agg) >= MAX_KEYS:
                        self._health["dropped"] += 1
                        self._wake.set()
                        return
                    self._agg[slot_key] = [n, None, 0.0, 0.0]
                else:
                    slot[0] += n
        except Exception:
            self._fail()

    def observe(self, event, key, ms, /, **dims):
        """One timing into the histogram `event.key` for these dims."""
        if not self.enabled:
            return
        try:
            value = float(ms)
            if not value >= 0:
                return
            index = schema.ms_bin(value)
            slot_key = (event, key, tuple(sorted(dims.items())))
            with self._lock:
                slot = self._agg.get(slot_key)
                if slot is None:
                    if len(self._agg) >= MAX_KEYS:
                        self._health["dropped"] += 1
                        self._wake.set()
                        return
                    bins = [0] * schema.HIST_BINS
                    bins[index] = 1
                    self._agg[slot_key] = [1, bins, value, value]
                else:
                    slot[0] += 1
                    slot[1][index] += 1
                    slot[2] += value
                    if value > slot[3]:
                        slot[3] = value
        except Exception:
            self._fail()

    def add_hist(self, event, key, bins, total_s=0.0, max_ms=0.0, /, **dims):
        """Fold an already-binned histogram (from a browser or a node)."""
        if not self.enabled:
            return
        try:
            bins = [int(b) for b in bins]
            if len(bins) != schema.HIST_BINS or any(b < 0 for b in bins):
                return
            n = sum(bins)
            if n == 0:
                return
            slot_key = (event, key, tuple(sorted(dims.items())))
            with self._lock:
                slot = self._agg.get(slot_key)
                if slot is None:
                    if len(self._agg) >= MAX_KEYS:
                        self._health["dropped"] += 1
                        return
                    self._agg[slot_key] = [n, bins, float(total_s), float(max_ms)]
                else:
                    slot[0] += n
                    slot[1] = [a + b for a, b in zip(slot[1], bins)]
                    slot[2] += float(total_s)
                    slot[3] = max(slot[3], float(max_ms))
        except Exception:
            self._fail()

    def emit(self, event, props, priority=None):
        """Queue one record. Validated on the writer thread, not here."""
        if not self.enabled:
            return
        try:
            spec = schema.EVENTS.get(event)
            priority = priority if priority is not None else getattr(spec, "priority", 5)
            with self._lock:
                if len(self._records) >= MAX_RECORDS:
                    self._health["dropped"] += 1
                    return
                self._records.append((event, dict(props), priority, window_of()))
            if priority <= 2:
                self._wake.set()
        except Exception:
            self._fail()

    def defer(self, fn):
        """Run `fn()` on the writer thread before its next flush -- for work
        (reading a manifest, describing a project) that must not happen on
        the request thread that noticed it was needed."""
        if not self.enabled:
            return
        try:
            with self._lock:
                if len(self._deferred) >= 100:
                    self._health["dropped"] += 1
                    return
                self._deferred.append(fn)
            self._wake.set()
        except Exception:
            self._fail()

    def feature(self, key, plugin="core"):
        self.count("feature.summary", "n", feature=key, plugin=plugin)

    def error(self, where, fp, /, **dims):
        """One error fingerprint. `dims` are schema dims, never a message."""
        if not self.enabled:
            return
        try:
            with self._lock:
                if where in self._errors:
                    self._errors[where] += 1
        except Exception:
            pass
        self.count("error.fingerprint", "n", where=where, fp=fp, **dims)

    def note_viewer(self, viewer_id):
        """A browser tab reported in. Held as a hash, in memory, per session."""
        if not self.enabled:
            return
        try:
            with self._lock:
                if len(self._viewers) < 10_000:
                    self._viewers.add(hashlib.sha256(str(viewer_id).encode()).digest()[:8])
        except Exception:
            pass

    def note_project(self, name) -> bool:
        """Whether this is the first time this session opened `name`. The
        name itself is kept only as an in-memory hash, never written."""
        if not self.enabled:
            return False
        try:
            digest = hashlib.sha256(str(name).encode("utf-8", "replace")).digest()[:8]
            with self._lock:
                if digest in self._projects:
                    return False
                self._projects.add(digest)
                return True
        except Exception:
            return False

    # -- lifecycle ------------------------------------------------------------

    def start(self, app=None, serve_mode="terminal", *, upload=True):
        """Turn telemetry on for this process, if the mode allows. Idempotent."""
        try:
            with self._state_lock:
                if self._started and self.serve_mode == "cli" and serve_mode != "cli":
                    # Library mode began first (a Python API call on the way
                    # to serving): become the serving process it now is.
                    self._app = app
                    self.serve_mode = serve_mode
                    self._upload = bool(upload)
                    self.defer(self._become_server)
                    return self.enabled
                if self._started:
                    return self.enabled
                self._started = True
                self._library_checked = True
                self._app = app
                self.serve_mode = serve_mode
                self._upload = bool(upload)
                resolved = config.resolve()
                self._apply(resolved)
                if not self.enabled:
                    return False
                self._started_at = time.time()
                self._writer = threading.Thread(target=self._run_writer,
                                                name="plexora-telemetry-writer", daemon=True)
                self._writer.start()
                if not self._atexit:
                    atexit.register(self.shutdown)
                    self._atexit = True
                return True
        except Exception:
            self._fail()
            return False

    def ensure_library(self):
        """Start in library mode (writer only, no uploader) the first time a
        Python API call is counted in a process that is not a server."""
        if self._library_checked:
            return
        self._library_checked = True
        if not self._started:
            self.start(None, "cli", upload=False)

    def _apply(self, resolved):
        self.resolved = resolved
        self.mode = resolved.mode
        self.enabled = resolved.mode != config.OFF and self._failures < MAX_INTERNAL_FAILURES

    def reconfigure(self):
        """Re-read the mode (settings changed, or the server said something).

        Turning telemetry off drops what is queued, unless it was the server
        that paused it: a pause is not the user asking for their data back.
        """
        try:
            with self._state_lock:
                server = self._queue.get_state("server", {}) if self._queue else None
                was = self.enabled
                self._apply(config.resolve(server=server))
                if was and not self.enabled:
                    with self._lock:
                        self._agg.clear()
                        self._records.clear()
                    if self._queue is not None and self.resolved.source != "server":
                        self._queue.clear()
                    self._wake.set()
                elif self.enabled and self._started and (
                        self._writer is None or not self._writer.is_alive()):
                    self._stop.clear()
                    self._writer = threading.Thread(target=self._run_writer,
                                                    name="plexora-telemetry-writer",
                                                    daemon=True)
                    self._writer.start()
                    if not self._atexit:
                        atexit.register(self.shutdown)
                        self._atexit = True
                elif not self._started:
                    # Never started in this process (a CLI command): nothing
                    # to run, and `enabled` must not claim otherwise.
                    self.enabled = False
            return self.resolved
        except Exception:
            self._fail()
            return self.resolved

    def shutdown(self, budget_s=2.0):
        """Final flush, the session summary, and one last upload attempt."""
        deadline = time.monotonic() + max(0.1, budget_s)
        writer = self._writer
        if writer is None:
            return
        self._stop.set()
        self._wake.set()
        writer.join(timeout=max(0.05, (deadline - time.monotonic()) * 0.6))
        self._writer = None
        if self._upload and self.enabled and self._queue is not None:
            try:
                from plexora.telemetry import uploader

                uploader.final_attempt(self, deadline)
            except Exception:
                pass
        if self._queue is not None:
            self._queue.close()

    # -- the writer thread ----------------------------------------------------

    def _run_writer(self):
        try:
            self._setup_writer()
        except Exception:
            self._fail()
        finally:
            self._ready.set()
        if not self.enabled:
            return
        self._loop()

    def _setup_writer(self):
        self._queue = self._queue or Queue()
        if not self._queue.open():
            self.enabled = False
            config.debug("the queue could not be opened; telemetry is off")
            return
        server = self._queue.get_state("server", {})
        self._apply(config.resolve(server=server))
        if not self.enabled:
            return
        identity.install_id(mint=True)
        if self.serve_mode == "cli":
            # A command or a notebook kernel: it counts into the shared queue
            # and a serving process uploads it. No session of its own, and
            # none of describe()'s subprocesses on a short-lived command.
            return
        self._env = environment.describe(self._app, self.serve_mode, self.context)
        self._recover_sessions()
        if self._upload and config.endpoint():
            from plexora.telemetry import uploader

            uploader.start(self)

    def _become_server(self):
        """The serving half of `_setup_writer`, for a process that started in
        library mode (runs on the writer thread, via `defer`)."""
        self._env = environment.describe(self._app, self.serve_mode, self.context)
        self._recover_sessions()
        if self._upload and config.endpoint():
            from plexora.telemetry import uploader

            uploader.start(self)

    def _loop(self):
        while True:
            self._wake.wait(FLUSH_SECONDS)
            self._wake.clear()
            stopping = self._stop.is_set()
            try:
                if self.enabled:
                    self.flush()
                    if stopping:
                        self._write_session_summary("clean")
            except Exception:
                self._fail()
            self._flushed.set()
            if stopping or not self.enabled:
                return

    def sync(self, timeout=5.0) -> bool:
        """Flush now, once the writer has finished starting (tests, `send`)."""
        if not self._ready.wait(timeout) or not self.enabled:
            return False
        self.flush()
        return True

    def flush(self):
        """Move the in-memory aggregate into the queue."""
        with self._flush_lock:
            self._flush()

    def _flush(self):
        with self._lock:
            deferred, self._deferred = self._deferred, []
        for fn in deferred:
            try:
                fn()
            except Exception:
                self._health["dropped"] += 1
        with self._lock:
            agg, self._agg = self._agg, {}
            records, self._records = self._records, []
        queue = self._queue
        if queue is None:
            return
        window = window_of()
        rows, rejected = self._rows(agg)
        good_records = []
        for event, props, priority, rec_window in records:
            stripped = schema.strip_record(event, props, self.mode) if event in schema.EVENTS \
                and isinstance(schema.EVENTS[event], schema.Record) else None
            if stripped is None or not schema.validate_record(event, stripped, self.mode):
                rejected[event] = rejected.get(event, 0) + 1
                continue
            good_records.append((rec_window, event, json.dumps(stripped, sort_keys=True,
                                                              separators=(",", ":")), priority))
        for event, n in rejected.items():
            self._health["rejected"] += n
            config.debug(f"rejected {n} {event} value(s) that the schema does not allow")
        for event, n in rejected.items():
            name = event if event in schema.EVENTS else "unknown.event"
            rows.append(("telemetry.health", "rejected",
                         json.dumps({"event": name}, separators=(",", ":")),
                         "count", n, None, 0.0, 0.0))
        ok = queue.add_counters(window, rows)
        by_window = {}
        for rec_window, event, props, priority in good_records:
            by_window.setdefault(rec_window, []).append((event, props, priority))
        for rec_window, items in by_window.items():
            ok = queue.add_events(rec_window, items) and ok
        if not ok:
            self._fail()
        self._flushes += 1
        if self._flushes % TRIM_EVERY == 1:
            dropped = queue.trim()
            if dropped:
                self._health["dropped"] += dropped
        self._save_session_state()

    def _rows(self, agg):
        rows, rejected = [], {}
        for (event, key, dims), (n, bins, total, peak) in agg.items():
            cache_key = (event, key, dims, self.mode)
            verdict = self._valid.get(cache_key)
            if verdict is None:
                verdict = self._validate(event, key, dict(dims))
                if len(self._valid) < 20_000:
                    self._valid[cache_key] = verdict
            if verdict is False:
                rejected[event] = rejected.get(event, 0) + 1
                continue
            agg_kind = schema.key_agg(event, key)
            if (agg_kind == "hist") != (bins is not None):
                rejected[event] = rejected.get(event, 0) + 1
                continue
            rows.append((event, key, verdict, agg_kind, n, bins, total, peak))
        return rows, rejected

    def _validate(self, event, key, dims):
        """The canonical dims JSON for a valid row, or False."""
        spec = schema.EVENTS.get(event)
        if not isinstance(spec, schema.Counter) or key not in spec.keys:
            return False
        stripped = schema.strip_dims(event, key, dims, self.mode)
        if stripped is None or not schema.validate_row(event, key, stripped, self.mode):
            return False
        return json.dumps(stripped, sort_keys=True, separators=(",", ":"))

    # -- sessions -------------------------------------------------------------

    def session_props(self, ended):
        env = self._env or {}
        with self._lock:
            errors = dict(self._errors)
            viewers = len(self._viewers)
            projects = len(self._projects)
        props = {
            "duration": schema.band_duration(time.time() - self._started_at),
            "ended": ended,
            "container": bool(env.get("container")),
            "remote_env": bool(env.get("remote_env")),
            "server_is_remote": bool(env.get("server_is_remote")),
            "detected": env.get("detected") or "none",
            "plugins_installed": [p["id"] for p in env.get("plugins") or []],
            "viewers": schema.band_pow2(viewers),
            "projects_opened": schema.band_pow2(projects),
            "errors": errors,
            "telemetry": {**self._health, "internal_failures": self._failures},
        }
        for key in ("cpus", "memory_gb", "gpu_present"):
            if key in env:
                props[key] = env[key]
        return schema.strip_record("session.summary", props, self.mode)

    def _save_session_state(self):
        if self._queue is None or self.serve_mode == "cli":
            return
        self._queue.set_state(f"session:{identity.session_id()}",
                              {"updated": time.time(), "props": self.session_props("unknown"),
                               "window": window_of()})

    def _recover_sessions(self):
        """Report sessions that ended without saying so (killed, walltime)."""
        now = time.time()
        for key, value in self._queue.state_items("session:"):
            if key == f"session:{identity.session_id()}" or not isinstance(value, dict):
                continue
            if now - float(value.get("updated") or 0) < STALE_SESSION_SECONDS:
                continue  # another live process on this data root
            props = value.get("props")
            if isinstance(props, dict):
                props = {**props, "ended": "unknown"}
                if schema.validate_record("session.summary", props, self.mode):
                    self._queue.add_events(value.get("window") or window_of(), [(
                        "session.summary",
                        json.dumps(props, sort_keys=True, separators=(",", ":")), 1)])
            self._queue.set_state(key, None)

    def _write_session_summary(self, ended):
        if self._queue is None or self._session_written or self.serve_mode == "cli":
            return
        props = self.session_props(ended)
        if schema.validate_record("session.summary", props, self.mode):
            self._queue.add_events(window_of(), [(
                "session.summary", json.dumps(props, sort_keys=True, separators=(",", ":")), 1)])
        self._queue.set_state(f"session:{identity.session_id()}", None)
        self._session_written = True

    # -- introspection --------------------------------------------------------

    def environment(self):
        if self._env is None:
            self._env = environment.describe(self._app, self.serve_mode or "cli", self.context)
        return self._env

    def client_block(self, mode=None):
        env = self.environment()
        block = {key: env[key] for key in environment.CLIENT_KEYS if key in env}
        block["install_id"] = identity.install_id(mint=False) or "0" * 32
        block["session_id"] = identity.session_id()
        block["mode"] = mode or (self.mode if self.mode != config.OFF else config.ANONYMOUS)
        block["license_tier"] = _license_tier()
        return block

    def live_rows(self):
        """The in-memory aggregate as queue-shaped rows (for `preview`)."""
        with self._lock:
            agg = dict(self._agg)
            records = list(self._records)
        rows, _rejected = self._rows(agg)
        window = window_of()
        counter_rows = []
        for event, key, dims, agg_kind, n, bins, total, peak in rows:
            counter_rows.append((window, event, key, dims, agg_kind, n,
                                 *(bins or [0] * schema.HIST_BINS), total, peak))
        record_rows = [(-(i + 1), rec_window, event, json.dumps(props), priority)
                       for i, (event, props, priority, rec_window) in enumerate(records)]
        return counter_rows, record_rows

    @property
    def queue(self):
        return self._queue

    def open_queue(self):
        """The queue, opened on demand for the CLI and the settings page."""
        if self._queue is None:
            queue = Queue()
            if not queue.open():
                return None
            self._queue = queue
        return self._queue

    # -- failure --------------------------------------------------------------

    def _fail(self):
        self._failures += 1
        config.debug(f"internal failure {self._failures}")
        if self._failures >= MAX_INTERNAL_FAILURES:
            self.enabled = False

    def reset_for_tests(self):
        """Stop threads and forget everything (tests only)."""
        try:
            self._stop.set()
            self._wake.set()
            if self._writer is not None:
                self._writer.join(timeout=2)
            from plexora.telemetry import uploader

            uploader.stop(timeout=2)
            if self._queue is not None:
                self._queue.close()
        except Exception:
            pass
        self.enabled = False
        self.mode = config.OFF
        self.context = {}
        self._reset_state()
        identity._reset_for_tests()


telemetry = Telemetry()


def _license_tier() -> str:
    """`free`, `paid` or `trial`, from the licence on this machine -- never a
    network call, never an identifier, and `free` if licensing misbehaves."""
    try:
        from plexora import licensing

        state = licensing.peek()
    except Exception:
        return "free"
    if not state.licensed:
        return "free"
    return "trial" if state.trial else "paid"
