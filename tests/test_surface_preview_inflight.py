"""The surface preview's block cache reads a cold block once (io-surface-7).

While the first read of a preview grid runs -- a cold read of a large stack
takes seconds -- every debounced slider move asks for the same block. They
wait for that read rather than starting the same read again in parallel.
"""

import threading
import time
from types import SimpleNamespace

import numpy as np

from plexora.server.surface import levels, preview


def test_concurrent_asks_for_one_cold_block_read_it_once(monkeypatch):
    preview.clear()
    plan = SimpleNamespace(level=0, rows=slice(0, 4), cols=slice(0, 4), z0=0, z1=1, stride=1,
                           grid=SimpleNamespace(shape=(2, 4, 4)))
    reads = []

    def plan_reader(source, plan, *, t, channel):
        def read(start, stop):
            reads.append((start, stop))
            time.sleep(0.2)
            return np.ones((2, 4, 4), np.uint16)
        return read

    monkeypatch.setattr(preview, "preview_plan", lambda volume, region, budget=None: plan)
    monkeypatch.setattr(preview, "_scale", lambda volume: ((1.0, 1.0, 1.0), False))
    monkeypatch.setattr(levels, "plan_reader", plan_reader)
    source = SimpleNamespace(volume=object())
    pre = SimpleNamespace(active=False, sigma_um=0.0, background_um=0.0)
    got = []

    def ask():
        block, _plan = preview.block_for(source, "id", channel=0, t=0, region=None, pre=pre)
        got.append(block)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    preview.clear()

    assert len(got) == 4 and all(block is got[0] for block in got)
    assert len(reads) == 1
