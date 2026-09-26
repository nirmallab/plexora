"""What one pixel of a project's reference image is worth, without loading it.

The viewer learns this from `/get_ome_metadata`, which goes through
`data_model` and therefore loads the project it asks about -- the right trade
for the page that is showing it, and exactly the wrong one for anything else.
An agent sizing a 150 µm field, or drawing a scale bar on a rendered region,
must not swap out the project on the user's screen to find out.

So this reads the same facts from where they live: the calibration somebody
typed (the project record), else what the file states (read straight off the
file, or asked of the node that holds it). The order and the rule are
`data_model._with_pixel_size`'s: manual beats metadata, and a missing
calibration is `None` -- never a conventional default, because a scale bar that
is wrong looks exactly like one that is right.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Units a stated physical size may arrive in, as microns per unit.
_TO_MICRONS = {
    "µm": 1.0, "um": 1.0, "micron": 1.0, "microns": 1.0, "micrometer": 1.0,
    "micrometre": 1.0, "μm": 1.0,
    "nm": 1e-3, "nanometer": 1e-3, "nanometre": 1e-3,
    "mm": 1e3, "millimeter": 1e3, "millimetre": 1e3,
    "cm": 1e4, "m": 1e6, "meter": 1e6, "metre": 1e6,
    "å": 1e-4, "angstrom": 1e-4,
}


def to_microns(value, unit) -> float | None:
    """`value` in `unit`, as microns; None when either is unusable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not value > 0:
        return None
    unit = getattr(unit, "value", unit)
    key = str(unit or "µm").strip().lower()
    factor = _TO_MICRONS.get(key)
    return value * factor if factor is not None else None


def _plain(raw) -> dict:
    if raw is None:
        return {}
    if hasattr(raw, "model_dump"):
        try:
            return raw.model_dump(mode="json")
        except Exception:
            return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _from_metadata(raw) -> dict | None:
    plain = _plain(raw)
    value = to_microns(plain.get("physical_size_x"), plain.get("physical_size_x_unit"))
    if value is None:
        return None
    return {"value": value, "unit": "µm", "source": "metadata",
            "raw": {"value": plain.get("physical_size_x"),
                    "unit": str(getattr(plain.get("physical_size_x_unit"), "value",
                                        plain.get("physical_size_x_unit")) or "µm")}}


def _local_metadata(path: str):
    """The file's own statement of scale, by the same layout dispatch
    `LocalImageProvider.open` makes -- but reading only the metadata."""
    from plexora.server.utils import brightfield, dicom_wsi, ome_zarr, xenium_focus

    if xenium_focus.is_focus_dir(path):
        return xenium_focus.physical_metadata(path)
    if ome_zarr.is_zarr_image_path(path):
        return ome_zarr.physical_metadata(ome_zarr.open_image(path))
    if dicom_wsi.is_dicom_path(path):
        return dicom_wsi.physical_metadata(dicom_wsi.open_image(path))
    if brightfield.is_rgb_layout(path):
        return brightfield.physical_metadata(path)

    import tifffile as tf
    from ome_types import from_xml

    with tf.TiffFile(path, is_ome=False) as handle:
        xml = handle.pages[0].tags["ImageDescription"].value
    return from_xml(xml).images[0].pixels


def pixel_size(project) -> dict | None:
    """`{value, unit: "µm", source: "manual"|"metadata", raw}` or None.

    `project` is a `Project`. Never loads a datasource and never guesses.
    """
    from plexora.server.models.project import normalize_pixel_size

    manual = normalize_pixel_size(project.image.pixel_size)
    if manual is not None:
        value = to_microns(manual["value"], manual["unit"])
        if value is not None:
            return {"value": value, "unit": "µm", "source": "manual",
                    "raw": {"value": manual["value"], "unit": manual["unit"]}}

    if project.image.is_blank:
        return None

    binding = project.resources.get("image")
    try:
        if binding is not None:
            from plexora.server.providers.node import NodeImageProvider

            return _from_metadata(NodeImageProvider(binding).ome_metadata())
        if not project.image.src:
            return None
        return _from_metadata(_local_metadata(str(project.image.src)))
    except Exception:
        # An unreadable header, a node that is asleep: no calibration is the
        # honest answer, and the caller already knows what to do with it.
        return None


def describe(size: Mapping[str, Any] | None) -> str:
    """A one-line statement of the calibration, for a manifest or a reply."""
    if not size:
        return "uncalibrated (no physical pixel size is known)"
    return f"{size['value']:.4g} µm/px ({size['source']})"
