"""Packaging Plexora for external agents.

- `skills.py`, `skill_manifest.yaml`, `skills/<name>/SKILL.md`: the runtime
  scientific skills -- how to combine Plexora's capabilities to answer a
  question well (triage a dataset, inspect an image, QC a marker, validate a
  gate visually). Served by the MCP server's `list_skills`/`read_skill` and
  installable into an agent's own skills directory.
- `setup.py`: `plexora ai init` and `plexora ai setup claude|codex|cursor`,
  which register `plexora mcp serve` with those clients.
"""
