"""What the website says where only the desktop app could act.

A web page cannot open a TCP socket, run a command-line tool or stop an
installed program (plan decision 1), so a few routes are answered on the
website by a notice instead (plexora/browser/routes.py `HANDOFF`), which
services/handoff.js draws in the panel that asked or in the corner -- never a
blank panel or a click that does nothing. These hold:

- the set of such routes is exactly the SSH-, CLI- and desktop-bound list
  below, so adding one is a decision, made here, not a default;
- every notice is short (the user's rule for dialogs: a few words of title,
  one line) and offers only actions the page knows how to take;
- the generated route table carries the notice to the page, and the page's
  drawer knows every action;
- the website's pages leave out what cannot work there: Quit, the server
  list of Settings > Remote servers.
"""

import re
from pathlib import Path

import pytest

from plexora.browser import routes

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "plexora" / "client"

#: The routes a web page cannot serve, by why. A new one is a decision: add
#: it to routes.HANDOFF *and* here, with the reason.
SSH = {  # a web page cannot open an SSH connection
    "settings_remotes_connect", "settings_remotes_answer", "settings_remotes_disconnect",
    "settings_remotes_status", "settings_remotes_askpass_prompt", "settings_remotes_askpass_answer",
}
# Google Cloud is not here: since plan L9 the website answers it over Google's
# JSON APIs with the page's sign-in (plexora/gcloud_rest.py).
INSTALLED_PROGRAM = {  # updating or stopping an installed Plexora
    "update_check", "update_install", "update_install_status", "update_prefs",
    "update_restart", "shutdown",
}
DATA_FOLDER = {  # a data folder on a disk, and moving one
    "settings_data", "settings_data_set", "settings_data_check", "settings_data_migration",
}
DESKTOP_WINDOW = {"desktop_info", "desktop_open"}  # the desktop app's own window
BENCH = {"bench_flush"}  # a server cache flush for the bench harness

EXPECTED = SSH | INSTALLED_PROGRAM | DATA_FOLDER | DESKTOP_WINDOW | BENCH


def test_the_handoff_set_is_exactly_the_ssh_cli_and_desktop_bound_routes():
    assert set(routes.HANDOFF) == EXPECTED, (
        "a route the website hands off is a decision: add it to tests/test_handoff_routes.py "
        f"with its reason. Extra: {sorted(set(routes.HANDOFF) - EXPECTED)}; "
        f"missing: {sorted(EXPECTED - set(routes.HANDOFF))}")


@pytest.mark.parametrize("endpoint", sorted(EXPECTED))
def test_every_notice_is_a_short_title_and_one_line(endpoint):
    notice = routes.HANDOFF[endpoint]
    assert notice.to in ("desktop", "none")
    # A few words, not a sentence: the user reads the choice, not an essay.
    assert 4 <= len(notice.title) <= 40 and not notice.title.endswith("."), notice.title
    assert notice.title[0].isupper()
    # One line: one sentence, ending with its full stop.
    assert notice.why.endswith(".") and 30 <= len(notice.why) <= 110, notice.why
    assert notice.why.count(". ") == 0, f"more than one sentence: {notice.why!r}"
    for word in ("upgrade", "unlock", "pro ", "sorry", "error"):
        assert word not in notice.why.lower() and word not in notice.title.lower(), (endpoint, word)


@pytest.mark.parametrize("endpoint", sorted(EXPECTED))
def test_every_notice_offers_only_known_actions(endpoint):
    notice = routes.HANDOFF[endpoint]
    assert isinstance(notice.actions, tuple)
    assert len(set(notice.actions)) == len(notice.actions)
    assert set(notice.actions) <= set(routes.ACTIONS)
    if notice.to == "desktop":
        # Somewhere to go: the desktop app, at least.
        assert "open_desktop" in notice.actions


def test_ssh_offers_the_desktop_app_and_a_paired_node():
    """Decision 1: SSH keeps its zero-cost routes -- the desktop app, or a node
    started on the server (and paired with this browser) -- and Google Cloud,
    which the website reaches without SSH since plan L9."""
    for endpoint in SSH:
        assert routes.HANDOFF[endpoint].actions == ("open_desktop", "pair_node",
                                                    "google_cloud"), endpoint


def test_the_route_table_carries_every_notice_to_the_page():
    text = (CLIENT / "src" / "js" / "services" / "backendRoutes.js").read_text(encoding="utf-8")
    import json

    rows = [json.loads(line.strip().rstrip(",")) for line in text.splitlines()
            if line.strip().startswith('{"class"')]
    handed = {row["endpoint"]: row["handoff"] for row in rows if row["class"] == "handoff"}
    assert set(handed) == EXPECTED, "backendRoutes.js is stale: python -m plexora.browser.routes --write"
    for endpoint, shown in handed.items():
        notice = routes.HANDOFF[endpoint]
        assert shown == {"to": notice.to, "title": notice.title, "why": notice.why,
                         "actions": list(notice.actions)}, endpoint


def test_the_page_draws_every_action():
    """services/handoff.js knows the same actions, each with a label and a
    function -- a button that does nothing is the failure this prevents."""
    text = (CLIENT / "src" / "js" / "services" / "handoff.js").read_text(encoding="utf-8")
    declared = re.search(r"const ACTIONS = Object\.freeze\(\[([^\]]*)\]\)", text).group(1)
    assert sorted(re.findall(r'"(\w+)"', declared)) == sorted(routes.ACTIONS)
    labels = re.search(r"const LABELS = Object\.freeze\(\{(.*?)\}\);", text, re.S).group(1)
    run = re.search(r"const RUN = \{([^}]*)\}", text).group(1)
    for action in routes.ACTIONS:
        assert re.search(rf"\b{action}:", labels), action
        assert re.search(rf"\b{action}:", run), action


def test_backend_answers_a_handoff_and_tells_the_page():
    text = (CLIENT / "src" / "js" / "services" / "backend.js").read_text(encoding="utf-8")
    assert '"plexora:handoff"' in text
    assert "handoff: row.handoff" in text and "501" in text


def test_every_page_loads_the_notice():
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    assert re.search(r'src="\{\{ data\.base_url \}\}/client/src/js/services/handoff\.js\?v=\w+"', base)


# -- the website's pages ---------------------------------------------------------


@pytest.fixture()
def website(monkeypatch, tmp_path):
    """The app as the website's worker builds it: PLEXORA_BACKEND=browser."""
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    import plexora

    plexora.app.config["TESTING"] = True
    return plexora.app.test_client()


def test_the_website_has_no_quit(website, monkeypatch):
    html = website.get("/settings").get_data(as_text=True)
    assert 'id="nav_quit"' not in html
    monkeypatch.delenv("PLEXORA_BACKEND")
    assert 'id="nav_quit"' in website.get("/settings").get_data(as_text=True)


def test_the_website_shows_the_notice_for_remote_servers(website, monkeypatch):
    html = website.get("/settings").get_data(as_text=True)
    notice = routes.HANDOFF["settings_remotes_connect"]
    assert 'id="settings_remotes_handoff"' in html
    assert 'data-handoff-path="settings/remotes/_/connect"' in html
    assert notice.title in html and notice.why in html
    # No saved-server list and no presets to fail.
    assert 'id="settings_remotes_list"' not in html
    assert 'id="settings_remote_catalogue"' not in html
    # Pairing a node is offered where nodes are listed.
    assert 'id="settings_node_pair"' in html
    monkeypatch.delenv("PLEXORA_BACKEND")
    served = website.get("/settings").get_data(as_text=True)
    assert 'id="settings_remotes_list"' in served and 'id="settings_remotes_handoff"' not in served
    assert 'id="settings_node_pair"' not in served
