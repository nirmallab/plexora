"""Training [torch]: `python -m biological_autocontrast train --examples DIR --out DIR`.

Runs in an environment with torch and onnx (no tifffile or plexora needed):
examples are materialised beforehand by `examples` in the plexora env.

Reported metrics are always held out: by specimen when there are >= 5, else
by channel folds on the specimen(s) there are -- and then the run says
`valid: false`, because a model that has seen one slide has not shown it
generalises to another. The shipped model.onnx is trained on everything.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from biological_autocontrast import augment, examples, metrics
from biological_autocontrast.config import MODEL_LAYOUT, VERSION

MIN_SPECIMENS = 5


def _items(meta, arrays):
    items = []
    for record in meta["examples"]:
        a = arrays[record["id"]]
        items.append({"id": record["id"], "patches": a["patches"].astype(np.float32),
                      "categories": a["categories"].astype(np.int64),
                      "pixels": a["pixels"].astype(np.float32), "hist": a["hist"],
                      "quant": a["quant"], "dom": a["dom"].astype(np.float32),
                      "dom_mask": a["dom_mask"].astype(np.float32),
                      "target_min": float(record["target_min_norm"]),
                      "target_max": float(record["target_max_norm"]),
                      "abstain": float(bool(record["abstain_target"])),
                      "weight": float(record.get("weight") or 1.0),
                      "clip": record.get("normalization_method") == "fixed_scale"})
    return items


def _batch(items, n_patches, patch_size, torch, device, max_patches=None):
    from biological_autocontrast.features import pack

    packed = pack([{"patches": i["patches"][:max_patches] if max_patches else i["patches"],
                    "categories": i["categories"], "hist": i["hist"], "quant": i["quant"],
                    "dom": i["dom"], "dom_mask": i["dom_mask"]} for i in items],
                  n_patches, patch_size)
    tensors = {k: torch.from_numpy(v).to(device) for k, v in packed.items()}
    targets = {k: torch.tensor([i[k] for i in items], dtype=torch.float32, device=device)
               for k in ("target_min", "target_max", "abstain", "weight")}
    return tensors, targets


def _loss(out, targets, torch):
    from torch.nn import functional as F

    min_p, max_p, conf, abst, span_p = out
    floor = metrics.FLOOR
    reg = 1.0 - targets["abstain"]
    w = targets["weight"]
    l_min = F.smooth_l1_loss(torch.log(min_p + floor), torch.log(targets["target_min"] + floor),
                             reduction="none", beta=0.1)
    l_max = F.smooth_l1_loss(torch.log(max_p + floor), torch.log(targets["target_max"] + floor),
                             reduction="none", beta=0.1)
    l_span = F.smooth_l1_loss(torch.log(span_p + floor),
                              torch.log(targets["target_max"] - targets["target_min"] + floor),
                              reduction="none", beta=0.1)
    regression = ((l_min + 2.0 * l_max + l_span) * reg * w).sum() / (reg * w).sum().clamp(min=1e-6)
    l_abstain = F.binary_cross_entropy(abst.clamp(1e-6, 1 - 1e-6), targets["abstain"], weight=w)
    with torch.no_grad():
        e_max = (torch.log(max_p + floor) - torch.log(targets["target_max"] + floor)).abs()
        e_min = (torch.log(min_p + floor) - torch.log(targets["target_min"] + floor)).abs()
        ok = ((e_max < metrics.TOL_MAX) & (e_min < metrics.TOL_MIN)).float()
    l_conf = F.binary_cross_entropy(conf.clamp(1e-6, 1 - 1e-6), ok, weight=w * reg + 1e-6)
    return regression + 0.2 * l_abstain + 0.1 * l_conf, float(regression.detach())


def _fit(items, *, epochs, batch_size, lr, draws, seed, device, hp, log=None, label=""):
    import torch

    from biological_autocontrast.nets import MAX_PARAMS, ContrastNet, param_count

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net = ContrastNet(use_patches=hp["use_patches"], use_hist=hp["use_hist"]).to(device)
    assert param_count(net) < MAX_PARAMS, param_count(net)
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=1e-4)
    steps = max(1, epochs * int(np.ceil(len(items) * draws / batch_size)))
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=steps, pct_start=0.15)
    history = []
    for epoch in range(epochs):
        net.train()
        order = np.concatenate([rng.permutation(len(items)) for _ in range(draws)])
        losses = []
        for start in range(0, len(order), batch_size):
            chosen = [augment.augment(items[i], rng, clip=items[i]["clip"])
                      for i in order[start:start + batch_size]]
            if len(chosen) < 2:
                continue  # BatchNorm needs two
            x, y = _batch(chosen, hp["n_patches"], hp["patch_size"], torch, device, hp.get("max_patches"))
            loss, reg = _loss(net(**x), y, torch)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
            opt.step()
            sched.step()
            losses.append(reg)
        entry = {"run": label, "epoch": epoch, "regression_loss": float(np.mean(losses)) if losses else None}
        history.append(entry)
        if log is not None:
            log.write(json.dumps(entry) + "\n")
            log.flush()
    return net.eval(), history


def _predict(net, items, hp, device):
    import torch

    out = []
    with torch.no_grad():
        for start in range(0, len(items), 16):
            chunk = items[start:start + 16]
            x, _ = _batch(chunk, hp["n_patches"], hp["patch_size"], torch, device, hp.get("max_patches"))
            min_p, max_p, conf, abst, _ = net(**x)
            for i, item in enumerate(chunk):
                out.append({"id": item["id"], "pred_min_norm": float(min_p[i]),
                            "pred_max_norm": float(max_p[i]), "confidence": float(conf[i]),
                            "abstain_prob": float(abst[i])})
    return out


def _folds(meta, mode, k, seed):
    records = meta["examples"]
    rng = np.random.default_rng(seed)
    if mode == "specimen":
        groups = sorted({r["specimen"] for r in records})
        rng.shuffle(groups)
        k = min(k, len(groups))
        assign = {g: i % k for i, g in enumerate(groups)}
        fold_of = [assign[r["specimen"]] for r in records]
    else:
        # channel folds, every copy of a marker name in one fold so a repeated
        # stain (a second DNA round) cannot leak across
        names = sorted({r["channel_name"] for r in records})
        rng.shuffle(names)
        k = min(k, len(names))
        assign = {n: i % k for i, n in enumerate(names)}
        fold_of = [assign[r["channel_name"]] for r in records]
    return [([i for i, f in enumerate(fold_of) if f != fold],
             [i for i, f in enumerate(fold_of) if f == fold]) for fold in range(k)]


def _svg_curve(history, path):
    runs = {}
    for entry in history:
        if entry["regression_loss"] is not None:
            runs.setdefault(entry["run"], []).append(entry["regression_loss"])
    if not runs:
        return
    width, height, pad = 640, 300, 30
    top = max(max(v) for v in runs.values())
    longest = max(len(v) for v in runs.values())
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
             f'<rect width="100%" height="100%" fill="white"/>'
             f'<text x="{pad}" y="18" font-size="12" font-family="sans-serif">regression loss per epoch</text>']
    for name, values in runs.items():
        pts = " ".join(f"{pad + i / max(1, longest - 1) * (width - 2 * pad):.1f},"
                       f"{height - pad - v / top * (height - 2 * pad):.1f}" for i, v in enumerate(values))
        colour = "#e8590c" if name == "final" else "#adb5bd"
        parts.append(f'<polyline fill="none" stroke="{colour}" stroke-width="{2 if name == "final" else 1}" points="{pts}"/>')
    parts.append("</svg>")
    Path(path).write_text("".join(parts), encoding="utf-8")


def train(examples_dir, out, *, epochs=60, batch_size=8, lr=2e-3, draws=8, device="cpu",
          seed=0, holdout="auto", folds=6, use_patches=True, use_hist=True, max_patches=None):
    import torch

    from biological_autocontrast.nets import export_onnx, param_count

    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    meta, arrays = examples.load(examples_dir)
    items = _items(meta, arrays)
    if len(items) < 4:
        raise ValueError(f"{len(items)} examples is too few to train on")
    specimens = sorted({r["specimen"] for r in meta["examples"]})
    mode = holdout if holdout != "auto" else ("specimen" if len(specimens) >= MIN_SPECIMENS else "channels")
    hp = {"n_patches": int(meta["n_patches"]), "patch_size": int(meta["patch_size"]),
          "use_patches": use_patches, "use_hist": use_hist, "max_patches": max_patches,
          "epochs": epochs, "batch_size": batch_size, "lr": lr, "draws": draws, "seed": seed,
          "device": device, "holdout": mode, "folds": folds}
    (out / "config.json").write_text(json.dumps({"hparams": hp, "examples_meta": {
        k: v for k, v in meta.items() if k != "examples"}, "examples_dir": str(examples_dir),
        "n_examples": len(items), "specimens": specimens}, indent=1))
    started = time.time()
    cv_rows = []
    with (out / "log.jsonl").open("w", encoding="utf-8") as log:
        history = []
        if mode != "none":
            for k, (train_idx, val_idx) in enumerate(_folds(meta, mode, folds, seed)):
                if not val_idx or len(train_idx) < 4:
                    continue
                net, h = _fit([items[i] for i in train_idx], epochs=epochs, batch_size=batch_size,
                              lr=lr, draws=draws, seed=seed + k + 1, device=device, hp=hp, log=log,
                              label=f"fold{k}")
                history += h
                for row in _predict(net, [items[i] for i in val_idx], hp, device):
                    row["fold"] = k
                    cv_rows.append(row)
        net, h = _fit(items, epochs=epochs, batch_size=batch_size, lr=lr, draws=draws, seed=seed,
                      device=device, hp=hp, log=log, label="final")
        history += h
    torch.save({"state_dict": {k: v.cpu() for k, v in net.state_dict().items()}, "hparams": hp,
                "model_layout": MODEL_LAYOUT, "package_version": VERSION}, out / "checkpoint.pt")
    model_version = f"ac-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    onnx_path = out / "model.onnx"
    export_onnx(net, onnx_path, n_patches=hp["n_patches"], patch_size=hp["patch_size"],
                metadata={"model_layout": MODEL_LAYOUT, "model_version": model_version,
                          "n_patches": hp["n_patches"], "patch_size": hp["patch_size"],
                          "package_version": VERSION})
    checkpoint_id = hashlib.sha256(onnx_path.read_bytes()).hexdigest()[:12]

    by_id = {r["id"]: r for r in meta["examples"]}
    regress = [r for r in cv_rows if not by_id[r["id"]]["abstain_target"]]
    cv = metrics.summary([r["pred_min_norm"] for r in regress], [r["pred_max_norm"] for r in regress],
                         [by_id[r["id"]]["target_min_norm"] for r in regress],
                         [by_id[r["id"]]["target_max_norm"] for r in regress]) if regress else {}
    valid = mode == "specimen" and len(specimens) >= MIN_SPECIMENS
    report = {
        "valid": valid,
        "reason": None if valid else (f"only {len(specimens)} labelled specimen(s); "
                                      f"{MIN_SPECIMENS} are needed before held-out numbers "
                                      "say anything about a new slide"),
        "holdout": mode, "cv": cv, "n_examples": len(items), "specimens": len(specimens),
        "params": param_count(net), "checkpoint_id": checkpoint_id, "model_version": model_version,
        "model_layout": MODEL_LAYOUT, "seconds": round(time.time() - started, 1),
    }
    (out / "cv_predictions.json").write_text(json.dumps(cv_rows, indent=1))
    (out / "metrics.json").write_text(json.dumps(report, indent=1))
    _svg_curve(history, out / "loss_curve.svg")
    return report


def export_checkpoint(checkpoint, out=None):
    import torch

    from biological_autocontrast.nets import ContrastNet, export_onnx

    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    hp = state["hparams"]
    net = ContrastNet(use_patches=hp["use_patches"], use_hist=hp["use_hist"])
    net.load_state_dict(state["state_dict"])
    out = Path(out) if out else Path(checkpoint).with_name("model.onnx")
    export_onnx(net, out, n_patches=hp["n_patches"], patch_size=hp["patch_size"],
                metadata={"model_layout": state["model_layout"], "model_version": "re-export",
                          "n_patches": hp["n_patches"], "patch_size": hp["patch_size"]})
    return str(out)
