"""What the hooks cost a request. The tile path is the hot path: a pan asks
for dozens of tiles a second, and telemetry must not be what makes it slow."""

import statistics
from time import perf_counter_ns

import pytest

import plexora
from plexora.telemetry import performance

ITERATIONS = 20_000


def hooks():
    before = next(f for f in plexora.app.before_request_funcs[None]
                  if f.__name__ == "_telemetry_begin")
    after = next(f for f in plexora.app.after_request_funcs[None]
                 if f.__name__ == "_telemetry_end")
    return before, after


def measure(path, tile=True):
    before, after = hooks()
    samples = []
    response = plexora.app.response_class(status=200)
    with plexora.app.test_request_context(path):
        for _ in range(ITERATIONS):
            response.headers.pop("Server-Timing", None)
            start = perf_counter_ns()
            before()
            if tile:
                performance.note("cache", "miss")
                performance.mark("read")
                performance.mark("lut")
                performance.mark("enc")
            after(response)
            samples.append(perf_counter_ns() - start)
    samples.sort()
    return statistics.median(samples) / 1000.0, samples[int(len(samples) * 0.99)] / 1000.0


def test_tile_hook_cost_enabled(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(plexora.app, "terminal", upload=False)
    p50, p99 = measure("/generated/data/p/c_0/0/0_0.png")
    assert p50 < 20, f"p50 {p50:.1f} us"
    assert p99 < 200, f"p99 {p99:.1f} us"


def test_tile_hook_cost_off():
    """Off, a tile still gets its Server-Timing header, and nothing else."""
    p50, _p99 = measure("/generated/data/p/c_0/0/0_0.png")
    assert p50 < 12, f"p50 {p50:.1f} us"


def test_non_tile_hook_cost_off():
    p50, _p99 = measure("/get_channel_names", tile=False)
    assert p50 < 2, f"p50 {p50:.1f} us"
