"""Pixel-side evidence for automatic workflows, generic over modality.

`image_qc` and `calibration` read one overview level per channel and turn it
into numbers (and a stored display calibration both the headless renderer and
an open viewer use); `crops` reads many cells' crops at once, a pyramid tile
at a time; `collage` lays crops out under a fixed pixel budget; `density_plot`
draws a two-marker density. Nothing here imports `data_model` -- pixels come
through `source_image`, masks through the segmentation provider, exactly as
`plexora.agent.render` reads them.
"""
