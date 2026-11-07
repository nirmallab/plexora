# Surfaces: surface nets over a label volume, and the mesh kernels. Included by _kernels.pyx.
#
# Ports of plexora/server/surface/nets.py and plexora/server/surface/mesh.py.


# -- surface nets ----------------------------------------------------------------------


def _pool_runs_t(const uint32_t[:, :, :] planes, int64_t stride, uint32_t[:, :, :] out):
    cdef Py_ssize_t n = planes.shape[0], height = planes.shape[1], width = planes.shape[2]
    cdef Py_ssize_t p, y, x, z, zz, z0, z1, best_n, count
    cdef uint32_t first, value, best
    cdef bint same
    with nogil:
        for p in range(out.shape[0]):
            z0 = p * stride
            z1 = z0 + stride if z0 + stride < n else n
            for y in range(height):
                for x in range(width):
                    first = planes[z0, y, x]
                    same = True
                    for z in range(z0 + 1, z1):
                        if planes[z, y, x] != first:
                            same = False
                            break
                    if same:
                        out[p, y, x] = first
                        continue
                    best = 0
                    best_n = 0
                    for z in range(z0, z1):
                        value = planes[z, y, x]
                        if value == 0 or value == best:
                            continue
                        count = 0
                        for zz in range(z0, z1):
                            if planes[zz, y, x] == value:
                                count += 1
                        if count > best_n:
                            best = value
                            best_n = count
                    out[p, y, x] = best


def _pool_runs(planes, stride, out):
    if (_arr(planes, _U4, 3) and _arr(out, _U4, 3, True)
            and _int(stride) and stride >= 1 and out.shape[1:] == planes.shape[1:]
            and (out.shape[0] == 0 or (out.shape[0] - 1) * stride < planes.shape[0])):
        _pool_runs_t(planes, stride, out)
        return out
    return _fall("plexora.server.surface.nets._pool_runs", (planes, stride, out))


cdef inline bint _same8(const uint32_t* p, Py_ssize_t sy, Py_ssize_t sz, uint32_t* c) noexcept nogil:
    """`nets._corners` on a C-contiguous block: the cube at `p` (its corner 0)."""
    cdef int m
    c[0] = p[0]
    c[1] = p[1]
    c[2] = p[sy]
    c[3] = p[sy + 1]
    c[4] = p[sz]
    c[5] = p[sz + 1]
    c[6] = p[sz + sy]
    c[7] = p[sz + sy + 1]
    for m in range(1, 8):
        if c[m] != c[0]:
            return False
    return True


cdef inline int _distinct8(const uint32_t* c, uint32_t* seen) noexcept nogil:
    """`nets._distinct`: the nonzero labels among the corners, in corner order."""
    cdef int m = 0, n, q
    cdef uint32_t value
    cdef bint new
    for n in range(8):
        value = c[n]
        if value == 0:
            continue
        new = True
        for q in range(m):
            if seen[q] == value:
                new = False
                break
        if new:
            seen[m] = value
            m += 1
    return m


def _corners_t(const uint32_t[:, :, :] data, Py_ssize_t i, Py_ssize_t j, Py_ssize_t k,
               uint32_t[:] c):
    cdef int n
    c[0] = data[i, j, k]
    c[1] = data[i, j, k + 1]
    c[2] = data[i, j + 1, k]
    c[3] = data[i, j + 1, k + 1]
    c[4] = data[i + 1, j, k]
    c[5] = data[i + 1, j, k + 1]
    c[6] = data[i + 1, j + 1, k]
    c[7] = data[i + 1, j + 1, k + 1]
    for n in range(1, 8):
        if c[n] != c[0]:
            return False
    return True


def _corners(data, i, j, k, c):
    if (_arr(data, _U4, 3) and _arr(c, _U4, 1, True)
            and c.shape[0] >= 8 and _int(i) and _int(j) and _int(k)
            and 0 <= i and i + 1 < data.shape[0] and 0 <= j and j + 1 < data.shape[1]
            and 0 <= k and k + 1 < data.shape[2]):
        return _corners_t(data, i, j, k, c)
    return _fall("plexora.server.surface.nets._corners", (data, i, j, k, c))


def _distinct_t(const uint32_t[:] c, uint32_t[:] seen):
    cdef int m = 0, n, q
    cdef uint32_t value
    cdef bint new
    for n in range(8):
        value = c[n]
        if value == 0:
            continue
        new = True
        for q in range(m):
            if seen[q] == value:
                new = False
                break
        if new:
            seen[m] = value
            m += 1
    return m


def _distinct(c, seen):
    if (_arr(c, _U4, 1) and _arr(seen, _U4, 1, True)
            and c.shape[0] >= 8 and seen.shape[0] >= 8):
        return _distinct_t(c, seen)
    return _fall("plexora.server.surface.nets._distinct", (c, seen))


def _nets_count_t(const uint32_t[:, :, ::1] data, const uint8_t[:] skip_y):
    cdef Py_ssize_t nz = data.shape[0], ny = data.shape[1], nx = data.shape[2]
    cdef Py_ssize_t sy = nx, sz = ny * nx
    cdef Py_ssize_t i, j, k
    cdef int axis
    cdef int64_t nv = 0, nq = 0
    cdef uint32_t c[8]
    cdef uint32_t seen[8]
    cdef uint32_t low, high
    cdef const uint32_t* row
    with nogil:
        for i in range(nz - 1):
            for j in range(ny - 1):
                if skip_y[j]:
                    continue
                row = &data[i, j, 0]
                for k in range(nx - 1):
                    if _same8(row + k, sy, sz, c):
                        continue
                    nv += _distinct8(c, seen)
                    low = c[0]
                    for axis in range(3):
                        high = c[1 << axis]
                        if high != low:
                            if low != 0:
                                nq += 1
                            if high != 0:
                                nq += 1
    return nv, nq


def _nets_count(data, skip_y):
    if (_arr(data, _U4, 3) and data.flags.c_contiguous and _arr(skip_y, _BOOL, 1)
            and min(data.shape) >= 1 and skip_y.shape[0] >= data.shape[1] - 1):
        return _nets_count_t(data, skip_y.view(np.uint8))
    return _fall("plexora.server.surface.nets._count", (data, skip_y))


def _fill_t(const uint32_t[:, :, ::1] data, const uint8_t[:] skip_y, int64_t cz0, int64_t cy0,
            int64_t cx0, int64_t stride_y, int64_t stride_z, const int64_t[:, :] corners,
            const int64_t[:, :] edges, int64_t[:] vcube, uint32_t[:] vlabel, double[:, :] vpos,
            int64_t[:] qcube, int8_t[:] qaxis, int8_t[:] qlow, uint32_t[:] qlabel):
    cdef Py_ssize_t nz = data.shape[0], ny = data.shape[1], nx = data.shape[2]
    cdef Py_ssize_t sy = nx, sz = ny * nx
    cdef Py_ssize_t i, j, k, nv = 0, nq = 0
    cdef Py_ssize_t vsize = min(vcube.shape[0], vlabel.shape[0], vpos.shape[0])
    cdef Py_ssize_t qsize = min(qcube.shape[0], qaxis.shape[0], qlow.shape[0], qlabel.shape[0])
    cdef int m, q, e, a, b, axis
    cdef int64_t gz, gy, gx, cube, n, sx, sy_, sz_
    cdef int64_t cor[8][3]
    cdef int ed[12][2]
    cdef uint32_t c[8]
    cdef uint32_t seen[8]
    cdef uint32_t label, low, high
    cdef const uint32_t* row
    cdef bint overflow = False
    for m in range(8):
        for q in range(3):
            cor[m][q] = corners[m, q]
    for e in range(12):
        ed[e][0] = <int>edges[e, 0]
        ed[e][1] = <int>edges[e, 1]
    with nogil:
        for i in range(nz - 1):
            for j in range(ny - 1):
                if skip_y[j]:
                    continue
                row = &data[i, j, 0]
                for k in range(nx - 1):
                    if _same8(row + k, sy, sz, c):
                        continue
                    gz = cz0 + i
                    gy = cy0 + j
                    gx = cx0 + k
                    cube = gz * stride_z + gy * stride_y + gx
                    m = _distinct8(c, seen)
                    for q in range(m):
                        label = seen[q]
                        n = 0
                        sx = 0
                        sy_ = 0
                        sz_ = 0
                        for e in range(12):
                            a = ed[e][0]
                            b = ed[e][1]
                            if (c[a] == label) != (c[b] == label):
                                n += 1
                                sx += cor[a][0] + cor[b][0]
                                sy_ += cor[a][1] + cor[b][1]
                                sz_ += cor[a][2] + cor[b][2]
                        if nv >= vsize:
                            overflow = True
                            nv += 1
                            continue
                        vcube[nv] = cube
                        vlabel[nv] = label
                        vpos[nv, 0] = <double>(gx - 1) + <double>sx / (2.0 * <double>n)
                        vpos[nv, 1] = <double>(gy - 1) + <double>sy_ / (2.0 * <double>n)
                        vpos[nv, 2] = <double>(gz - 1) + <double>sz_ / (2.0 * <double>n)
                        nv += 1
                    low = c[0]
                    for axis in range(3):
                        high = c[1 << axis]
                        if high == low:
                            continue
                        if low != 0:
                            if nq < qsize:
                                qcube[nq] = cube
                                qaxis[nq] = axis
                                qlow[nq] = 1
                                qlabel[nq] = <uint32_t>low
                            else:
                                overflow = True
                            nq += 1
                        if high != 0:
                            if nq < qsize:
                                qcube[nq] = cube
                                qaxis[nq] = axis
                                qlow[nq] = 0
                                qlabel[nq] = <uint32_t>high
                            else:
                                overflow = True
                            nq += 1
    if overflow:
        raise IndexError(f"{nv} vertices and {nq} quads do not fit arrays of {vsize} and {qsize}")
    return nv, nq


def _fill(data, skip_y, cz0, cy0, cx0, stride_y, stride_z, corners, edges, vcube, vlabel, vpos,
          qcube, qaxis, qlow, qlabel):
    if (_arr(data, _U4, 3) and data.flags.c_contiguous and _arr(skip_y, _BOOL, 1)
            and min(data.shape) >= 1 and skip_y.shape[0] >= data.shape[1] - 1
            and _int(cz0) and _int(cy0) and _int(cx0) and _int(stride_y) and _int(stride_z)
            and _arr(corners, _I8, 2) and corners.shape[0] >= 8 and corners.shape[1] >= 3
            and _arr(edges, _I8, 2) and edges.shape[0] >= 12 and edges.shape[1] >= 2
            and bool(((edges[:12, :2] >= 0) & (edges[:12, :2] < 8)).all())
            and _arr(vcube, _I8, 1, True) and _arr(vlabel, _U4, 1, True)
            and _arr(vpos, _F8, 2, True) and vpos.shape[1] >= 3 and _arr(qcube, _I8, 1, True)
            and _arr(qaxis, _I1, 1, True) and _arr(qlow, _I1, 1, True)
            and _arr(qlabel, _U4, 1, True)):
        return _fill_t(data, skip_y.view(np.uint8), cz0, cy0, cx0, stride_y, stride_z, corners,
                       edges, vcube, vlabel, vpos, qcube, qaxis, qlow, qlabel)
    return _fall("plexora.server.surface.nets._fill",
                 (data, skip_y, cz0, cy0, cx0, stride_y, stride_z, corners, edges, vcube, vlabel,
                  vpos, qcube, qaxis, qlow, qlabel))


def _triangulate_t(const int64_t[:] vkey, const double[:, :] positions, const int64_t[:] qrank,
                   const int64_t[:] qcube, const int64_t[:] qaxis, const int8_t[:] qlow,
                   const int64_t[:, :] offsets, int64_t ncubes, int64_t[:, :] out):
    cdef Py_ssize_t n = vkey.shape[0], nquads = qcube.shape[0]
    cdef Py_ssize_t q, lo, hi, mid, step
    cdef int c, o, a
    cdef int64_t axis, base, key
    cdef int64_t corner[4]
    cdef Py_ssize_t hint[3][4]
    cdef double d02, d13, u, v
    cdef bint ok = True
    for a in range(3):
        for c in range(4):
            hint[a][c] = 0
    with nogil:
        for q in range(nquads):
            axis = qaxis[q]
            base = qrank[q] * ncubes + qcube[q]
            for c in range(4):
                o = c if qlow[q] else 3 - c
                key = base + offsets[axis, o]
                lo = hint[axis][o]
                step = 1
                hi = lo + 1
                while hi < n and vkey[hi] <= key:
                    lo = hi
                    step *= 2
                    hi = lo + step
                if hi > n:
                    hi = n
                while lo < hi:
                    mid = (lo + hi) >> 1
                    if vkey[mid] < key:
                        lo = mid + 1
                    else:
                        hi = mid
                if lo >= n or vkey[lo] != key:
                    ok = False
                    break
                hint[axis][o] = lo
                corner[c] = lo
            if not ok:
                break
            d02 = 0.0
            d13 = 0.0
            for a in range(3):
                u = positions[corner[0], a] - positions[corner[2], a]
                v = positions[corner[1], a] - positions[corner[3], a]
                d02 += u * u
                d13 += v * v
            if d02 <= d13:
                out[2 * q, 0] = corner[0]
                out[2 * q, 1] = corner[1]
                out[2 * q, 2] = corner[2]
                out[2 * q + 1, 0] = corner[0]
                out[2 * q + 1, 1] = corner[2]
                out[2 * q + 1, 2] = corner[3]
            else:
                out[2 * q, 0] = corner[1]
                out[2 * q, 1] = corner[2]
                out[2 * q, 2] = corner[3]
                out[2 * q + 1, 0] = corner[1]
                out[2 * q + 1, 1] = corner[3]
                out[2 * q + 1, 2] = corner[0]
    return ok


def _triangulate(vkey, positions, qrank, qcube, qaxis, qlow, offsets, ncubes, out):
    if (_arr(vkey, _I8, 1) and _arr(positions, _F8, 2) and positions.shape[1] >= 3
            and positions.shape[0] >= vkey.shape[0]
            and _arr(qrank, _I8, 1) and _arr(qcube, _I8, 1) and _arr(qaxis, _I8, 1)
            and _arr(qlow, _I1, 1) and _arr(offsets, _I8, 2) and offsets.shape[0] >= 3
            and offsets.shape[1] >= 4 and _int(ncubes) and _arr(out, _I8, 2, True)
            and out.shape[1] >= 3 and out.shape[0] >= 2 * qcube.shape[0]
            and qrank.shape[0] >= qcube.shape[0] and qaxis.shape[0] >= qcube.shape[0]
            and qlow.shape[0] >= qcube.shape[0]
            and bool(((qaxis[:qcube.shape[0]] >= 0) & (qaxis[:qcube.shape[0]] < 3)).all())):
        return _triangulate_t(vkey, positions, qrank, qcube, qaxis, qlow, offsets, ncubes, out)
    return _fall("plexora.server.surface.nets._triangulate",
                 (vkey, positions, qrank, qcube, qaxis, qlow, offsets, ncubes, out))


# -- meshes ----------------------------------------------------------------------------


def _face_normals_into_t(const double[:, :] p, const int64_t[:, :] t, double[:, :] out):
    cdef Py_ssize_t i, m = t.shape[0]
    cdef int64_t a, b, c
    cdef double ux, uy, uz, vx, vy, vz, nx, ny, nz
    with nogil:
        for i in range(m):
            a = t[i, 0]
            b = t[i, 1]
            c = t[i, 2]
            ux = p[b, 0] - p[a, 0]
            uy = p[b, 1] - p[a, 1]
            uz = p[b, 2] - p[a, 2]
            vx = p[c, 0] - p[a, 0]
            vy = p[c, 1] - p[a, 1]
            vz = p[c, 2] - p[a, 2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            out[a, 0] += nx
            out[a, 1] += ny
            out[a, 2] += nz
            out[b, 0] += nx
            out[b, 1] += ny
            out[b, 2] += nz
            out[c, 0] += nx
            out[c, 1] += ny
            out[c, 2] += nz


def _face_normals_into(p, t, out):
    if (_arr(p, _F8, 2) and p.shape[1] >= 3 and _arr(t, _I8, 2) and t.shape[1] >= 3
            and _arr(out, _F8, 2, True) and out.shape[1] >= 3 and out.shape[0] >= p.shape[0]):
        _face_normals_into_t(p, t, out)
        return out
    return _fall("plexora.server.surface.mesh._face_normals_into", (p, t, out))


def _adjacency_count_t(const int64_t[:, :] t, int64_t[:] count):
    cdef Py_ssize_t i, k
    with nogil:
        for i in range(t.shape[0]):
            for k in range(3):
                count[t[i, k] + 1] += 2


def _adjacency_fill_t(const int64_t[:, :] t, int64_t[:] fill, int64_t[:] entries):
    cdef Py_ssize_t i, k
    cdef int64_t a, b
    with nogil:
        for i in range(t.shape[0]):
            for k in range(3):
                a = t[i, k]
                b = t[i, (k + 1) % 3]
                entries[fill[a]] = b
                fill[a] += 1
                entries[fill[b]] = a
                fill[b] += 1


def _adjacency_unique_t(Py_ssize_t n, const int64_t[:] start, int64_t[:] entries,
                        int64_t[:] indptr, int64_t[:] indices):
    cdef Py_ssize_t v, i, j, a0, a1, m = 0
    cdef int64_t key, last
    with nogil:
        for v in range(n):
            a0 = start[v]
            a1 = start[v + 1]
            for i in range(a0 + 1, a1):
                key = entries[i]
                j = i - 1
                while j >= a0 and entries[j] > key:
                    entries[j + 1] = entries[j]
                    j -= 1
                entries[j + 1] = key
            last = -1
            for i in range(a0, a1):
                if entries[i] != last:
                    last = entries[i]
                    indices[m] = last
                    m += 1
            indptr[v + 1] = m
    return m


def _adjacency(n, t):
    if _int(n) and n >= 0 and _arr(t, _I8, 2) and t.shape[1] >= 3:
        if t.shape[0] and (int(t[:, :3].min()) < 0 or int(t[:, :3].max()) >= n):
            return _fall("plexora.server.surface.mesh._adjacency", (n, t))
        count = np.zeros(n + 1, np.int64)
        _adjacency_count_t(t, count)
        # (This module is compiled without wraparound: no negative indices.)
        start = np.cumsum(count)
        total = int(start[n])
        fill = start[:n].copy()
        entries = np.empty(total, np.int64)
        _adjacency_fill_t(t, fill, entries)
        indptr = np.zeros(n + 1, np.int64)
        indices = np.empty(total, np.int64)
        m = _adjacency_unique_t(n, start, entries, indptr, indices)
        return indptr, indices[:m]
    return _fall("plexora.server.surface.mesh._adjacency", (n, t))


def _umbrella_step_t(const double[:, :] p, const int64_t[:] indptr, const int64_t[:] indices,
                     double factor, double[:, :] out):
    cdef Py_ssize_t v, k, k0, k1
    cdef int64_t j
    cdef double sx, sy, sz, d
    with nogil:
        for v in range(p.shape[0]):
            k0 = indptr[v]
            k1 = indptr[v + 1]
            if k1 == k0:
                out[v, 0] = p[v, 0]
                out[v, 1] = p[v, 1]
                out[v, 2] = p[v, 2]
                continue
            sx = 0.0
            sy = 0.0
            sz = 0.0
            for k in range(k0, k1):
                j = indices[k]
                sx += p[j, 0]
                sy += p[j, 1]
                sz += p[j, 2]
            d = <double>(k1 - k0)
            out[v, 0] = p[v, 0] + factor * (sx / d - p[v, 0])
            out[v, 1] = p[v, 1] + factor * (sy / d - p[v, 1])
            out[v, 2] = p[v, 2] + factor * (sz / d - p[v, 2])


def _umbrella_step(p, indptr, indices, factor, out):
    if (_arr(p, _F8, 2) and p.shape[1] >= 3 and _arr(indptr, _I8, 1)
            and indptr.shape[0] >= p.shape[0] + 1 and _arr(indices, _I8, 1) and _real(factor)
            and _arr(out, _F8, 2, True) and out.shape[0] >= p.shape[0] and out.shape[1] >= 3):
        _umbrella_step_t(p, indptr, indices, factor, out)
        return out
    return _fall("plexora.server.surface.mesh._umbrella_step", (p, indptr, indices, factor, out))


cdef inline bint _objects_ok(object v, object t, object starts, object counts, object scale):
    return (_arr(v, _F8, 2) and v.shape[1] >= 3 and _arr(t, _I8, 2) and t.shape[1] >= 3
            and _arr(starts, _I8, 1) and _arr(counts, _I8, 1)
            and counts.shape[0] >= starts.shape[0] and _arr(scale, _F8, 1)
            and scale.shape[0] >= 3)


def _signed_volumes_t(const double[:, :] p, const int64_t[:, :] t, const int64_t[:] starts,
                      const int64_t[:] counts, const double[:] scale, double[:] out):
    cdef Py_ssize_t o, i
    cdef int64_t a, b, c
    cdef double sx = scale[0], sy = scale[1], sz = scale[2]
    cdef double total, ax, ay, az, bx, by, bz, cx, cy, cz
    with nogil:
        for o in range(starts.shape[0]):
            total = 0.0
            for i in range(starts[o], starts[o] + counts[o]):
                a = t[i, 0]
                b = t[i, 1]
                c = t[i, 2]
                ax = p[a, 0] * sx
                ay = p[a, 1] * sy
                az = p[a, 2] * sz
                bx = p[b, 0] * sx
                by = p[b, 1] * sy
                bz = p[b, 2] * sz
                cx = p[c, 0] * sx
                cy = p[c, 1] * sy
                cz = p[c, 2] * sz
                total += (ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz)
                          + az * (bx * cy - by * cx))
            out[o] = total / 6.0


def _signed_volumes(p, t, starts, counts, scale, out):
    if (_objects_ok(p, t, starts, counts, scale) and _arr(out, _F8, 1, True)
            and out.shape[0] >= starts.shape[0]):
        _signed_volumes_t(p, t, starts, counts, scale, out)
        return out
    return _fall("plexora.server.surface.mesh._signed_volumes", (p, t, starts, counts, scale, out))


def _object_sums_t(const double[:, :] v, const int64_t[:, :] t, const int64_t[:] starts,
                   const int64_t[:] counts, const double[:] scale, double[:, :] sums,
                   double[:, :] box):
    cdef Py_ssize_t o, i, q
    cdef int64_t a, b, c, k
    cdef int corner
    cdef int64_t abc[3]
    cdef double sx = scale[0], sy = scale[1], sz = scale[2]
    cdef double ax, ay, az, bx, by, bz, cx, cy, cz, ux, uy, uz, wx, wy, wz, nx, ny, nz
    cdef double tri_area, tri_vol, mx, my, mz, value
    with nogil:
        for o in range(starts.shape[0]):
            for q in range(8):
                sums[o, q] = 0.0
            for q in range(3):
                box[o, q] = INFINITY
                box[o, 3 + q] = -INFINITY
            for i in range(starts[o], starts[o] + counts[o]):
                a = t[i, 0]
                b = t[i, 1]
                c = t[i, 2]
                ax = v[a, 0] * sx
                ay = v[a, 1] * sy
                az = v[a, 2] * sz
                bx = v[b, 0] * sx
                by = v[b, 1] * sy
                bz = v[b, 2] * sz
                cx = v[c, 0] * sx
                cy = v[c, 1] * sy
                cz = v[c, 2] * sz
                ux = bx - ax
                uy = by - ay
                uz = bz - az
                wx = cx - ax
                wy = cy - ay
                wz = cz - az
                nx = uy * wz - uz * wy
                ny = uz * wx - ux * wz
                nz = ux * wy - uy * wx
                tri_area = 0.5 * sqrt(nx * nx + ny * ny + nz * nz)
                tri_vol = (ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz)
                           + az * (bx * cy - by * cx)) / 6.0
                mx = ax + bx + cx
                my = ay + by + cy
                mz = az + bz + cz
                sums[o, 0] += tri_area
                sums[o, 1] += tri_vol
                sums[o, 2] += mx / 4.0 * tri_vol
                sums[o, 3] += my / 4.0 * tri_vol
                sums[o, 4] += mz / 4.0 * tri_vol
                sums[o, 5] += mx / 3.0 * tri_area
                sums[o, 6] += my / 3.0 * tri_area
                sums[o, 7] += mz / 3.0 * tri_area
                abc[0] = a
                abc[1] = b
                abc[2] = c
                for corner in range(3):
                    k = abc[corner]
                    for q in range(3):
                        value = v[k, q]
                        if value < box[o, q]:
                            box[o, q] = value
                        if value > box[o, 3 + q]:
                            box[o, 3 + q] = value


def _object_sums(v, t, starts, counts, scale, sums, box):
    if (_objects_ok(v, t, starts, counts, scale) and _arr(sums, _F8, 2, True)
            and sums.shape[0] >= starts.shape[0] and sums.shape[1] >= 8
            and _arr(box, _F8, 2, True) and box.shape[0] >= starts.shape[0]
            and box.shape[1] >= 6):
        _object_sums_t(v, t, starts, counts, scale, sums, box)
        return sums
    return _fall("plexora.server.surface.mesh._object_sums", (v, t, starts, counts, scale, sums, box))


def _object_moments_t(const double[:, :] v, const int64_t[:, :] t, const int64_t[:] starts,
                      const int64_t[:] counts, const double[:] scale, const double[:, :] centroid,
                      double[:, :] out):
    cdef Py_ssize_t o, i, q
    cdef int64_t a, b, c
    cdef double sx = scale[0], sy = scale[1], sz = scale[2]
    cdef double gx, gy, gz, ax, ay, az, bx, by, bz, cx, cy, cz, ux, uy, uz, wx, wy, wz
    cdef double nx, ny, nz, tri_area, rx, ry, rz
    with nogil:
        for o in range(starts.shape[0]):
            for q in range(6):
                out[o, q] = 0.0
            gx = centroid[o, 0]
            gy = centroid[o, 1]
            gz = centroid[o, 2]
            for i in range(starts[o], starts[o] + counts[o]):
                a = t[i, 0]
                b = t[i, 1]
                c = t[i, 2]
                ax = v[a, 0] * sx
                ay = v[a, 1] * sy
                az = v[a, 2] * sz
                bx = v[b, 0] * sx
                by = v[b, 1] * sy
                bz = v[b, 2] * sz
                cx = v[c, 0] * sx
                cy = v[c, 1] * sy
                cz = v[c, 2] * sz
                ux = bx - ax
                uy = by - ay
                uz = bz - az
                wx = cx - ax
                wy = cy - ay
                wz = cz - az
                nx = uy * wz - uz * wy
                ny = uz * wx - ux * wz
                nz = ux * wy - uy * wx
                tri_area = 0.5 * sqrt(nx * nx + ny * ny + nz * nz)
                rx = (ax + bx + cx) / 3.0 - gx
                ry = (ay + by + cy) / 3.0 - gy
                rz = (az + bz + cz) / 3.0 - gz
                out[o, 0] += rx * rx * tri_area
                out[o, 1] += rx * ry * tri_area
                out[o, 2] += rx * rz * tri_area
                out[o, 3] += ry * ry * tri_area
                out[o, 4] += ry * rz * tri_area
                out[o, 5] += rz * rz * tri_area


def _object_moments(v, t, starts, counts, scale, centroid, out):
    if (_objects_ok(v, t, starts, counts, scale) and _arr(centroid, _F8, 2)
            and centroid.shape[0] >= starts.shape[0] and centroid.shape[1] >= 3
            and _arr(out, _F8, 2, True) and out.shape[0] >= starts.shape[0]
            and out.shape[1] >= 6):
        _object_moments_t(v, t, starts, counts, scale, centroid, out)
        return out
    return _fall("plexora.server.surface.mesh._object_moments",
                 (v, t, starts, counts, scale, centroid, out))
