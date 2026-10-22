"""A scratch Plexora server for Video Studio benchmarks and the browser test.

    python tools/bench_video/serve_licensed.py ROOT DATA PORT LICENCE_DIR

Video Studio is Pro (`plugin:video_studio`), so a scratch server needs a
licence that includes it. This installs a TEST licence -- signed by a
throwaway key that only this process trusts (biocognia.testing.Issuer, the
test suite's own issuer) -- into LICENCE_DIR, which must be a scratch
directory: never the user's own licence directory. Nothing here can mint a
licence any other process accepts.
"""
import os
import sys

root, data, port, licence_dir = sys.argv[1:5]
sys.path.insert(0, root)
os.environ["PLEXORA_DATA_PATH"] = data
os.environ["BIOCOGNIA_DIR"] = licence_dir
os.environ["BIOCOGNIA_OFFLINE"] = "1"
os.environ["BIOCOGNIA_NO_HEARTBEAT"] = "1"
os.environ.setdefault("PLEXORA_TELEMETRY", "off")

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402
from biocognia.testing import Issuer  # noqa: E402

from plexora.licensing import LICENSING, PRODUCT  # noqa: E402

issuer = Issuer(MonkeyPatch(), PRODUCT)
issuer.install(LICENSING, issuer.issue(ent=["ai", "plugin:video_studio"]))

from waitress import serve  # noqa: E402

from plexora import app  # noqa: E402

print(f"Serving on 127.0.0.1:{port}", flush=True)
serve(app, host="127.0.0.1", port=int(port), threads=12)
