"""The reader audit (CANDIDATES_PLAN.md, E0.5): cell panels of a person's
labelled cells, read blind by fresh model conversations, scored against the
person's words. No session, nothing stored in the data root.

    PYTHONPATH=.:../spatialagents/src python tools/bench_gates/reader_audit.py render \
        --labels RUNS/2026-10-09-labels --name lsp --out OUT
    python tools/bench_gates/reader_audit.py briefs --out OUT --pass A:new --pass D:old ...
    python tools/bench_gates/reader_audit.py score --out OUT

`render` draws one panel per marker the way a live look does
(`hypotheses.packet`): the marker's labelled near-gate cells (the label
page's repeats left out) plus four clear negatives below them as sentinels,
shuffled and numbered (`panel.arrange`), the 2 x 2 blocks with the
partners `partner_plan` gives on the image's panel context and inferred
biology, the partners windowed at the label page's gates (a person's or the
scoring proposal). Writes `OUT/panels/<name>/<marker>.png` and
`OUT/key/<name>.json` (number -> cell id, sentinel, the person's word,
crowded); the readers are only ever pointed at the panels. `--window`
draws the marker otherwise, still blind: `floor:F` sets the morphology
tile's floor (`crops.MORPHOLOGY_FLOOR`, 0.5) to F, so a dim cell's own
stain is stretched further; `span:F` puts the marker's white at F of its
calibrated span (black unchanged: the negatives), which brightens the
context tile and lowers the morphology floor with it. `--crop-um` draws a wider (or narrower) field around each cell at the same
tile size (default 24 um). `--seed-name` keeps
another render's numbering, so two windows show the same cells in the same
places. Each panel also records how it drew the marker (`drawn`): the
window and curve, and per cell its own disc p99, the level the context
window draws that at (0 black, 1 white) and whether the morphology tile
floored it -- what a rule could read before any look.

`briefs` writes one brief per (pass, set): the gate-packets rules, the
reading guide's panel texts (`new`: today's; `old`: the partner sentence of
283fbb92, partners decisive), and per panel its question and context. A
pass is read by one fresh conversation per set (a live worker reads up to
12 packets), which writes `OUT/answers/<pass>_<name>.json`.

`score` reads every answer file: per pass the share answered, accuracy and
balanced accuracy on answered cells (the person's `own` against `glow` and
`none`; their `unsure` left out), the reader's own-call rate minus the
person's on the same cells (leniency, + = more lenient), sentinels called
own, the share `uncertain` (or `artifact`) by the person's word -- the
reads Plexora drops are only harmless when that does not depend on the
word; per pair of passes the cells read alike, Cohen's kappa where both
answered, and per marker the flips each way -- flips that all run one way
within a marker are a conversation's standard moving, flips both ways are
cell-level noise.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import zlib
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

PERSON = {"own": 1, "glow": 0, "none": 0}
READER = {"own_stain": 1, "not_own_stain": 0}
OLD_PANEL_M = (
    "each cell is a 2 x 2 block. Top: morphology (the marker windowed on this cell's own "
    "brightest pixels) and context (one fixed window for the marker, the judged cell "
    "outlined white). Bottom: the expected partner (`partner_panels.expected`) and the "
    "not-expected one (`partner_panels.not_expected`) alone in grey, the judged cell "
    "outlined magenta, black when there is none. An expected partner lit inside the "
    "outline supports the cell's own stain; a not-expected one says it is another cell "
    "type, so marker light on it is likely a neighbour's")
SKILL = (
    "You answer a Plexora gating session's cell panels. Every numbered cell gets a word "
    "(`cell_panel`: no gate, value or direction is shown or asked). `uncertain` beats a "
    "guess: a wrong word costs more.")


def _session(project):
    from plexora.agent import AgentSession, registry

    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    return session, session.data(project)


def render(args) -> int:
    import numpy as np

    from plexora.agent.evidence import collage
    from plexora.agent.evidence import crops as cropmod
    from plexora.plugins.gating.server.autogate import biology, context, packets, partners, views
    from plexora.plugins.gating.server.autogate.hypotheses import panel
    from plexora.server.utils import pixel_scale

    folder = Path(args.labels)
    key = json.loads((folder / "key.json").read_text())
    words = json.loads((folder / "labels.json").read_text())["labels"]
    project = key["project"]
    session, ds = _session(project)
    kind, _sep, amount = (args.window or "calibrated").partition(":")
    resolve = collage.resolve_windows
    if kind == "floor":
        cropmod.MORPHOLOGY_FLOOR = float(amount)
    elif kind != "span" and kind != "calibrated":
        raise SystemExit(f"--window is calibrated, floor:F or span:F, not {args.window}")
    try:
        base = context.for_project(ds, create=False)
        bio = biology.infer(base)
        entries = (biology.overlay(base, bio).get("entries") or {})
        by_marker = defaultdict(list)
        for tid, tile in key["tiles"].items():
            if tid.startswith("r") or tid not in words:
                continue
            by_marker[tile["marker"]].append((int(tile["cell_id"]), words[tid]["label"],
                                              tile.get("gate"), tile.get("value")))
        gates = {m: float(st.median(g for _c, _w, g, _v in rows if g is not None))
                 for m, rows in by_marker.items() if any(r[2] is not None for r in rows)}
        channels = {m: views.image_channel(ds, m) for m in ds.table.markers}
        pixel = pixel_scale.pixel_size(session.project(project))
        out = Path(args.out)
        (out / "panels" / args.name).mkdir(parents=True, exist_ok=True)
        (out / "key").mkdir(parents=True, exist_ok=True)
        record = {"project": project, "labels": str(folder), "biology": bio,
                  "window": args.window or "calibrated", "crop_um": args.crop_um,
                  "panels": {}}
        for marker in sorted(by_marker):
            rows = by_marker[marker]
            ids = [c for c, *_ in rows]
            word = {c: w for c, w, *_ in rows}
            floor = min(float(v) for *_x, v in rows if v is not None)
            seed = zlib.crc32(f"{args.seed_name or args.name}:{marker}".encode()) & 0x7FFFFFFF
            if kind == "span":
                def spanned(session_, record_, names, _channel=channels[marker]):
                    out_ = resolve(session_, record_, names)
                    if _channel in out_:
                        lo, hi = out_[_channel]["window"]
                        out_[_channel] = {**out_[_channel],
                                          "window": [lo, lo + float(amount) * (hi - lo)]}
                    return out_
                collage.resolve_windows = spanned
            sampled = panel.panel_cells(ds, marker, [], ids=ids, n_sentinels=4,
                                        sentinel_below=floor - 0.2, seed=seed,
                                        crowding_margin=0.2)
            arranged = panel.arrange(sampled["cells"], sampled["sentinels"], seed)
            present = {m for m, c in channels.items() if c and m != marker}
            plan = partners.partner_plan(entries, marker, present, {}, 1, set(gates) - {marker})
            pp = {"markers": {}, "gates": {}}
            for p in plan:
                if p["kind"] in pp["markers"]:
                    continue
                pp["markers"][p["kind"]] = p["marker"]
                pp[p["kind"]] = channels[p["marker"]]
                if p["marker"] in gates:
                    pp["gates"][channels[p["marker"]]] = gates[p["marker"]]
            pp = pp if pp["markers"] else {}
            layout = "t2m" if pp else "t2"
            drawn = panel.rows_of([{k: c[k] for k in ("cell_id", "x", "y", "caption", "number")}
                                   for c in arranged], views._per_row(layout))
            picture = collage.render_collage(
                session, ds, layout=layout, rows=drawn, marker=channels[marker], gate=None,
                fmt="png", store=False, pixel=pixel, partners=pp or None,
                crop_um=args.crop_um,
                title=f"{marker} · numbered cells in random order (each cell: "
                      f"{packets._panel_words(layout, pp) or 'morphology | context'})")
            (out / "panels" / args.name / f"{marker}.png").write_bytes(picture["png"])
            entry = entries.get(marker) or {}
            drawn = _drawn(picture["manifest"], channels[marker])
            missing = sorted(set(ids) - {int(c["cell_id"]) for c in sampled["cells"]})
            record["panels"][marker] = {
                "layout": layout, "dropped": picture["manifest"].get("dropped_cells"),
                "partners": pp.get("markers") or {},
                "compartment": entry.get("compartment"), "lineage": entry.get("lineage"),
                "fibrous": entry.get("morphology") == "fibrous",
                "not_drawn": missing, "drawn": drawn,
                "cells": {c["number"]: {"cell_id": int(c["cell_id"]),
                                        **({"sentinel": True} if c.get("sentinel")
                                           else {"person": word[int(c["cell_id"])]}),
                                        **({"crowded": int(c["crowded"])}
                                           if c.get("crowded") is not None else {})}
                          for c in arranged}}
            print(f"{args.name} {marker}: {len(arranged)} cells ({len(missing)} not drawn), "
                  f"{layout}, partners {pp.get('markers')}", flush=True)
        (out / "key" / f"{args.name}.json").write_text(json.dumps(record, indent=1))
    finally:
        collage.resolve_windows = resolve
        session.close()
    return 0


def _drawn(manifest, channel):
    """{window, curve, cells: {cell id: {own_p99, level, floored}}}."""
    from plexora.server.utils import display_curve

    found = next((c for c in manifest.get("channels") or () if c.get("name") == channel), None)
    if found is None:
        return None
    window, curve = found["window"], found.get("curve")
    cells = {}
    for row in manifest.get("rows") or ():
        for cell in row.get("cells") or ():
            info = cell.get("morphology") or {}
            if "own_p99" not in info:
                continue
            cells[str(cell["cell_id"])] = {
                "own_p99": info["own_p99"], "floored": bool(info.get("floored")),
                "level": round(display_curve.level(info["own_p99"], window, curve), 4)}
    return {"window": window, "curve": curve, "cells": cells}


def _guide(variant, fibrous, layout="t2m"):
    from plexora.plugins.gating.server.autogate import packets

    texts = [("panel", packets.READING_GUIDE["panel"])]
    if layout == "t2m":
        texts.append(("collage_panel_m", OLD_PANEL_M if variant == "old"
                      else packets.READING_GUIDE["collage_panel_m"]))
    else:
        texts.append(("collage_panel", packets.READING_GUIDE["collage_panel"]))
    if fibrous:
        texts.append(("panel_fibrous", packets.READING_GUIDE["panel_fibrous"]))
    return texts


def briefs(args) -> int:
    out = Path(args.out)
    (out / "briefs").mkdir(exist_ok=True)
    (out / "answers").mkdir(exist_ok=True)
    for spec in args.passes:
        name, variant = spec.split(":")
        for keyfile in sorted((out / "key").glob("*.json")):
            if args.sets and keyfile.stem not in args.sets.split(","):
                continue
            record = json.loads(keyfile.read_text())
            image = keyfile.stem
            answer = (out / "answers" / f"{name}_{image}.json").resolve()
            parts = [SKILL, "",
                     "Read each panel image below with the Read tool, one at a time, and answer "
                     "its packet: one word for EVERY numbered cell, from own_stain, "
                     "not_own_stain, uncertain, artifact. Read only the files named here.", ""]
            guide_shown = set()
            for marker, p in record["panels"].items():
                guide = _guide(variant, p["fibrous"], p.get("layout", "t2m"))
                stain = f"{p['compartment']} {marker}" if p["compartment"] else marker
                packet = {
                    "question": (f"{marker} ({record['project']}): for EVERY numbered cell, "
                                 f"does it carry its OWN {stain} stain, around the nucleus the "
                                 "ticks mark? One word per cell."),
                    "evidence": {"marker": marker, "cells": len(p["cells"]),
                                 **({"partner_panels": {
                                     "expected": p["partners"].get("co"),
                                     "not_expected": p["partners"].get("anti")}}
                                    if p["partners"] else {}),
                                 "context": {"compartment": p["compartment"],
                                             **({"lineage": p["lineage"]}
                                                if p["lineage"] else {})},
                                 "guide": [k for k, _t in guide]},
                    "allowed": {"cells": sorted(p["cells"], key=int),
                                "labels": ["own_stain", "not_own_stain", "uncertain",
                                           "artifact"]}}
                for k, text in guide:
                    if k not in guide_shown:
                        parts += [f"Reading guide `{k}`: {text}", ""]
                        guide_shown.add(k)
                image_path = (out / "panels" / image / f"{marker}.png").resolve()
                parts += [f"## Packet {marker}", f"image: {image_path}",
                          "```json", json.dumps(packet, indent=1), "```", ""]
            parts += [f"When every packet is answered, write ONE JSON file with the Write tool "
                      f"at {answer}, shaped exactly",
                      '{"<marker>": {"1": "own_stain", "2": "not_own_stain", ...}, ...}',
                      "with every numbered cell of every packet. Then reply with just \"done\" "
                      "and the number of cells answered."]
            path = out / "briefs" / f"{name}_{image}.md"
            path.write_text("\n".join(parts))
            print(path)
    return 0


def _kappa(pairs):
    n = len(pairs)
    if not n:
        return None
    agree = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    return None if chance >= 1 else (agree - chance) / (1 - chance)


def score(args) -> int:
    out = Path(args.out)
    keys = {f.stem: json.loads(f.read_text()) for f in sorted((out / "key").glob("*.json"))}
    answers = {}
    for f in sorted((out / "answers").glob("*.json")):
        name, image = f.stem.split("_", 1)
        answers.setdefault(name, {})[image] = json.loads(f.read_text())
    report = {"passes": {}, "pairs": {}}
    print("| pass | set | cells | answered | acc (answered) | bal acc | sens | spec | "
          "leniency | uncertain crowded / isolated | uncertain own / glow / none | "
          "sentinels own |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    calls = {}
    for name in sorted(answers):
        for image, record in keys.items():
            got = answers[name].get(image)
            if got is None:
                continue
            rows, sentinel_own, sentinels = [], 0, 0
            by_word = defaultdict(lambda: [0, 0])
            unc = {0: [0, 0], 1: [0, 0]}
            for marker, p in record["panels"].items():
                said = got.get(marker) or {}
                for number, cell in p["cells"].items():
                    word = said.get(str(number))
                    calls[(name, image, marker, number)] = word
                    if cell.get("sentinel"):
                        sentinels += 1
                        sentinel_own += word == "own_stain"
                        continue
                    if cell["person"] not in PERSON:
                        continue
                    by_word[cell["person"]][1] += 1
                    by_word[cell["person"]][0] += word not in READER
                    crowd = cell.get("crowded")
                    if crowd is not None:
                        unc[crowd][1] += 1
                        unc[crowd][0] += word not in READER
                    rows.append((marker, PERSON[cell["person"]], READER.get(word)))
            answered = [(m, y, r) for m, y, r in rows if r is not None]
            if not rows:
                continue
            pos = [r for _m, y, r in answered if y == 1]
            neg = [r for _m, y, r in answered if y == 0]
            sens = sum(pos) / len(pos) if pos else None
            spec = (len(neg) - sum(neg)) / len(neg) if neg else None
            lenient = (sum(r for *_x, r in answered) - sum(y for _m, y, _r in answered)) \
                / max(1, len(answered))
            res = {"cells": len(rows), "answered": len(answered) / len(rows),
                   "acc": sum(y == r for _m, y, r in answered) / max(1, len(answered)),
                   "bal": None if sens is None or spec is None else (sens + spec) / 2,
                   "sens": sens, "spec": spec, "leniency": lenient,
                   "uncertain_crowded": unc[1][0] / unc[1][1] if unc[1][1] else None,
                   "uncertain_isolated": unc[0][0] / unc[0][1] if unc[0][1] else None,
                   "sentinels_own": sentinel_own / sentinels if sentinels else None,
                   "uncertain_by_word": {w: round(a / b, 3) for w, (a, b) in by_word.items()
                                         if b}}
            report["passes"][f"{name}_{image}"] = res
            fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
            print(f"| {name} | {image} | {res['cells']} | {res['answered']:.0%} | "
                  f"{res['acc']:.3f} | {fmt(res['bal'])} | {fmt(sens)} | {fmt(spec)} | "
                  f"{lenient:+.3f} | {fmt(res['uncertain_crowded'])} / "
                  f"{fmt(res['uncertain_isolated'])} | "
                  + " / ".join(fmt(res["uncertain_by_word"].get(w)) for w in ("own", "glow",
                                                                             "none"))
                  + f" | {fmt(res['sentinels_own'])} |")
    print("\n| pair | set | same word | kappa (both answered) | markers with >= 3 flips | "
          "of them one-way (>= 80 %) |")
    print("|---|---|---|---|---|---|")
    for a, b in combinations(sorted(answers), 2):
        for image, record in keys.items():
            same, both, flips = 0, [], defaultdict(Counter)
            n = 0
            for marker, p in record["panels"].items():
                for number, cell in p["cells"].items():
                    if cell.get("sentinel"):
                        continue
                    wa = calls.get((a, image, marker, number))
                    wb = calls.get((b, image, marker, number))
                    if wa is None or wb is None:
                        continue
                    n += 1
                    same += wa == wb
                    ra, rb = READER.get(wa), READER.get(wb)
                    if ra is not None and rb is not None:
                        both.append((ra, rb))
                        if ra != rb:
                            flips[marker]["up" if rb > ra else "down"] += 1
            if not n:
                continue
            busy = {m: c for m, c in flips.items() if sum(c.values()) >= 3}
            oneway = [m for m, c in busy.items() if max(c.values()) / sum(c.values()) >= 0.8]
            k = _kappa(both)
            report["pairs"][f"{a}-{b}_{image}"] = {
                "same": same / n, "kappa": k,
                "flips": {m: dict(c) for m, c in flips.items()}, "one_way": oneway}
            print(f"| {a}-{b} | {image} | {same / n:.0%} | "
                  f"{'-' if k is None else f'{k:.2f}'} | {len(busy)} | "
                  f"{len(oneway)} {sorted(oneway)} |")
    (out / "score.json").write_text(json.dumps(report, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--labels", required=True)
    r.add_argument("--name", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--window", default=None, metavar="calibrated|floor:F|span:F")
    r.add_argument("--seed-name", default=None)
    r.add_argument("--crop-um", type=float, default=None)
    b = sub.add_parser("briefs")
    b.add_argument("--out", required=True)
    b.add_argument("--pass", dest="passes", action="append", required=True,
                   metavar="NAME:new|old")
    b.add_argument("--sets", default=None, help="comma-separated key names (default: all)")
    s = sub.add_parser("score")
    s.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return {"render": render, "briefs": briefs, "score": score}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
