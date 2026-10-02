"""The staging licence Worker (`[env.staging]` in licensing/wrangler.toml) stays
apart from production and in step with it.

wrangler does not inherit `vars` into an environment, so a knob added to
`[vars]` and forgotten in `[env.staging.vars]` would silently take its code
default on staging. And it DOES inherit `routes`, so a staging block without
`routes = []` would claim license.plexoraapp.com on deploy.
"""

from __future__ import annotations

import json

import pytest

from plexora.licensing import keys
from tools import ai_e2e, ai_staging

#: Values staging sets differently on purpose; every other knob must match production.
DIFFERS = {"PUBLIC_BASE_URL", "PUBLIC_KEYS_JSON", "ACTIVE_KID", "ACCESS_TEAM_DOMAIN", "ACCESS_AUD", "MAIL_FROM"}


@pytest.fixture(scope="module")
def cfg():
    return ai_staging.config()


def test_staging_is_separate_from_production(cfg):
    assert ai_staging.problems(cfg) == []
    s, p = cfg["staging"], cfg["production"]
    assert s["routes"] == [] and s["workers_dev"] is True
    assert s["name"] != p["name"] and s["database_name"] != p["database_name"] and s["bucket"] != p["bucket"]
    assert s["database_id"] != p["database_id"]


def test_staging_repeats_every_production_knob(cfg):
    prod, stg = cfg["production"]["vars"], cfg["staging"]["vars"]
    assert set(prod) == set(stg), {"missing on staging": sorted(set(prod) - set(stg)),
                                   "only on staging": sorted(set(stg) - set(prod))}
    drift = {k: (prod[k], stg[k]) for k in prod if k not in DIFFERS and prod[k] != stg[k]}
    assert drift == {}


def test_route_tests_keep_the_bench_gate_on():
    # Production and staging accept any catalogued model (AI_ALLOW_UNBENCHED_ROUTES = 1); the route
    # tests pin the gate on so that it stays tested.
    config = (ai_staging.LICENSING / "vitest.workers.config.ts").read_text(encoding="utf-8")
    assert "AI_ALLOW_UNBENCHED_ROUTES: '0'" in config


def test_no_plexora_build_trusts_the_staging_key(cfg):
    staging_keys = json.loads(cfg["staging"]["vars"]["PUBLIC_KEYS_JSON"])
    assert cfg["staging"]["vars"]["ACTIVE_KID"] in staging_keys
    assert not set(staging_keys) & set(keys.PUBLIC_KEYS)
    assert not set(staging_keys.values()) & set(keys.PUBLIC_KEYS.values())


def test_staging_access_is_off(cfg):
    # No Access application fronts workers.dev: admin is ADMIN_TOKEN only.
    assert cfg["staging"]["vars"]["ACCESS_TEAM_DOMAIN"] == "" and cfg["staging"]["vars"]["ACCESS_AUD"] == ""


def test_guards_refuse_production(cfg):
    for url in ("https://license.plexoraapp.com", "https://LICENSE.plexoraapp.com/", "https://x.license.plexoraapp.com"):
        with pytest.raises(SystemExit):
            ai_staging.require_staging_url(url)
    assert ai_staging.require_staging_url("https://plexora-licensing-staging.lab.workers.dev/") == \
        "https://plexora-licensing-staging.lab.workers.dev"

    bad = json.loads(json.dumps(cfg))
    bad["staging"]["routes"] = cfg["production"]["routes"]
    bad["staging"]["database_id"] = cfg["production"]["database_id"]
    bad["staging"]["vars"]["PUBLIC_KEYS_JSON"] = cfg["production"]["vars"]["PUBLIC_KEYS_JSON"]
    found = " ".join(ai_staging.problems(bad))
    assert "routes" in found and "production's database" in found and "signing key" in found
    with pytest.raises(SystemExit):
        ai_staging.require_safe(bad, need_db=False)


def test_inherited_routes_count_as_production(tmp_path):
    # A staging block that omits `routes` inherits production's: the guard must see that.
    text = ai_staging.WRANGLER_TOML.read_text(encoding="utf-8").replace("routes = []\n", "", 1)
    path = tmp_path / "wrangler.toml"
    path.write_text(text, encoding="utf-8")
    assert any("routes" in p for p in ai_staging.problems(ai_staging.config(path)))


def test_e2e_remote_refuses_the_stub():
    with pytest.raises(SystemExit):
        ai_e2e.main(["--stub", "--remote", "staging"])
