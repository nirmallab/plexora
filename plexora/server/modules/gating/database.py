"""Where gating's saved gates used to live.

Persistence now goes through `plexora.api.store`, which namespaces every table
as `plugin_gating_<name>` so a plugin's data can be identified and removed.
This table predates that: builds up to and including the one before the plugin
API wrote a bare `gatinglist` table straight into the shared per-datasource
sqlite file.

The name is kept so `PluginStore(legacy_state_table=...)` can still read gates
saved by those builds. Nothing writes here any more, and the table is
deliberately left in place rather than migrated-and-dropped -- a user who rolls
the host back should still find their gates.
"""

#: Pre-namespacing table name. Read-only compatibility shim; do not write to it.
LEGACY_STATE_TABLE = "gatinglist"


class GatingList:
    """Marker class for the legacy table, for `database_model.get()`.

    Retained only for tests and for anything still reading the old table
    directly. New code should use `plexora.api.store(...)`.
    """

    __tablename__ = LEGACY_STATE_TABLE
