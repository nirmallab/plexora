"""Moved to gating core: `plexora/plugins/gating/server/provenance.py`.

Where a gate came from is part of gating itself -- the sidebar's locked and
approved gates, the manual routes and the Free capabilities all read it -- so
it cannot live in the automatic-gating (Paid) package that may one day ship
separately. This name stays importable, as the SAME module object (not a
copy), so every existing `from ...autogate import provenance` and anything
that patches it keeps working unchanged.
"""

import sys

from plexora.plugins.gating.server import provenance as _core

sys.modules[__name__] = _core
