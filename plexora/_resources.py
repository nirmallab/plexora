"""How much CPU this process actually has, and everything sized from it.

One number, computed once, feeding three decisions that used to be made
independently and wrongly:

- how many threads the numeric stack (BLAS/OpenMP, blosc) may start,
- how many request workers waitress runs,
- how many requests the page may have in flight while restoring channels.

The reason this module exists is that **`os.cpu_count()` is the wrong question
on a cluster**. It reports the machine's cores, not the allocation's. A SLURM
job asking for `--cpus-per-task=2` on a 64-core node still sees 64 there, so
OpenBLAS starts 64 threads, numcodecs starts 8 decompression threads, and all
of them contend for two cores' worth of quota. Measured on HMS O2, that
oversubscription is what turned "opening a project is slow" into a task queue
that never drained and a reverse proxy returning 502.

There is no single reliable signal, so this takes the minimum of the ones that
exist, each of which is authoritative when present:

- `sched_getaffinity` -- right when the site pins jobs with a cpuset. Silent
  (reports every core) when the site uses CPU *quota* instead.
- the cgroup CPU quota -- right in exactly the case affinity is silent.
- `SLURM_CPUS_PER_TASK` -- what was actually requested, and the only signal
  that survives a site configuring neither of the above.

Deliberately a leaf module: stdlib only, imports nothing from `plexora`, so
`plexora/__init__.py` can call it before anything numeric is imported.
"""

from __future__ import annotations

import os

#: Never fewer than this many request workers, whatever the allocation. A
#: single-core allocation still needs enough workers that a blocked tile read
#: cannot starve /health -- the liveness probe does no work at all, and a page
#: that cannot reach it reports the server as dead while it is merely busy.
MIN_WORKER_THREADS = 8

#: Above this, more workers only add concurrent full-resolution reads to hold
#: in memory. All image I/O is serialized downstream anyway (see data_model's
#: note on the zarr io thread and tifffile's per-file read lock), so the extra
#: workers would queue on that lock rather than do useful work.
MAX_WORKER_THREADS = 32

#: The page never fans out wider than this even on a large node. Past roughly
#: this point the restore burst is bounded by the serialized reader rather than
#: by the client, and a wider burst just moves the queue from the browser into
#: waitress where it is harder to see.
MAX_CLIENT_CONCURRENCY = 8

#: Floor for the same, so a one- or two-core allocation still overlaps a little
#: rather than restoring strictly one channel at a time.
MIN_CLIENT_CONCURRENCY = 2


def _affinity_cpus():
    """Cores this process may be scheduled on, or None where unsupported.

    Absent on macOS and Windows, which is why every caller has to tolerate None
    rather than treating it as the answer.
    """
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is None:
        return None
    try:
        count = len(getaffinity(0))
    except OSError:
        return None
    return count or None


def _cgroup_cpus():
    """Cores implied by the cgroup CPU quota, or None when unlimited/absent.

    This is the signal that catches a site using `cpu.max` quota without a
    cpuset: affinity reports the whole node, and only the quota knows the job
    was given two cores. Both cgroup versions are read because clusters run
    both, and a wrong answer here is worse than no answer -- so anything
    unparseable returns None and lets another signal decide.
    """
    # cgroup v2: "<quota> <period>", or "max <period>" when unlimited.
    try:
        quota_text, period_text = _read_text("/sys/fs/cgroup/cpu.max").split()
        if quota_text != "max":
            return _quota_to_cpus(int(quota_text), int(period_text))
    except (OSError, ValueError):
        pass

    # cgroup v1: the two halves live in separate files, and a quota of -1
    # means unlimited.
    try:
        quota = int(_read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
        period = int(_read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
        if quota > 0:
            return _quota_to_cpus(quota, period)
    except (OSError, ValueError):
        pass

    return None


def _read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _quota_to_cpus(quota, period):
    """Whole cores a quota/period pair is worth, rounded up, minimum 1.

    Rounded UP rather than down because a fractional allocation (say 1.5 cores)
    still benefits from two threads: one of them is usually blocked on I/O, and
    rounding down to 1 would serialize work the quota can genuinely overlap.
    """
    if period <= 0:
        return None
    return max(1, -(-quota // period))


def _scheduler_cpus():
    """What the batch scheduler was asked for, or None outside a job.

    Last of the three signals and the crudest, but it is the only one that
    survives a site configuring neither cpuset nor quota -- which does happen,
    and is precisely the configuration where the other two lie by reporting the
    whole node.

    Scheduler variables only. `OMP_NUM_THREADS` looks like it belongs here and
    does not: it states a thread-pool preference rather than an allocation, and
    `configure_thread_pools` sets it -- so reading it back would make this
    function's own output one of its own inputs.
    """
    for name in ("SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE",
                 "PBS_NP", "NSLOTS"):
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def allocated_cpus():
    """Cores this process can actually use. Never zero.

    The minimum of every signal that answered, because each one is right in a
    situation where the others are silent, and a signal that is silent reports
    the whole machine rather than admitting it does not know. Taking the
    minimum is therefore the only combination that cannot over-subscribe.
    """
    candidates = [value for value in (_affinity_cpus(), _cgroup_cpus(),
                                      _scheduler_cpus(), os.cpu_count())
                  if value]
    return max(1, min(candidates)) if candidates else 1


def worker_threads():
    """How many request workers waitress should run.

    Deliberately NOT equal to the core count. Most workers spend their time
    blocked on the serialized image reader rather than computing, so more of
    them than cores is both harmless and necessary -- it is what keeps a worker
    free to answer /health while the rest wait.
    """
    return max(MIN_WORKER_THREADS, min(allocated_cpus() * 2, MAX_WORKER_THREADS))


def client_concurrency():
    """How many requests the page may have in flight restoring channels.

    Sized from cores rather than from workers because each of these requests
    does real per-channel work at the far end, and one worker short of the pool
    is reserved so the liveness probe is always answerable.
    """
    ceiling = min(allocated_cpus(), worker_threads() - 1, MAX_CLIENT_CONCURRENCY)
    return max(MIN_CLIENT_CONCURRENCY, ceiling)


def configure_thread_pools(cpus=None):
    """Cap every numeric thread pool at the allocation. Returns the cap used.

    Belt AND braces, because the two mechanisms cover different cases and
    neither covers both:

    - The environment variables are read by OpenBLAS/MKL/OpenMP when they are
      first loaded, so they work only if nothing numeric has been imported
      yet. That is the sidecar's situation (`plexora.server_cli` imports this
      package before anything else) but not the notebook's, where the user may
      well have imported numpy in an earlier cell.
    - `threadpool_limits` reaches into already-loaded libraries and is the only
      thing that helps in the notebook case. It cannot help with blosc, which
      is not a BLAS-like pool and has to be set through numcodecs.

    Every step is individually guarded: a capped pool is an optimisation, and
    failing to apply one must never stop the server starting.
    """
    cpus = allocated_cpus() if cpus is None else max(1, int(cpus))
    value = str(cpus)

    # setdefault, not assignment: an operator who exported OMP_NUM_THREADS
    # meant it, and silently overriding them would be the same class of bug
    # this module exists to fix.
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                 "BLIS_NUM_THREADS"):
        os.environ.setdefault(name, value)

    try:
        from threadpoolctl import threadpool_limits

        # Called for its side effect and the result deliberately dropped:
        # `threadpool_limits` restores the previous limits on __exit__ or an
        # explicit restore, never on garbage collection, so a bare call sets
        # the cap for the life of the process. This is also the fix for the
        # call this replaced, which passed no limits at all and therefore
        # capped nothing -- it only ever forced the BLAS libraries to load.
        threadpool_limits(limits=cpus)
    except Exception:
        pass

    try:
        # zarr's decompression pool, and the one signal that is definitely
        # wrong by default: numcodecs sizes it from the machine's core count,
        # so a 2-core allocation still gets 8 decompression threads.
        import numcodecs.blosc

        numcodecs.blosc.set_nthreads(cpus)
    except Exception:
        pass

    return cpus
