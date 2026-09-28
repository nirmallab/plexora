"""What an external agent may ask the QC plugin to do.

Two halves with one store. The session tools (`capabilities_session.py`) are
Paid (`ai:qc:session`); everything that reads, changes or exports what a
session (or a user drawing QC regions by hand) produced is Free, and stays
usable after a licence lapses.
"""

from __future__ import annotations


def capabilities():
    from plexora.plugins.qc import capabilities_session

    return list(capabilities_session.capabilities())
