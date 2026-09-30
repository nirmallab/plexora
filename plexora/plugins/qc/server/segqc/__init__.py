"""Segmentation QC: where the mask splits one nucleus or merges several.

One framework (`analysis.py`): DNA intensity peaks, found once and
independently of the mask, checked against the mask's labels on the labels'
adjacency graph, judged against each cell's own neighbourhood. `run.py`
reads the image and mask tile by tile, caches the result by its inputs'
fingerprint and serves it to the panel and to agents
(`capabilities_checks.py`: run_segmentation_qc, get_segmentation_qc).
"""
