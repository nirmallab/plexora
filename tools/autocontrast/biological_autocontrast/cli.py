"""`python -m biological_autocontrast <command>`; `--help` on any command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _config(args):
    from biological_autocontrast.config import Config

    return Config.load(getattr(args, "config", None))


def _plane_args(p):
    p.add_argument("--z", type=int, default=None, help="Z plane (default 0, with a warning on a stack)")
    p.add_argument("--t", type=int, default=None, help="timepoint (default 0)")
    p.add_argument("--assume-bits", type=int, default=None,
                   help="effective bit depth when the metadata lacks it (recorded as bits_source=user)")


def _channels(value):
    if not value:
        return None
    return [int(v) if v.strip().isdigit() else v.strip() for v in value.split(",")]


def cmd_inspect(args):
    from biological_autocontrast import features as feat
    from biological_autocontrast.io import open_image, plane_or_warn

    cfg = _config(args)
    with open_image(args.image) as image:
        z, t, warns = plane_or_warn(image, args.z, args.t)
        ctx = feat.ImageContext.build(image, z, t, cfg, warns)
        report = image.describe()
        report.update({"z": z, "t": t, "overview_level": ctx.overview_level,
                       "tissue_fraction": ctx.tissue_fraction, "tissue_source": ctx.tissue_source,
                       "warnings": warns, "domains": []})
        for c in range(image.n_channels):
            f = feat.extract_channel(ctx, c, assume_bits=args.assume_bits, with_patches=False)
            report["domains"].append({"channel_index": c, "channel_name": f.channel_name,
                                      **f.domain.to_dict()})
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    print(f"{report['path']}\n  {report['kind']} {report['dtype']} · {len(report['channels'])} channels"
          f" · z={report['n_z']} t={report['n_t']} · levels {report['levels'][:3]}..."
          f" · pixel {report['pixel_size_um']} µm · overview level {report['overview_level']}"
          f" · tissue {report['tissue_fraction']:.1%} ({report['tissue_source']})")
    for d in report["domains"]:
        print(f"  {d['channel_index']:>3} {d['channel_name']:<28} bits {d['effective_bits'] or '-':>3} "
              f"({d['bits_source']}) range {d['offset']:g}..{d['full_scale']:g} "
              f"max {d['raw_max']} sat {d['saturated_fraction']:.2%}"
              + (f"  ! {'; '.join(d['warnings'])}" if d["warnings"] else ""))
    return 0


def _labels_for(image_path):
    """{channel_index: (min, max)} from stored labels of this exact file, if any."""
    try:
        from biological_autocontrast.io import fingerprint_of as fingerprint
        from biological_autocontrast.labels import load_all

        frame = load_all()
        if frame.empty:
            return {}
        fp = fingerprint(image_path)
        frame = frame[frame["image_fingerprint"] == fp]
        return {int(r.channel_index): (float(r.display_min_raw), float(r.display_max_raw))
                for r in frame.itertuples()}
    except Exception:
        return {}


def cmd_propose(args, checkpoint=None):
    from biological_autocontrast import export, predict, review

    cfg = _config(args)
    checkpoint = checkpoint or getattr(args, "checkpoint", None)
    rows, kept, _ctx = predict.run(args.image, checkpoint, _channels(args.channels), z=args.z,
                                   t=args.t, seed=args.seed, config=cfg, force=args.force,
                                   assume_bits=args.assume_bits, keep_features=args.review_html)
    out = Path(args.output).expanduser()
    written = export.write_predictions(rows, out)
    if args.review_html:
        written["html"] = str(review.write_review(rows, kept, out, labels=_labels_for(args.image)))
    for r in rows:
        flag = f"  ABSTAIN {r['abstain_reason']}" if r["abstain"] else ""
        print(f"{r['channel_index']:>3} {r['channel_name']:<28} {r['display_min_raw']:>10.5g} "
              f"{r['display_max_raw']:>10.5g}  ({r['display_min_norm']:.4f}-{r['display_max_norm']:.4f})"
              f" {r['method']}{flag}")
    print(json.dumps(written, indent=1))
    return 0


def cmd_predict(args):
    return cmd_propose(args, checkpoint=args.checkpoint)


def cmd_labels(args):
    from biological_autocontrast import labels

    if args.labels_cmd == "pull":
        summary = labels.pull_plexora(args.project, args.annotator, data_root=args.data_root,
                                      image_path=args.image, channels=_channels(args.channels),
                                      assume_bits=args.assume_bits, cfg=_config(args))
        print(json.dumps(summary, indent=1))
    elif args.labels_cmd == "add":
        record = labels.add_label(args.image, args.channel, args.min, args.max,
                                  annotator=args.annotator, decision=args.decision,
                                  image_id=args.image_id, z=args.z or 0, t=args.t or 0,
                                  assume_bits=args.assume_bits,
                                  rationale_tags=_channels(args.tags) or (),
                                  rationale_text=args.note or "", cfg=_config(args))
        print(json.dumps({k: record[k] for k in ("label_id", "channel_name", "display_min_raw",
                                                 "display_max_raw", "decision")}, indent=1))
    elif args.labels_cmd == "export":
        path, n = labels.export_labels(args.out, args.format)
        print(f"{n} labels -> {path}")
    elif args.labels_cmd == "index":
        path, frame = labels.build_index(args.out)
        specimens = frame["specimen"].nunique() if not frame.empty else 0
        print(f"{len(frame)} labels from {specimens} specimen(s) -> {path}")
    return 0


def cmd_examples(args):
    from biological_autocontrast import examples

    summary = examples.build(args.index, args.out, seed=args.seed, cfg=_config(args))
    print(json.dumps(summary, indent=1))
    return 0


def cmd_train(args):
    from biological_autocontrast import train

    summary = train.train(args.examples, args.out, epochs=args.epochs, device=args.device,
                          seed=args.seed, holdout=args.holdout, batch_size=args.batch_size,
                          lr=args.lr, draws=args.draws, folds=args.folds,
                          use_patches=not args.no_patches, use_hist=not args.no_hist,
                          max_patches=args.max_patches)
    print(json.dumps(summary, indent=1))
    return 0


def cmd_export(args):
    from biological_autocontrast import train

    print(train.export_checkpoint(args.checkpoint, args.out))
    return 0


def cmd_evaluate(args):
    from biological_autocontrast import evaluate

    summary = evaluate.run(args.examples, args.checkpoint, args.out, cv_dir=args.cv)
    print(json.dumps(summary, indent=1))
    return 0


def cmd_synth(args):
    from biological_autocontrast.synthetic import make_all

    print(json.dumps(make_all(args.folder, seed=args.seed, size=args.size), indent=1))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="biological_autocontrast", description=__doc__)
    parser.add_argument("--config", help="YAML/JSON config overriding the defaults")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="dimensions, metadata and each channel's intensity domain")
    p.add_argument("image")
    p.add_argument("--json", action="store_true")
    _plane_args(p)
    p.set_defaults(func=cmd_inspect)

    for name, help_text in (("propose", "heuristic proposals (no model), CSV/JSON and review HTML"),
                            ("predict", "model proposals from an ONNX checkpoint")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("image")
        p.add_argument("--output", required=True, help="output folder (never a synced folder)")
        if name == "predict":
            p.add_argument("--checkpoint", required=True, help="model.onnx")
        p.add_argument("--channels", help="comma-separated indices or names")
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--force", action="store_true", help="ignore the cache")
        p.add_argument("--review-html", action="store_true")
        _plane_args(p)
        p.set_defaults(func=cmd_propose if name == "propose" else cmd_predict)

    p = sub.add_parser("labels", help="pull, add, export and index human windows")
    lsub = p.add_subparsers(dest="labels_cmd", required=True)
    q = lsub.add_parser("pull", help="windows saved in a Plexora project (read-only)")
    q.add_argument("project", help="project name, project folder, or its .db")
    q.add_argument("--annotator", required=True)
    q.add_argument("--data-root", help="Plexora data root (default: Plexora's own)")
    q.add_argument("--image", help="image path, when the project's has moved")
    q.add_argument("--channels", help="only these channel names")
    q.add_argument("--assume-bits", type=int, default=None)
    q = lsub.add_parser("add", help="one label by hand")
    q.add_argument("image")
    q.add_argument("--channel", required=True)
    q.add_argument("--min", type=float)
    q.add_argument("--max", type=float)
    q.add_argument("--decision", default="edit", choices=("accept", "edit", "reject", "no_signal"))
    q.add_argument("--annotator", required=True)
    q.add_argument("--image-id")
    q.add_argument("--tags", help="comma-separated rationale tags")
    q.add_argument("--note")
    _plane_args(q)
    q = lsub.add_parser("export")
    q.add_argument("--out", required=True)
    q.add_argument("--format", choices=("csv", "json", "parquet"), default="csv")
    q = lsub.add_parser("index", help="all labels + manifest -> labels_index.parquet")
    q.add_argument("--out")
    p.set_defaults(func=cmd_labels)

    p = sub.add_parser("examples", help="materialise training examples (plexora env)")
    p.add_argument("--index", help="labels_index.parquet (default: $AUTOCONTRAST_HOME's)")
    p.add_argument("--out", help="examples folder (default: $AUTOCONTRAST_HOME/examples)")
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=cmd_examples)

    p = sub.add_parser("train", help="train on materialised examples (torch env)")
    p.add_argument("--examples", help="examples folder")
    p.add_argument("--out", required=True)
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--lr", type=float, default=2e-3)
    p.add_argument("--draws", type=int, default=6, help="augmented draws per example per epoch")
    p.add_argument("--device", default="cpu", help="cpu | mps | cuda")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--holdout", default="auto",
                   help="auto | none | specimen | channels: what the reported metrics hold out")
    p.add_argument("--folds", type=int, default=6, help="channel-CV folds on one specimen")
    p.add_argument("--no-patches", action="store_true", help="ablation: histogram branch only")
    p.add_argument("--no-hist", action="store_true", help="ablation: patch branch only")
    p.add_argument("--max-patches", type=int, default=None,
                   help="ablation: use only the first K of each example's patches (8, 16, ...)")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("export", help="re-export a checkpoint.pt to ONNX (torch env)")
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("evaluate", help="model vs labels vs baselines (plexora env)")
    p.add_argument("--examples")
    p.add_argument("--checkpoint", help="model.onnx; omit to score the baselines alone")
    p.add_argument("--cv", help="a training run folder with cv_predictions.json")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("synth", help="write the synthetic fixtures")
    p.add_argument("folder")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--size", type=int, default=512)
    p.set_defaults(func=cmd_synth)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
