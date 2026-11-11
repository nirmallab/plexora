"""What the platform holds back from every Plexora licence, and whether it offers a trial (licensing/switches.py).

The platform already leaves a held-back feature out of every certificate, so
nothing here gates anything: these tests are about what Plexora says -- the
Included / Not included rows, "Pro adds ...", the trial offer and a refusal's
words.
"""

import json
import time

import pytest

import plexora
from plexora.licensing import guards, manifest, switches

HELD = ["plugin:video_studio", "volume"]


@pytest.fixture
def platform(monkeypatch, tmp_path):
    """GET /v1/features answering `value["products"]` (or raising it), counted."""
    value = {"products": {"plexora": {"off": list(HELD), "trial": True}, "scimappro": {"off": [], "trial": True}},
             "asked": 0, "url": None}

    def fetch(url):
        value["asked"] += 1
        value["url"] = url
        if isinstance(value["products"], Exception):
            raise value["products"]
        return value["products"]

    from biocognia import store

    monkeypatch.delenv(store.ENV_OFFLINE, raising=False)
    monkeypatch.setenv("PLEXORA_FEATURES_URL", "https://api.biocognia.test/v1/features")
    monkeypatch.setattr(switches, "_fetch", fetch)
    switches.reset_for_tests()
    yield value
    switches.reset_for_tests()


@pytest.fixture
def client():
    with plexora.app.test_client() as test_client:
        yield test_client


def test_in_the_suite_nothing_is_asked_nothing_held_back_and_the_trial_offered(tmp_path):
    assert switches.current() == {"off": [], "trial": True}
    assert switches.held() == () and switches.trial_offered() is True
    assert not (tmp_path / switches.FILENAME).exists()


def test_reads_plexoras_entry_once_an_hour_and_keeps_it_in_the_data_root(platform, tmp_path):
    assert switches.held() == tuple(HELD)
    assert switches.trial_offered() is True
    assert platform["url"] == "https://api.biocognia.test/v1/features"
    switches.current()
    assert platform["asked"] == 1
    assert json.loads((tmp_path / switches.FILENAME).read_text())["products"]["plexora"]["off"] == HELD
    # Another process (the memo empty) reads the data root, not the platform.
    switches.reset_for_tests()
    assert switches.held() == tuple(HELD) and platform["asked"] == 1


def test_the_last_answer_stands_when_the_platform_cannot_be_reached(platform, tmp_path):
    switches.held()
    saved = tmp_path / switches.FILENAME
    stale = json.loads(saved.read_text())
    stale["at"] = time.time() - switches.MAX_AGE - 5
    saved.write_text(json.dumps(stale))
    switches.reset_for_tests()
    platform["products"] = OSError("offline")
    # Still held back -- an outage never releases a feature in what Plexora says.
    assert switches.held() == tuple(HELD)
    assert platform["asked"] == 2
    switches.held()
    assert platform["asked"] == 2, "a failure is not asked again at once"


def test_offline_or_with_no_platform_nothing_is_asked(platform, monkeypatch):
    from biocognia import store

    monkeypatch.setenv(store.ENV_OFFLINE, "1")
    assert switches.current() == {"off": [], "trial": True}
    monkeypatch.delenv(store.ENV_OFFLINE)
    monkeypatch.delenv("PLEXORA_FEATURES_URL")
    monkeypatch.setenv(store.ENV_SERVER, "")
    monkeypatch.setattr(store, "DEFAULT_SERVER", "")
    switches.reset_for_tests()
    assert switches.current() == {"off": [], "trial": True}
    assert platform["asked"] == 0
    # A refusal never asks, whatever is configured.
    monkeypatch.setenv("PLEXORA_FEATURES_URL", "https://api.biocognia.test/v1/features")
    switches.reset_for_tests()
    assert switches.is_held("volume:surfaces") is False and platform["asked"] == 0


def test_anything_unreadable_holds_nothing_back_and_offers_the_trial(platform):
    platform["products"] = {"plexora": {"off": "volume", "trial": "no"}}
    assert switches.current() == {"off": [], "trial": True}
    switches.reset_for_tests()
    platform["products"] = {"plexora": {"off": ["volume", 3, "", "volume"], "trial": False}}
    (switches._path()).unlink()
    assert switches.current() == {"off": ["volume"], "trial": False}


def test_held_back_features_are_not_listed_as_included_or_not(platform):
    rows = {row["entitlement"] for row in manifest.unlocks(["ai"], paid=True, held=switches.held())}
    assert rows == {"ai", "transcriptomics", "mcp"}
    # A licence given early access on its licence page has it anyway: listed, as included.
    early = {row["entitlement"]: row["granted"]
             for row in manifest.unlocks(["ai", "volume"], paid=True, held=switches.held())}
    assert early == {"ai": True, "transcriptomics": False, "mcp": False, "volume": True}
    words = manifest.pro_words(held=switches.held())
    assert words["included"] == "Plexora AI and Spatial transcriptomics"
    assert "Plexora 3D" not in words["included"] and "Video Studio" not in words["included"]
    # Without the switches, the Pro default as before.
    assert "Plexora 3D" in manifest.pro_words()["included"]


def test_status_leaves_out_what_is_held_back_and_the_trial_when_none_is_offered(platform, client, license_service):
    info = client.get("/license/status").get_json()["license"]
    assert {row["entitlement"] for row in info["unlocks"]} == {"ai", "transcriptomics", "mcp"}
    assert info["trial_offered"] is True and info["trial_url"].endswith("/start?product=plexora")
    platform["products"] = {"plexora": {"off": HELD, "trial": False}}
    switches.reset_for_tests()
    (switches._path()).unlink()
    info = client.get("/license/status").get_json()["license"]
    assert info["trial_offered"] is False and info["trial_url"] == ""


def test_a_refusal_of_a_held_back_feature_offers_no_trial_and_no_price(platform):
    switches.held(network=True)
    detail = guards.refusal_detail("volume:surfaces")
    assert detail["held"] is True and detail["off_by_default"] is True
    assert detail["hint"].startswith("Surface Studio (part of Plexora 3D) is not available yet")
    assert "trial" not in detail["hint"].replace("licence or trial", "") and "buy" not in detail["hint"]
    assert guards._needs(detail) == "Surface Studio is not available yet: no Plexora licence includes it today"
    assert guards.refusal_detail("volume")["hint"].startswith("Plexora 3D is not available yet")
    assert guards.refusal_detail("plugin:video_studio")["hint"].startswith("Video Studio is not available yet")
    # A feature that is on reads as before.
    assert guards.refusal_detail("transcriptomics")["held"] is False
    assert "part of Plexora Pro" in guards.refusal_detail("transcriptomics")["hint"]


def test_the_trial_command_says_when_none_is_offered(platform, capsys):
    from plexora.licensing import cli

    platform["products"] = {"plexora": {"off": [], "trial": False}}
    lines = []
    assert cli._trial(type("Args", (), {"email": None})(), lines.append) == 1
    assert lines[0].startswith("Plexora is not offering trials at the moment.")
