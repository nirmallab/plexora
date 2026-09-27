"""Automatic gating: the deterministic evidence and the decision session.

Gating owns gate semantics, so the intensity-marker implementation lives here;
the pixel-side evidence it draws on (display calibration, batched cell crops,
collages) is generic and lives in `plexora.agent.evidence`, and the session
framework in `plexora.agent.sessions`.

Placement rule: anything that reads a whole marker column is a table operation
in `tableops.py` (JSON in, JSON out) so it runs where the table is, a data node
included. Nothing here imports `data_model`.
"""
