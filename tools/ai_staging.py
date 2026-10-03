"""Set up, deploy and seed the STAGING licence Worker (Plexora AI gateway included).

    python tools/ai_staging.py status                 # read-only: config, secrets present, health
    python tools/ai_staging.py create                 # wrangler d1 create + r2 bucket create; writes the D1 id
    python tools/ai_staging.py secrets                # fresh peppers/tokens + staging signing key + provider keys
    python tools/ai_staging.py deploy                 # wrangler deploy --env staging; records the workers.dev URL
    python tools/ai_staging.py schema                 # schema.sql -> the staging D1
    python tools/ai_staging.py seed [--dev]           # models, task assignments, a test account with `ai`, a credit grant

    python tools/ai_e2e.py --live --remote staging    # then the e2e checks against it

Everything here acts on `[env.staging]` in licensing/wrangler.toml only: a
Worker named plexora-licensing-staging on workers.dev, its own D1 and R2. Each
command that writes to Cloudflare first checks that the resolved config is
not production's (name, database, bucket, routes) and refuses otherwise.

Local state lives in ~/.plexora-staging/ (override: PLEXORA_STAGING_HOME), outside the
repository and outside any synced folder: the staging signing key
(signing-keys.json, from licensing/tools/generate_keys.py --kid pxs1), the
staging ADMIN_TOKEN (admin-token), and state.json (the URL and the test
account, including its seat key). No secret value is ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSING = ROOT / "licensing"
WRANGLER_TOML = LICENSING / "wrangler.toml"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV = "staging"
PLACEHOLDER_DB = "00000000-0000-0000-0000-000000000000"
PRODUCTION_HOST = "license.plexoraapp.com"

#: Generated fresh for staging; never production's values.
GENERATED = ("FP_PEPPER", "IP_HASH_KEY", "SESSION_KEY", "AI_USER_PEPPER", "ADMIN_TOKEN")
#: Taken from the environment or licensing/.dev.vars when present.
PROVIDER_KEYS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ORCAROUTER_API_KEY", "SAYGM_API_KEY")
#: Production's slots: never set on staging.
FORBIDDEN = ("SIGNING_KEY_PX1", "SIGNING_KEY_PX2", "RESEND_API_KEY")

TEST_EMAIL = "staging-test@lab.example.org"


def home() -> Path:
    return Path(os.environ.get("PLEXORA_STAGING_HOME") or "~/.plexora-staging").expanduser()


# -- config -------------------------------------------------------------------------------


def config(path: Path = WRANGLER_TOML) -> dict:
    """The production and staging halves of wrangler.toml, as wrangler resolves the parts that matter."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    staging = data["env"][ENV]
    prod_db, stg_db = data["d1_databases"][0], staging["d1_databases"][0]
    return {
        "production": {"name": data["name"], "database_name": prod_db["database_name"],
                       "database_id": prod_db["database_id"], "bucket": data["r2_buckets"][0]["bucket_name"],
                       "routes": data.get("routes", []), "vars": data["vars"]},
        "staging": {"name": staging["name"], "database_name": stg_db["database_name"],
                    "database_id": stg_db["database_id"], "bucket": staging["r2_buckets"][0]["bucket_name"],
                    # `routes` is inherited when absent, so absent means production's.
                    "routes": staging.get("routes", data.get("routes", [])),
                    "workers_dev": staging.get("workers_dev", data.get("workers_dev")),
                    "vars": staging.get("vars", {})},
    }


def problems(cfg: dict) -> list[str]:
    """Why the staging config could touch production, or is not ready. Empty = safe and ready."""
    p, s = cfg["production"], cfg["staging"]
    out = []
    if s["name"] == p["name"] or not s["name"].endswith("-staging"):
        out.append(f"staging Worker name {s['name']!r} is not a separate -staging Worker")
    if s["database_name"] == p["database_name"] or s["database_id"] == p["database_id"]:
        out.append("staging D1 is production's database")
    if s["bucket"] == p["bucket"]:
        out.append("staging R2 bucket is production's bucket")
    if s["routes"]:
        out.append(f"staging has routes {s['routes']} (it would claim a production hostname)")
    if not s["workers_dev"]:
        out.append("staging has workers_dev off, so it would have no URL")
    keys = json.loads(s["vars"].get("PUBLIC_KEYS_JSON") or "{}")
    prod_keys = json.loads(p["vars"].get("PUBLIC_KEYS_JSON") or "{}")
    if set(keys) & set(prod_keys) or set(keys.values()) & set(prod_keys.values()):
        out.append("staging shares a signing key id or public key with production")
    if s["vars"].get("ACTIVE_KID") not in keys:
        out.append("staging ACTIVE_KID is not one of its PUBLIC_KEYS_JSON")
    return out


def require_safe(cfg: dict, *, need_db: bool = True) -> None:
    found = problems(cfg)
    if need_db and cfg["staging"]["database_id"] == PLACEHOLDER_DB:
        found.append("the staging database_id is still the placeholder: run `create` first")
    if found:
        raise SystemExit("refusing: " + "; ".join(found))


def require_staging_url(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        raise SystemExit(f"not a URL: {url!r}")
    if host == PRODUCTION_HOST or host.endswith("." + PRODUCTION_HOST):
        raise SystemExit(f"refusing: {url} is the production licence service")
    return url.rstrip("/")


# -- local state --------------------------------------------------------------------------


def _private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def load_state() -> dict:
    path = home() / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(**changes) -> dict:
    state = {**load_state(), **changes}
    _private(home() / "state.json", json.dumps(state, indent=2))
    return state


def admin_token() -> str:
    token = os.environ.get("PLEXORA_STAGING_ADMIN_TOKEN")
    path = home() / "admin-token"
    if not token and path.exists():
        token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"no staging admin token: set PLEXORA_STAGING_ADMIN_TOKEN or run `secrets` ({path})")
    return token


def staging_url(arg: str | None = None) -> str:
    url = arg if arg and arg != "staging" else load_state().get("url")
    if not url:
        raise SystemExit("no staging URL: pass --url, or run `deploy` (it records the workers.dev URL)")
    return require_staging_url(url)


def signing_seed(cfg: dict) -> tuple[str, str]:
    """(kid, private seed) for staging's ACTIVE_KID, checked against its public half in wrangler.toml."""
    kid = cfg["staging"]["vars"]["ACTIVE_KID"]
    public = json.loads(cfg["staging"]["vars"]["PUBLIC_KEYS_JSON"])[kid]
    path = home() / "signing-keys.json"
    if not path.exists():
        raise SystemExit(f"no {path}: python licensing/tools/generate_keys.py --out {path} --kid {kid}")
    for key in json.loads(path.read_text(encoding="utf-8"))["keys"]:
        if key["kid"] == kid:
            if key["public_b64url"] != public:
                raise SystemExit(f"{path} holds a different {kid} key than wrangler.toml's PUBLIC_KEYS_JSON")
            return kid, key["private_b64url"]
    raise SystemExit(f"{path} has no key {kid}")


def provider_keys() -> dict[str, str]:
    """Provider keys from the environment, else licensing/.dev.vars (KEY=value lines)."""
    found = {}
    dev_vars = LICENSING / ".dev.vars"
    if dev_vars.exists():
        for line in dev_vars.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in PROVIDER_KEYS and value.strip():
                found[name.strip()] = value.strip().strip('"').strip("'")
    for name in PROVIDER_KEYS:
        if os.environ.get(name):
            found[name] = os.environ[name]
    return found


# -- wrangler -----------------------------------------------------------------------------


def wrangler(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx not found")
    if not (LICENSING / "node_modules" / ".bin").exists():
        raise SystemExit("licensing/node_modules has no .bin: run `npm ci` in licensing/ first.")
    done = subprocess.run([npx, "wrangler", *args], cwd=LICENSING, input=input, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and done.returncode:
        raise SystemExit(f"wrangler {args[0]} {args[1] if len(args) > 1 else ''} failed:\n"
                         f"{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return done


def secret_names() -> set[str] | None:
    """The secret names set on the staging Worker, or None when wrangler cannot say (not deployed, not logged in)."""
    done = wrangler("secret", "list", "--env", ENV, "--format", "json", check=False)
    if done.returncode:
        return None
    try:
        return {s["name"] for s in json.loads(done.stdout[done.stdout.index("["):])}
    except (ValueError, KeyError, TypeError):
        return None


def set_toml_value(key: str, value: str, *, after: str = f"[env.{ENV}") -> None:
    """Rewrite one `key = "..."` line inside the staging part of wrangler.toml (the first one after `after`)."""
    text = WRANGLER_TOML.read_text(encoding="utf-8")
    start = text.index(after)
    pattern = re.compile(rf'^{re.escape(key)} = "[^"]*"$', re.M)
    match = pattern.search(text, start)
    if not match:
        raise SystemExit(f"no {key} line in the staging part of wrangler.toml")
    WRANGLER_TOML.write_text(text[:match.start()] + f'{key} = "{value}"' + text[match.end():], encoding="utf-8")


# -- commands -----------------------------------------------------------------------------


def cmd_status(args) -> int:
    cfg = config()
    s = cfg["staging"]
    found = problems(cfg)
    print(f"Worker      {s['name']} (workers.dev only, routes {s['routes']})")
    print(f"D1          {s['database_name']} ({'PLACEHOLDER: run create' if s['database_id'] == PLACEHOLDER_DB else s['database_id']})")
    print(f"R2          {s['bucket']}")
    print(f"signing kid {s['vars'].get('ACTIVE_KID')}  key file {'present' if (home() / 'signing-keys.json').exists() else 'MISSING'}")
    print(f"admin token {'present' if (home() / 'admin-token').exists() or os.environ.get('PLEXORA_STAGING_ADMIN_TOKEN') else 'MISSING'}")
    print("config      " + ("safe" if not found else "PROBLEMS: " + "; ".join(found)))
    names = secret_names()
    if names is None:
        print("secrets     unknown (Worker not deployed yet, or wrangler not logged in)")
    else:
        need = {f"SIGNING_KEY_{s['vars']['ACTIVE_KID'].upper()}", *GENERATED, "OPENROUTER_API_KEY"}
        print(f"secrets     set: {', '.join(sorted(names)) or 'none'}")
        if need - names:
            print(f"            missing: {', '.join(sorted(need - names))}")
        if names & set(FORBIDDEN):
            print(f"            PRODUCTION-ONLY secrets present: {', '.join(sorted(names & set(FORBIDDEN)))}")
    state = load_state()
    if state.get("url"):
        from tools.ai_e2e import http

        status, body = http("GET", f"{state['url']}/healthz", timeout=15)
        print(f"health      {state['url']} -> HTTP {status}")
        if status == 200:
            status, providers = http("GET", f"{state['url']}/admin/api/ai/providers",
                                     headers={"Authorization": f"Bearer {admin_token()}"}, timeout=15)
            if status == 200:
                print(f"providers   {json.dumps(providers.get('providers', providers))[:400]}")
    else:
        print("url         unknown (run deploy)")
    if state.get("account_id"):
        print(f"test account {state['account_id']} (licence {state.get('license_id')})")
    return 1 if found else 0


def cmd_create(args) -> int:
    cfg = config()
    require_safe(cfg, need_db=False)
    s = cfg["staging"]
    if s["database_id"] == PLACEHOLDER_DB:
        done = wrangler("d1", "create", s["database_name"], check=False)
        match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", done.stdout)
        if not match:  # already exists: look it up
            listed = wrangler("d1", "list", "--json")
            rows = json.loads(listed.stdout[listed.stdout.index("["):])
            row = next((r for r in rows if r.get("name") == s["database_name"]), None)
            if not row:
                raise SystemExit(f"could not create or find {s['database_name']}:\n{done.stdout}\n{done.stderr}")
            database_id = row["uuid"]
        else:
            database_id = match.group(0)
        if database_id == cfg["production"]["database_id"]:
            raise SystemExit("refusing: that is production's database id")
        set_toml_value("database_id", database_id)
        print(f"D1 {s['database_name']}: {database_id} (written to wrangler.toml; commit it)")
    else:
        print(f"D1 {s['database_name']}: {s['database_id']} (already configured)")
    done = wrangler("r2", "bucket", "create", s["bucket"], check=False)
    already = "already exists" in (done.stdout + done.stderr).lower()
    if done.returncode and not already:
        raise SystemExit(f"could not create bucket {s['bucket']}:\n{done.stdout}\n{done.stderr}")
    print(f"R2 {s['bucket']}: {'already there' if already else 'created'}")
    return 0


def cmd_secrets(args) -> int:
    cfg = config()
    require_safe(cfg, need_db=False)
    present = secret_names() or set()
    rotate = {n.strip() for n in (args.rotate or "").split(",") if n.strip()}
    kid, seed = signing_seed(cfg)
    plan: dict[str, str] = {f"SIGNING_KEY_{kid.upper()}": seed}
    for name in GENERATED:
        plan[name] = secrets.token_urlsafe(32)
    plan.update(provider_keys())
    if "OPENROUTER_API_KEY" not in plan and "OPENROUTER_API_KEY" not in present:
        print("note: no OPENROUTER_API_KEY in the environment or licensing/.dev.vars; the e2e needs one")
    token_file = home() / "admin-token"
    for name, value in plan.items():
        if name in FORBIDDEN:
            raise SystemExit(f"refusing to set {name} on staging")
        if name in present and name not in rotate:
            print(f"  {name}: already set (keep; --rotate {name} to replace)")
            if name == "ADMIN_TOKEN" and not token_file.exists() and not os.environ.get("PLEXORA_STAGING_ADMIN_TOKEN"):
                print(f"    warning: {token_file} is missing, so this machine cannot administer staging")
            continue
        if args.dry_run:
            print(f"  {name}: would set")
            continue
        if name == "ADMIN_TOKEN":
            _private(token_file, value)    # before the put: a token the Worker has but we lost is useless
        wrangler("secret", "put", name, "--env", ENV, input=value)
        print(f"  {name}: set")
    return 0


def cmd_deploy(args) -> int:
    cfg = config()
    require_safe(cfg)
    done = wrangler("deploy", "--env", ENV)
    match = re.search(r"https://[\w.-]+\.workers\.dev", done.stdout)
    if not match:
        print(done.stdout[-1500:])
        raise SystemExit("deployed, but no workers.dev URL in wrangler's output")
    url = require_staging_url(match.group(0))
    save_state(url=url)
    print(f"deployed {cfg['staging']['name']} -> {url}")
    if cfg["staging"]["vars"].get("PUBLIC_BASE_URL") != url:
        set_toml_value("PUBLIC_BASE_URL", url, after=f"[env.{ENV}.vars]")
        print("PUBLIC_BASE_URL updated in wrangler.toml (commit it); deploying once more so mail links match")
        wrangler("deploy", "--env", ENV)
    return 0


def cmd_schema(args) -> int:
    cfg = config()
    require_safe(cfg)
    wrangler("d1", "execute", cfg["staging"]["database_name"], "--env", ENV, "--remote", "--yes",
             "--file=schema.sql")
    tables = sql(cfg, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    names = [t["name"] for t in tables]
    print(f"schema applied: {len(names)} tables, {sum(n.startswith('ai_') for n in names)} ai_*")
    return 0


def sql(cfg: dict, query: str) -> list[dict]:
    """A read against the STAGING D1 (by name, --env staging): never production's."""
    require_safe(cfg)
    done = wrangler("d1", "execute", cfg["staging"]["database_name"], "--env", ENV, "--remote", "--json",
                    "--command", query)
    out = json.loads(done.stdout[done.stdout.index("["):])
    return out[0]["results"] if isinstance(out, list) else out["results"]


def cmd_seed(args) -> int:
    from tools.ai_e2e import catalogue_models, discover_free_models, http, pick, publish_routes

    url = staging_url(args.url)
    token = admin_token()

    def admin(method, path, body=None):
        return http(method, f"{url}/admin/api{path}", body, {"Authorization": f"Bearer {token}"})

    status, _ = http("GET", f"{url}/healthz", timeout=15)
    if status != 200:
        raise SystemExit(f"{url}/healthz answered {status}")

    # Approved models and task assignments: free OpenRouter models at a NOMINAL price, as the e2e does.
    models = discover_free_models(None)
    by_id = {m["id"]: m for m in models}
    text = by_id.get(args.text_model) if args.text_model else pick(models, need=("tools",))
    vision = by_id.get(args.vision_model) if args.vision_model else pick(models, need=("vision",))
    fallback = (by_id.get(args.fallback_model) if args.fallback_model
                else pick(models, need=("vision",), avoid={vision and vision["id"]}))
    catalogue_models(admin, [m for m in (text, vision, fallback) if m], args.price, label="staging")
    published = publish_routes(admin, text, vision, fallback)
    print(f"models: text {text and text['id']}, vision {vision and vision['id']}, fallback {fallback and fallback['id']}")
    print(f"assignments: {len(published)} published (unbenched; staging allows it)")

    # The test account: reused while it exists.
    state = load_state()
    account = state.get("account_id")
    if account and admin("GET", f"/ai/accounts/{account}")[0] != 200:
        account = None
    if not account:
        status, issued = admin("POST", "/licenses", {
            "owner_email": TEST_EMAIL, "account_name": "Plexora staging test", "use_class": "academic", "seats": 2,
            "days": 365, "entitlements": ["ai"], "send_email": False, "notes": "tools/ai_staging.py seed"})
        if status != 201:
            raise SystemExit(f"could not issue the test licence: HTTP {status} {issued}")
        account = issued["account_id"]
        state = save_state(account_id=account, license_id=issued["license"]["id"],
                           seat_id=issued["seat"]["id"], seat_key=issued["seat"]["key"])
        print(f"test account {account}: licence {issued['license']['id']} with `ai` (seat key in {home() / 'state.json'})")
    else:
        print(f"test account {account}: reused")
    mode = "dev" if args.dev else "credits"
    status, body = admin("PATCH", f"/ai/accounts/{account}", {"mode": mode, "notes": "staging test account"})
    if status != 200:
        raise SystemExit(f"could not set the account mode: HTTP {status} {body}")
    # One grant per (account, amount): re-running the seed does not post it twice.
    status, body = admin("POST", f"/ai/accounts/{account}/credit", {
        "credits": args.credits, "kind": "grant", "note": "staging seed",
        "journal_id": f"staging-seed:{account}:{args.credits}"})
    if status not in (200, 201):
        raise SystemExit(f"could not grant credit: HTTP {status} {body}")
    print(f"mode {mode}; grant of {args.credits} credits {'posted' if body.get('posted') else 'already posted'}; "
          f"balance {json.dumps(body.get('balance'))}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read-only: config safety, local files, secrets set, health.")
    sub.add_parser("create", help="Create the staging D1 and R2 bucket; write the D1 id into wrangler.toml.")
    p = sub.add_parser("secrets", help="Set the staging secrets (fresh values; never production's).")
    p.add_argument("--rotate", default="", help="Comma-separated secrets to replace even if already set.")
    p.add_argument("--dry-run", action="store_true", help="Say what would be set; set nothing.")
    sub.add_parser("deploy", help="wrangler deploy --env staging; record the workers.dev URL.")
    sub.add_parser("schema", help="Apply licensing/schema.sql to the staging D1.")
    p = sub.add_parser("seed", help="Models, task assignments, a test account with `ai`, and a credit grant.")
    p.add_argument("--url", default=None, help="The staging URL (default: the one `deploy` recorded).")
    p.add_argument("--dev", action="store_true", help="Put the test account in dev mode (at-cost dev route).")
    p.add_argument("--credits", type=int, default=2000, help="Credits to grant (1 credit = $0.01; default 2000).")
    p.add_argument("--price", type=int, default=1_000_000,
                   help="Nominal test price for free models, micro-USD per 1M input tokens (default $1).")
    p.add_argument("--text-model", default=None)
    p.add_argument("--vision-model", default=None)
    p.add_argument("--fallback-model", default=None)
    args = parser.parse_args(argv)
    return {"status": cmd_status, "create": cmd_create, "secrets": cmd_secrets, "deploy": cmd_deploy,
            "schema": cmd_schema, "seed": cmd_seed}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
