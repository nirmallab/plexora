"""`mirror.run_script` stops where the user detached the viewer.

"Continue in background" flips the session's control while a packet's
script may be half sent: the rest of it is not sent, and the pause before
the next command is not waited out.
"""

from plexora.agent.sessions import mirror

SCRIPT = [{"type": "set_channels"}, {"type": "fit_region"}, {"type": "preview_gate"}]


def test_a_script_stops_at_the_command_after_a_detach():
    sent, slept = [], []
    attached = {"on": True}

    def send(type, arguments):
        sent.append(type)
        attached["on"] = False          # the user detaches while this command runs

    result = mirror.run_script(send, SCRIPT, delay_ms=100, sleep=slept.append,
                               keep_going=lambda: attached["on"])
    assert sent == ["set_channels"]
    assert result["sent"] == 1 and result["aborted"] is True
    assert result["status"] == "ok"     # the vocabulary the panel and the agent read
    assert slept == []


def test_a_detached_viewer_is_sent_nothing():
    sent = []
    result = mirror.run_script(lambda t, a: sent.append(t), SCRIPT, delay_ms=0,
                               keep_going=lambda: False)
    assert sent == [] and result["aborted"] is True and result["sent"] == 0


def test_without_keep_going_the_whole_script_is_sent():
    sent = []
    result = mirror.run_script(lambda t, a: sent.append(t), SCRIPT, delay_ms=0)
    assert sent == [c["type"] for c in SCRIPT] and "aborted" not in result
