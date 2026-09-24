"""Server half of the visium_hd plugin.

`routes` is the Flask surface; `tenx` reads a Space Ranger bin matrix into the
blocks core's bin store takes. The tiling, pooling and colouring are core's
(`plexora/server/models/bin_tiles.py`), because a counted grid is a rendering
primitive -- what is here is which file it came from and how its barcodes name
squares.
"""
