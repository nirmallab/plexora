"""The website's event loop for a browser without JSPI (plexora/browser/boot.py
`SyncLoop`, `without_jspi`): zarr's synchronous API runs its coroutines to
completion inside the call, with Pyodide's loop said to be running, and what a
store binds to the loop works on the next call too."""

import asyncio
import sys
import types

import pytest

from plexora.browser import boot


@pytest.fixture
def pyodide_loop_running():
    """A loop said to be running, as Pyodide's always is."""
    loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(loop)
    try:
        yield loop
    finally:
        asyncio.events._set_running_loop(None)
        loop.close()


def test_a_coroutine_runs_to_completion_while_another_loop_is_said_to_run(pyodide_loop_running):
    async def work():
        await asyncio.sleep(0)
        parts = await asyncio.gather(*(asyncio.sleep(0.001 * i, result=i) for i in range(4)))
        return sum(parts)

    slept = []
    assert boot.run_to_completion(work(), sleep=lambda s: slept.append(s)) == 6
    assert asyncio.events._get_running_loop() is pyodide_loop_running


def test_a_lock_bound_on_the_first_call_works_on_the_next(pyodide_loop_running):
    lock = asyncio.Lock()

    async def guarded(value):
        async with lock:
            await asyncio.sleep(0)
            return value

    assert boot.run_to_completion(guarded(1)) == 1
    assert boot.run_to_completion(guarded(2)) == 2


def test_a_call_from_inside_a_coroutine_gets_a_loop_of_its_own(pyodide_loop_running):
    async def inner():
        return 5

    async def outer():
        return boot.run_to_completion(inner()) + 1

    assert boot.run_to_completion(outer()) == 6


def test_pyodides_loop_runs_a_coroutine_without_jspi(monkeypatch):
    class WebLoop:
        def run_until_complete(self, future):
            raise RuntimeError("WebAssembly stack switching not supported in this JavaScript runtime")

    webloop = types.ModuleType("pyodide.webloop")
    webloop.WebLoop = WebLoop
    package = types.ModuleType("pyodide")
    package.webloop = webloop
    monkeypatch.setitem(sys.modules, "pyodide", package)
    monkeypatch.setitem(sys.modules, "pyodide.webloop", webloop)
    assert boot.without_jspi(sleep=lambda s: None) is True
    assert boot.without_jspi(sleep=lambda s: None) is True  # once

    async def answer():
        await asyncio.sleep(0)
        return "read"

    assert WebLoop().run_until_complete(answer()) == "read"
