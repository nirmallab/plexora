# Points: neighbours within a radius. Included by _kernels.pyx.
#
# Port of plexora/plugins/gating/server/autogate/kernels.py `_count` (keyed
# under that name in KERNELS; called _neighbour_count here, beside surface
# nets' own `_count`).


def _neighbour_count_t(const double[:] qx, const double[:] qy, const int64_t[:] qself,
                       const double[:] xs, const double[:] ys, const uint8_t[:] flags, double r2,
                       const int64_t[:] order, const int64_t[:] starts, double x0, double y0,
                       double side, int64_t nx, int64_t ny, int64_t[:] out_n, int64_t[:] out_f):
    cdef Py_ssize_t i, m = qx.shape[0]
    cdef int64_t gx, gy, bx, by, b, k, j, n, f
    cdef double x, y, dx, dy
    with nogil:
        for i in range(m):
            x = qx[i]
            y = qy[i]
            if not (isfinite(x) and isfinite(y)):
                out_n[i] = 0
                out_f[i] = 0
                continue
            gx = <int64_t>_floordiv(x - x0, side)
            gy = <int64_t>_floordiv(y - y0, side)
            n = 0
            f = 0
            for by in range(gy - 1, gy + 2):
                if by < 0 or by >= ny:
                    continue
                for bx in range(gx - 1, gx + 2):
                    if bx < 0 or bx >= nx:
                        continue
                    b = by * nx + bx
                    for k in range(starts[b], starts[b + 1]):
                        j = order[k]
                        if j == qself[i]:
                            continue
                        dx = xs[j] - x
                        dy = ys[j] - y
                        if dx * dx + dy * dy <= r2:
                            n += 1
                            if flags[j]:
                                f += 1
            out_n[i] = n
            out_f[i] = f


def _neighbour_count(qx, qy, qself, xs, ys, flags, r2, order, starts, x0, y0, side, nx, ny,
                     out_n, out_f):
    if (_arr(qx, _F8, 1) and _arr(qy, _F8, 1) and _arr(qself, _I8, 1) and _arr(xs, _F8, 1)
            and _arr(ys, _F8, 1) and _arr(flags, _BOOL, 1) and _real(r2) and _arr(order, _I8, 1)
            and _arr(starts, _I8, 1) and _real(x0) and _real(y0) and _real(side) and side > 0
            and _int(nx) and _int(ny) and _arr(out_n, _I8, 1, True) and _arr(out_f, _I8, 1, True)
            and qy.shape[0] >= qx.shape[0] and qself.shape[0] >= qx.shape[0]
            and out_n.shape[0] >= qx.shape[0] and out_f.shape[0] >= qx.shape[0]
            and starts.shape[0] >= nx * ny + 1):
        _neighbour_count_t(qx, qy, qself, xs, ys, flags.view(np.uint8), r2, order, starts, x0, y0,
                           side, nx, ny, out_n, out_f)
        return None
    return _fall("plexora.plugins.gating.server.autogate.kernels._count",
                 (qx, qy, qself, xs, ys, flags, r2, order, starts, x0, y0, side, nx, ny, out_n,
                  out_f))
