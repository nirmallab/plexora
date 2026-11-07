"""plexora-kernels: every compiled port agrees with numba and with the Python it ports.

The ports (kernels/, plan L5) are what the website runs instead of Python and
what the desktop runs instead of numba, so each is held to both, on the same
inputs (tests/kernel_cases.py): the `test` size of every case, run three ways
in three fresh interpreters -- the port (default), numba (PLEXORA_KERNELS=0)
and the Python (and PLEXORA_NO_NUMBA=1) -- and every result compared, along
with every array the kernel wrote into.

The rule:
- **Port and numba: identical**, bit for bit, integers and floats alike (NaN
  where NaN), except `segqc.label_stats`'s `excess`, which takes `expm1f`
  from the C library each was linked against (equal to 1e-12 relative).
- **Port and Python: identical for integers; floats to 1e-6 relative.** The
  Python form under numpy 2 keeps a float32 sum in float32 where numba and
  the port widen it (NEP 50), and `math.expm1` is double precision.

Skipped where plexora-kernels is not installed; the rest of the suite runs
the kernels whichever way this interpreter has them.
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]

plexora_kernels = pytest.importorskip("plexora_kernels")

from plexora.server.utils import jit  # noqa: E402
from tests import kernel_cases  # noqa: E402

if jit.kernels_module() is None:
    pytest.skip("plexora-kernels is switched off or speaks another interface",
                allow_module_level=True)

MODES = {
    "compiled": {},
    "numba": {"PLEXORA_KERNELS": "0"},
    "python": {"PLEXORA_KERNELS": "0", "PLEXORA_NO_NUMBA": "1"},
}

#: (case, output index) pairs whose floats come from a C library function and
#: may differ between the port's and numba's builds of it in the last place.
LIBM = {("segqc.label_stats", 7)}


def _have_numba():
    try:
        import numba  # noqa: F401
    except Exception:
        return False
    return True


def _run(mode, inputs, out):
    env = {**os.environ, **MODES[mode]}
    for name in ("PLEXORA_KERNELS", "PLEXORA_NO_NUMBA"):
        if name not in MODES[mode]:
            env.pop(name, None)
    done = subprocess.run([sys.executable, "-m", "tests.kernel_cases", "run", "--inputs",
                           str(inputs), "--out", str(out)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    with open(out, "rb") as handle:
        return pickle.load(handle)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("kernel-cases")
    inputs = folder / "inputs.pkl"
    with open(inputs, "wb") as handle:
        pickle.dump(kernel_cases.build("test"), handle)
    modes = [m for m in MODES if m != "numba" or _have_numba()]
    return {mode: _run(mode, inputs, folder / f"{mode}.pkl") for mode in modes}


def _outputs(entry):
    result = entry["result"]
    if isinstance(result, tuple):
        values = list(result)
    elif result is None:
        values = []
    else:
        values = [result]
    return values + entry["arrays"]


def _same(a, b, *, rtol=0.0):
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        return False
    if a.dtype.kind in "fc" or b.dtype.kind in "fc":
        if rtol:
            return np.allclose(a, b, rtol=rtol, atol=1e-9, equal_nan=True)
        return np.array_equal(a, b, equal_nan=True)
    return np.array_equal(a, b)


def _compare(left, right, *, float_rtol, libm_rtol=None):
    differ = []
    for name, entry in left.items():
        other = right[name]
        a, b = _outputs(entry), _outputs(other)
        if len(a) != len(b):
            differ.append(f"{name}: {len(a)} outputs vs {len(b)}")
            continue
        for i, (x, y) in enumerate(zip(a, b)):
            rtol = float_rtol
            if (name, i) in LIBM and libm_rtol is not None:
                rtol = max(rtol, libm_rtol)
            if isinstance(x, (bytes, bytearray)):
                ok = bytes(x) == bytes(y)
            else:
                ok = _same(x, y, rtol=rtol)
            if not ok:
                differ.append(f"{name}[{i}]")
    return differ


def test_every_case_ran_the_way_its_mode_says(runs):
    for mode, got in runs.items():
        for name, entry in got.items():
            want = mode if not (mode == "numba" and entry["kernel"] == "lzw_decode") else "python"
            assert entry["impl"] == want, (mode, name, entry["impl"])


def test_the_port_equals_numba(runs):
    if "numba" not in runs:
        pytest.skip("no numba here")
    assert _compare(runs["compiled"], runs["numba"], float_rtol=0.0, libm_rtol=1e-12) == []


def test_the_port_equals_the_python_it_ports(runs):
    assert _compare(runs["compiled"], runs["python"], float_rtol=1e-6) == []


def test_every_kernel_has_a_case():
    cases = kernel_cases.build("test")
    covered = {name.split("/")[0] for name in cases}
    keys = {f"{kernel_cases.MODULES[name.split('.')[0]]}.{name.split('.')[1]}"
            for name in covered if name != "lzw_decode"}
    assert keys == set(plexora_kernels.KERNELS)
    assert "lzw_decode" in covered
    assert set(kernel_cases.FEATURES) == covered


def test_the_ports_follow_this_checkout():
    """A kernel changed here is no longer taken from the package: re-port it,
    then `python kernels/tools/fingerprints.py` (see kernels/README.md)."""
    assert jit.stale_kernels() == {}


def _probe(code, **env):
    full = {**os.environ, **env}
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


#: Every product path into the kernels, through its public function.
PRODUCT = """
import json, sys
import numpy as np
from plexora.server.utils import jit, label_overlay, label_kernels, cell_features
from plexora.server.surface import nets, mesh
from plexora.plugins.qc.server.segqc import kernels as segqc
from plexora.plugins.gating.server.autogate import kernels as autogate
from tests import kernel_cases
rng = np.random.default_rng(5)
labels = kernel_cases.cells_2d(96, 120, 60, rng)
label_overlay.boundary_mask(labels, 2)
label_overlay.boundary_mask(labels.astype(np.int32), 1)
label_kernels.label_bboxes(labels.astype(np.uint16))
marker = rng.random(labels.shape).astype(np.float32) * 100
def reader(a):
    held = np.pad(a, 2)
    return lambda box: held[box[1] + 2:box[3] + 2, box[0] + 2:box[2] + 2]
cell_features.accumulate(reader(marker), reader(labels), width=120, height=96, tile=64,
                         read_dna=reader(marker))
dna = np.log1p(marker)
arrays = [np.zeros(int(labels.max()) + 1) for _ in range(7)]
segqc.label_stats(labels, dna, 0, 0, 1, 95, 1, 119, 0.5, *arrays)
segqc.pairs(labels, dna, 0, 0, (1, 95, 1, 119))
xs, ys = rng.uniform(0, 300, 500), rng.uniform(0, 300, 500)
grid = autogate.Grid(xs, ys, 20.0)
autogate.neighbour_counts(grid, flags=xs > 100)
autogate.neighbour_counts_at(grid, np.array([5.0, 50.0]), np.array([5.0, 70.0]))
volume = kernel_cases.cells_3d(8, 40, 44, 10, rng)
pooled = nets.pool(volume, 2)
cube_shape = tuple(int(v) + 1 for v in volume.shape)
piece = nets.block_surface(np.pad(volume, 1), (0, 0, 0), cube_shape)
positions, triangles, ids = nets.assemble([piece], cube_shape)
m = mesh.sort_by_object(mesh.Mesh(positions=positions.astype(np.float32),
                                  triangles=triangles.astype(np.uint32), object_id=ids))
mesh.vertex_normals(m.positions, m.triangles)
mesh.taubin(m.positions, m.triangles, mesh.Smoothing(iterations=2))
mesh.object_volumes(m)
mesh.measure(m, dims=(44, 40, 8))
from plexora.server.volume import measure
measure.prime()
package = jit.kernels_module()
print(json.dumps({"fallbacks": dict(package.FALLBACK_CALLS) if package else None,
                  "numba": "numba" in sys.modules,
                  "impl": {k: jit.implementation(v) for k, v in {
                      "boundary": label_kernels._boundary, "nets": nets._count,
                      "mesh": mesh._adjacency, "segqc": segqc.label_stats,
                      "autogate": autogate._count, "features": cell_features._tile_kernel,
                      "measure": measure._bbox_kernel}.items()}}))
"""


def test_the_product_never_falls_back_and_never_loads_numba():
    """Every product path's arguments are ones a port was built for; and with
    every kernel compiled ahead, numba is never imported (no LLVM warm-up)."""
    got = _probe(PRODUCT)
    assert got["fallbacks"] == {}
    assert set(got["impl"].values()) == {"compiled"}
    assert got["numba"] is False


def test_switched_off_the_kernels_are_numba_or_python():
    got = _probe(PRODUCT, PLEXORA_KERNELS="0")
    assert got["fallbacks"] is None
    assert set(got["impl"].values()) == ({"numba"} if _have_numba() else {"python"})


def test_a_call_a_port_was_not_built_for_runs_the_fallback():
    got = _probe("""
        import json
        import numpy as np
        from plexora.server.utils import jit
        from plexora.server.volume import measure
        labels = np.array([[0, 2], [1, 2]], dtype=np.int64)      # the port takes uint32
        boxes = [np.full(3, v, np.int64) for v in (99, -1) * 3]
        measure._bbox_kernel(labels, 4, 7, *boxes)
        print(json.dumps({"boxes": [b.tolist() for b in boxes],
                          "fallbacks": jit.kernels_module().FALLBACK_CALLS}))
    """)
    assert got["fallbacks"] == {"plexora.server.volume.measure._bbox_kernel": 1}
    assert got["boxes"] == [[99, 0, 1], [-1, 0, 1], [99, 5, 4], [-1, 5, 5], [99, 7, 7],
                            [-1, 7, 7]]


def test_a_port_of_older_code_is_not_used():
    got = _probe("""
        import json, logging
        logging.basicConfig()
        import plexora_kernels
        plexora_kernels.FINGERPRINTS["plexora.server.utils.label_kernels"]["_bboxes"] = "0" * 20
        from plexora.server.utils import jit, label_kernels
        print(json.dumps({"boundary": jit.implementation(label_kernels._boundary),
                          "bboxes": jit.implementation(label_kernels._bboxes),
                          "stale": jit.stale_kernels()}))
    """)
    # The whole module falls back, not only the kernel that changed.
    assert got["boundary"] == got["bboxes"] != "compiled"
    assert got["stale"] == {"plexora.server.utils.label_kernels": ["_bboxes"]}


def test_lzw_runs_compiled_and_agrees():
    from plexora.browser import imagecodecs_shim as shim

    assert shim._lzw() is plexora_kernels.lzw_decode
    rng = np.random.default_rng(9)
    for data in (b"", b"x", bytes(range(256)) * 50, rng.integers(0, 256, 200_000, np.uint8).tobytes(),
                 np.repeat(np.arange(70_000, dtype=np.uint16), 2).tobytes()):
        encoded = kernel_cases.lzw_encode(data)
        assert shim.lzw_decode(encoded) == shim.lzw_decode_python(encoded) == data
