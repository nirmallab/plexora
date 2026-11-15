"""The website reads an ssh `Port` option in either spelling (audit nr-remotes-7).

ssh takes `-o Port=2222` and `-o "Port 2222"` alike, and the desktop passes a
profile's ssh options to ssh as they are -- so the website, which picks the
port itself, has to read both or it signs in on 22.
"""
import pytest

from plexora.server.models import web_remote


@pytest.mark.parametrize("option", ["Port=2222", "Port 2222", "port = 2222", "Port\t2222"])
def test_port_option_in_either_spelling(option):
    assert web_remote._host_port("example.org", (option,)) == ("example.org", 2222)


def test_host_port_wins_and_default_is_22():
    assert web_remote._host_port("example.org:2200", ("Port 2222",)) == ("example.org", 2200)
    assert web_remote._host_port("example.org", ("ServerAliveInterval 30",)) == ("example.org", 22)
