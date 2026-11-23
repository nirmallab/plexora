"""vis_run.py <root> <port> <model>: one visual-bench run -- for each scene a
QC session (visual pass only) started on the MCP server, then a headless
Claude Code coordinator on <model> (effort high) that runs the pass with
Plexora's tools. Writes <root>/sessions.json and <root>/agent_<scene>.txt."""
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.expanduser("~/worktrees/plexora-qcbench"))
from plexora.ai import visual_bench as vb

root, port, model = sys.argv[1], sys.argv[2], sys.argv[3]
url = f"http://127.0.0.1:{port}/mcp"
PX = ["/Users/aj/miniconda3/envs/plexora/bin/python",
      "/Users/aj/softwares/biocognia/plexora/docs/internal/bench/runs/2026-11-23-p193-nonbinary/tools/px.py"]
cfg = os.path.join(root, "mcp.json")
json.dump({"mcpServers": {"plexora": {"type": "http", "url": url}}}, open(cfg, "w"))
env = {**os.environ, "PX_URL": url, "PX_IMAGES": os.path.join(root, "px_images"),
       "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
sessions = {}
for scene, _a, _s in vb.SCENES:
    out = subprocess.run(PX + ["qc_session_start", json.dumps({
        "project": vb._project(scene), "map_cell_um": 25.0, "checks": vb.CHECKS,
        "on_limit": "extend", "agent": "visual_bench"})], capture_output=True, text=True, env=env).stdout
    sid = json.loads(out.split("\n[")[0])["session_id"]
    sessions[scene] = sid
    brief = vb.BRIEF.format(sid=sid, project=vb._project(scene))
    tools = ",".join(f"mcp__plexora__{t}" for t in vb.TOOLS)
    t0 = time.time()
    r = subprocess.run(["claude", "-p", brief, "--model", model, "--effort", "high",
                        "--mcp-config", cfg, "--strict-mcp-config", "--allowedTools", tools,
                        "--output-format", "text"], capture_output=True, text=True, env=env, cwd=root)
    open(os.path.join(root, f"agent_{scene}.txt"), "w").write(r.stdout + "\n--- stderr\n" + r.stderr)
    print(scene, sid, f"rc {r.returncode}", f"{time.time() - t0:.0f}s", flush=True)
    json.dump(sessions, open(os.path.join(root, "sessions.json"), "w"), indent=1)
