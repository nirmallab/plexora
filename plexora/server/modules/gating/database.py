"""Where gating's saved gates used to live.

Persistence goes through `plexora.api.store`, which namespaces every table as
`plugin_gating_<name>`. This name is all that remains of the older scheme, in
which gating wrote a bare `gatinglist` table straight into the shared
per-datasource sqlite file.

It is declared so `PluginStore` can adopt gates saved by those builds: the row
is copied into the namespaced table on first read and this table is then
dropped. Once every project a user opens has been read once, nothing refers to
it and the constant can go.
"""

#: Pre-namespacing table name, retained only so existing projects can be
#: converted. Nothing writes here; the store deletes it after adopting it.
LEGACY_STATE_TABLE = "gatinglist"
