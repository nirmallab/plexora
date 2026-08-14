"""Gating module's own DB-backed persistence model.

Physically separate from the core `ChannelList` model in
`server/models/database_model.py`, but both share that module's generic
sqlite `get()`/`save_list()` engine and per-datasource-file storage -- this
class (and its table) only ever get created for a build that has the
gating module active.
"""


class GatingList:
    __tablename__ = 'gatinglist'
