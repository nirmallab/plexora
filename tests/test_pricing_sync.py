"""The website's price list, and the tool that keeps it in step with the platform.

Plexora's prices are the BioCognia platform's to edit
(``GET /v1/products/plexora``); the website reads a checked-in copy,
``website/data/pricebook.json``. A page that quotes one figure beside a
checkout that charges another is the kind of bug customers do not forgive, so
the copy is checked rather than trusted -- and none of this touches the
network: the platform's product record is built here, in the shape its API
returns. SCIMAP Pro's tests/test_pricing_sync.py holds its own copy the same way.

Plexora Pro has one price per seat per year for each use class, so the total
is that price times the seats: no seat brackets, no volume discount.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "sync_pricing.py"
COPY = REPO / "website" / "data" / "pricebook.json"


def _book() -> dict:
    if not COPY.exists():
        pytest.skip(f"{COPY} is not present (running against a wheel)")
    return json.loads(COPY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sync():
    if not SCRIPT.exists():
        pytest.skip("running against a wheel; tools/ is not shipped")
    spec = importlib.util.spec_from_file_location("sync_pricing", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(use_class: str, intro: int | None, standard: int, *, seats_min=1, seats_max=None,
         plan_id: str = "plexora.pro", term_days: int = 365) -> dict:
    return {
        "plan_id": plan_id, "use_class": use_class,
        "seats_min": seats_min, "seats_max": seats_max,
        "term_days": term_days, "currency": "USD",
        "intro_cents": intro, "standard_cents": standard, "self_serve": 1,
    }


def _product(book: dict, *, extra: list[dict] | None = None) -> dict:
    """What ``GET /v1/products/plexora`` would return for ``book``.

    The shape of biocognia-platform's ``workers/core/src/routes/v1.ts``: plans,
    and one price row per plan, use class and term, for one seat upward
    (``seats_min`` 1, ``seats_max`` null), in cents.
    """
    prices = [
        _row(tier["audience"], tier["price"]["introCents"], tier["price"]["standardCents"],
             term_days=tier["termDays"])
        for tier in book["seatTiers"].values()
    ]
    return {
        "id": "plexora", "name": "Plexora", "trial_days": 30, "client_min_version": None,
        "manifest": {}, "manifest_hash": "0" * 64,
        "plans": [
            {"id": "plexora.pro", "name": "Pro",
             "entitlements": ["ai", "volume", "transcriptomics", "plugin:video_studio"],
             "limits": {}, "seat_model": "seat", "term_days": 365, "add_on": 0,
             "credits_micro": book["notes"]["creditsPerSeat"] * 10_000,
             "trial_credits_micro": 1_000_000},
        ],
        "prices": prices + (extra or []),
    }


#: The pricebook as it was before seat brackets were dropped.
V1_BOOK = {
    "version": 1,
    "currency": "USD",
    "billing": "annual",
    "brackets": [
        {"id": "list", "minSeats": 1, "maxSeats": 2, "discountPct": 0, "label": "1-2 seats"},
        {"id": "small", "minSeats": 3, "maxSeats": 9, "discountPct": 10, "label": "3-9 seats"},
    ],
    "seatTiers": {
        "academic-seat": {
            "name": "Academic", "audience": "academic", "machinesPerSeat": 2, "termDays": 365,
            "prices": {"list": {"introCents": 24900, "standardCents": 39900},
                       "small": {"introCents": 22400, "standardCents": 35900}},
        },
    },
    "notes": {"renewal": "", "creditsPerSeat": 1000},
}


# ---------------------------------------------------------------------------
# The checked-in copy
# ---------------------------------------------------------------------------

def test_the_website_copy_is_current():
    if not SCRIPT.exists():
        pytest.skip("running against a wheel; tools/ is not shipped")
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"],
                            cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_copy_has_one_price_per_seat_in_every_tier():
    book = _book()
    assert book["version"] == 2
    assert "brackets" not in book
    assert {tier["audience"] for tier in book["seatTiers"].values()} == {"academic", "commercial"}
    for name, tier in book["seatTiers"].items():
        assert "prices" not in tier, name
        assert tier["price"]["introCents"] <= tier["price"]["standardCents"], name
    # Site licences are quoted, not advertised.
    assert "siteThreshold" not in book["notes"] and "siteHint" not in book["notes"]


@pytest.mark.parametrize("tier,seats,expected", [
    ("academic-seat", 1, 24900),
    ("academic-seat", 3, 74700),
    ("academic-seat", 10, 249000),
    ("commercial-seat", 2, 99800),
])
def test_the_worked_examples_are_what_the_page_will_show(tier, seats, expected):
    """The total is the price of one seat times the seats, checked against the
    book rather than against a second implementation of the arithmetic."""
    book = _book()
    assert book["seatTiers"][tier]["price"]["introCents"] * seats == expected


# ---------------------------------------------------------------------------
# Converting the platform's product record
# ---------------------------------------------------------------------------

def test_the_platform_record_round_trips_to_the_same_page(sync):
    book = _book()
    converted, notes = sync.from_product(_product(book), book)
    assert sync.render(converted) == COPY.read_text(encoding="utf-8")
    assert notes == []


def test_an_absent_seats_min_means_one_seat(sync):
    book = _book()
    record = _product(book)
    for row in record["prices"]:
        del row["seats_min"]
    converted, _ = sync.from_product(record, book)
    assert sync.render(converted) == COPY.read_text(encoding="utf-8")


def test_the_pro_plan_is_the_one_shown(sync):
    """`plexora.pro` is the default; another priced plan's prices are not the page's."""
    assert sync.DEFAULT_PLAN == "plexora.pro"
    book = _book()
    extra = [_row("academic", 5000, 5000, plan_id="plexora.other"),
             _row("commercial", 9000, 9000, plan_id="plexora.other")]
    record = _product(book, extra=extra)
    record["plans"].append({"id": "plexora.other", "name": "Another plan", "entitlements": ["ai"],
                            "limits": {}, "seat_model": "seat", "term_days": 365, "add_on": 1,
                            "credits_micro": 0, "trial_credits_micro": 0})
    converted, _ = sync.from_product(record, book)
    assert converted["seatTiers"] == book["seatTiers"]


def test_a_price_changed_on_the_platform_reaches_the_page(sync):
    book = _book()
    record = _product(book)
    row = next(p for p in record["prices"] if p["use_class"] == "commercial")
    row["intro_cents"] = 44000
    converted, _ = sync.from_product(record, book)
    assert converted["seatTiers"]["commercial-seat"]["price"]["introCents"] == 44000
    # The words are the website's and are not touched.
    assert converted["notes"] == book["notes"]
    assert converted["seatTiers"]["commercial-seat"]["name"] == book["seatTiers"]["commercial-seat"]["name"]


def test_the_credits_a_seat_includes_come_from_the_plan(sync):
    book = _book()
    record = _product(book)
    record["plans"][0]["credits_micro"] = 2_000 * 10_000
    converted, _ = sync.from_product(record, book)
    assert converted["notes"]["creditsPerSeat"] == 2_000

    for wrong in (12_345, 0, None):
        record["plans"][0]["credits_micro"] = wrong
        with pytest.raises(sync.PricingError, match="whole"):
            sync.from_product(record, book)


def test_a_missing_intro_price_means_the_standard_one(sync):
    book = _book()
    record = _product(book)
    for row in record["prices"]:
        row["intro_cents"] = None
    converted, _ = sync.from_product(record, book)
    for tier in converted["seatTiers"].values():
        assert tier["price"]["introCents"] == tier["price"]["standardCents"]


def test_a_use_class_the_page_does_not_show_is_left_out_and_said_so(sync):
    book = _book()
    extra = [_row("nonprofit", 1, 1)]
    converted, notes = sync.from_product(_product(book, extra=extra), book)
    assert converted["seatTiers"] == book["seatTiers"]
    assert any("nonprofit" in note for note in notes)


def test_a_bracketed_platform_is_refused(sync):
    """The platform as it was: three rows per use class, priced by seat count.
    The tool does not pick one of them; it says what is wrong."""
    book = _book()
    record = _product(book)
    record["prices"] = [
        row
        for tier in book["seatTiers"].values()
        for row in (
            _row(tier["audience"], tier["price"]["introCents"], tier["price"]["standardCents"], seats_max=2),
            _row(tier["audience"], 22400, 35900, seats_min=3, seats_max=9),
            _row(tier["audience"], 19900, 31900, seats_min=10, seats_max=None),
        )
    ]
    with pytest.raises(sync.PricingError, match="brackets"):
        sync.from_product(record, book)


@pytest.mark.parametrize("seats_min,seats_max", [(1, 50), (3, None)])
def test_a_price_that_is_not_for_one_seat_upward_is_refused(sync, seats_min, seats_max):
    book = _book()
    record = _product(book)
    record["prices"][0]["seats_min"] = seats_min
    record["prices"][0]["seats_max"] = seats_max
    with pytest.raises(sync.PricingError, match="seats_min 1, seats_max null"):
        sync.from_product(record, book)


def test_two_prices_for_one_use_class_are_refused(sync):
    book = _book()
    extra = [_row("academic", 19900, 39900)]
    with pytest.raises(sync.PricingError, match="exactly one"):
        sync.from_product(_product(book, extra=extra), book)


def test_a_use_class_the_platform_does_not_price_is_refused(sync):
    book = _book()
    record = _product(book)
    record["prices"] = [p for p in record["prices"] if p["use_class"] != "academic"]
    with pytest.raises(sync.PricingError, match="no academic price"):
        sync.from_product(record, book)


def test_another_product_is_refused(sync):
    book = _book()
    record = _product(book)
    record["id"] = "scimappro"
    with pytest.raises(sync.PricingError):
        sync.from_product(record, book)


# ---------------------------------------------------------------------------
# What the tool accepts as a book
# ---------------------------------------------------------------------------

def test_a_version_1_book_is_refused(sync, tmp_path):
    with pytest.raises(sync.PricingError, match="version"):
        sync.validate(V1_BOOK)
    target = tmp_path / "pricebook.json"
    target.write_text(json.dumps(V1_BOOK), encoding="utf-8")
    assert sync.main(["--check"], target=target) == 1


def test_brackets_in_a_version_2_book_are_refused(sync):
    book = _book()
    book["brackets"] = V1_BOOK["brackets"]
    with pytest.raises(sync.PricingError, match="brackets"):
        sync.validate(book)


def test_an_introductory_price_above_the_standard_one_is_refused(sync):
    book = _book()
    price = book["seatTiers"]["academic-seat"]["price"]
    price["introCents"] = price["standardCents"] + 1
    with pytest.raises(sync.PricingError, match="introductory"):
        sync.validate(book)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def test_check_with_a_saved_record_compares_without_writing(sync, tmp_path):
    book = _book()
    target = tmp_path / "pricebook.json"
    target.write_text(COPY.read_text(encoding="utf-8"), encoding="utf-8")
    saved = tmp_path / "product.json"

    saved.write_text(json.dumps(_product(book)), encoding="utf-8")
    assert sync.main(["--source", str(saved), "--check"], target=target) == 0

    record = _product(book)
    row = next(p for p in record["prices"] if p["use_class"] == "academic")
    row["intro_cents"] -= 100
    saved.write_text(json.dumps(record), encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    assert sync.main(["--source", str(saved), "--check"], target=target) == 1
    assert target.read_text(encoding="utf-8") == before

    assert sync.main(["--source", str(saved)], target=target) == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["seatTiers"]["academic-seat"]["price"]["introCents"] == 24800


def test_the_default_source_is_the_platform_and_names_its_client(sync, tmp_path, monkeypatch):
    """`--source` with no value asks the platform. The call is intercepted here:
    this suite never touches the network."""
    book = _book()
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(_product(book)).encode("utf-8")

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["client"] = request.get_header("Biocognia-client")
        seen["agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Reply()

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    target = tmp_path / "pricebook.json"
    target.write_text(COPY.read_text(encoding="utf-8"), encoding="utf-8")

    assert sync.main(["--source", "--check"], target=target) == 0
    assert seen["url"] == "https://api.biocognia.com/v1/products/plexora"
    assert seen["client"].startswith("plexora/")
    # Cloudflare answers urllib's own User-Agent with error 1010.
    assert seen["agent"] and not seen["agent"].startswith("Python-urllib")
    assert seen["timeout"] <= 30


def test_an_unreachable_platform_fails_cleanly(sync, tmp_path, monkeypatch):
    def unreachable(request, timeout):
        raise OSError("no route to host")

    monkeypatch.setattr(sync.urllib.request, "urlopen", unreachable)
    target = tmp_path / "pricebook.json"
    target.write_text(COPY.read_text(encoding="utf-8"), encoding="utf-8")
    assert sync.main(["--source"], target=target) == 2
    assert target.read_text(encoding="utf-8") == COPY.read_text(encoding="utf-8")
