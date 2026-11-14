"""A figure in a `.plexora` file brings only files under its own
`figures/<id>/` (server/models/bundles.py `_validate`, `_merge_figures`): a
crafted file record that does not start with that prefix must not be placed
outside the new figure's folder, so another figure this user has keeps its
`figure.db`."""

import json
import zipfile

from plexora.server.models import bundles

from tests.test_bundles import _members, project  # noqa: F401 -- fixture

FIGURE_ID = "fig_aaaaaa"
PAYLOAD = b"OVERWRITTEN BY BUNDLE"


def _escaping_path(victim):
    """A member name whose every part is safe, which, sliced at
    len("figures/<id>/") as `_merge_figures` slices it, reads
    "../<victim>/figure.db"."""
    prefix = f"figures/{FIGURE_ID}/"
    evil = "y" * (len(prefix) - 2) + "/a../" + victim + "/figure.db"
    assert bundles._safe_member(evil) and not evil.startswith(prefix)
    assert evil[len(prefix):] == f"../{victim}/figure.db"
    return evil


def _victim():
    from plexora.plugins.figure_builder.server import repository

    victim = repository.create("Mine")
    database = repository.figure_dir(victim) / "figure.db"
    return victim, database, database.read_bytes()


def test_merging_a_figure_never_places_a_file_outside_its_own_folder(tmp_path):
    victim, database, before = _victim()
    evil = _escaping_path(victim)
    staged_file = tmp_path / "staged.bin"
    staged_file.write_bytes(PAYLOAD)
    manifest = {"figures": [{"id": FIGURE_ID, "title": "Crafted", "projects": ["p" * 32],
                             "files": [{"path": evil, "size": len(PAYLOAD), "sha256": None}]}]}
    try:
        bundles._merge_figures(manifest, {evil: staged_file}, {}, {"p" * 32: "slide"}, [])
    except bundles.BundleError:
        pass  # refusing it is right too
    assert database.read_bytes() == before, "the figure's file was moved onto another figure's figure.db"


def test_a_bundle_with_a_figure_file_outside_its_folder_is_refused(project, tmp_path):
    victim, database, before = _victim()
    sent = tmp_path / "slide.plexora"
    bundles.export(["slide"], sent, include={"tables": True, "captures": True, "figures": False})
    names, manifest = _members(sent)
    pid = manifest["projects"][0]["id"]
    evil = _escaping_path(victim)
    manifest["figures"] = [{"id": FIGURE_ID, "title": "Crafted", "projects": [pid],
                            "files": [{"path": evil, "size": len(PAYLOAD), "sha256": None}]}]
    crafted = tmp_path / "crafted.plexora"
    with zipfile.ZipFile(sent) as source, zipfile.ZipFile(crafted, "w", zipfile.ZIP_DEFLATED) as out:
        for name in names:
            if name == bundles.MANIFEST:
                out.writestr(name, json.dumps(manifest))
            else:
                out.writestr(source.getinfo(name), source.read(name))
        out.writestr(evil, PAYLOAD)

    raised = None
    try:
        bundles.apply(crafted, choices={pid: "keep_both"})
    except Exception as error:  # noqa: BLE001 -- which one is the point
        raised = error
    assert database.read_bytes() == before, "the import overwrote another figure's figure.db"
    assert isinstance(raised, bundles.BundleError), \
        f"a figure file outside figures/<id>/ was not refused before unpacking: {raised!r}"
