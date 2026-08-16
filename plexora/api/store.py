"""Per-datasource, per-plugin persistence.

Plugins are expected to write results back -- derived measurements,
annotations, classifications, and their own UI state. They do that here rather
than by reaching into `database_model`, for two reasons.

**Namespacing.** Every table this store creates is prefixed
`plugin_<plugin>_<name>`, so two plugins can both persist "results" without
colliding, and `drop_all()` can remove exactly one plugin's data on uninstall.
Before this existed, the gating module wrote a bare `gatinglist` table straight
into the shared per-datasource file, which then outlived the module: switch to
a build without gating and the table stayed behind with nothing able to name
it, let alone remove it.

A plugin may declare the table it used before namespacing. State there is
adopted on first read and the old table is then dropped, so an existing project
ends up fully converted rather than carrying a dead table forever.

**Injection.** `database_model` interpolates the table name directly into SQL,
which was safe while every table name was a literal in first-party source. With
third-party plugins the name derives from a package the host did not write, so
identifiers are validated here rather than trusted.

Storage reuses `database_model`'s engine unchanged -- one sqlite file per
datasource, one row per (table, datasource), lazily created. Tabular values are
serialized as Parquet so dtypes survive the round trip; opaque state is stored
as raw bytes exactly as handed over.
"""

from __future__ import annotations

import io
import re
import sqlite3

import polars as pl

from plexora.server.models import database_model

#: Table and plugin names become SQL identifiers, so they are restricted rather
#: than escaped: lowercase, digits and underscore, starting with a letter.
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

_PREFIX = "plugin_"

#: Name of the per-plugin table holding opaque plugin state, as opposed to a
#: named result table.
_STATE = "state"


def _validate(kind, value):
    if not isinstance(value, str) or not _SAFE_NAME.match(value):
        raise ValueError(
            f"invalid {kind} name {value!r}: expected lowercase letters, digits "
            "and underscores, starting with a letter"
        )
    return value


def _model_for(table):
    """database_model keys everything off a class attribute, so give it one."""
    return type("PluginTable", (), {"__tablename__": table})


class PluginStore:
    """One plugin's storage for one datasource."""

    def __init__(self, datasource: str, plugin: str, legacy_state_table: str | None = None):
        self._datasource = datasource
        self._plugin = _validate("plugin", plugin)
        #: Pre-namespacing table this plugin's state used to live in. Read once
        #: as a fallback so upgrading the host does not lose saved work.
        self._legacy_state_table = legacy_state_table

    # -- naming ---------------------------------------------------------

    @property
    def prefix(self) -> str:
        return f"{_PREFIX}{self._plugin}_"

    def table_name(self, name: str) -> str:
        return f"{self.prefix}{_validate('table', name)}"

    # -- opaque state ---------------------------------------------------

    def get_state(self) -> bytes | None:
        """This plugin's saved state, or None if it has never saved any.

        Adopts pre-namespacing state on first read -- see `_adopt_legacy_state`.
        """
        row = database_model.get(_model_for(self.table_name(_STATE)), datasource=self._datasource)
        if row is not None:
            return row.cells
        if self._legacy_state_table is None:
            return None
        return self._adopt_legacy_state()

    def _adopt_legacy_state(self) -> bytes | None:
        """Move state out of a plugin's pre-namespacing table, once.

        Copies the row into the namespaced table and drops the old one, so the
        upgrade is complete rather than permanent: no project keeps a table
        that nothing can name. Idempotent -- once the old table is gone this
        does nothing, and the namespaced read above short-circuits first.

        Existence is checked directly rather than via `database_model.get`,
        which creates the table it queries. Going through it would recreate the
        very table being retired on every fresh project.
        """
        if not self._has_table(self._legacy_state_table):
            return None
        legacy = database_model.get(
            _model_for(self._legacy_state_table), datasource=self._datasource
        )
        cells = legacy.cells if legacy is not None else None
        if cells is not None:
            self.put_state(cells)
        self._drop_tables([self._legacy_state_table])
        return cells

    def put_state(self, blob: bytes) -> None:
        if not isinstance(blob, (bytes, bytearray)):
            raise TypeError(f"state must be bytes, got {type(blob).__name__}")
        database_model.save_list(
            _model_for(self.table_name(_STATE)), datasource=self._datasource, cells=bytes(blob)
        )

    # -- result tables --------------------------------------------------

    def put_table(self, name: str, frame: pl.DataFrame) -> None:
        """Store a result table -- measurements, annotations, classifications.

        Parquet rather than CSV or pickle: dtypes survive, it is compact, and
        it is readable by anything, which matters for data a user may want to
        get back out without running Plexora.
        """
        if not isinstance(frame, pl.DataFrame):
            raise TypeError(f"expected a polars DataFrame, got {type(frame).__name__}")
        buffer = io.BytesIO()
        frame.write_parquet(buffer)
        database_model.save_list(
            _model_for(self.table_name(name)), datasource=self._datasource, cells=buffer.getvalue()
        )

    def get_table(self, name: str) -> pl.DataFrame | None:
        row = database_model.get(_model_for(self.table_name(name)), datasource=self._datasource)
        if row is None:
            return None
        return pl.read_parquet(io.BytesIO(row.cells))

    # -- file storage ---------------------------------------------------

    def directory(self):
        """A directory this plugin owns for this datasource, created on demand.

        For inputs a plugin collects itself -- an uploaded CSV, a cached model,
        anything that is a file rather than a table. Scoped per plugin so an
        uninstall knows what to remove and two plugins cannot overwrite each
        other's uploads, which writing directly into the datasource directory
        allowed.
        """
        path = (
            database_model._db_path_for_datasource(self._datasource).parent
            / "plugins"
            / self._plugin
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- introspection and uninstall ------------------------------------

    def _existing_tables(self, like: str) -> list[str]:
        db_file = database_model._db_path_for_datasource(self._datasource)
        if not db_file.exists():
            return []
        conn = sqlite3.connect(str(db_file), timeout=10)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?", (like,)
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()
        return sorted(name for (name,) in rows)

    def _has_table(self, name: str) -> bool:
        return name in self._existing_tables(name)

    def _drop_tables(self, names) -> None:
        db_file = database_model._db_path_for_datasource(self._datasource)
        if not db_file.exists():
            return
        conn = sqlite3.connect(str(db_file), timeout=10)
        try:
            for name in names:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            conn.commit()
        finally:
            conn.close()

    def list_tables(self) -> list[str]:
        """Names this plugin has stored, without the namespace prefix."""
        return [name[len(self.prefix):] for name in self._existing_tables(self.prefix + "%")]

    def drop_all(self) -> None:
        """Remove every table this plugin owns for this datasource.

        The uninstall path that did not exist before namespacing. Includes the
        plugin's pre-namespacing table when it declared one: that table is this
        plugin's too, so leaving it behind would defeat the point.
        """
        names = [self.table_name(name) for name in self.list_tables()]
        if self._legacy_state_table and self._has_table(self._legacy_state_table):
            names.append(self._legacy_state_table)
        self._drop_tables(names)


def store(datasource: str, plugin: str, legacy_state_table: str | None = None) -> PluginStore:
    """Storage handle for one plugin against one datasource."""
    return PluginStore(datasource, plugin, legacy_state_table=legacy_state_table)
