"""Unit tests for the single-pass mask converter that replaced the old
pyramid_assemble -> pyramid_upgrade -> ensure_outline_segmentation pipeline.

Two things here are worth more than the usual "does it run" coverage:

- The output has to be readable through *exactly* the reader the tile server
  uses (`TiffFile(..., is_ome=False)` -> `series[0].aszarr()` -> zarr group),
  because the writer emits OME metadata that reader deliberately ignores. If
  levels stop resolving there, the viewer silently loses its pyramid.
- `looks_like_outline_mask` used to sample with a full-plane stride, which on a
  wide mask put neighbouring samples ~20px apart. Interior-ness is a local
  property, so every large filled mask scored as "already outlines" and outline
  generation was skipped -- the viewer got filled labels. See
  test_a_large_filled_mask_is_not_mistaken_for_outlines.
"""

import os
from pathlib import Path

import numpy as np
import pytest
import tifffile
import zarr

from plexora.server.utils import segmentation_pyramid as sp


def _touching_cells(height, width, cell=30, dtype=np.uint32):
    """Filled label mask of abutting square cells. They must *touch*: that is
    the only place exact (cell-cell) and fast (cell-background) boundary
    detection disagree."""
    labels = np.zeros((height, width), dtype=dtype)
    next_id = 1
    for y in range(0, height - cell + 1, cell):
        for x in range(0, width - cell + 1, cell):
            labels[y:y + cell, x:x + cell] = next_id
            next_id += 1
    return labels


def _read_as_the_tile_server_does(path):
    """Returns (levels, is_multiscales, is_tiled) via data_model's reader path."""
    with tifffile.TiffFile(str(path), is_ome=False) as reader:
        series = reader.series[0]
        store = series.aszarr()
        group = zarr.open(store, mode="r")
        if isinstance(group, zarr.Array):
            levels = [np.asarray(group)]
        else:
            levels = [np.asarray(group[str(i)]) for i in range(len(group))]
        return levels, store.is_multiscales, series.levels[0].pages[0].is_tiled


@pytest.fixture
def filled_mask(tmp_path):
    path = tmp_path / "mask.tiff"
    labels = _touching_cells(900, 1300)
    tifffile.imwrite(path, labels)
    return path, labels


def test_output_is_readable_the_way_the_tile_server_reads_it(tmp_path, filled_mask):
    source, _ = filled_mask
    out = sp.pyramidize_segmentation_mask(source, tmp_path / "out.ome.tiff", tile_size=256)

    levels, is_multiscales, is_tiled = _read_as_the_tile_server_does(out)
    assert is_multiscales
    assert is_tiled
    assert len(levels) > 1
    assert levels[0].dtype == np.uint32
    # Each level halves, rounding up.
    for finer, coarser in zip(levels, levels[1:]):
        assert coarser.shape == ((finer.shape[0] + 1) // 2, (finer.shape[1] + 1) // 2)


def test_outline_keeps_label_ids_on_boundaries_and_clears_interiors(tmp_path, filled_mask):
    source, labels = filled_mask
    out = sp.pyramidize_segmentation_mask(source, tmp_path / "out.ome.tiff", tile_size=256)

    levels, _, _ = _read_as_the_tile_server_does(out)
    base = levels[0][: labels.shape[0], : labels.shape[1]]
    drawn = base != 0

    assert drawn.sum() < (labels != 0).sum()
    # Boundary pixels carry the original cell ID, not a flag.
    assert np.array_equal(base[drawn], labels[drawn])
    # Nothing is drawn where the source had no cell.
    assert not (drawn & (labels == 0)).any()
    # A 30px cell's interior is empty.
    assert base[5:25, 5:25].sum() == 0


def test_full_read_and_streaming_paths_agree(tmp_path, filled_mask):
    """The bulk-read path exists purely for speed on network-mounted sources, so
    it must be pixel-identical to streaming."""
    source, _ = filled_mask
    bulk = sp.pyramidize_segmentation_mask(
        source, tmp_path / "bulk.ome.tiff", tile_size=256, full_read=True
    )
    streamed = sp.pyramidize_segmentation_mask(
        source, tmp_path / "streamed.ome.tiff", tile_size=256, full_read=False
    )

    bulk_levels, _, _ = _read_as_the_tile_server_does(bulk)
    streamed_levels, _, _ = _read_as_the_tile_server_does(streamed)
    assert len(bulk_levels) == len(streamed_levels)
    for one, other in zip(bulk_levels, streamed_levels):
        assert np.array_equal(one, other)


def test_exact_finds_seams_between_touching_cells_that_fast_misses(tmp_path, filled_mask):
    source, _ = filled_mask
    exact = sp.pyramidize_segmentation_mask(
        source, tmp_path / "exact.ome.tiff", tile_size=256, method="exact"
    )
    fast = sp.pyramidize_segmentation_mask(
        source, tmp_path / "fast.ome.tiff", tile_size=256, method="fast"
    )

    exact_levels, _, _ = _read_as_the_tile_server_does(exact)
    fast_levels, _, _ = _read_as_the_tile_server_does(fast)
    # Every cell here abuts another, so "fast" only outlines the slide edge.
    assert (exact_levels[0] != 0).sum() > (fast_levels[0] != 0).sum()


def test_generated_masks_are_recognised_by_their_marker(tmp_path, filled_mask):
    source, _ = filled_mask
    out = sp.pyramidize_segmentation_mask(source, tmp_path / "out.ome.tiff", tile_size=256)

    assert sp.is_generated_outline_mask(out)
    assert not sp.is_generated_outline_mask(source)
    # Recognition is by OME marker, not filename, so a rename must not break it.
    renamed = tmp_path / "user-renamed-this.tiff"
    out_path = tmp_path / "out.ome.tiff"
    out_path.rename(renamed)
    assert sp.is_generated_outline_mask(renamed)
    assert str(out) == str(out_path)


def test_a_large_filled_mask_is_not_mistaken_for_outlines(tmp_path):
    """Regression: the old sniff strided the whole plane down to ~1536px, so on
    a mask this wide neighbouring samples were 16px apart -- wider than a cell,
    which made a solidly filled mask look boundary-only."""
    wide = tmp_path / "wide.tiff"
    tifffile.imwrite(wide, _touching_cells(256, 1536 * 16, cell=16, dtype=np.uint16))

    assert sp.looks_like_outline_mask(wide) is False


def test_the_sniff_reads_windows_not_whole_strips(tmp_path):
    """Regression: the sniff sampled through tifffile's Zarr adapter, which has
    to decode a whole strip to answer any read inside it. Masks are commonly
    written untiled as a *single* strip spanning the image, so each of the twelve
    windows pulled the entire file -- 59s on a real 12GB Orion mask, paid inside
    load_config() while a page load waited, and again at the head of every
    conversion job.

    Asserts the mechanism rather than a wall-clock threshold: with a memory map
    available, sampling must not touch anything like the whole plane.
    """
    single_strip = tmp_path / "single_strip.tiff"
    labels = _touching_cells(3000, 3000, cell=24, dtype=np.uint32)
    tifffile.imwrite(single_strip, labels, rowsperstrip=labels.shape[0])
    with tifffile.TiffFile(single_strip) as reader:
        page = reader.pages[0]
        assert not page.is_tiled and page.rowsperstrip >= labels.shape[0]

    assert sp._memmap_plane(single_strip, labels.shape) is not None

    reads = []
    real_asarray = np.asarray

    def counting_asarray(obj, *args, **kwargs):
        out = real_asarray(obj, *args, **kwargs)
        reads.append(getattr(out, "size", 0))
        return out

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sp.np, "asarray", counting_asarray)
        assert sp.looks_like_outline_mask(single_strip) is False

    # Twelve 512x512 windows, so a whole-plane read would be ~11x the total.
    assert max(reads) <= 512 * 512
    assert sum(reads) < labels.size


def test_a_generated_outline_mask_also_passes_the_content_sniff(tmp_path, filled_mask):
    """A user can hand us an outline export we did not generate, so the content
    heuristic has to agree with the marker on masks we *did* generate."""
    source, _ = filled_mask
    out = sp.pyramidize_segmentation_mask(source, tmp_path / "out.ome.tiff", tile_size=256)

    assert sp.looks_like_outline_mask(out) is True


def test_already_pyramidal_input_yields_the_same_outlines(tmp_path, filled_mask):
    """There is no separate pyramidise step any more: pyramidal and flat input
    both mean "read level 0, derive an outline pyramid"."""
    source, _ = filled_mask
    filled_pyramid = sp.pyramidize_segmentation_mask(
        source, tmp_path / "filled.ome.tiff", tile_size=256, outline=False
    )
    from_flat = sp.pyramidize_segmentation_mask(
        source, tmp_path / "from_flat.ome.tiff", tile_size=256
    )
    from_pyramid = sp.pyramidize_segmentation_mask(
        filled_pyramid, tmp_path / "from_pyramid.ome.tiff", tile_size=256
    )

    flat_levels, _, _ = _read_as_the_tile_server_does(from_flat)
    pyramid_levels, _, _ = _read_as_the_tile_server_does(from_pyramid)
    assert np.array_equal(flat_levels[0], pyramid_levels[0])


def test_filled_mode_writes_labels_the_shader_can_outline(tmp_path, filled_mask):
    """The viewer's on-the-fly mode needs the labels themselves, not their
    boundaries: the shader compares a pixel against its neighbours, which only
    works if cell interiors are still present."""
    source, labels = filled_mask
    filled = sp.pyramidize_segmentation_mask(
        source, tmp_path / "filled.ome.tiff", tile_size=256, outline=False
    )

    levels, is_multiscales, is_tiled = _read_as_the_tile_server_does(filled)
    assert is_multiscales and is_tiled and len(levels) > 1
    base = levels[0][: labels.shape[0], : labels.shape[1]]
    assert np.array_equal(base, labels)
    # Interiors intact is the whole point -- an outline pyramid has none.
    assert base[5:25, 5:25].sum() > 0


def test_the_two_generated_kinds_are_told_apart(tmp_path, filled_mask):
    """Serving a filled pyramid to a viewer that is not outlining paints solid
    blobs over the image, so the two kinds must never be confused -- including
    by the mapping check that decides whether to reuse a derived file."""
    source, _ = filled_mask
    outlines = sp.pyramidize_segmentation_mask(
        source, tmp_path / "o.ome.tiff", tile_size=256, outline=True
    )
    filled = sp.pyramidize_segmentation_mask(
        source, tmp_path / "f.ome.tiff", tile_size=256, outline=False
    )

    assert sp.generated_mask_kind(outlines) == sp.MODE_OUTLINES
    assert sp.generated_mask_kind(filled) == sp.MODE_FILLED
    assert sp.generated_mask_kind(source) is None
    assert sp.is_generated_outline_mask(outlines) is True
    assert sp.is_generated_outline_mask(filled) is False


def test_each_mode_gets_its_own_derived_filename(tmp_path, filled_mask):
    source, _ = filled_mask
    outlines = sp.derived_output_path(source, tmp_path / "d", mode=sp.MODE_OUTLINES)
    filled = sp.derived_output_path(source, tmp_path / "d", mode=sp.MODE_FILLED)

    assert outlines != filled
    assert outlines.name.endswith(sp.OUTLINE_SUFFIX)
    assert filled.name.endswith(sp.FILLED_SUFFIX)


def test_servable_label_pyramid_requires_tiles_and_levels(tmp_path, filled_mask):
    """A user-supplied filled mask is only servable as-is if the tile route can
    answer every zoom level from it: flat masks have no coarse levels (the
    viewer would draw full resolution at every level) and untiled ones make
    each tile request decode whole strips."""
    source, labels = filled_mask
    assert sp.is_servable_label_pyramid(source) is False

    pyramidal = sp.pyramidize_segmentation_mask(
        source, tmp_path / "pyramidal.ome.tiff", tile_size=256, outline=False
    )
    assert sp.is_servable_label_pyramid(pyramidal) is True

    tiled_but_flat = tmp_path / "flat.ome.tiff"
    tifffile.imwrite(tiled_but_flat, labels, tile=(256, 256))
    assert sp.is_servable_label_pyramid(tiled_but_flat) is False


def test_output_path_defaults_into_the_dataset_directory(tmp_path, filled_mask):
    source, _ = filled_mask
    destination = sp.outline_output_path(source, tmp_path / "dataset")

    assert destination.parent == tmp_path / "dataset"
    assert destination.name == "mask" + sp.OUTLINE_SUFFIX


# -- where a derived mask goes, and where one is found ---------------------
#
# The rule is "beside the source, falling back to the project's own derived
# root". Beside the source is what makes the file findable from the path alone,
# which is what lets a second project, a second user on the same mount, and a
# data node with no projects at all reuse one conversion. The fallback is what
# stops that from being a regression: pipeline output routinely lands in a
# directory that is read-only to the person opening it, and every mask
# generated before this convention is still in a project root.


def test_a_new_pyramid_is_written_beside_the_mask(tmp_path, filled_mask):
    source, _ = filled_mask
    project_root = tmp_path / "project"

    location = sp.resolve_derived_mask(source, project_root, mode=sp.MODE_FILLED)

    assert location.existing is None
    assert location.writable
    assert location.target.parent == source.parent
    assert location.target.name == "mask" + sp.FILLED_SUFFIX


def test_a_read_only_source_directory_falls_back_to_the_project(tmp_path,
                                                                filled_mask,
                                                                monkeypatch):
    """The case the old scheme sidestepped by never writing beside a source at
    all. It still has to work, so the project's own root stays the fallback."""
    from plexora import paths

    source, _ = filled_mask
    project_root = tmp_path / "project"
    monkeypatch.setattr(
        paths, "is_writable", lambda root: Path(root) != source.parent)

    location = sp.resolve_derived_mask(source, project_root, mode=sp.MODE_FILLED)

    assert location.writable
    assert location.target.parent == project_root


def test_nowhere_to_write_is_reported_rather_than_guessed(tmp_path, filled_mask,
                                                          monkeypatch):
    """A node has no project root to fall back to. Saying so beats starting a
    conversion that fails minutes later with an OSError."""
    from plexora import paths

    source, _ = filled_mask
    monkeypatch.setattr(paths, "is_writable", lambda root: False)

    assert sp.resolve_derived_mask(source, mode=sp.MODE_FILLED).writable is False


def test_a_pyramid_beside_the_mask_is_found_from_the_path_alone(tmp_path,
                                                               filled_mask):
    """The whole point: no project, no config, no recorded fingerprint -- just
    the mask's path, and the conversion somebody already paid for."""
    source, _ = filled_mask
    written = sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, mode=sp.MODE_FILLED),
        tile_size=256, outline=False,
    )

    location = sp.resolve_derived_mask(source, mode=sp.MODE_FILLED)
    assert location.existing == Path(written)


def test_a_legacy_pyramid_in_the_project_root_is_still_found(tmp_path,
                                                            filled_mask):
    """Every mask derived before this convention is in a project root. Finding
    it there is what keeps an existing project from rebuilding on next open."""
    source, _ = filled_mask
    project_root = tmp_path / "project"
    project_root.mkdir()
    written = sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, project_root, mode=sp.MODE_FILLED),
        tile_size=256, outline=False,
    )

    location = sp.resolve_derived_mask(source, project_root, mode=sp.MODE_FILLED)
    assert location.existing == Path(written)


def test_a_pyramid_older_than_its_mask_is_not_adopted(tmp_path, filled_mask):
    """Re-running a segmentation pipeline over the same path has to be
    noticed. Callers with a recorded fingerprint have a stronger check; this is
    the one available to callers that have none."""
    source, _ = filled_mask
    written = Path(sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, mode=sp.MODE_FILLED),
        tile_size=256, outline=False,
    ))
    assert sp.resolve_derived_mask(source, mode=sp.MODE_FILLED).existing == written

    stale = source.stat().st_mtime_ns - 10 ** 9
    os.utime(written, ns=(stale, stale))

    assert sp.resolve_derived_mask(source, mode=sp.MODE_FILLED).existing is None


def test_the_other_mode_is_not_mistaken_for_this_one(tmp_path, filled_mask):
    """Filled and outline pyramids sit beside each other under distinct names,
    and each mode finds only its own -- serving one as the other renders
    wrongly without failing."""
    source, _ = filled_mask
    sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, mode=sp.MODE_OUTLINES),
        tile_size=256, outline=True,
    )

    assert sp.resolve_derived_mask(source, mode=sp.MODE_OUTLINES).existing is not None
    assert sp.resolve_derived_mask(source, mode=sp.MODE_FILLED).existing is None


def test_the_output_preference_moves_new_pyramids_into_the_project(tmp_path,
                                                                   filled_mask,
                                                                   monkeypatch):
    """`plexora config set mask-output project` for a mask in a folder that
    should not accumulate large files -- one that is synced, or backed up."""
    from plexora import paths

    source, _ = filled_mask
    project_root = tmp_path / "project"
    monkeypatch.setenv(paths.ENV_MASK_OUTPUT, "project")

    location = sp.resolve_derived_mask(source, project_root, mode=sp.MODE_FILLED)

    assert location.target.parent == project_root
    assert location.target.name == "mask" + sp.FILLED_SUFFIX


def test_the_preference_never_narrows_the_search(tmp_path, filled_mask,
                                                 monkeypatch):
    """Changing the setting must not strand a pyramid that already exists, in
    either direction: both places are still looked in, so the answer is
    "adopt", never "rebuild somewhere else"."""
    from plexora import paths

    source, _ = filled_mask
    project_root = tmp_path / "project"
    project_root.mkdir()
    beside = Path(sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, mode=sp.MODE_FILLED),
        tile_size=256, outline=False,
    ))

    monkeypatch.setenv(paths.ENV_MASK_OUTPUT, "project")
    assert sp.resolve_derived_mask(
        source, project_root, mode=sp.MODE_FILLED).existing == beside


def test_the_preference_decides_which_of_two_wins(tmp_path, filled_mask,
                                                  monkeypatch):
    """With a pyramid in both places the setting has to pick one, and it has to
    pick the same one it would have written -- otherwise a build and the next
    load disagree about which file the project is using."""
    from plexora import paths

    source, _ = filled_mask
    project_root = tmp_path / "project"
    project_root.mkdir()
    beside = Path(sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, mode=sp.MODE_FILLED),
        tile_size=256, outline=False,
    ))
    inside = Path(sp.pyramidize_segmentation_mask(
        source, sp.derived_output_path(source, project_root, mode=sp.MODE_FILLED),
        tile_size=256, outline=False,
    ))

    monkeypatch.delenv(paths.ENV_MASK_OUTPUT, raising=False)
    assert sp.resolve_derived_mask(
        source, project_root, mode=sp.MODE_FILLED).existing == beside

    monkeypatch.setenv(paths.ENV_MASK_OUTPUT, "project")
    assert sp.resolve_derived_mask(
        source, project_root, mode=sp.MODE_FILLED).existing == inside


def test_an_unreadable_preference_falls_back_to_the_default(tmp_path,
                                                            filled_mask,
                                                            monkeypatch):
    """A hand-edited settings file should not stop Plexora from starting."""
    from plexora import paths

    source, _ = filled_mask
    monkeypatch.setenv(paths.ENV_MASK_OUTPUT, "somewhere-else")

    assert paths.mask_output_preference() == "beside"
    assert sp.resolve_derived_mask(
        source, tmp_path / "project", mode=sp.MODE_FILLED
    ).target.parent == source.parent


def test_invalid_input_is_rejected(tmp_path, filled_mask):
    source, labels = filled_mask
    floats = tmp_path / "floats.tiff"
    tifffile.imwrite(floats, labels.astype(np.float32))

    with pytest.raises(TypeError):
        sp.pyramidize_segmentation_mask(floats, tmp_path / "floats.ome.tiff")
    with pytest.raises(ValueError):
        sp.pyramidize_segmentation_mask(source, tmp_path / "bad.ome.tiff", tile_size=100)
    with pytest.raises(FileNotFoundError):
        sp.pyramidize_segmentation_mask(tmp_path / "missing.tiff", tmp_path / "x.ome.tiff")

    sp.pyramidize_segmentation_mask(source, tmp_path / "once.ome.tiff", tile_size=256)
    with pytest.raises(FileExistsError):
        sp.pyramidize_segmentation_mask(source, tmp_path / "once.ome.tiff", tile_size=256)


def test_progress_is_reported_to_completion(tmp_path, filled_mask):
    source, _ = filled_mask
    seen = []
    sp.pyramidize_segmentation_mask(
        source, tmp_path / "out.ome.tiff", tile_size=256,
        progress_callback=lambda done, total: seen.append((done, total)),
    )

    assert seen
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1][0] == seen[-1][1]


def test_a_failed_conversion_leaves_no_partial_output(tmp_path, filled_mask):
    """Output is written to a temp file and moved into place, so an interrupted
    conversion must not leave a half-written mask that later looks usable."""
    source, _ = filled_mask
    destination = tmp_path / "out.ome.tiff"

    def explode(done, total):
        raise RuntimeError("interrupted mid-conversion")

    with pytest.raises(RuntimeError):
        sp.pyramidize_segmentation_mask(
            source, destination, tile_size=256, progress_callback=explode
        )

    assert not destination.exists()
    assert [p.name for p in tmp_path.iterdir() if ".tmp.ome.tiff" in p.name] == []


def test_fingerprint_tracks_source_changes(tmp_path, filled_mask):
    source, labels = filled_mask
    original = sp.source_fingerprint(source)

    assert original is not None
    assert sp.source_fingerprint(source) == original
    assert sp.source_fingerprint(tmp_path / "missing.tiff") is None

    tifffile.imwrite(source, labels[:, :-40])
    assert sp.source_fingerprint(source) != original
