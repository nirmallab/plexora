"""Every forward-carrying ssh exits when a forward cannot bind (audit lead N8).

Without ExitOnForwardFailure ssh only warns: a srun tunnel binding a local
port probed hours earlier, or a `-R` on a shared login host, went on with
the forward missing -- and whatever else held that port got the traffic.
"""
from tests.test_connect import connect_mod


def test_the_forwarding_ssh_commands_exit_on_a_failed_forward():
    direct = connect_mod.direct_ssh_argv("me@host", 9000, 8123, "plexora",
                                         reverse=[(41000, 41001)])
    tunnel = connect_mod.tunnel_ssh_argv("me@login", 9000, "compute-1", 8123)
    for argv in (direct, tunnel):
        assert "ExitOnForwardFailure=yes" in argv
        assert argv[argv.index("ExitOnForwardFailure=yes") - 1] == "-o"


def test_a_saved_option_still_wins():
    argv = connect_mod.direct_ssh_argv("me@host", 9000, 8123, "plexora",
                                       ssh_opts=["ExitOnForwardFailure=no"])
    assert "ExitOnForwardFailure=yes" not in argv
    assert "ExitOnForwardFailure=no" in argv
