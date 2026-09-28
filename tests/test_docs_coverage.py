"""Every public name is documented, excluded with a reason, or an alias.

The model is built in this process from the defining modules (never through
`plexora.<name>`, which would cache the telemetry wrapper); conftest's
autouse data-root isolation covers the `import plexora` it needs.
"""

import pytest

from tools.docs import cli_api, manifest
from tools.docs.python_api import build_model


@pytest.fixture(scope="module")
def symbols():
    return build_model()["symbols"]


def test_every_public_name_is_classified(symbols):
    problems = manifest.classify(manifest.load(), symbols)
    assert not problems, "tools/docs/manifest.yaml is out of step with the code:\n  " + "\n  ".join(problems)


def test_every_cli_command_is_classified():
    cli = cli_api.load_cli()
    problems = manifest.cli_problems(manifest.load(), cli.SUBCOMMANDS)
    assert not problems, "\n".join(problems)


def test_the_public_api_map_is_what_the_model_enumerates(symbols):
    import plexora
    import plexora.api as api

    names = {q for q in symbols if q.count(".") == 1}
    assert names == {f"plexora.{n}" for n in plexora._PUBLIC_API} | {"plexora.view"}
    assert {f"plexora.api.{n}" for n in api.__all__} <= set(symbols)


def test_excluded_names_keep_agent_and_licensing_surface_out():
    m = manifest.load()
    for qualname, page in m.pages.items():
        assert not page.path.startswith(("mcp", "ai", "agent", "licens", "telemetry")), qualname
    plugin = m.extras("plexora.api.Plugin").get("exclude_members") or {}
    for member in ("capabilities_factory", "entitlement", "endpoint_entitlements", "load_capabilities"):
        assert member in plugin
