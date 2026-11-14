"""A bare `plexora` typed in an ssh session keeps its viewer token.

Typed over ssh, `plexora` detects the session (SSH_CONNECTION) and turns on
`--remote` for the user, so it prints the tunnel to take. An explicit
`--remote` is left without a token only because an older `plexora connect`
reads nothing but the port off the ready line -- and `plexora connect` always
types `--remote` itself, which turns detection off. A remote that was only
DETECTED has no such caller to stay compatible with, and is exactly the case
the token exists for: a shared login node or lab workstation, where every
other account logged in can reach 127.0.0.1:<port> with curl, which the
browser guard does not judge.
"""

import os
import sys

# Imported for real before `_fake_serve` swaps a stand-in in for `plexora`:
# main() and the root conftest's teardowns reach these by submodule path,
# which only resolves once they are loaded (as they are in a wider run).
import plexora._lifetime  # noqa: F401
import plexora.server.models.layer_jobs  # noqa: F401
import plexora.server.volume.prefetch  # noqa: F401
from plexora import notebook_env

from tests.test_cli import _fake_serve, cli


def _an_ssh_session_with_no_jupyter(monkeypatch):
    for name in cli.REMOTE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PLEXORA_HOST", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.5 51515 10.0.0.9 22")
    # A plain workstation or login node: no notebook server, not Colab.
    monkeypatch.setattr(notebook_env, "discover_jupyter_prefix",
                        lambda env=None, echo=print: None)
    monkeypatch.setattr(notebook_env, "in_colab", lambda: False)
    monkeypatch.setattr(cli, "detect_environment",
                        lambda resolver=None, echo=print:
                        notebook_env.resolve_display(echo=echo))


def test_bare_plexora_over_ssh_still_guards_the_port_with_a_token(monkeypatch, capsys):
    """Detection turns --remote on; it must not also take the token away, nor
    print a tunnel URL the browser cannot get in with."""
    monkeypatch.setenv("PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setenv("PLEXORA_BASE_URL", "")
    served = {}
    _fake_serve(monkeypatch, served)
    _an_ssh_session_with_no_jupyter(monkeypatch)

    cli.main(["--no-browser", "--port", "0"])

    out = capsys.readouterr().out
    config = sys.modules["plexora"].app.config
    # The launch really went down the detected-remote path.
    assert "Detected a machine reached over SSH" in out
    assert config.get("PLEXORA_SERVER_IS_REMOTE") is True
    assert served["host"] == "127.0.0.1"

    token = config.get("PLEXORA_AUTH_TOKEN")
    assert token, (
        "a bare `plexora` over ssh (remote only by detection) serves "
        "127.0.0.1 with no auth token")
    assert os.environ["PLEXORA_AUTH_TOKEN"] == token
    # The tunnel directions are the only URL the user is given.
    assert f"?token={token}" in out


def test_an_explicit_remote_is_still_tokenless_for_an_older_connect(monkeypatch):
    """The compatibility case keeps today's behaviour: `plexora connect` types
    --remote itself."""
    monkeypatch.setenv("PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setenv("PLEXORA_BASE_URL", "")
    served = {}
    _fake_serve(monkeypatch, served)
    _an_ssh_session_with_no_jupyter(monkeypatch)
    monkeypatch.setattr(cli, "_print_remote_instructions", lambda *a, **k: None)

    cli.main(["--remote", "--no-browser", "--port", "0"])

    assert not sys.modules["plexora"].app.config.get("PLEXORA_AUTH_TOKEN")
