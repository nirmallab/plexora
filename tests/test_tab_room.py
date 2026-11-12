"""What a browser tab can hold, asked before a build or a read (server/utils/tab_room.py).

On the website (PLEXORA_BACKEND=browser) a build past BUILD_BUDGET, or one
read that cannot stream past READ_LIMIT, is refused before it starts, with a
sentence naming what, its size and the desktop app; the page is told too (the
teller). Off the website every answer is "go ahead". A request whose handler
is refused this way is a 507 with that sentence.
"""

import pytest

from plexora.server.utils import tab_room
from plexora.server.utils.tab_room import TooLargeForTab


@pytest.fixture
def website(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)


@pytest.fixture(autouse=True)
def no_teller():
    """Every test starts and ends with no page to tell."""
    tab_room.set_teller(None)
    yield
    tab_room.set_teller(None)


def _recording_teller():
    told = []
    tab_room.set_teller(told.append)
    return told


# -- check_build / check_read --------------------------------------------------

CHECKS = [
    pytest.param(tab_room.check_build, "BUILD_BUDGET", "This mask's pyramid", id="build"),
    pytest.param(tab_room.check_read, "READ_LIMIT", "This stack's planes", id="read"),
]


@pytest.mark.parametrize("check,limit,what", CHECKS)
def test_off_the_website_nothing_is_ever_refused(desktop, check, limit, what):
    told = _recording_teller()
    for nbytes in (0, getattr(tab_room, limit), getattr(tab_room, limit) + 1, 1e15):
        check(nbytes, what)
    assert told == []


@pytest.mark.parametrize("check,limit,what", CHECKS)
def test_on_the_website_at_or_under_the_limit_goes_ahead(website, check, limit, what):
    told = _recording_teller()
    check(0, what)
    check(getattr(tab_room, limit) // 2, what)
    check(getattr(tab_room, limit), what)
    assert told == []


@pytest.mark.parametrize("check,limit,what", CHECKS)
def test_on_the_website_past_the_limit_is_refused_by_name_size_and_the_desktop_app(
        website, check, limit, what):
    told = _recording_teller()
    asked = getattr(tab_room, limit) + 1
    with pytest.raises(TooLargeForTab) as raised:
        check(asked, what)
    message = str(raised.value)
    assert what in message
    assert "GB" in message or "MB" in message
    assert "desktop app" in message.lower()
    assert raised.value.nbytes == asked
    # The page was told once, in one short line (the notice's house style:
    # tests/test_envelope.py); the error carries the whole sentence.
    assert len(told) == 1
    notice = told[0]
    assert {"reason", "title", "note"} <= set(notice)
    assert notice["reason"] == "tab-limit"
    assert notice["title"]
    assert what in notice["note"] and ("GB" in notice["note"] or "MB" in notice["note"])
    assert len(notice["note"]) <= 60


@pytest.mark.parametrize("check,limit,what", CHECKS)
def test_the_size_in_the_sentence_is_the_size_asked(website, check, limit, what):
    with pytest.raises(TooLargeForTab) as raised:
        check(3.4e9, what)
    assert "3.4 GB" in str(raised.value)
    assert raised.value.nbytes == int(3.4e9)


@pytest.mark.parametrize("check,limit,what", CHECKS)
def test_a_teller_that_fails_does_not_stop_the_refusal(website, check, limit, what):
    calls = []

    def broken(notice):
        calls.append(notice)
        raise RuntimeError("the page is gone")

    tab_room.set_teller(broken)
    with pytest.raises(TooLargeForTab):
        check(getattr(tab_room, limit) * 4, what)
    assert len(calls) == 1


@pytest.mark.parametrize("check,limit,what", CHECKS)
def test_with_no_teller_it_is_still_refused(website, check, limit, what):
    tab_room.set_teller(None)
    with pytest.raises(TooLargeForTab):
        check(getattr(tab_room, limit) * 4, what)


def test_too_large_for_tab_is_a_runtime_error_with_its_size():
    exc = TooLargeForTab("too big", 123.7)
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "too big" and exc.nbytes == 123


# -- streaming reads ---------------------------------------------------------------

def test_full_read_max_on_the_website_is_capped_at_the_read_budget(website):
    assert tab_room.full_read_max(8 * 2**30) == tab_room.READ_BUDGET
    assert tab_room.full_read_max(1000) == 1000


def test_full_read_max_elsewhere_is_what_was_asked(desktop):
    assert tab_room.full_read_max(8 * 2**30) == 8 * 2**30
    assert tab_room.full_read_max(1000) == 1000


def test_slab_max_on_the_website_is_capped_at_the_slab_budget(website):
    assert tab_room.slab_max(1 * 2**30) == tab_room.SLAB_BUDGET
    assert tab_room.slab_max(4096) == 4096


def test_slab_max_elsewhere_is_what_was_asked(desktop):
    assert tab_room.slab_max(1 * 2**30) == 1 * 2**30
    assert tab_room.slab_max(4096) == 4096


def test_on_website_is_the_browser_backend_only(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    assert tab_room.on_website()
    monkeypatch.setenv("PLEXORA_BACKEND", "flask")
    assert not tab_room.on_website()
    monkeypatch.delenv("PLEXORA_BACKEND")
    assert not tab_room.on_website()


# -- a size for a sentence -------------------------------------------------------

@pytest.mark.parametrize("nbytes,text", [(640e6, "640 MB"), (3.4e9, "3.4 GB"), (62e9, "62 GB")])
def test_size_text(nbytes, text):
    assert tab_room.size_text(nbytes) == text


# -- over HTTP ---------------------------------------------------------------------

def test_a_request_refused_for_the_tab_is_a_507_with_the_sentence(monkeypatch):
    import plexora

    app = plexora.app
    # An existing route made to refuse: a new one cannot be added once the app
    # has served a request.
    endpoint, _ = app.url_map.bind("localhost").match("/health", method="GET")
    sentence = "This mask's pyramid would take about 3.4 GB of this browser tab's memory."

    def refused(*args, **kwargs):
        raise TooLargeForTab(sentence, int(3.4e9))

    monkeypatch.setitem(app.view_functions, endpoint, refused)
    answer = app.test_client().get("/health")
    assert answer.status_code == 507
    body = answer.get_json()
    assert body["success"] is False
    assert body["tab_limit"] is True
    assert body["error"] == sentence
