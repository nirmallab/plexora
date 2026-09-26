"""plexora._lifetime: how a process with no terminal is told to stop."""

import io
import signal
import sys
import threading
import time

import pytest

from plexora import _lifetime


@pytest.fixture(autouse=True)
def fresh():
    _lifetime._reset_for_tests()
    yield
    _lifetime._reset_for_tests()


def test_request_shutdown_interrupts_the_main_thread_once():
    interrupts = []
    exits = []
    assert _lifetime.request_shutdown("test", grace=60,
                                      interrupt=lambda: interrupts.append(1),
                                      exit_fn=exits.append) is True
    assert _lifetime.request_shutdown("again", grace=60,
                                      interrupt=lambda: interrupts.append(2),
                                      exit_fn=exits.append) is False
    assert interrupts == [1]
    assert exits == []
    assert _lifetime.shutdown_requested()


def test_a_wedged_teardown_is_hard_exited_after_the_grace_period():
    exits = []
    _lifetime.request_shutdown("test", grace=0.05, interrupt=lambda: None,
                               exit_fn=exits.append)
    deadline = time.time() + 5
    while not exits and time.time() < deadline:
        time.sleep(0.01)
    assert exits == [0]


def test_the_reason_is_logged():
    said = []
    _lifetime.request_shutdown("stdin closed", grace=60, interrupt=lambda: None,
                               exit_fn=lambda code: None, log=said.append)
    assert said == ["Stopping: stdin closed."]


def test_watch_stdin_fires_on_end_of_file_and_ignores_lines():
    fired = threading.Event()
    thread = _lifetime.watch_stdin(fired.set, stream=io.StringIO("one\ntwo\n"))
    assert fired.wait(5)
    thread.join(5)


def test_watch_stdin_without_a_stream_does_nothing(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert _lifetime.watch_stdin(lambda: None) is None


def test_missing_std_streams_are_replaced_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)
    _lifetime.ensure_std_streams()
    try:
        print("goes nowhere")
        print("also nowhere", file=sys.stderr)
        assert sys.stdin.read() == ""
    finally:
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            stream.close()


def test_flush_std_survives_closed_and_missing_streams(monkeypatch):
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)
    monkeypatch.setattr(sys, "stderr", None)
    _lifetime.flush_std()


def test_signal_handlers_raise_keyboard_interrupt_on_the_main_thread():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        installed = _lifetime.install_signal_handlers([signal.SIGTERM])
        assert installed == [signal.SIGTERM]
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_signal_handlers_are_left_alone_off_the_main_thread():
    result = []
    thread = threading.Thread(
        target=lambda: result.append(_lifetime.install_signal_handlers()))
    thread.start()
    thread.join(5)
    assert result == [[]]


def test_the_node_uses_the_shared_stdin_tie(monkeypatch):
    from plexora.server.node import app as node_app

    called = []
    monkeypatch.setattr(_lifetime, "exit_when_stdin_closes",
                        lambda log=print: called.append(log) or "thread")
    assert node_app._exit_when_stdin_closes(log=print) == "thread"
    assert called == [print]
