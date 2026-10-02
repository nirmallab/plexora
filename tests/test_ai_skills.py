"""The scientific skills: parse, carry every section, and name only what the code has.

A skill tells an agent what to type and what to look for. Every name it puts
in backticks must be one the code still uses, and it restates no number the
server decides with -- those are `{{placeholders}}`, filled in from the code
when the skill is read -- so a renamed tool, state or field, or a recalibrated
cut-point, fails here instead of misleading an agent.
"""

import pytest

from plexora.agent import registry
from plexora.ai import skills


def test_the_manifest_and_the_skill_folders_agree():
    listed = [s["name"] for s in skills.list_skills()]
    on_disk = sorted(p.parent.name for p in skills.SKILLS_DIR.glob("*/SKILL.md"))
    assert sorted(listed) == on_disk and len(set(listed)) == len(listed)
    assert all(s["available"] for s in skills.list_skills())


def test_every_skill_is_valid_against_the_live_registry():
    registry.discover()
    tools = {cap.tool_name for cap in registry.all_capabilities()}
    assert skills.validate(tools) == []


def test_skills_name_only_what_the_code_uses_and_restate_no_numbers():
    assert skills.lint() == []


def test_the_lint_catches_a_stale_name_a_number_and_a_bad_placeholder(monkeypatch):
    text = ("# Gate\n\n## When to use\n\nCall `gating_nextt` and `gating_next` every 15 "
            "markers; `confidence: 0.95` is fine in code, and so is\n\n```\nlimit 12\n```\n"
            "{{ENGINE.strip_batch}} renders, {{ENGINE.no_such}} does not.\n")
    monkeypatch.setattr(skills, "raw_skill", lambda name: text)
    problems = skills.lint()
    assert any("gating_nextt" in p and "'gating_next'" not in p.split(":", 1)[1]
               for p in problems)
    assert any("a number in prose" in p and "15" in p for p in problems)
    assert any("ENGINE.no_such" in p for p in problems)
    assert not any("0.95" in p or "limit 12" in p for p in problems)


def test_placeholders_are_filled_from_the_code():
    from plexora.plugins.gating.server.autogate.schemas import ENGINE

    text = skills.read_skill("gate-image")
    assert "{{" not in text
    assert f"every {ENGINE['markers_per_worker']} markers" in text
    assert skills.render("{{strip.cells_each_side}}") == str(ENGINE["strip_cells"] // 2)
    with pytest.raises(KeyError):
        skills.render("{{ENGINE.nope}}")


def test_the_gating_skills_stay_short():
    """The coordinator reads gate-image once and keeps it for the whole
    session; every worker reads gate-packets. Both say how to judge and point
    at the session's reading guide instead of restating it (the live run's
    gate-image was 31k characters, re-read on every call)."""
    assert len(skills.read_skill("gate-image")) <= 12_500
    assert len(skills.read_skill("gate-packets")) <= 6_500


def test_every_skill_names_its_model_tier():
    from plexora.ai import delegation

    listed = skills.list_skills()
    assert all(s["tier"] in delegation.TIERS for s in listed)
    for role, spec in delegation.ROLES.items():
        assert spec["skill"] in {s["name"] for s in listed}, role


def test_installed_skill_copies_are_rendered(tmp_path):
    from plexora.ai import setup

    written = setup._install_skills(tmp_path, dry_run=False)
    assert written
    for path in written:
        assert "{{" not in open(path, encoding="utf-8").read()


def test_read_skill():
    assert skills.read_skill("visual-gating").startswith("# Visual gating")
    with pytest.raises(KeyError):
        skills.read_skill("nope")


def test_the_qc_skills_teach_the_score_review_and_the_steps():
    from plexora.ai import skills
    from plexora.plugins.qc.server import schemas

    image = skills.read_skill("qc-image")
    assert "score_review" in image and "whole_tissue" in image
    assert str(schemas.ENGINE["score_rounds"]) in image
    checks = skills.read_skill("qc-checks")
    assert "qc_next" not in checks and "qc_session_start" not in checks
    assert f"{schemas.ENGINE['adjust_max_steps']} steps either way" in checks
    assert "sample_qc_examples" in skills.read_skill("review-qc")
