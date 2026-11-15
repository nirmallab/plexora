"""A video's revision check holds between processes (audit flow-user-state-3).

`plexora mcp serve` edits videos through the same capabilities as the viewer,
in its own process, and the repository's lock is a thread lock. A real second
Python process is paused after it has read revision 0; a save from this one
lands in that window; the second process must then be refused rather than
write revision 1 over it.
"""

import os
import subprocess
import sys
import textwrap

from plexora.plugins.video_studio.server import repository



def _document():
    return {"title": "Rotation", "duration_s": 4.0,
            "source": {"datasource": "demo", "image_size": [800, 600], "depth": 12,
                       "frames": 1},
            "tracks": [{"track_id": "trk_z", "kind": "acquired_z", "keyframes": [
                {"kf_id": "kf_0", "t": 0, "value": 0}, {"kf_id": "kf_1", "t": 2, "value": 5}]}]}

SECOND_PROCESS = textwrap.dedent("""
    import sys, time
    from plexora.plugins.video_studio.server import operations, repository

    real = operations.apply_operations

    def paused(document, ops):
        print("loaded", flush=True)  # revision 0 read, nothing written yet
        time.sleep(2.0)
        return real(document, ops)

    operations.apply_operations = paused
    try:
        repository.apply(sys.argv[1], 0, [{"op": "set_title", "title": "From the agent"}])
        print("written", flush=True)
    except repository.ConflictError as exc:
        print("conflict", exc.current_revision, flush=True)
""")


def test_an_edit_racing_another_process_is_refused_not_lost():
    video_id = repository.create(_document())
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    other = subprocess.Popen([sys.executable, "-c", SECOND_PROCESS, video_id], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "loaded", other.stderr.read()
        result = repository.apply(video_id, 0, [{"op": "set_title", "title": "From the viewer"}])
        assert result["revision"] == 1
    finally:
        out, err = other.communicate(timeout=60)
    assert other.returncode == 0, err
    assert out.strip().splitlines()[-1] == "conflict 1"
    document = repository.load(video_id)
    assert document["revision"] == 1
    assert document["title"] == "From the viewer"
