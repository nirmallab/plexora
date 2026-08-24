"""`python -m plexora` -- the same command as `plexora`, for anyone whose
PATH does not have the console script on it (a common state on Windows, and
inside conda environments that were activated after the shell started).

A pass-through. It carries no special handling of its own: the one thing `-m`
makes awkward -- that the package is imported before any argument is read, so
`--plugins` would arrive after Blueprint registration -- is equally true of the
console script, so the fix lives in `cli.main` where both reach it. See
`plexora.cli.maybe_reexec_for_plugins`.
"""

from plexora.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
