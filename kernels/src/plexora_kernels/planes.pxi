# Label planes: outlines, boxes, per-cell sums. Included by _kernels.pyx.
#
# Ports of plexora/server/utils/label_kernels.py, plexora/server/volume/measure.py,
# plexora/server/utils/cell_features.py and plexora/plugins/qc/server/segqc/kernels.py.


# -- label_kernels ---------------------------------------------------------------------


def _boundary_t(const label_t[:, :] labels, Py_ssize_t radius, uint8_t[:, :] out):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1]
    cdef Py_ssize_t y, x, yy, xx, y0, y1, x0, x1
    cdef label_t value
    cdef bint found
    with nogil:
        for y in range(height):
            for x in range(width):
                value = labels[y, x]
                if value == 0:
                    continue
                y0 = y - radius if y - radius > 0 else 0
                y1 = y + radius if y + radius < height - 1 else height - 1
                x0 = x - radius if x - radius > 0 else 0
                x1 = x + radius if x + radius < width - 1 else width - 1
                found = False
                for yy in range(y0, y1 + 1):
                    for xx in range(x0, x1 + 1):
                        if labels[yy, xx] != value:
                            found = True
                            break
                    if found:
                        break
                out[y, x] = found


def _boundary(labels, radius, out):
    if (_arr(labels, _INTS, 2) and _arr(out, _BOOL, 2, True) and _int(radius)
            and out.shape[0] >= labels.shape[0] and out.shape[1] >= labels.shape[1]):
        _boundary_t(labels, radius, out.view(np.uint8))
        return out
    return _fall("plexora.server.utils.label_kernels._boundary", (labels, radius, out))


def _bboxes_t(const label_t[:, :] labels, const label_t[:] ids, int64_t[:, :] stats):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1], n = ids.shape[0]
    cdef Py_ssize_t y, x, lo, hi, mid, row
    cdef label_t value
    with nogil:
        for y in range(height):
            for x in range(width):
                value = labels[y, x]
                if value == 0:
                    continue
                lo = 0
                hi = n
                while lo < hi:
                    mid = (lo + hi) >> 1
                    if ids[mid] < value:
                        lo = mid + 1
                    else:
                        hi = mid
                if lo >= n or ids[lo] != value:
                    continue
                row = lo
                if stats[row, 0] == 0:
                    stats[row, 1] = y
                    stats[row, 2] = x
                    stats[row, 3] = y
                    stats[row, 4] = x
                else:
                    if y < stats[row, 1]:
                        stats[row, 1] = y
                    if x < stats[row, 2]:
                        stats[row, 2] = x
                    if y > stats[row, 3]:
                        stats[row, 3] = y
                    if x > stats[row, 4]:
                        stats[row, 4] = x
                stats[row, 0] += 1
                stats[row, 5] += y
                stats[row, 6] += x


def _bboxes(labels, ids, stats):
    if (_arr(labels, _INTS, 2) and _arr(ids, _INTS, 1) and ids.dtype == labels.dtype
            and _arr(stats, _I8, 2, True) and stats.shape[1] >= 7
            and stats.shape[0] >= ids.shape[0]):
        _bboxes_t(labels, ids, stats)
        return stats
    return _fall("plexora.server.utils.label_kernels._bboxes", (labels, ids, stats))


# -- volume.measure --------------------------------------------------------------------


def _bbox_kernel_t(const uint32_t[:, :] labels, int64_t y0, int64_t z, int64_t[:] minx,
                   int64_t[:] maxx, int64_t[:] miny, int64_t[:] maxy, int64_t[:] minz,
                   int64_t[:] maxz):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1]
    cdef Py_ssize_t r, c, label, bad = 0
    cdef Py_ssize_t size = min(minx.shape[0], maxx.shape[0], miny.shape[0], maxy.shape[0],
                               minz.shape[0], maxz.shape[0])
    cdef int64_t y
    with nogil:
        for r in range(height):
            for c in range(width):
                label = <Py_ssize_t>labels[r, c]
                if label == 0:
                    continue
                if label < 0 or label >= size:
                    bad = label
                    continue
                y = y0 + r
                if c < minx[label]:
                    minx[label] = c
                if c > maxx[label]:
                    maxx[label] = c
                if y < miny[label]:
                    miny[label] = y
                if y > maxy[label]:
                    maxy[label] = y
                if z < minz[label]:
                    minz[label] = z
                if z > maxz[label]:
                    maxz[label] = z
    if bad:
        raise IndexError(f"label {bad} is outside the box arrays ({size})")


def _bbox_kernel(labels, y0, z, minx, maxx, miny, maxy, minz, maxz):
    boxes = (minx, maxx, miny, maxy, minz, maxz)
    if (_arr(labels, _U4, 2) and _int(y0) and _int(z)
            and all(_arr(b, _I8, 1, True) for b in boxes)):
        _bbox_kernel_t(labels, y0, z, minx, maxx, miny, maxy, minz, maxz)
        return None
    return _fall("plexora.server.volume.measure._bbox_kernel",
                 (labels, y0, z, minx, maxx, miny, maxy, minz, maxz))


# -- cell_features ---------------------------------------------------------------------


cdef inline void _swap(double* a, Py_ssize_t i, Py_ssize_t j) noexcept nogil:
    cdef double t = a[i]
    a[i] = a[j]
    a[j] = t


cdef Py_ssize_t _partition(double* a, Py_ssize_t low, Py_ssize_t high) noexcept nogil:
    """numba's `_partition` (np/arraymath.py): median of three, then Hoare."""
    cdef Py_ssize_t mid = (low + high) >> 1
    cdef Py_ssize_t i, j
    cdef double pivot
    if a[mid] < a[low]:
        _swap(a, low, mid)
    if a[high] < a[mid]:
        _swap(a, high, mid)
    if a[mid] < a[low]:
        _swap(a, low, mid)
    pivot = a[mid]
    _swap(a, high, mid)
    i = low
    j = high - 1
    while True:
        while i < high and a[i] < pivot:
            i += 1
        while j >= low and pivot < a[j]:
            j -= 1
        if i >= j:
            break
        _swap(a, i, j)
        i += 1
        j -= 1
    _swap(a, i, high)
    return i


cdef double _select(double* a, Py_ssize_t k, Py_ssize_t low, Py_ssize_t high) noexcept nogil:
    cdef Py_ssize_t i = _partition(a, low, high)
    while i != k:
        if i < k:
            low = i + 1
        else:
            high = i - 1
        i = _partition(a, low, high)
    return a[k]


cdef void _select_two(double* a, Py_ssize_t k, Py_ssize_t low, Py_ssize_t high) noexcept nogil:
    """numba's `_select_two`: a[k] and a[k + 1] in place."""
    cdef Py_ssize_t i
    while True:
        i = _partition(a, low, high)
        if i < k:
            low = i + 1
        elif i > k + 1:
            high = i - 1
        elif i == k:
            _select(a, k + 1, i + 1, high)
            break
        else:
            _select(a, k, low, i - 1)
            break


cdef double _percentile(double* a, Py_ssize_t n, double q) noexcept nogil:
    """`np.percentile(a[:n], q)` as numba computes it, for 0 < q < 100 and
    n >= 1 (the port's wrapper sends anything else to the fallback)."""
    cdef Py_ssize_t i, k
    cdef double rank, f, m, lower, upper
    for i in range(n):
        if isnan(a[i]):
            return NAN
    if n == 1:
        return a[0] if isfinite(a[0]) else NAN
    rank = 1 + <double>(n - 1) * (q / 100.0)
    f = floor(rank)
    m = rank - f
    k = <Py_ssize_t>(f - 1)
    _select_two(a, k, 0, n - 1)
    lower = a[k]
    upper = a[k + 1]
    return lower * (1 - m) + upper * m


def _background_blocks_t(const float[:, :] marker, const uint32_t[:, :] labels, Py_ssize_t step,
                         double percentile, Py_ssize_t min_pixels, double[:, :] out,
                         double[::1] buffer):
    cdef Py_ssize_t height = marker.shape[0], width = marker.shape[1]
    cdef Py_ssize_t rows = out.shape[0], cols = out.shape[1]
    cdef Py_ssize_t by, bx, y, x, count, y1, x1
    with nogil:
        for by in range(rows):
            for bx in range(cols):
                count = 0
                y1 = (by + 1) * step if (by + 1) * step < height else height
                x1 = (bx + 1) * step if (bx + 1) * step < width else width
                for y in range(by * step, y1):
                    for x in range(bx * step, x1):
                        if labels[y, x] == 0:
                            buffer[count] = marker[y, x]
                            count += 1
                if count >= min_pixels:
                    out[by, bx] = _percentile(&buffer[0], count, percentile)


def _background_blocks(marker, labels, step, percentile, min_pixels):
    if (_arr(marker, _F4, 2) and _arr(labels, _U4, 2) and _int(step) and step >= 1
            and _real(percentile) and 0 < percentile < 100 and _int(min_pixels)
            and min_pixels >= 1 and labels.shape[0] >= marker.shape[0]
            and labels.shape[1] >= marker.shape[1]):
        height, width = marker.shape
        out = np.full(((height + step - 1) // step, (width + step - 1) // step), np.nan)
        buffer = np.empty(step * step, dtype=np.float64)
        _background_blocks_t(marker, labels, step, percentile, min_pixels, out, buffer)
        return out
    return _fall("plexora.server.utils.cell_features._background_blocks",
                 (marker, labels, step, percentile, min_pixels))


def _tile_kernel_t(const uint32_t[:, :] labels_h, const float[:, :] marker_h,
                   const float[:, :] dna_h, bint use_dna, const double[:, :] background,
                   Py_ssize_t step, const uint32_t[:] uniq, double[:, :] sums, double[:] maxima):
    cdef Py_ssize_t height = labels_h.shape[0] - 2, width = labels_h.shape[1] - 2
    cdef Py_ssize_t n = uniq.shape[0]
    cdef Py_ssize_t size = min(sums.shape[0], maxima.shape[0])
    cdef Py_ssize_t y, x, slot = -1, lo, hi, mid, bad = -1
    cdef uint32_t label, last_label = 0
    cdef double value, back, gx, gy, dna
    with nogil:
        for y in range(height):
            for x in range(width):
                label = labels_h[y + 1, x + 1]
                if label == 0:
                    continue
                if label != last_label:
                    lo = 0
                    hi = n
                    while lo < hi:
                        mid = (lo + hi) >> 1
                        if uniq[mid] < label:
                            lo = mid + 1
                        else:
                            hi = mid
                    slot = lo
                    last_label = label
                if slot >= size:
                    bad = slot
                    continue
                value = <double>marker_h[y + 1, x + 1]
                sums[slot, 0] += 1.0
                sums[slot, 1] += value
                sums[slot, 2] += value * value
                if (labels_h[y, x + 1] != label or labels_h[y + 2, x + 1] != label
                        or labels_h[y + 1, x] != label or labels_h[y + 1, x + 2] != label):
                    sums[slot, 3] += 1.0
                    sums[slot, 4] += value
                back = background[y // step, x // step]
                if back == back:
                    sums[slot, 5] += 1.0
                    sums[slot, 6] += back
                gx = (<double>marker_h[y + 1, x + 2] - <double>marker_h[y + 1, x]) * 0.5
                gy = (<double>marker_h[y + 2, x + 1] - <double>marker_h[y, x + 1]) * 0.5
                sums[slot, 7] += sqrt(gx * gx + gy * gy)
                if use_dna:
                    dna = <double>dna_h[y + 1, x + 1]
                    if dna < 0.0:
                        dna = 0.0
                    sums[slot, 8] += dna
                    sums[slot, 9] += dna * value
                if value > maxima[slot]:
                    maxima[slot] = value
    if bad >= 0:
        raise IndexError(f"a label's slot {bad} is outside the sums ({size})")


def _tile_kernel(labels_h, marker_h, dna_h, use_dna, background, step, uniq, sums, maxima):
    if (_arr(labels_h, _U4, 2) and _arr(uniq, _U4, 1)
            and _arr(marker_h, _F4, 2) and _arr(dna_h, _F4, 2) and isinstance(use_dna, (bool, np.bool_, int))
            and _arr(background, _F8, 2) and _int(step) and step >= 1
            and _arr(sums, _F8, 2, True) and sums.shape[1] >= 10 and _arr(maxima, _F8, 1, True)
            and labels_h.shape[0] >= 2 and labels_h.shape[1] >= 2
            and marker_h.shape[0] >= labels_h.shape[0] and marker_h.shape[1] >= labels_h.shape[1]
            and (not use_dna or (dna_h.shape[0] >= labels_h.shape[0]
                                 and dna_h.shape[1] >= labels_h.shape[1]))):
        _tile_kernel_t(labels_h, marker_h, dna_h, bool(use_dna), background, step, uniq, sums,
                       maxima)
        return None
    return _fall("plexora.server.utils.cell_features._tile_kernel",
                 (labels_h, marker_h, dna_h, use_dna, background, step, uniq, sums, maxima))


def _run_labels_t(const uint32_t[:, :] labels, uint32_t[:] out):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1]
    cdef Py_ssize_t y, x, count = 0
    cdef uint32_t label, last
    with nogil:
        for y in range(height):
            last = 0
            for x in range(width):
                label = labels[y, x]
                if label != last and label != 0:
                    out[count] = label
                    count += 1
                last = label
    return count


def _run_labels(labels):
    if _arr(labels, _U4, 2):
        out = np.empty(labels.shape[0] * labels.shape[1], dtype=labels.dtype)
        count = _run_labels_t(labels, out)
        return out[:count]
    return _fall("plexora.server.utils.cell_features._run_labels", (labels,))


# -- segmentation QC -------------------------------------------------------------------


def _label_stats_t(const uint32_t[:, :] labels, const float[:, :] dna, int64_t y_off,
                   int64_t x_off, Py_ssize_t iy0, Py_ssize_t iy1, Py_ssize_t ix0, Py_ssize_t ix1,
                   double bg, double[:] count, double[:] sum_dna, double[:] sum_y,
                   double[:] sum_x, double[:] perimeter, double[:] max_dna, double[:] excess):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1]
    cdef Py_ssize_t y, x, edge, at, bad = 0
    cdef Py_ssize_t size = min(count.shape[0], sum_dna.shape[0], sum_y.shape[0], sum_x.shape[0],
                               perimeter.shape[0], max_dna.shape[0], excess.shape[0])
    cdef uint32_t label
    cdef float value
    cdef double above
    with nogil:
        for y in range(iy0, iy1):
            for x in range(ix0, ix1):
                label = labels[y, x]
                if label == 0:
                    continue
                at = <Py_ssize_t>label
                if at < 0 or at >= size:
                    bad = at
                    continue
                value = dna[y, x]
                count[at] += 1
                sum_dna[at] += value
                if value > max_dna[at]:
                    max_dna[at] = value
                # numba's math.expm1 of a float32 is expm1f (float32).
                above = expm1f(value) - bg
                if above > 0:
                    excess[at] += above
                sum_y[at] += <double>(y + y_off)
                sum_x[at] += <double>(x + x_off)
                edge = 0
                if y == 0 or labels[y - 1, x] != label:
                    edge += 1
                if y == height - 1 or labels[y + 1, x] != label:
                    edge += 1
                if x == 0 or labels[y, x - 1] != label:
                    edge += 1
                if x == width - 1 or labels[y, x + 1] != label:
                    edge += 1
                perimeter[at] += edge
    if bad:
        raise IndexError(f"label {bad} is outside the per-label arrays ({size})")


cdef inline bint _window(object labels, iy0, iy1, ix0, ix1):
    return (_int(iy0) and _int(iy1) and _int(ix0) and _int(ix1) and 0 <= iy0 and 0 <= ix0
            and iy1 <= labels.shape[0] and ix1 <= labels.shape[1])


def label_stats(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, bg, count, sum_dna, sum_y, sum_x,
                perimeter, max_dna, excess):
    arrays = (count, sum_dna, sum_y, sum_x, perimeter, max_dna, excess)
    if (_arr(labels, _U4, 2) and _arr(dna, _F4, 2) and _int(y_off) and _int(x_off)
            and _window(labels, iy0, iy1, ix0, ix1) and _real(bg)
            and dna.shape[0] >= labels.shape[0] and dna.shape[1] >= labels.shape[1]
            and all(_arr(a, _F8, 1, True) for a in arrays)):
        _label_stats_t(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, bg, *arrays)
        return None
    return _fall("plexora.plugins.qc.server.segqc.kernels.label_stats",
                 (labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, bg, *arrays))


def _count_pairs_t(const uint32_t[:, :] labels, Py_ssize_t iy0, Py_ssize_t iy1, Py_ssize_t ix0,
                   Py_ssize_t ix1):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1]
    cdef Py_ssize_t y, x
    cdef int64_t n = 0
    cdef uint32_t a, b
    with nogil:
        for y in range(iy0, iy1):
            for x in range(ix0, ix1):
                a = labels[y, x]
                if a == 0:
                    continue
                if x + 1 < width:
                    b = labels[y, x + 1]
                    if b != 0 and b != a:
                        n += 1
                if y + 1 < height:
                    b = labels[y + 1, x]
                    if b != 0 and b != a:
                        n += 1
    return n


def count_pairs(labels, iy0, iy1, ix0, ix1):
    if _arr(labels, _U4, 2) and _window(labels, iy0, iy1, ix0, ix1):
        return _count_pairs_t(labels, iy0, iy1, ix0, ix1)
    return _fall("plexora.plugins.qc.server.segqc.kernels.count_pairs",
                 (labels, iy0, iy1, ix0, ix1))


def _fill_pairs_t(const uint32_t[:, :] labels, const float[:, :] dna, int64_t y_off,
                  int64_t x_off, Py_ssize_t iy0, Py_ssize_t iy1, Py_ssize_t ix0, Py_ssize_t ix1,
                  int64_t[:] lo, int64_t[:] hi, double[:] value, double[:] py, double[:] px):
    cdef Py_ssize_t height = labels.shape[0], width = labels.shape[1]
    cdef Py_ssize_t y, x, k = 0
    cdef Py_ssize_t size = min(lo.shape[0], hi.shape[0], value.shape[0], py.shape[0],
                               px.shape[0])
    cdef uint32_t a, b
    with nogil:
        for y in range(iy0, iy1):
            for x in range(ix0, ix1):
                a = labels[y, x]
                if a == 0:
                    continue
                if x + 1 < width:
                    b = labels[y, x + 1]
                    if b != 0 and b != a:
                        if k >= size:
                            k += 1
                            continue
                        lo[k] = <int64_t>(a if a < b else b)
                        hi[k] = <int64_t>(a if a > b else b)
                        value[k] = 0.5 * (dna[y, x] + dna[y, x + 1])
                        py[k] = <double>(y + y_off)
                        px[k] = <double>(x + x_off) + 0.5
                        k += 1
                if y + 1 < height:
                    b = labels[y + 1, x]
                    if b != 0 and b != a:
                        if k >= size:
                            k += 1
                            continue
                        lo[k] = <int64_t>(a if a < b else b)
                        hi[k] = <int64_t>(a if a > b else b)
                        value[k] = 0.5 * (dna[y, x] + dna[y + 1, x])
                        py[k] = <double>(y + y_off) + 0.5
                        px[k] = <double>(x + x_off)
                        k += 1
    if k > size:
        raise IndexError(f"{k} pairs do not fit arrays of {size}")
    return k


def fill_pairs(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, lo, hi, value, py, px):
    if (_arr(labels, _U4, 2) and _arr(dna, _F4, 2) and _int(y_off) and _int(x_off)
            and _window(labels, iy0, iy1, ix0, ix1)
            and dna.shape[0] >= labels.shape[0] and dna.shape[1] >= labels.shape[1]
            and _arr(lo, _I8, 1, True) and _arr(hi, _I8, 1, True) and _arr(value, _F8, 1, True)
            and _arr(py, _F8, 1, True) and _arr(px, _F8, 1, True)):
        return _fill_pairs_t(labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, lo, hi, value, py, px)
    return _fall("plexora.plugins.qc.server.segqc.kernels.fill_pairs",
                 (labels, dna, y_off, x_off, iy0, iy1, ix0, ix1, lo, hi, value, py, px))
