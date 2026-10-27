"""scripts/release.py: the parts that decide things, without building anything.

The build steps themselves are exercised by running the script (see
DEPLOYMENT.md); what is pinned here is the logic a release depends on and a
mistake in which would ship quietly -- a version bumped in one file and not
another, an artifact named for the wrong platform, a "signed" build that fell
back to ad-hoc, a wheel missing the server.
"""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("plexora_release_under_test",
                                                  ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass looks its own module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _load()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("PLEXORA_RUNTIME_DIR", raising=False)
    return release.Ctx(target="x86_64-pc-windows-msvc", build_dir=tmp_path / "b",
                       release_dir=tmp_path / "r")


@pytest.mark.parametrize(("current", "spec", "expected"), [
    ("0.0.23", "patch", "0.0.24"),
    ("0.0.23", "minor", "0.1.0"),
    ("0.9.4", "major", "1.0.0"),
    ("0.0.23", "1.2.3", "1.2.3"),
])
def test_next_version(current, spec, expected):
    assert release.next_version(current, spec) == expected


def test_a_bump_that_is_not_a_version_is_refused():
    with pytest.raises(release.StepError):
        release.next_version("0.0.23", "0.1")


def test_the_version_is_read_from_pyproject():
    assert release._SEMVER.match(release.read_version())


def test_every_file_that_carries_the_version_agrees_with_pyproject():
    """What `propagate --check` enforces before every bundle and in CI."""
    assert release.propagate(check=True) == []


def test_propagation_rewrites_json_and_cargo_versions(tmp_path, monkeypatch):
    conf = tmp_path / "tauri.conf.json"
    conf.write_text(json.dumps({"productName": "Plexora", "version": "0.0.1"}), encoding="utf-8")
    cargo = tmp_path / "Cargo.toml"
    cargo.write_text('[package]\nname = "x"\nversion = "0.0.1"\n\n[dependencies]\n'
                     'serde = { version = "1" }\n', encoding="utf-8")
    targets = release._propagation_targets()
    readers = {"json": targets[0], "cargo": targets[1]}
    readers["json"][2](conf, "2.3.4")
    readers["cargo"][2](cargo, "2.3.4")
    assert json.loads(conf.read_text())["version"] == "2.3.4"
    text = cargo.read_text()
    assert 'version = "2.3.4"' in text.split("[dependencies]")[0]
    assert 'serde = { version = "1" }' in text, "dependency versions are left alone"


def test_artifact_names_say_what_and_where(ctx, monkeypatch):
    monkeypatch.setattr(release, "read_version", lambda: "1.2.3")
    ctx.dev = False
    [(built, name)] = release.expected_artifacts(ctx)
    assert name == "Plexora-1.2.3-windows-x64-setup.exe"
    assert built.name == "Plexora_1.2.3_x64-setup.exe"

    mac = release.Ctx(target="aarch64-apple-darwin", build_dir=ctx.build_dir, dev=False)
    assert release.expected_artifacts(mac)[0][1] == "Plexora-1.2.3-macos-arm64.dmg"
    linux = release.Ctx(target="x86_64-unknown-linux-gnu", build_dir=ctx.build_dir, dev=False)
    assert [n for _b, n in release.expected_artifacts(linux)] == [
        "Plexora-1.2.3-linux-x64.deb"]


def test_a_dev_build_carries_the_commit_in_its_file_names_only(ctx, monkeypatch):
    monkeypatch.setattr(release, "read_version", lambda: "1.2.3")
    monkeypatch.setattr(release, "git_sha", lambda short=7: "abc1234")
    ctx.dev = True
    [(built, name)] = release.expected_artifacts(ctx)
    assert name == "Plexora-1.2.3-dev+gabc1234-windows-x64-setup.exe"
    assert built.name == "Plexora_1.2.3_x64-setup.exe", "the bundle's own version stays numeric"


def test_every_target_has_a_pinned_interpreter():
    for target in release.TARGETS:
        runtime = release.RUNTIME_TRIPLE.get(target, target)
        assert runtime in release.PBS_SHA256
        assert len(release.PBS_SHA256[runtime]) == 64
    assert release.pbs_asset_name("x86_64-pc-windows-msvc") == (
        f"cpython-{release.PBS_PYTHON}+{release.PBS_TAG}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz")


def test_the_overlay_ships_the_runtime_and_no_signing_by_default(ctx, monkeypatch):
    for name in ("PLEXORA_WIN_SIGN_COMMAND", "PLEXORA_WIN_CERT_THUMBPRINT"):
        monkeypatch.delenv(name, raising=False)
    overlay = json.loads(release.tauri_config_overlay(ctx, sign=False).read_text())
    assert list(overlay["bundle"]["resources"].values()) == ["runtime/"]
    assert "windows" not in overlay["bundle"]


def test_a_sign_command_in_the_environment_reaches_the_bundler(ctx, monkeypatch):
    monkeypatch.setenv("PLEXORA_WIN_SIGN_COMMAND", "signtool sign /fd sha256 %1")
    overlay = json.loads(release.tauri_config_overlay(ctx, sign=True).read_text())
    assert overlay["bundle"]["windows"]["signCommand"] == "signtool sign /fd sha256 %1"


def test_asking_to_sign_without_credentials_fails_rather_than_shipping_unsigned(ctx, monkeypatch):
    for name in ("PLEXORA_WIN_SIGN_COMMAND", "PLEXORA_WIN_CERT_THUMBPRINT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(release.StepError):
        release.tauri_config_overlay(ctx, sign=True)
    mac = release.Ctx(target="aarch64-apple-darwin", build_dir=ctx.build_dir)
    monkeypatch.delenv("APPLE_SIGNING_IDENTITY", raising=False)
    with pytest.raises(release.StepError):
        release.tauri_config_overlay(mac, sign=True)


def test_macos_builds_are_at_least_ad_hoc_signed(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLE_SIGNING_IDENTITY", raising=False)
    mac = release.Ctx(target="aarch64-apple-darwin", build_dir=tmp_path)
    overlay = json.loads(release.tauri_config_overlay(mac, sign=False).read_text())
    assert overlay["bundle"]["macOS"]["signingIdentity"] == "-"


def _wheel(path, names, entry_points="[plexora.plugins]\nroi = x\n"):
    with zipfile.ZipFile(path, "w") as wheel:
        for name in names:
            wheel.writestr(name, "")
        wheel.writestr("plexora-1.0.dist-info/entry_points.txt", entry_points)
    return path


COMPLETE = [
    "plexora/cli.py", "plexora/_lifetime.py", "plexora/server/routes/desktop_routes.py",
    "plexora/server/routes/page_routes.py", "plexora/client/dist/vendor_bundle.js",
    "plexora/client/src/js/services/desktopBridge.js", "plexora/client/templates/base.html",
    "plexora/plugins/roi/static/roi.js",
    # A webpack chunk named after node_modules is not node_modules.
    "plexora/client/dist/vendors-node_modules_dompurify_bundle.js",
]


def test_a_complete_wheel_passes(tmp_path):
    release.verify_wheel(_wheel(tmp_path / "ok.whl", COMPLETE))


def test_a_wheel_without_the_server_fails(tmp_path):
    names = [n for n in COMPLETE if "server/" not in n]
    with pytest.raises(release.StepError, match="desktop_routes"):
        release.verify_wheel(_wheel(tmp_path / "bad.whl", names))


def test_a_wheel_that_swept_up_node_modules_fails(tmp_path):
    names = COMPLETE + ["plexora/client/node_modules/flatted/python/flatted.py"]
    with pytest.raises(release.StepError, match="leaked"):
        release.verify_wheel(_wheel(tmp_path / "leak.whl", names))


def test_mach_o_files_are_found_by_their_magic(tmp_path):
    (tmp_path / "lib.dylib").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 16)
    (tmp_path / "fat.so").write_bytes(b"\xca\xfe\xba\xbe" + b"\0" * 16)
    (tmp_path / "script.py").write_bytes(b"print('hi')\n")
    found = sorted(p.name for p in release.iter_mach_o(tmp_path))
    assert found == ["fat.so", "lib.dylib"]


def test_checksums_cover_every_artifact(tmp_path):
    (tmp_path / "a.exe").write_bytes(b"one")
    (tmp_path / "b.whl").write_bytes(b"two")
    release.write_checksums(tmp_path)
    sums = (tmp_path / "SHA256SUMS.txt").read_text().splitlines()
    assert [line.split()[1] for line in sums] == ["a.exe", "b.whl"]
    assert "SIZES.txt" not in (tmp_path / "SHA256SUMS.txt").read_text()


def test_a_synced_folder_keeps_build_products_out_of_it():
    assert release._in_synced_folder(Path(r"C:\Users\x\Partners HealthCare Dropbox\repo"))
    assert not release._in_synced_folder(Path("/home/x/src/plexora"))


def test_the_script_parses_every_documented_subcommand():
    parser = release.build_parser()
    for argv in (["doctor"], ["bump", "patch"], ["propagate", "--check"], ["client", "--verify-only"],
                 ["wheel"], ["runtime"], ["bundle", "--sign"], ["collect"],
                 ["validate", "--bundle-dir"], ["checksums"], ["all"], ["ci", "--bump", "minor"],
                 ["clean", "--all"], ["telemetry"], ["telemetry", "--check"], ["telemetry", "--deploy"]):
        assert parser.parse_args(argv).command == argv[0]
    assert parser.parse_args(["--skip-telemetry", "all"]).skip_telemetry
    with pytest.raises(SystemExit):
        parser.parse_args(["telemetry", "--check", "--deploy"])


def test_relative_directories_become_absolute(tmp_path, monkeypatch):
    # CI passes `--build-dir build`; cargo and the moved-runtime test run
    # with other working directories, so a relative path breaks them.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLEXORA_RUNTIME_DIR", raising=False)
    ctx = release.Ctx(target="x86_64-unknown-linux-gnu", build_dir=Path("build"),
                      release_dir=Path("release"))
    assert ctx.build_dir == (tmp_path / "build").resolve()
    assert ctx.runtime_dir.is_absolute()
    assert ctx.release_dir == (tmp_path / "release").resolve()


def test_empty_signing_secrets_never_reach_tauri(tmp_path, monkeypatch):
    # CI maps absent secrets to "", and Tauri tries to import an empty
    # APPLE_CERTIFICATE; unsigned builds must not see the variable at all.
    monkeypatch.setenv("APPLE_CERTIFICATE", "")
    monkeypatch.setenv("APPLE_ID", "someone@example.org")
    ctx = release.Ctx(target="aarch64-apple-darwin", build_dir=tmp_path)
    env = release._cargo_env(ctx)
    assert env["APPLE_CERTIFICATE"] is None
    assert "APPLE_ID" not in env


def test_run_drops_variables_set_to_none(monkeypatch):
    monkeypatch.setenv("PLEXORA_RELEASE_PROBE", "leaked")
    done = release.run([sys.executable, "-c",
                        "import os; print(os.environ.get('PLEXORA_RELEASE_PROBE'))"],
                       env={"PLEXORA_RELEASE_PROBE": None}, capture=True)
    assert done.stdout.strip() == "None"


def test_the_updater_manifest_names_only_signed_artifacts(tmp_path):
    (tmp_path / "Plexora-0.0.25-macos-arm64.app.tar.gz").write_bytes(b"x")
    (tmp_path / "Plexora-0.0.25-macos-arm64.app.tar.gz.sig").write_text("SIGMAC\n")
    (tmp_path / "Plexora-0.0.25-windows-x64-setup.exe").write_bytes(b"x")
    (tmp_path / "Plexora-0.0.25-windows-x64-setup.exe.sig").write_text("SIGWIN")
    # A .deb with no signature: never offered, rather than offered and failing.
    (tmp_path / "Plexora-0.0.25-linux-x64.deb").write_bytes(b"x")

    manifest = release.updater_manifest(tmp_path, "0.0.25",
                                        pub_date="2026-09-26T00:00:00+00:00")
    platforms = manifest["platforms"]
    assert manifest["version"] == "0.0.25"
    assert manifest["pub_date"] == "2026-09-26T00:00:00Z"
    assert set(platforms) == {"darwin-aarch64", "darwin-aarch64-app",
                              "windows-x86_64", "windows-x86_64-nsis"}
    assert platforms["darwin-aarch64"]["signature"] == "SIGMAC"
    assert platforms["windows-x86_64"]["url"] == (
        "https://github.com/nirmallab/plexora/releases/download/v0.0.25/"
        "Plexora-0.0.25-windows-x64-setup.exe")


def test_updater_artifacts_only_with_a_signing_key(ctx, monkeypatch, tmp_path):
    ctx.dev = False
    monkeypatch.delenv("TAURI_SIGNING_PRIVATE_KEY", raising=False)
    overlay = json.loads(release.tauri_config_overlay(ctx, sign=False).read_text())
    assert "createUpdaterArtifacts" not in overlay["bundle"]
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "secret")
    monkeypatch.setenv("PLEXORA_UPDATER_PUBKEY", "PUBLIC")
    overlay = json.loads(release.tauri_config_overlay(ctx, sign=False).read_text())
    assert overlay["bundle"]["createUpdaterArtifacts"] is True
    assert overlay["plugins"]["updater"]["pubkey"] == "PUBLIC"


def test_a_dev_build_is_never_offered_updates(ctx, monkeypatch):
    # The committed public key would turn the updater on; a dev build blanks it.
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "secret")
    monkeypatch.setenv("PLEXORA_UPDATER_PUBKEY", "PUBLIC")
    overlay = json.loads(release.tauri_config_overlay(ctx, sign=False).read_text())
    assert ctx.dev
    assert overlay["plugins"]["updater"]["pubkey"] == ""
    assert "createUpdaterArtifacts" not in overlay["bundle"]


# -- the telemetry Worker ------------------------------------------------------


@pytest.fixture
def worker(ctx, monkeypatch):
    """`ctx` with the live Worker and the deploy replaced: `live` is what
    /healthz reports (None = unreachable), `deploys` counts deploys, and a
    deploy makes /healthz report `after_deploy`."""
    ctx.skip_tests = True  # no sync_telemetry_vectors.py run
    state = {"live": None, "deploys": 0, "after_deploy": None}

    def health(timeout=10.0):
        return None if state["live"] is None else {"ok": True, "schema": 1, "vectors": state["live"]}

    def deploy(_ctx):
        state["deploys"] += 1
        state["live"] = state["after_deploy"]

    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(release, "deployed_health", health)
    monkeypatch.setattr(release, "deploy_worker", deploy)
    monkeypatch.setattr(release.time, "sleep", lambda _s: None)
    monkeypatch.setattr(release.time, "monotonic", lambda: next(clock))
    return ctx, state


def test_release_py_fingerprints_the_vectors_exactly_as_the_client_does():
    from plexora.telemetry import schema

    data = json.loads(release.VECTORS.read_text(encoding="utf-8"))
    assert release.vectors_fingerprint(data) == schema.fingerprint(data) == data["fingerprint"]
    assert release.local_vectors_fingerprint() == data["fingerprint"]


def test_vectors_whose_fingerprint_does_not_recompute_are_refused(tmp_path, monkeypatch):
    data = json.loads(release.VECTORS.read_text(encoding="utf-8"))
    data["first_party"].append("new_plugin")  # edited by hand, not regenerated
    stale = tmp_path / "backend" / "vectors" / "plexora-vectors.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "VECTORS", stale)
    with pytest.raises(release.StepError, match="does not match its contents"):
        release.local_vectors_fingerprint()


def test_a_current_worker_is_left_alone(worker):
    ctx, state = worker
    state["live"] = release.local_vectors_fingerprint()
    assert release.cmd_telemetry(ctx) == "current"
    assert state["deploys"] == 0


def test_check_says_behind_and_never_deploys(worker):
    ctx, state = worker
    state["live"] = "0" * 16
    with pytest.raises(release.StepError, match="behind this checkout"):
        release.cmd_telemetry(ctx, release.argparse.Namespace(check=True, deploy=False))
    state["live"] = None
    with pytest.raises(release.StepError, match="live unreachable"):
        release.cmd_telemetry(ctx, release.argparse.Namespace(check=True, deploy=False))
    assert state["deploys"] == 0


def test_a_worker_that_is_behind_is_deployed_and_then_verified(worker):
    ctx, state = worker
    state["live"] = "0" * 16
    state["after_deploy"] = release.local_vectors_fingerprint()
    assert release.cmd_telemetry(ctx) == "deployed"
    assert state["deploys"] == 1


def test_a_deploy_the_live_worker_does_not_show_fails(worker):
    ctx, state = worker
    state["live"] = state["after_deploy"] = "0" * 16
    with pytest.raises(release.StepError, match="still reports vectors 0000000000000000"):
        release.cmd_telemetry(ctx)
    assert state["deploys"] == 1


def test_deploy_redeploys_a_current_worker(worker):
    ctx, state = worker
    state["live"] = state["after_deploy"] = release.local_vectors_fingerprint()
    assert release.cmd_telemetry(ctx, release.argparse.Namespace(check=False, deploy=True)) == "deployed"
    assert state["deploys"] == 1


def test_a_dev_build_only_warns_about_the_worker(worker, capsys):
    ctx, state = worker
    assert ctx.dev
    release.telemetry_for_build(ctx)  # unreachable: no exception
    assert "warning: The telemetry Worker is behind" in capsys.readouterr().out
    assert state["deploys"] == 0


def test_a_release_build_deploys_a_worker_that_is_behind(worker):
    ctx, state = worker
    ctx.dev = False
    state["live"] = "0" * 16
    state["after_deploy"] = release.local_vectors_fingerprint()
    release.telemetry_for_build(ctx)
    assert state["deploys"] == 1


def test_skip_telemetry_skips_it(worker, monkeypatch):
    ctx, _state = worker
    ctx.dev = False
    ctx.skip_telemetry = True
    monkeypatch.setattr(release, "cmd_telemetry", lambda *_a, **_k: pytest.fail("checked"))
    release.telemetry_for_build(ctx)


def test_a_dry_run_bump_never_touches_the_worker(ctx, monkeypatch):
    ctx.dry_run = True
    monkeypatch.setattr(release, "cmd_telemetry", lambda *_a, **_k: pytest.fail("deployed"))
    release.cmd_bump(ctx, release.argparse.Namespace(spec="99.0.0", commit=False, tag=False))


@pytest.mark.parametrize(("code", "text", "who"), [
    (0, "👋 You are logged in with an OAuth Token, associated with the email someone@lab.org.\n", "someone@lab.org"),
    (0, "You are logged in with an API Token, associated with the email ci@lab.org.", "ci@lab.org"),
    (0, "👋 You are logged in with an API Token. Unable to retrieve email for this user.", "API Token"),
    (0, "You are not authenticated. Please run `wrangler login`.", None),
    (1, "You are logged in with an OAuth Token, associated with the email someone@lab.org.", None),
    (0, "Getting User settings...", None),
])
def test_wrangler_whoami_is_read(code, text, who):
    assert release.parse_whoami(code, text) == who
