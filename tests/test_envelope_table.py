"""The website's envelope page (docs/internal/bench/envelope/ENVELOPE.md) is
generated from the limits and the self-check records, and is up to date
(tools/bench_envelope/envelope_table.py); `add` files a downloaded
self-check record and refuses a file that is not one."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("envelope_table", ROOT / "tools" / "bench_envelope" / "envelope_table.py")
envelope_table = importlib.util.module_from_spec(spec)
spec.loader.exec_module(envelope_table)


def test_the_page_is_up_to_date():
    assert envelope_table.main(["--check"]) == 0, "run python tools/bench_envelope/envelope_table.py"


def test_every_limit_and_record_is_on_the_page():
    text = envelope_table.page()
    limits = json.loads(envelope_table.LIMITS.read_text(encoding="utf-8"))
    for name, section in limits.items():
        if isinstance(section, dict):
            assert f"| {name} |" in text
    for label, _ in envelope_table.records():
        assert f"| {label} |" in text


def _record(**steps):
    ok = {name: {"ok": True, "detail": {}} for name in envelope_table.STEPS}
    ok.update(steps)
    return {"selfcheck": 1, "date": "2026-11-07T10:00:00Z", "ok": True, "steps": ok}


def test_add_files_a_record_and_refuses_anything_else(tmp_path, monkeypatch):
    monkeypatch.setattr(envelope_table, "RECORDS", tmp_path / "selfcheck")
    good = tmp_path / "plexora-selfcheck-2026-11-07.json"
    good.write_text(json.dumps(_record()), encoding="utf-8")
    assert envelope_table.add(good, "Mac Safari").name == "2026-11-07-mac-safari.json"
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(SystemExit):
        envelope_table.add(other, "x")
    with pytest.raises(SystemExit):
        envelope_table.add(good, "!!!")


def test_a_row_shows_the_estimate_beside_the_measurement():
    row = envelope_table._row("pc", _record(
        browser={"ok": True, "detail": {"engine": "Chrome 156", "platform": "Windows", "cores": 8, "memory_gb": 8}},
        pyramid={"ok": True, "detail": {"bytes_per_second": 1.2e8, "estimate": {"flat_limit_bytes": 2.088e10},
                                        "measured": {"flat_limit_bytes": 1.67e10}}}))
    assert "| Chrome 156 | Windows | 8 / 8+ |" in row
    assert "17 (21, ×0.80)" in row
