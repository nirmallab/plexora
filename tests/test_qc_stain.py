"""Low quality staining in a QC session (`plugins/qc/server/stain.py`): one
picture per marker channel, read real, weak or failed; a first `failed` read
again by a second, blind reader; the verdict a flag in the result, never a
removal."""

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.ai import tasks
from plexora.plugins.qc.server import stain
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, _record, drive, ok, start

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


ONLY_STAIN = {"blur": False, "registration": False, "segmentation": False,
              "artifacts": False, "visual": False}


def test_the_nuclear_stains_and_non_marker_channels_are_not_read():
    names = ["DNA_1", "CD3", "AF1", "Blank_cy3", "background", "CD20", "DNA_2"]
    assert stain.checked_channels(names, ["DNA_1", "DNA_2"]) == ["CD3", "CD20"]


def test_a_failed_reading_needs_a_second_and_both_must_agree():
    assert stain.settle(["real"]) == "real"
    assert stain.settle(["weak"]) == "weak"
    assert stain.settle(["failed"]) is None
    assert stain.settle(["failed"], rereads=False) == "failed"
    assert stain.settle(["failed", "failed"]) == "failed"
    # The two differ: a third decides, best of three.
    assert stain.settle(["failed", "real"]) is None
    assert stain.settle(["failed", "weak"], rereads=False) == "weak"
    assert stain.settle(["failed", "real", "failed"]) == "failed"
    assert stain.settle(["failed", "real", "real"]) == "real"
    assert stain.settle(["failed", "weak", "weak"]) == "weak"
    assert stain.settle(["failed", "real", "weak"]) == "weak"
    assert stain.settle(["failed", "real", "cannot_tell"]) == "cannot_tell"


def test_the_partner_is_the_most_specific_population_the_graph_offers():
    names = ["DNA1", "CD3E", "CD4", "CD8a", "CD45", "TIGIT", "GZMB", "FOXP3", "CD163",
             "CD206"]
    # CD45 holds every candidate T-cell marker: never chosen beside them.
    assert stain.partner(names, "TIGIT")[0] not in (None, "CD45")
    assert stain.partner(names, "FOXP3")[0] == "CD4"
    assert stain.partner(names, "GZMB")[0] == "CD8a"
    assert stain.partner(names, "CD206")[0] == "CD163"
    assert stain.partner(["DNA1", "CD3E"], "CD3E") == (None, None)


def test_a_second_and_a_third_reading_are_their_own_tasks():
    assert tasks.task_for("qc", "stain_check") == "qc.stain_check"
    assert tasks.task_for("qc", "stain_check", check="reader_2") == "qc.stain_reader_2"
    assert tasks.task_for("qc", "stain_check", check="reader_3") == "qc.stain_reader_3"


class _SplitOracle(QCOracle):
    """CD3 read failed first, then real by the second and third readers."""

    def answer(self, packet, session_id):
        if packet["kind"] == "stain_check" and packet["evidence"]["marker"] == "CD3":
            self.cd3 = getattr(self, "cd3", 0) + 1
            verdict = "failed" if self.cd3 == 1 else "real"
            return {"kind": "stain_check", "expectation": "", "observed": "",
                    "structure": "yes", "verdict": verdict, "why": f"reading {self.cd3}"}
        return super().answer(packet, session_id)


def test_readers_who_differ_are_settled_by_a_third(tmp_path):
    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    sid = start(session, checks=ONLY_STAIN)["session_id"]
    packets = [p for p in drive(session, sid, _SplitOracle(info))
               if p["kind"] == "stain_check" and p["evidence"]["marker"] == "CD3"]
    assert len(packets) == 3
    assert packets[0]["evidence"] == packets[2]["evidence"]
    cd3 = _record(sid)["units"]["qcsynth::check::stain:CD3"]
    assert [r["verdict"] for r in cd3["readings"]] == ["failed", "real", "real"]
    assert cd3["verdict"] == "real" and not cd3["low_quality"]


def test_a_failed_stain_is_read_twice_blind_and_kept_as_a_flag(tmp_path):
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=("empty_channel",))
    assert info["truth"]["channels"].get("CD20") == "failed"
    session = AgentSession()
    started = start(session, checks=ONLY_STAIN)
    sid = started["session_id"]
    assert sorted(c for c in started["checks"] if c.startswith("stain:")) == \
        ["stain:CD20", "stain:CD3", "stain:CD8"]
    packets = [p for p in drive(session, sid, QCOracle(info)) if p["kind"] == "stain_check"]
    by_marker = {}
    for packet in packets:
        by_marker.setdefault(packet["evidence"]["marker"], []).append(packet)
    assert [len(by_marker[m]) for m in ("CD3", "CD8", "CD20")] == [1, 1, 2]
    first, second = by_marker["CD20"]
    # Blind: the second reader is sent exactly what the first was.
    assert first["question"] == second["question"]
    assert first["evidence"] == second["evidence"]
    units = _record(sid)["units"]
    cd20 = units["qcsynth::check::stain:CD20"]
    assert cd20["state"] == "decided" and cd20["verdict"] == "failed"
    assert cd20["low_quality"] and [r["read"] for r in cd20["readings"]] == [1, 2]
    assert units["qcsynth::check::stain:CD3"]["verdict"] == "real"
    ok(invoke(session, "qc_session_finish", {"session_id": sid}))
    checks = results.active(results.load("qcsynth"))["checks"]["stain"]
    assert checks["CD20"]["verdict"] == "failed" and checks["CD20"]["picture"]
    assert checks["CD3"]["verdict"] == "real" and not checks["CD3"]["low_quality"]
    report = ok(invoke(session, "qc_report", {"session_id": sid}))
    page = open(report["html"], encoding="utf-8").read()
    section = page.split("<h2>Low quality staining</h2>", 1)[1].split("<h2>", 1)[0]
    assert "CD20" in section and "failed / failed" in section and "CD3<" not in section
    assert _pdf_text(report["pdf"]).count("Low quality staining") >= 1


def _pdf_text(path):
    """The PDF's text-drawing operators, its streams decoded (reportlab
    writes them ASCII85 over Flate): enough to find a heading without a PDF
    library."""
    import base64
    import re
    import zlib

    raw = open(path, "rb").read()
    out = []
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", raw, re.S):
        data = stream.strip()
        try:
            if data.endswith(b"~>"):
                data = base64.a85decode(data[:-2])
            out.append(zlib.decompress(data).decode("latin-1"))
        except (ValueError, zlib.error):
            continue
    return "".join(out)


def test_low_quality_staining_can_be_turned_off(tmp_path):
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    started = start(session, checks={**ONLY_STAIN, "stain": False})
    assert not [c for c in started["checks"] if c.startswith("stain:")]
