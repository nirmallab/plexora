"""story.json: every fact the video shows, from the real session record."""
import json, glob, os, datetime as dt
from plexora.plugins.qc.server import packets as P
from plexora.plugins.qc.capabilities_session import packet_subject

# the live data root (read only): PLEXORA_LIVE_ROOT, else the platform default
from plexora import paths
L = os.environ.get("PLEXORA_LIVE_ROOT") or str(paths.data_root())
S = "session"
s = json.load(open(f"{S}/session.json"))
iso = lambda v: dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
job = json.load(open(f"{L}/.agent/jobs/{s['bulk_job_id']}.json"))
t0 = iso(s["created_at"])
ev = [json.loads(l) for l in open(f"{S}/decisions.jsonl")]
issued = {e.get("packet_id"): e["t"] for e in ev if e["event"] == "issued"}
pk = []
for f in sorted(glob.glob(f"{S}/packets/pk_*.json")):
    d = json.load(open(f))
    pk.append({"id": d["packet_id"], "kind": d["kind"], "subject": packet_subject(d),
               "narration": P.narrate(d), "t": round(issued.get(d["packet_id"], t0) - t0, 1),
               "artifacts": [i.get("artifact_id") for i in d.get("images") or []],
               "units_done": (d.get("progress") or {}).get("units_done"),
               "by_type": (d.get("progress") or {}).get("by_type")})
answered = {}
for e in ev:
    if e["event"] == "answered":
        answered[e.get("packet_id")] = {"t": round(e["t"] - t0, 1), "outcome": e.get("outcome") or e.get("outcomes")}
closed = [{"t": round(e["t"] - t0, 1), "unit": e["unit"], "state": e["state"], "reason": e.get("reason")}
          for e in ev if e["event"] == "closed"]
regions = []
for u in s["units"].values():
    if u["type"] == "candidate" and u.get("roi_id"):
        d = u.get("decision") or {}
        regions.append({"roi_id": u["roi_id"], "label": u.get("label"), "review": u.get("review_label"),
                        "channel": u.get("channel"), "class": u.get("class") or d.get("artifact_class"),
                        "action": u.get("action"), "state": u["state"], "bbox": u.get("bbox"),
                        "detector": u.get("detector"), "note": (u.get("notes") or [None])[-1]})
story = {"session_id": s["session_id"], "run_s": round(iso(s["finished_at"]) - t0, 1),
         "bulk_s": round(iso(job["finished_at"]) - iso(job["started_at"]), 1),
         "units_total": len(s["units"]), "packets": pk, "answered": answered, "closed": closed,
         "regions": regions}
json.dump(story, open("story.json", "w"), indent=1)
print("run_s", story["run_s"], "bulk_s", story["bulk_s"], "units", story["units_total"], "packets", len(pk), "regions", len(regions))
for p in pk[:10] + pk[-6:]:
    print(p["id"], p["t"], p["kind"], "|", p["subject"], "|", p["narration"])
print(json.dumps(list(answered.items())[:2])[:400])
