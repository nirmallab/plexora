"""What QC adds to Plexora's MCP server (`Plugin.mcp_factory`).

Resources under `plexora://qc/` -- each a capability call underneath, so a
resource and the tool that report the same thing never disagree -- and the
builders of the three prompts the skill manifest names (`qc_image`,
`review_qc`, `qc_checks`).
"""

from __future__ import annotations


def register_resources(server, runtime):
    from plexora.mcp import serialize
    from plexora.mcp.resources import JSON, _answer

    def resource(uri, name, description):
        def decorate(fn):
            server.resource(uri, name=name, description=description, mime_type=JSON)(fn)
            return fn
        return decorate

    @resource("plexora://qc/sessions", "qc-sessions", "Recent QC sessions and their states.")
    def sessions() -> str:
        return _answer(runtime, "qc.session_status", {})

    @resource("plexora://qc/session/{session_id}", "qc-session",
              "One QC session: every channel, candidate region and check, with its "
              "state; what it spent; its limit questions.")
    def session(session_id: str) -> str:
        return _answer(runtime, "qc.session_status", {"session_id": session_id})

    @resource("plexora://qc/session/{session_id}/packet", "qc-packet",
              "The QC session's outstanding decision packet, without its images.")
    def packet(session_id: str) -> str:
        from plexora.plugins.qc.server import engine

        try:
            # Reads the session store directly rather than through a
            # capability, so it carries the session tools' licence check.
            from plexora.licensing import guards

            guards.check("ai:qc:session", what="qc-packet")
            guards.check(guards.MCP, what="qc-packet")  # read only over MCP
            record = engine.store().load(session_id)
            outstanding = record.get("outstanding_packet")
            if not outstanding:
                return serialize.bound({"outstanding": None, "state": record.get("state")})
            found, _images = engine.store().read_packet(session_id, outstanding)
            return serialize.bound(found)
        except Exception as exc:
            from plexora.agent.errors import as_agent_error

            return serialize.bound({"error": as_agent_error(exc).to_problem()})

    @resource("plexora://project/{name}/qc", "project-qc",
              "A project's QC: channels, regions, cells, strictness, provenance.")
    def project_qc(name: str) -> str:
        return _answer(runtime, "qc.get_results", {"project": name})


def _skill(name):
    from plexora.ai import skills

    return skills.read_skill(name)


def _qc_image(skill):
    def qc_image(project: str, mode: str = "apply", strictness: str = "standard") -> str:
        if mode not in ("apply", "propose"):
            raise ValueError("mode is one of apply, propose")
        if strictness not in ("lenient", "standard", "strict"):
            raise ValueError("strictness is one of lenient, standard, strict")
        return (f"Quality-control the image `{project}` in `{mode}` mode at `{strictness}` "
                "strictness with `qc_session_start`, following this skill exactly.\n\n"
                + _skill(skill))
    return qc_image


def _review_qc(skill):
    def review_qc(project: str) -> str:
        return (f"Review the quality control of `{project}` with me, following this "
                "skill.\n\n" + _skill(skill))
    return review_qc


def _qc_checks(skill):
    def qc_checks(project: str, checks: str = "blur,registration,segmentation") -> str:
        wanted = [c.strip() for c in str(checks).split(",") if c.strip()]
        unknown = [c for c in wanted
                   if c not in ("blur", "registration", "segmentation", "artifacts")]
        if unknown or not wanted:
            raise ValueError("checks is a comma list of blur, registration, segmentation, "
                             "artifacts")
        return (f"Run the {', '.join(wanted)} check(s) on `{project}`, look at the places "
                "each flags, and write only what is an artifact, following this skill."
                "\n\n" + _skill(skill))
    return qc_checks


def contributions() -> dict:
    return {"resources": register_resources,
            "prompts": {"qc-image": _qc_image, "review-qc": _review_qc,
                        "qc-checks": _qc_checks}}
