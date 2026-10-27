"""Judgments reused on identical evidence: the same packet gets the same answer.

Gating's binding of the shared memo (`plexora.agent.sessions.memo`): answers
kept per project and per `SessionOptions.agent`, the key covering the
versions of everything that drew a packet's numbers and pictures -- the
profile, the lattice, the scoring, the display calibration and the collage
renderer. A changed partner gate, a new version of any of them or a
different expression matrix changes the evidence, hence the key, and the
question is asked afresh; an unchanged packet is answered from memory, so a
rerun reaches the same gates with no model call.
"""

from __future__ import annotations

from plexora.agent.sessions import memo as _memo

KIND = "gating"
VERSION = _memo.VERSION
VOLATILE = _memo.VOLATILE


def versions() -> tuple:
    from plexora.agent.evidence import calibration, collage, display_grid
    from plexora.plugins.gating.server.autogate import absence, lattice, schemas, scoring

    return (schemas.PROFILE_VERSION, lattice.VERSION, scoring.VERSION, calibration.VERSION,
            f"scale:{calibration.SCALE}", collage.RENDERER, display_grid.RENDERER,
            display_grid.SPREAD_RENDERER, absence.RENDERER)


def key(packet, images, *, versions=()) -> str:
    return _memo.key(packet, images, versions=(*globals()["versions"](), *versions))


def path(project):
    return _memo.path(KIND, project)


def get(project, packet_key, agent):
    return _memo.get(KIND, project, packet_key, agent)


def put(project, packet_key, agent, answer, **about) -> None:
    _memo.put(KIND, project, packet_key, agent, answer, **about)


def forget(project, *, agent=None) -> int:
    return _memo.forget(KIND, project, agent=agent)
