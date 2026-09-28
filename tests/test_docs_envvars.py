"""tools/docs/env_vars.yaml lists every PLEXORA_* name in the source, and
nothing that is not there any more."""

from tools.docs import envvars


def test_every_environment_variable_is_classified():
    data = envvars.load()
    listed = {e["name"] for e in data.get("public") or []} | set(data.get("internal") or []) \
        | set((data.get("ignore") or {}).keys())
    found = envvars.scan()
    missing = sorted(set(found) - listed)
    assert not missing, ("PLEXORA_* names in the source that tools/docs/env_vars.yaml does not list "
                         "(add each under public, internal or ignore):\n  "
                         + "\n  ".join(f"{n}  ({', '.join(found[n][:3])})" for n in missing))
    gone = sorted(listed - set(found))
    assert not gone, "env_vars.yaml lists names no longer in the source:\n  " + "\n  ".join(gone)


def test_no_name_is_listed_twice():
    data = envvars.load()
    names = [e["name"] for e in data.get("public") or []] + list(data.get("internal") or []) \
        + list((data.get("ignore") or {}).keys())
    assert len(names) == len(set(names))


def test_public_entries_are_described():
    for entry in envvars.load().get("public") or []:
        assert entry.get("description", "").strip(), entry["name"]
        assert entry.get("group"), entry["name"]
