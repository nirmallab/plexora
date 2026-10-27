"""Reading back the pickles Plexora itself writes, without trusting their bytes.

Three things are stored as pickles: the cell index cache next to a project's
derived files, and the channel-list and gate rows in the project databases.
None of that is a secret, so all of it can sit somewhere other people can
write -- a lab's shared root is exactly where `paths.derived_root` prefers to
put it. A plain `pickle.load` of a file someone else can replace runs whatever
that file asks for, as whoever opens the project next.

So the format stays (a database written by this version still opens in an
older one, and an older one's caches still open here, at the same speed) and
only the reading changes: `find_class` admits the handful of classes those
pickles are actually made of -- numpy's array and scalar reconstructors, the
scipy tree, the datetime types a table cell can hold -- and refuses every
other import. Data that names anything else is not data Plexora wrote.

The cost is a dictionary lookup per class reference, which a pickle of a
million-row index makes three of.
"""

from __future__ import annotations

import io
import pickle

#: (module, name) pairs a Plexora pickle may reference. numpy moved its
#: internals from `numpy.core` to `numpy._core` in 2.0; both spellings are
#: here because a cache written under either must still read.
_ALLOWED = frozenset({
    ("numpy", "ndarray"),
    ("numpy", "dtype"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy._core.numeric", "_frombuffer"),
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"),
    ("scipy.spatial._ckdtree", "cKDTree"),
    ("scipy.spatial.ckdtree", "cKDTree"),
    ("datetime", "date"),
    ("datetime", "datetime"),
    ("datetime", "time"),
    ("datetime", "timedelta"),
    ("datetime", "timezone"),
    ("decimal", "Decimal"),
    ("collections", "OrderedDict"),
    ("builtins", "set"),
    ("builtins", "frozenset"),
    ("builtins", "complex"),
    ("builtins", "bytearray"),
    ("builtins", "slice"),
    ("builtins", "range"),
})


class UnsafePickle(pickle.UnpicklingError):
    """The pickle referenced something Plexora never writes into one."""


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _ALLOWED:
            return super().find_class(module, name)
        # numpy dtypes pickle as `numpy.dtypes.Float64DType` and the like in
        # numpy >= 2; every class in that module is a plain dtype class.
        if module == "numpy.dtypes" and name.endswith("DType"):
            return super().find_class(module, name)
        raise UnsafePickle(f"refusing to load {module}.{name} from a stored pickle")


def load(handle):
    """`pickle.load`, admitting only the classes Plexora's own pickles hold."""
    return _Unpickler(handle).load()


def loads(data):
    """`pickle.loads`, admitting only the classes Plexora's own pickles hold."""
    return _Unpickler(io.BytesIO(data)).load()
