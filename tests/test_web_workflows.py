"""The website's CI and release workflows (.github/workflows/web.yml, the web
jobs of release.yml): what a mistake in would ship quietly or leak -- a
preview indexed or a release noindexed, production deployed before the
installers built, wrangler unpinned, the Cloudflare token handed to a job that
runs the project's code, a test file the workflow names that no longer
exists. zizmor (security.yml) audits the rest; nothing here runs a workflow."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def web():
    return _load("web.yml")


@pytest.fixture(scope="module")
def release():
    return _load("release.yml")


def _runs(job) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


def _uses(job) -> list:
    return [step["uses"].split()[0] for step in job.get("steps", []) if "uses" in step]


@pytest.mark.parametrize("name", ["web.yml", "release.yml"])
def test_every_action_is_pinned_to_a_commit_and_every_job_has_a_timeout(name):
    for job_name, job in _load(name)["jobs"].items():
        assert "timeout-minutes" in job, job_name
        for used in _uses(job):
            if used.startswith(("nirmallab/", "./")):
                continue
            assert SHA.match(used), f"{name} {job_name}: {used}"


def test_wrangler_is_only_ever_an_exact_version():
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for found in re.findall(r"wrangler@\S*", text):
            assert re.fullmatch(r"wrangler@\d+\.\d+\.\d+", found), f"{path.name}: {found}"


def test_the_cloudflare_token_reaches_only_the_deploy_jobs(web, release):
    for name, workflow in (("web.yml", web), ("release.yml", release)):
        for job_name, job in workflow["jobs"].items():
            text = yaml.safe_dump(job)
            if "CLOUDFLARE_API_TOKEN" in text:
                assert (name, job_name) in {("web.yml", "preview"), ("release.yml", "web_deploy")}
                # The deploy jobs run publish.py and wrangler, never the project.
                assert "uv run" not in _runs(job) and "uv sync" not in _runs(job), job_name


def test_the_preview_is_mains_noindexed_and_on_a_development_prefix(web):
    preview = web["jobs"]["preview"]
    assert "refs/heads/main" in preview["if"] and "push" in preview["if"]
    assert {"import_check", "smoke"} <= set(preview["needs"])
    built = _runs(web["jobs"]["static"])
    assert re.search(r'--out "\$out/preview" \\\s+--py "\$ASSETS/py/dev/\$\{SHA::12\}/" --noindex', built)
    deploy = _runs(preview)
    assert 'prefix="py/dev/${SHA::12}/"' in deploy
    assert '--prefix "$prefix" --dev' in deploy
    assert "--project plexora-viewer-preview" in deploy
    assert "current --set" in deploy and "prune --prefix" in deploy


def test_a_release_deploys_production_only_once_every_installer_built(release):
    jobs = release["jobs"]
    assert set(jobs["web_deploy"]["needs"]) == {"preflight", "build", "web"}
    assert "web_deploy" in jobs["publish"]["needs"]
    assert jobs["web_deploy"]["if"].startswith("startsWith(github.ref, 'refs/tags/v')")
    built = _runs(jobs["web"])
    assert '--py "https://assets.plexoraapp.com/py/$VERSION/"' in built
    assert "--noindex" not in built
    assert "propagate --check --bundle" in built and "import_check.mjs" in built
    assert "actions/cache" not in " ".join(_uses(jobs["web"]))  # a release reads no cache
    deploy = _runs(jobs["web_deploy"])
    assert '--prefix "py/$VERSION/"' in deploy and "--dev" not in deploy
    assert "--project plexora-viewer --branch release" in deploy
    assert "https://assets.plexoraapp.com/py/$VERSION/" in deploy  # verified where it is served


def test_every_test_file_the_workflow_names_exists(web):
    named = re.findall(r"tests/[\w/]+\.(?:py|mjs)", _runs(web["jobs"]["static"]) + _runs(web["jobs"]["smoke"])
                       + _runs(web["jobs"]["import_check"]))
    assert named
    assert [path for path in named if not (ROOT / path).is_file()] == []
