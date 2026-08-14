"""Gating feature module: marker-threshold/GMM gating routes, DB-backed
gate persistence, and the AnnData gates-table adapter. See SKILL.md's
"Multi-Modal Datasource Support" section for the broader gating design
this module implements against.
"""

from plexora.server.modules.gating.routes import gating_bp


def register(app):
    app.register_blueprint(gating_bp)
