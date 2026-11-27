"""Run one Scimap Pro function on a working AnnData file.

Ported from the retired `scimappro_plexora._handlers.execute`, which ran a
function on a project's own table; here the table is the notebook's working
file in the results folder, and the caller (server/runner.py) has already
taken the one-run-at-a-time slot and applied Scimap Pro's settings on this
thread.

What a run does, in order:

1. `scimappro.io.init` opens the file. Scimap Pro's own licence decides how:
   with out-of-core work (`streaming`, every paid plan) nothing is loaded and
   the result goes straight into the file one column at a time; on Starter it
   opens in memory under Starter's gates (100,000 cells, one worker), and what
   the run wrote is written back section by section afterwards;
2. the function's declared obs inputs are checked against the file
   (`precondition_missing` names what is absent);
3. the write runs inside the shared workspace's `writing` block when
   spatialbridge is present, which takes the `obs` lease Plexora's own
   rewrites respect and tells open viewers;
4. what the run recorded is read back: the products it wrote and the
   experimental unit with its replicate count.

Only this module, `runner`, `working` and `licence.activate` import scimappro.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import sys
import warnings
from pathlib import Path

from plexora.plugins.scimap.server import catalog


class RunError(Exception):
    """A refusal the page shows as it is: `code` picks the treatment, `message`
    is the sentence, `detail` carries what a retry needs."""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


def scimappro():
    """`import scimappro`, quietly, as an agent host."""
    if "ipykernel" not in sys.modules:
        os.environ.setdefault("SCIMAPPRO_AGENT_HOST", "plexora")
    os.environ.setdefault("SCIMAPPRO_QUIET", "1")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import scimappro as sm
    except ImportError as exc:
        raise RunError("not_installed", "Scimap Pro is not installed here.") from exc
    return sm


def function_for(sm, key):
    namespace, name = key.split(".", 1)
    fn = getattr(getattr(sm, namespace, None), name, None)
    if fn is None:
        raise RunError("not_available",
                       f"This Scimap Pro ({_version()}) has no {key}. A newer release adds it.")
    return fn


def _version():
    try:
        from importlib.metadata import version

        return version("scimappro")
    except Exception:  # noqa: BLE001
        return "?"


def _older_than(minimum) -> bool:
    import re

    parts = [int(x) for x in re.findall(r"\d+", _version())[:3]]
    return bool(parts) and tuple(parts + [0] * (3 - len(parts))) < tuple(minimum)


def licence_refusal(exc):
    """Scimap Pro's `LicenseError` as a refusal with its own sentence. Over the
    Starter cell cap the library says `limit_exceeded`, kept in `reason`."""
    detail = {"provider": "scimappro"}
    if getattr(exc, "entitlement", None):
        detail["entitlement"] = exc.entitlement
    reason = getattr(exc, "code", None)
    if reason not in (None, "license_required"):
        detail["reason"] = reason
    return RunError("license_required", str(exc), detail)


def _unquote(value):
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] in "\"'":
        return value[1:-1]
    return value


def _missing(record, adata_or_handle, kwargs):
    columns = set(map(str, adata_or_handle.obs.columns))
    missing = []
    for name in (record.get("requires") or {}).get("obs") or []:
        actual = name
        for parameter in record.get("params") or []:
            if _unquote(parameter.get("default")) == name and kwargs.get(parameter["name"]):
                actual = kwargs[parameter["name"]]
        if isinstance(actual, str) and actual not in columns:
            missing.append(actual)
    return missing


def streams():
    """Whether this licence grants out-of-core work (`streamData=True`)."""
    from scimappro._license import GUARD
    from scimappro._license._policy import FEATURE_STREAMING

    return bool(GUARD.allows(FEATURE_STREAMING))


class _InMemory:
    """An in-memory run's bookkeeping: what the table held before, what the run
    wrote, and writing that -- and only that -- back into the file.

    The sections are Scimap Pro's own diff, the one its MCP server makes
    (`_execution._snapshot`/`_diff`/`writtenSections`), plus the registry's
    declared outputs when the run's ledger moved -- an in-memory diff sees new
    keys but not a key rewritten in place. What `flushSections` cannot write
    one element at a time (X, a change in the cells, a graph in obsp) is
    refused: that needs out-of-core work.
    """

    def __init__(self, adata, record, kwargs, function, label):
        from scimappro._store import openTable
        from scimappro.ai._mcp import _execution

        self.adata = adata
        self.record, self.kwargs, self.function, self.label = record, kwargs, function, label
        table = openTable(adata)
        self.snapshot = _execution._snapshot(table)
        self.ledgers = _execution._ledgerKeys(table)
        self.ledger = _execution._ledgerState(table, label)
        self.allLedgers = _ledger_states(table)
        self.shape = (_execution._cells(table), int(adata.n_vars))
        self.matrix = adata.X
        self.matrixPrint = _matrix_print(table)
        self.why = None

    def written(self, output):
        from scimappro._store import openTable
        from scimappro.ai._mcp import _execution, _lineage

        if _execution._isDataset(output) and output is not self.adata:
            self.why = "returns a new dataset rather than adding to this one"
            return "all"
        table = openTable(self.adata)
        if (_execution._cells(table), int(self.adata.n_vars)) != self.shape:
            self.why = "changes which cells or features the table holds"
            return "all"
        if self.adata.X is not self.matrix or _matrix_print(table) != self.matrixPrint:
            self.why = "rewrites the table's matrix (X)"
            return "all"
        changed = _execution._diff(self.snapshot, _execution._snapshot(table))
        products, _ = _execution._products(table, self.label, self.ledgers)
        sections = _execution.writtenSections(changed, products)
        if sections != "all":
            sections = set(sections)
            ledger = _execution._ledgerState(table, self.label)
            if ledger is None or ledger != self.ledger:
                sections |= _lineage.declaredOutputs(self.record, self.kwargs, self.function)
            if _ledger_states(table) != self.allLedgers:
                sections.add("uns:scimappro")
        if sections == "all" or "X" in sections:
            self.why = "rewrites the table's matrix (X)"
            return "all"
        return sorted(s for s in sections if _holds(self.adata, s))

    def write_back(self, output, path, key, writes=()):
        from scimappro._store._flush import FLUSHABLE, flushSections, splitSection

        sections = self.written(output)
        if sections == "all":
            raise _whole_file_refusal(key, path, self.why, "all")
        # What the step says it rewrites in place (a layer `pp.rescale`
        # rescales where it stands): a diff of keys cannot see it.
        sections = sorted(set(sections) | {s for s in writes if _holds(self.adata, s)})
        # A neighbor graph in obsp: a Scimap Pro that flushes obsp writes it
        # back (PCA & neighbours builds the graph Clustering reads). An older
        # one cannot write it element by element, so it is left out rather
        # than refusing the whole run -- the next step builds it again.
        if "obsp" not in FLUSHABLE:
            sections = [s for s in sections if not s.startswith("obsp:")]
        whole = [s for s in sections if splitSection(s)[0] not in FLUSHABLE]
        if whole:
            raise _whole_file_refusal(key, path, f"writes {', '.join(whole)}", whole)
        if not sections:
            return []
        return flushSections(self.adata, path, sections, tablePath="")


def _ledger_states(table):
    from scimappro.ai._mcp import _execution

    return {label: _execution._ledgerState(table, label)
            for label in _execution._ledgerKeys(table)}


def _matrix_print(table):
    from scimappro._fingerprint import fingerprintMatrix

    try:
        return fingerprintMatrix(table)
    except Exception:  # noqa: BLE001 - no X to fingerprint
        return None


def _holds(adata, section):
    kind, _, name = section.partition(":")
    holder = {"obs": adata.obs.columns, "var": adata.var.columns, "uns": adata.uns,
              "obsm": adata.obsm, "layers": adata.layers, "obsp": adata.obsp,
              "varm": adata.varm, "varp": adata.varp}.get(kind)
    return holder is not None and name in holder


def _whole_file_refusal(key, path, why, sections):
    from scimappro._license import LICENSING
    from scimappro._license._errors import featureMessage
    from scimappro._license._gates import gateState
    from scimappro._license._policy import FEATURE_STREAMING

    state = gateState(LICENSING.current(network=False))
    message = featureMessage(
        f"Running {key}, which {why}, needs Scimap Pro's out-of-core work to write "
        f"the result into {Path(path).name}: nothing was written. It",
        state.mode, state.reason)
    return RunError("license_required", message,
                    {"provider": "scimappro", "entitlement": FEATURE_STREAMING,
                     "sections": sections})


#: The notebook's Memory setting, as `io.init`'s `streamData`. Real booleans
#: only: Scimap Pro reads any other string as "stream".
MEMORY = {"auto": "auto", "memory": False, "stream": True}


def execute(path, key, kwargs, *, label=None, say=None, run_id=None, memory="auto", writes=()):
    """Run `key` on the working file at `path` with exactly `kwargs`.

    Returns `{outputs, mode, warnings, experimental_unit, written, revision}`.
    The kwargs are the step's effective arguments: the caller has already
    filled in the implicit ones (`imageId`, `randomState`, `label`, ...), so
    what is stored with the run is what was called, and what an export writes.
    """
    say = say or (lambda message: None)
    sm = scimappro()
    from scimappro._license._errors import LicenseError

    function = function_for(sm, key)
    signature = inspect.signature(inspect.unwrap(function)).parameters
    call = {k: as_frame(v) for k, v in kwargs.items()}
    takes_extra = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.values())
    unknown = sorted(k for k in call if k not in signature)
    if unknown and not takes_extra:
        raise RunError("invalid_input", f"{key} has no parameter(s) {unknown}")
    for knob, setting in (("verbose", False), ("progress", False)):
        if knob in signature:
            call[knob] = setting
    namespace = key.split(".", 1)[0]
    record = (catalog.load().get("functions") or {}).get(key) or {}

    want = MEMORY.get(memory or "auto", "auto")
    if want == "auto" and not streams():
        want = False
    if want is True and not streams():
        raise RunError("license_required",
                       "Streaming from disk is part of a Scimap Pro license. Set Memory to "
                       "Automatic or In memory in Prepare data, or add a license.",
                       {"provider": "scimappro", "entitlement": "streaming"})
    if (want is not True and key == "tl.cluster" and call.get("layer") == "raw"
            and call.get("subsetMarkers") and _older_than((0, 1, 29))):
        # Scimap Pro 0.1.28 in memory assigns the whole raw matrix to the
        # chosen markers ("cannot reshape array"); 0.1.29 reads only those
        # markers. Its streamed path is correct, so a licensed run streams.
        if not streams():
            raise RunError("not_available",
                           f"Scimap Pro {_version()} cannot cluster a subset of markers from the raw "
                           "layer in memory. Choose every marker, or set Matrix to X, or update to "
                           "Scimap Pro 0.1.29.")
        want = True
    say("Opening the working file")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            handle = sm.io.init(str(path), streamData=want, verbose=False)
    except LicenseError as exc:
        raise licence_refusal(exc) from exc
    from scimappro._lazy import isLazy

    streaming = bool(isLazy(handle))
    memory = None
    written_sections = None
    revision = None
    try:
        if not streaming and namespace != "pl":
            memory = _InMemory(handle, record, call, function, label)
        missing = _missing(record, handle, call)
        if missing:
            raise RunError("precondition_missing",
                           f"{key} needs the column(s) {', '.join(missing)}, which the "
                           "working file does not have.", {"missing": missing})
        workspace = None
        try:
            from spatialbridge import workspace as _workspace

            workspace = _workspace.ensure(str(path), producer="scimappro")
        except ImportError:
            pass
        except Exception:  # noqa: BLE001 -- the run does not need the bridge
            workspace = None
        writing = (workspace.writing("all", "scimappro", scope="obs",
                                     execution_id=f"ex_scimap_{run_id or os.getpid()}")
                   if workspace is not None and namespace != "pl"
                   else contextlib.nullcontext())
        say("Running")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                with writing as written, contextlib.redirect_stdout(sys.stderr):
                    output = function(handle, **call)
                    if memory is not None:
                        say("Writing the result")
                        written_sections = memory.write_back(output, path, key, writes)
                revision = getattr(written, "revision", None)
            except LicenseError as exc:
                raise licence_refusal(exc) from exc
            except RunError:
                raise
            except (KeyError, ValueError, TypeError) as exc:
                text = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
                raise RunError("invalid_input", text) from exc
            except Exception as exc:  # noqa: BLE001 -- a bridge refusal, by name
                if type(exc).__name__ == "BridgeError":
                    raise RunError("conflict", getattr(exc, "message", str(exc))) from exc
                raise
    finally:
        with contextlib.suppress(Exception):
            handle.close()

    result = {"mode": "out-of-core" if streaming else "in-memory",
              "revision": revision,
              "warnings": sorted({str(w.message) for w in caught
                                  if w.category.__name__ in ("UserWarning", "RuntimeWarning")})[:20]}
    if written_sections is not None:
        result["written"] = written_sections
    result.update(evidence(path, label))
    return result


def as_frame(value):
    """A table argument stored as JSON (`tools.frame`) as a pandas DataFrame;
    anything else as it is."""
    if isinstance(value, dict) and "__frame__" in value:
        import pandas as pd

        spec = value["__frame__"]
        return pd.DataFrame(spec.get("rows") or [], columns=spec.get("columns") or None)
    return value


def evidence(path, label):
    """What the run wrote and the claim ceiling it recorded, read back."""
    from scimappro._incremental import readLedgers
    from scimappro._store import openTable

    outputs, unit = [], {"unit": None, "nReplicates": None}
    with contextlib.suppress(Exception):
        table = openTable(str(path), path="", writable=False)
        try:
            ledger = readLedgers(table).get(label) if label else None
            if ledger is not None:
                for stage in (ledger.products or {}).values():
                    outputs.extend(spec for spec in stage if spec not in outputs)
            stored = table.uns(f"{label}_params") if label and table.hasUns(
                f"{label}_params") else None
            if isinstance(stored, dict):
                unit = {"unit": stored.get("unitOfReplication"),
                        "nReplicates": stored.get("nReplicates")}
        finally:
            table.close()
    return {"outputs": outputs, "experimental_unit": unit}


def drop_result(path, label):
    """Remove one result Scimap Pro wrote: its products, its `_params` and
    `_samples` records and its ledger entry. Returns the elements dropped."""
    scimappro()
    from scimappro._incremental import forget, readLedgers
    from scimappro._store import openTable

    table = openTable(str(path), path="", writable=True)
    try:
        ledger = readLedgers(table).get(label)
        specs = []
        if ledger is not None:
            specs = [spec for stage in (ledger.products or {}).values() for spec in stage
                     if not spec.startswith("~")]
            forget(table, label)
    finally:
        table.close()
    specs += [f"obs:{label}", f"uns:{label}_params", f"uns:{label}_samples"]
    return drop_elements(path, specs)


def drop_elements(path, specs):
    """Delete `obs:<col>` / `uns:<key>` / `obsm:<key>` elements in place."""
    import h5py

    dropped = []
    with h5py.File(str(path), "r+") as root:
        for spec in dict.fromkeys(specs):
            kind, _, name = spec.partition(":")
            if kind not in ("obs", "uns", "obsm") or kind not in root:
                continue
            group = root[kind]
            if name not in group:
                continue
            del group[name]
            if kind == "obs":
                order = [c.decode() if isinstance(c, bytes) else str(c)
                         for c in group.attrs.get("column-order", [])]
                group.attrs["column-order"] = [c for c in order if c != name]
            dropped.append(spec)
    return dropped
