"""Split Composite, and the linked row it produces.

The move this plugin is worth building for, and the one whose failure mode is
invisible: a split whose derived panels do not share the composite's exact crop
and windows produces a row that LOOKS like a split-channel figure and is not
comparable across its panels. That is the same defect as making one by hand,
which is what this feature exists to replace.

It runs entirely in the browser, so nothing else in this suite can see it.

    node tests/js/figure_split_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder.server import operations, schema

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_split_probe.mjs"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_a_split_produces_a_comparable_row(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


def test_a_split_draws_the_panels_it_creates():
    """The half that was missing, and the reason both split modes produced rows
    of empty frames.

    `splitComposite` writes N panels whose scenes are correct and which have no
    RASTER -- a panel's picture on the canvas is a cached preview fetched from
    `/previews/<panel_id>`, and nothing had ever stored one for a derived panel.
    Every split therefore looked like a rendering bug in the split; it was the
    absence of a render.

    `refreshPreviews` is the machinery Apply Rendering already used: read each
    visible channel over the panel's own viewport, composite in the browser with
    the exporter's arithmetic, show it, upload it. Which also means a split row
    is drawn from the SOURCE at the windows the user set, rather than from a
    crop of the composite -- so a channel that was faint under three others
    comes out looking the way it does on its own.

    Asserted against the source because `FigureWorkspace` is too much of a page
    to stand up in a probe. The probe holds the other half: that the derived
    panels are findable in the link group by their lineage alone.
    """
    workspace = (REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static"
                 / "figureWorkspace.js").read_text(encoding="utf-8")
    body = workspace[workspace.index("\n    async split() {"):]
    body = body[:body.index("\n    addPage(")]

    assert "splitComposite" in body
    assert 'operation === "split_channel"' in body, \
        "the derived panels have to be picked out of the row, not guessed at"
    assert "refreshPreviews" in body, "a split that renders nothing is the bug"


def test_the_server_accepts_a_whole_split_as_one_batch():
    """The client's half is only useful if the server takes it in one piece: a
    batch that had to be sent as five requests would be five revisions, five
    conflict windows and five undo steps."""
    document = schema.new_document("fig_aaaaaaaaaaaa", title="F")
    document = operations.apply_operations(document, [{
        "op": "add_source",
        "source": {"source_id": "src_1", "kind": "plexora_project", "datasource": "demo"},
    }])

    def panel(panel_id, key):
        return {
            "op": "add_panel",
            "panel": {
                "panel_id": panel_id, "source_id": "src_1",
                "scene": {"viewport": {"x": 10, "y": 20, "w": 400, "h": 300},
                          "channels": [{"key": key, "color": {"r": 255, "g": 0, "b": 0},
                                        "window": [0, 1000]}]},
                "placement": {"page_id": "pg_1", "x_mm": 10, "y_mm": 10,
                              "w_mm": 30, "h_mm": 22, "z": 0},
                "derived_from": {"panel_id": "pnl_c", "operation": "split_channel",
                                 "layer": key},
            },
        }

    batch = [panel("pnl_c", "demo_0")]
    batch += [panel(f"pnl_{i}", f"demo_{i}") for i in range(1, 4)]
    batch.append({"op": "link_panels", "group": {
        "group_id": "grp_1",
        "panel_ids": ["pnl_c", "pnl_1", "pnl_2", "pnl_3"],
        "sync": ["viewport", "size"]}})

    updated = operations.apply_operations(document, batch)
    assert len(updated["panels"]) == 4
    assert updated["link_groups"]["grp_1"]["sync"] == ["viewport", "size"]
    # Lineage survives the round trip: the provenance page names it, and a
    # future "regenerate this split" has to be able to find it.
    assert updated["panels"]["pnl_2"]["derived_from"] == {
        "panel_id": "pnl_c", "operation": "split_channel", "layer": "demo_2"}


def test_a_whole_split_costs_exactly_one_revision(tmp_path, monkeypatch):
    """The server-side half of "one split, one undo step": `apply` bumps the
    revision once per CALL, not once per operation. Six operations that cost six
    revisions would be six conflict windows for other tabs and six steps to
    walk back."""
    import plexora
    from plexora.plugins.figure_builder.server import repository

    figure_id = repository.create("Figure 1")
    repository.apply(figure_id, 0, [{
        "op": "add_source",
        "source": {"source_id": "src_1", "kind": "plexora_project", "datasource": "demo"},
    }])

    batch = [{
        "op": "add_panel",
        "panel": {"panel_id": f"pnl_{i}", "source_id": "src_1",
                  "scene": {"viewport": {"x": 0, "y": 0, "w": 100, "h": 80},
                            "channels": [{"key": f"demo_{i}"}]},
                  "placement": {"page_id": "pg_1", "x_mm": 10 * i, "y_mm": 10,
                                "w_mm": 30, "h_mm": 22, "z": i}},
    } for i in range(4)]
    batch.append({"op": "link_panels", "group": {
        "group_id": "grp_1", "panel_ids": [f"pnl_{i}" for i in range(4)],
        "sync": ["viewport", "size"]}})

    assert repository.apply(figure_id, 1, batch) == 2
    assert len(repository.load(figure_id)["panels"]) == 4
