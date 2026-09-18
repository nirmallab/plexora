"""Server half of the transcripts plugin.

`routes` is the Flask surface; `xenium` reads a vendor's transcript table and is
the ONLY thing in Plexora that imports pyarrow. The tiling itself is core's
(`plexora/server/models/transcript_tiles.py`), because points and density are
rendering primitives -- what is here is the interpretation: which genes exist,
which file format this is, and how to turn microns into reference pixels.
"""
