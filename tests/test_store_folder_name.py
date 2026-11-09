"""The website's folder for projects (client services/browserStore.js
`chooseHome`): a folder named Plexora in any case is the library itself.

Found testing website 0.0.44 on a Mac whose library folder is
`~/Documents/plexora`: picked, it was not recognised (the test compared
"plexora" with "Plexora") and a second library was made inside it,
`plexora/Plexora/projects`, beside the desktop app's `plexora/projects`, the
same folder to macOS as `Documents/Plexora`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORE = (ROOT / "plexora" / "client" / "src" / "js" / "services" / "browserStore.js").read_text(encoding="utf-8")


def _async_function(source, name):
    start = source.index(f"async function {name}(")
    depth, at = 0, source.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(source[at], 0)
        if depth == 0:
            return source[start:at + 1]
        at += 1


def _choose(picked):
    script = f"""
const FOLDER = "Plexora";
let remembered = null;
const dir = (name) => ({{
    name,
    async getFileHandle() {{ throw new Error("no such file"); }},
    async getDirectoryHandle(child) {{ return dir(name + "/" + child); }},
}});
const global = {{ showDirectoryPicker: async () => dir({json.dumps(picked)}) }};
{_async_function(STORE, "chooseHome")}
chooseHome().then((c) => console.log(JSON.stringify({{ parent: c.parent, home: c.home.name }})));
"""
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("picked", ["Plexora", "plexora", "PLEXORA"])
def test_a_folder_named_plexora_in_any_case_is_the_library(picked):
    if not shutil.which("node"):
        pytest.skip("needs node")
    assert _choose(picked) == {"parent": None, "home": picked}


def test_any_other_folder_gets_a_plexora_folder_inside():
    if not shutil.which("node"):
        pytest.skip("needs node")
    assert _choose("Research") == {"parent": "Research", "home": "Research/Plexora"}
