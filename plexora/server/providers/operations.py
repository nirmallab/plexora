"""Work that has to happen where the table's file is.

Most of what Plexora asks of a cell table is a read that can be answered with a
buffer -- a column, a mask, a histogram. Three things are not, and they are the
reason this registry exists:

- **ROI -> cell mapping.** A polygon is a few hundred coordinates; the cells it
  is tested against are millions of points. Shipping the polygon to the cells
  is a kilobyte; shipping the cells to the polygon is the entire table.
- **Writing annotations back into the source file.** `roi/server/adapters.py`
  aligns per-row values against the file's own `obs` and refuses when the row
  counts disagree, because a positional write against a file that changed puts
  every label on the wrong cell. That check compares the loaded frame with the
  file on disk, so both have to be on the same machine for it to mean anything.
- **Writing gate thresholds into `uns`.** Same argument, same file.

So they are registered here by name, and a `TableHandle` runs one either
directly (the table is local) or by naming it to the node that holds the table,
which runs the identical registered function against its own loaded copy. One
implementation, two transports -- the plugin code does not know which it is on.

Registration happens as a side effect of importing the plugin's server module,
which the plugin loader already does. A node runs the same pip package and
loads the same bundled plugins, so the names resolve identically at both ends.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

#: name -> implementation. Keys are namespaced by plugin ("roi.map_cells") so
#: two plugins cannot claim one name, and so an unknown name coming off the
#: wire names the plugin that should have provided it.
_OPERATIONS: dict[str, Callable] = {}


class UnknownOperation(LookupError):
    """No implementation is registered under this name.

    On the primary that means a plugin was not loaded. Coming back from a node
    it means the node is running a build without that plugin, which is worth
    saying plainly rather than reporting as a generic node error.
    """


def table_operation(name: str):
    """Register a function as the implementation of a named table operation.

        @table_operation("roi.write_cell_columns")
        def _write(dataset, payload):
            ...

    The function takes the `Dataset` handle for the project the table belongs
    to and a JSON-serializable payload, and returns a JSON-serializable result.
    That signature is the wire format: anything that cannot survive
    `json.dumps` cannot be an argument, which is a real constraint and a
    deliberate one -- an operation that wants to pass a DataFrame is an
    operation that has not decided where it runs.
    """

    def register(function: Callable) -> Callable:
        existing = _OPERATIONS.get(name)
        if existing is not None and existing is not function:
            # Re-registering the same function is ordinary (a module imported
            # twice under different names during tests); two different ones is
            # a collision that would make which implementation runs depend on
            # import order.
            raise ValueError(f"table operation {name!r} is already registered")
        _OPERATIONS[name] = function
        return function

    return register


def run_table_operation(name: str, dataset, payload: Mapping[str, Any] | None = None) -> Any:
    """Run a registered operation against a dataset on this machine.

    Called on the primary when the table is local, and on the node when it is
    not -- the same call either way, which is what makes the two transports
    interchangeable.
    """
    implementation = _OPERATIONS.get(name)
    if implementation is None:
        raise UnknownOperation(
            f"no table operation named {name!r} is registered; the plugin that "
            "provides it may not be installed on this server"
        )
    return implementation(dataset, dict(payload or {}))


#: name -> implementation, for operations whose result is a stream rather than
#: a value. Kept separate from `_OPERATIONS` because the two have different
#: transports at the far end -- a value is one JSON body, a stream is a chunked
#: response -- and a caller has to know which it is asking for.
_STREAMS: dict[str, Callable] = {}


def table_stream(name: str):
    """Register a table operation that yields its result in chunks.

    For the one case a JSON round trip cannot serve: exporting the whole table
    as CSV. That is genuinely megabytes-to-gigabytes of text, it is a download
    the user is watching, and materializing it as a string to put in a JSON
    body would hold the serialized copy alongside the frame it came from --
    which is exactly what `routes._stream_csv` exists to avoid locally.

    The function takes (dataset, payload) like any other operation and yields
    `str` or `bytes` chunks.
    """

    def register(function: Callable) -> Callable:
        existing = _STREAMS.get(name)
        if existing is not None and existing is not function:
            raise ValueError(f"table stream {name!r} is already registered")
        _STREAMS[name] = function
        return function

    return register


def run_table_stream(name: str, dataset, payload: Mapping[str, Any] | None = None):
    """Run a registered streaming operation against a dataset on this machine."""
    implementation = _STREAMS.get(name)
    if implementation is None:
        raise UnknownOperation(
            f"no table stream named {name!r} is registered; the plugin that "
            "provides it may not be installed on this server"
        )
    return implementation(dataset, dict(payload or {}))


def registered_operations() -> list[str]:
    """Every operation this process can run, for a node's capability list.

    Streams are listed with the same names as values -- a node either has the
    plugin or it does not, and a caller checking a capability is asking whether
    the work can happen there, not how the bytes come back.
    """
    return sorted(set(_OPERATIONS) | set(_STREAMS))
