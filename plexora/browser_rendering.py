"""The desktop app's "Browser rendering" setting (plan Phase 6, decision 3).

The website's browser node reads a project's image and mask in the page, in
workers, at about a third of the desktop's time per tile (P6: open 0.30x,
tile steps 0.35x, renderer memory 0.68 vs 1.3 GiB, no WebP encode). The
desktop app adopts that path inside its own window: the page reads the files
an opened project reads through the shell's `plexora-file` scheme
(desktop/src-tauri/src/localfiles.rs) instead of asking Python for every tile.

Everything the user can see of it waits for the website's GA, so the setting
is OFF unless the settings file says ``"browser_rendering": "auto"``
(Settings > Advanced on the desktop app). With it off nothing changes:
`/resource_routing` answers exactly what it always did, the shell grants no
file, and its scheme has nothing to serve.

- ``auto``: where the page reads a project's files exactly as Python does
  (`browser/node_spec.py`: a local pyramidal (OME-)TIFF with 8- or 16-bit
  samples, a TIFF label pyramid; checked level by level in the page before a
  single tile), the page answers those tiles; Python answers everything else.
- ``off``: Python answers every tile, as before.

Only the desktop app's server (``PLEXORA_DESKTOP``) ever offers it: in a
browser tab of a terminal `plexora` there is no shell to read the files.
"""

from __future__ import annotations

#: The key in the settings file (`paths.read_settings`).
SETTING = "browser_rendering"
AUTO = "auto"
OFF = "off"
MODES = (AUTO, OFF)
#: Off until the website's GA (plan decision 3).
DEFAULT = OFF

#: What the page takes over when the setting is on, in the plan's adoption
#: order (Phase 6). Steps not yet built stay with Python and say so.
ADOPTED = {
    "image_tiles": "browser",
    "label_tiles": "browser",
    "tables": "server",
    "contrast": "server",
}


def mode() -> str:
    """The stored mode, or the default when there is none (or it is not one
    of `MODES`)."""
    from plexora import paths

    value = paths.read_settings().get(SETTING)
    return value if value in MODES else DEFAULT


def set_mode(value: str) -> str:
    """Store `value` (one of `MODES`) and return it. Raises ValueError for
    anything else."""
    from plexora import paths

    if value not in MODES:
        raise ValueError(f"browser_rendering must be one of {', '.join(MODES)}")
    with paths.settings_lock():
        settings = paths.read_settings()
        settings[SETTING] = value
        paths.write_settings(settings)
    return value


def is_desktop(app) -> bool:
    return bool(app.config.get("PLEXORA_DESKTOP"))


def active(app) -> bool:
    """Whether this server offers the page its projects' files: the desktop
    app's server, with the setting on."""
    return is_desktop(app) and mode() == AUTO


def paths_in_use(app) -> dict:
    """Which path each kind of data takes (`/desktop/info`), or {} when the
    setting is off: Python answers everything, as it always has."""
    return dict(ADOPTED) if active(app) else {}


def local_route(app, datasource: str) -> dict | None:
    """What `/resource_routing` adds for the desktop page when the setting is
    on: the image and the mask as the page may read them (`node_spec.describe`
    with ``local=True``), with ``setting``. None when it is off or this is not
    the desktop app's server."""
    if not datasource or not active(app):
        return None
    from plexora.browser import node_spec

    try:
        spec = node_spec.describe(datasource, local=True)
    except Exception as exc:  # noqa: BLE001 -- a project that does not open says why; Python serves it
        spec = {"served": False, "reason": f"the project could not be described ({exc})",
                "labels": {"served": False, "reason": "the project could not be described"}}
    return {"setting": AUTO, **spec}
