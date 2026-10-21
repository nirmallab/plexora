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
    session; every worker reads gate-packets. Both point at the session's
    reading guide instead of restating it, and leave what is needed only
    sometimes to a gating/... reference (the live run's gate-image was 31k
    characters, re-read on every call; 12.6k and 6.5k before the seven gating
    skills became three)."""
    assert len(skills.read_skill("gate-image")) <= 4_000
    assert len(skills.read_skill("gate-packets")) <= 2_000
    assert len(skills.read_skill("gate-review")) <= 9_800
    gating = {"gate-image", "gate-packets", "gate-review"}
    listed = {s["name"] for s in skills.list_skills()}
    assert gating <= listed and not listed & set(skills.aliases())


def test_old_names_and_references_are_read():
    """An old skill name reads what replaced it, so a brief, a prompt or an
    installed agent file that names it keeps working; `list_skills` notes it
    under the skill that now holds it, beside that skill's references."""
    old = {"visual-gating": "gating/visual-gating", "gate-dataset": "gating/dataset",
           "review-gating": "gate-review", "diagnose-marker": "gate-review"}
    for name, target in old.items():
        assert skills.resolve(name) == target
        assert skills.read_skill(name) == skills.read_skill(target)
    rows = {s["name"]: s for s in skills.list_skills()}
    assert {"review-gating", "diagnose-marker"} <= set(rows["gate-review"]["aliases"])
    assert rows["gate-image"]["aliases"]["gate-dataset"] == "gating/dataset"
    refs = {r["name"] for r in rows["gate-image"]["references"]}
    assert {"gating/setup", "gating/dataset", "gating/qc-cells"} <= refs
    for ref in refs:
        assert "{{" not in skills.read_skill(ref)


@pytest.mark.parametrize("name", ["../gate-image/SKILL", "gating/../gate-image/SKILL",
                                  "gating/nope", "/etc/passwd", "gating", "gate-image/SKILL",
                                  "gating\\setup", "GATING/SETUP"])
def test_a_reference_name_never_becomes_a_path(name):
    with pytest.raises(KeyError):
        skills.read_skill(name)


def test_the_tools_section_is_the_manifests():
    """`## Tools` is written from the manifest when a skill is read, so the two
    cannot drift; a hand-written list, or a note naming a tool the manifest
    leaves out, is a validation problem."""
    import re

    for entry in skills.manifest()["skills"]:
        text = skills.read_skill(entry["name"])
        body = re.search(r"^## Tools\n\n(.*?)\n\n", text, re.S | re.M).group(1)
        assert body == skills.tools_line(entry), entry["name"]
    entry = {"name": "x", "tools": ["get_gate"]}
    tools = {"get_gate", "set_gate"}
    hand = "## Tools\n\n`get_gate`.\n\n## Evidence\n"
    assert any("must open with" in p for p in skills._tools_section("x", entry, hand, tools))
    note = "## Tools\n\n{{tools}}\n\n`set_gate` too.\n\n## Evidence\n"
    assert any("set_gate" in p for p in skills._tools_section("x", entry, note, tools))
    assert skills._tools_section("x", entry, "## Tools\n\n{{tools}}\n", tools) == []


def test_a_skill_never_restates_the_reading_guide(monkeypatch):
    """The session sends its reading guide itself: a guide clause repeated word
    for word in a skill is a second copy that drifts."""
    clauses = skills.guide_clauses()
    assert clauses and skills.guide_repeats(clauses) == []
    sentence = max(sorted(clauses), key=len)
    monkeypatch.setattr(skills, "raw_skill", lambda name: "# X\n\nJudge it: " + sentence + ".\n")
    problems = skills.guide_repeats(clauses)
    assert problems and all("restates the gating reading guide" in p for p in problems)
    monkeypatch.setattr(skills, "raw_skill",
                        lambda name: "# X\n\nRead the session's reading guide.\n")
    assert skills.guide_repeats(clauses) == []


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
    # The bulk pass is waited on through its job, not by polling the next packet.
    assert "`job_wait`" in image and "`job_id`" in image
    checks = skills.read_skill("qc-checks")
    assert "qc_next" not in checks and "qc_session_start" not in checks
    assert f"{schemas.ENGINE['adjust_max_steps']} steps either way" in checks
    assert "sample_qc_examples" in skills.read_skill("review-qc")
