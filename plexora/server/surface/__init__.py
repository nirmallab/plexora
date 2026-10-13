"""Surfaces: triangle meshes extracted from a volume, kept as layers.

A surface is a representation of the 3-D data, never the data itself: an
isosurface is the boundary at one chosen intensity, a label surface is the
outside of one segmented object, and an imported mesh is whatever its file
says. Every surface records how it was made (`manifest.json`), and nothing
here ever writes back into an image or a mask.

    mesh.py        the Mesh, its file format, welding, normals, smoothing, measurements
    marching.py    marching cubes over z-strips, welded across the seams
    isosurface.py  intensity isosurfaces (preprocessing, min size, components)
    labels.py      one closed surface per object of a 3-D label volume
    levels.py      which level to extract at, and how many triangles that makes
    suggest.py     threshold suggestions and the histogram they are drawn on
    preview.py     the quick, cached extraction behind the threshold slider
    store.py       the surface layer: files, builds, cancel, forget
    refine.py      non-destructive mesh edits (each one a new version)
    export.py      PLY / OBJ / STL / GLB writers and the provenance sidecar
    importers.py   the same four formats read back
    contacts.py    neighbours and contact areas between objects

Licensed: making and analysing surfaces needs the `volume:surfaces` entitlement
(plexora/licensing/manifest.py); reading, drawing, exporting and deleting a
surface that exists never does.
"""

ENTITLEMENT = "volume:surfaces"
