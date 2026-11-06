"""The core's routes. Each module registers its own on `plexora.app` at import.

The app is built on first use (`plexora.create_app`, reached through the
package's `__getattr__`), and building it is what imports these modules. One
imported directly before that -- a test that starts at `data_routes`, say --
would otherwise build the app from inside its own `from plexora import app`
line, half-imported, and register its routes only after the build had finished
with it. So the package builds the app first; inside a build this is a lookup.
"""

import plexora

plexora.app  # noqa: B018 -- built here, or already being built
