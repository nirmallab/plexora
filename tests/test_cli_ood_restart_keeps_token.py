"""An Open OnDemand server restarted after an in-app update keeps its token.

Help > Check for Updates > Restart stops `plexora --ood` and starts it again
with the same arguments (cli.restart_command) and the same environment, which
already holds the token the first launch chose. The page that asked for the
restart is still open, holding that token in its cookie, and polls /health to
see the new version come up. A new token would refuse every request that page
makes -- the dialog then says Plexora "did not come back" although it did.
Every other launcher keeps an inherited token for exactly this case
(cli._terminal_token).
"""

import os
import sys

import pytest

# The real submodules main() imports by path, loaded before _fake_serve swaps a
# stand-in in for the `plexora` package itself.
import plexora._lifetime  # noqa: F401
from plexora.server.models import data_model, layer_jobs  # noqa: F401
from plexora.server.volume import prefetch  # noqa: F401 - conftest's teardown
from tests.test_cli import FIXED_PORT, _fake_serve, _inside_a_job, cli


@pytest.fixture(autouse=True)
def _claim_the_environment(monkeypatch):
    """main() writes the token and the restart flag into os.environ; claimed
    here so neither outlives the test."""
    monkeypatch.setenv("PLEXORA_AUTH_TOKEN", "")
    monkeypatch.delenv(cli.RESTART_ENV_VAR, raising=False)


def test_ood_restart_after_an_update_keeps_the_token_the_open_page_holds(monkeypatch):
    first = {}
    _fake_serve(monkeypatch, first)
    _inside_a_job(monkeypatch)
    rest = ["--ood", "--port", str(FIXED_PORT)]

    cli.main(rest)
    token = os.environ["PLEXORA_AUTH_TOKEN"]
    assert token
    assert first["app"].config["PLEXORA_AUTH_TOKEN"] == token

    # What restart_after_update hands the new process: the argv restart_command
    # builds, and this process's environment plus the restart flag.
    command = cli.restart_command(rest, first["port"])
    argv = command[command.index("-m") + 2:]
    monkeypatch.setenv(cli.RESTART_ENV_VAR, "1")
    monkeypatch.setattr(cli, "wait_for_port", lambda host, port, *a, **k: True)
    second = {}
    _fake_serve(monkeypatch, second)

    cli.main(argv)

    assert second["port"] == first["port"]
    assert os.environ["PLEXORA_AUTH_TOKEN"] == token
    assert second["app"].config["PLEXORA_AUTH_TOKEN"] == token, (
        "the restarted --ood server minted a new token, so the page that asked "
        "for the restart is refused (403) on every request")
