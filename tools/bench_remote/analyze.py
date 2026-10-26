"""Tables, statistics and figures from a remote-access campaign's raw rows.

    python analyze.py RESULTS_DIR [--timeout-s 120]

Reads runs.jsonl (blocks), c0.jsonl (first-ever opens), probes.jsonl and
sessions.jsonl in RESULTS_DIR; writes steps.csv, passes.csv, stats.json,
tables.md and figures/ (SVG, PDF and 300-dpi PNG) beside them. Rerunnable:
nothing is read but those files.

Conventions. The unit of replication is the block (one paired N+S run on one
waypoint set). A step that did not reach full resolution within the step
cap T is right-censored at T: it is never dropped, it counts as "not full by
T" in completion curves, and a median is reported as ">T" when more than
half of its values are censored. Paired effects are geometric-mean ratios
S/N over blocks of each block's per-arm median, with percentile-bootstrap
95% CIs (10,000 resamples, seed 1); a censored S value makes the ratio a
lower bound, and the tables say so.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

N_COLOR, S_COLOR = "#0072B2", "#E69F00"
ARM_NAME = {"N": "Data node (direct access)", "S": "SFTP streaming"}
DATA_KINDS = ("tile", "batch", "level", "brick", "section", "stats")
NAV_STEPS = ("zoom_L2_a", "zoom_L0_a", "pan_L0_b", "home", "revisit_L0_a", "jump_L0_c",
             "z_plus1", "z_plus8", "z_plus50", "anim_zoom_L2", "anim_pan_L2")
STEP_LABEL = {"open": "open", "channels": "3 channels on", "zoom_L2_a": "zoom to L2",
              "zoom_L0_a": "zoom to L0", "pan_L0_b": "pan (far)", "home": "home",
              "revisit_L0_a": "revisit", "jump_L0_c": "jump (new)", "z_plus1": "z +1",
              "z_plus8": "z +8", "z_plus50": "z +50", "anim_zoom_L2": "zoom (animated)",
              "anim_pan_L2": "pan (animated)", "volume": "3-D view"}
RNG = np.random.default_rng(1)


def load(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- tidy tables --------------------------------------------------------------------

def step_rows(row: dict, timeout_ms: float) -> list:
    out = []
    for p in row.get("passes", []):
        for s in p["steps"]:
            net = s.get("net") or {}
            data_bytes = sum((net.get(k) or {}).get("bytes", 0) for k in DATA_KINDS)
            data_disk = sum((net.get(k) or {}).get("disk", 0) for k in DATA_KINDS)
            by = net.get("byOrigin") or {}
            cap = (row.get("cap_ms") or {}).get(s["step"], timeout_ms)
            frames = s.get("frames") or {}
            out.append({
                "label": row.get("label"), "block": row.get("block", row.get("rep")),
                "arm": row["arm"], "pass": p["pass"], "step": s["step"],
                "full_ms": s.get("full"), "censored": s.get("full") is None,
                "cap_ms": cap if s["step"] not in ("open", "volume") else None,
                "first_tile_ms": s.get("firstPixel"), "meaningful_ms": s.get("meaningful"),
                "data_bytes": data_bytes, "data_from_disk_cache": data_disk,
                "node_direct_bytes": (by.get("node-direct") or {}).get("bytes", 0),
                "node_proxy_bytes": (by.get("node-proxy") or {}).get("bytes", 0),
                "local_bytes": (by.get("local") or {}).get("bytes", 0),
                "tile_requests": (net.get("tile") or {}).get("n", 0) + (net.get("batch") or {}).get("n", 0),
                "frame_p50_ms": frames.get("p50"), "frame_p95_ms": frames.get("p95"),
                "frames": frames.get("frames"), "dropped": frames.get("dropped"),
                "long_tasks": frames.get("longTasks"),
            })
    return out


def _io_sum(io: dict | None, key: str) -> int | None:
    if not isinstance(io, dict) or "error" in io:
        return None
    values = [v.get(key) for v in io.values() if isinstance(v, dict) and key in v]
    return sum(values) if values else None


def _sftp_value(counters: dict, key: str) -> int | None:
    clients = ((counters.get("sftp") or {}).get("clients") or {})
    values = [c.get(key) for c in clients.values() if isinstance(c, dict) and c.get(key) is not None]
    return sum(values) if values else None


def pass_rows(row: dict) -> list:
    marks = row.get("marks") or []
    out = []
    for i, p in enumerate(row.get("passes", [])):
        if i + 1 >= len(marks):
            continue
        a, b = marks[i][1], marks[i + 1][1]
        wire_in = wire_out = None
        if a.get("wire") and b.get("wire"):
            wire_in = b["wire"]["in"] - a["wire"]["in"]
            wire_out = b["wire"]["out"] - a["wire"]["out"]
        if row["arm"] == "N":
            ra, rb = _io_sum(a.get("node_io"), "read_bytes"), _io_sum(b.get("node_io"), "read_bytes")
            ca, cb = _io_sum(a.get("node_io"), "rchar"), _io_sum(b.get("node_io"), "rchar")
            ua, ub = _io_sum(a.get("node_io"), "utime"), _io_sum(b.get("node_io"), "utime")
            far_storage = rb - ra if ra is not None and rb is not None else None
            far_read = cb - ca if ca is not None and cb is not None else None
            far_cpu_s = (ub - ua) / 100 if ua is not None and ub is not None else None
        else:
            ra, rb = _sftp_value(a, "read_bytes"), _sftp_value(b, "read_bytes")
            far_read = rb - ra if ra is not None and rb is not None else None
            far_storage = None
            far_cpu_s = None
        steps = p["steps"]
        data = sum(s.get("data_bytes", 0) for s in step_rows({**row, "passes": [p]}, 0))
        nav = [s for s in steps if s["step"] in NAV_STEPS]
        out.append({
            "label": row.get("label"), "block": row.get("block"), "arm": row["arm"],
            "pass": p["pass"], "wire_in": wire_in, "wire_out": wire_out,
            "far_read": far_read, "far_storage_read": far_storage, "far_cpu_s": far_cpu_s,
            "app_data_bytes": data,
            "nav_steps": len(nav), "nav_full": sum(1 for s in nav if s.get("full") is not None),
            "pass_s": (p["passTo"] - p["passFrom"]) / 1000 if p.get("passTo") else None,
            "t_connect": (row.get("prep") or {}).get("t_connect"),
            "t_node_up": ((row.get("prep") or {}).get("node") or {}).get("t_node_up"),
            "rtt_ms": _rtt(row),
            "laptop_load1": (row.get("laptop_before") or {}).get("load", [None])[0],
            "laptop_pytest": (row.get("laptop_before") or {}).get("pytest"),
        })
    return out


def _rtt(row):
    r = row.get("rtt_ms") or {}
    for key in ("node_http", "transfer_tcp", "login_tcp"):
        if (r.get(key) or {}).get("median") is not None:
            return r[key]["median"]
    return None


def write_csv(path: Path, rows: list) -> None:
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


# -- statistics ----------------------------------------------------------------------

def censored_values(rows, cap_default):
    """Values with censored ones set to their cap; and how many were censored."""
    vals, cens = [], 0
    for r in rows:
        if r["full_ms"] is None:
            vals.append(r.get("cap_ms") or cap_default)
            cens += 1
        else:
            vals.append(r["full_ms"])
    return vals, cens


def describe(values, censored=0):
    if not values:
        return {"n": 0}
    a = np.asarray(values, float)
    q1, med, q3, p95 = np.percentile(a, [25, 50, 75, 95])
    boots = [np.median(RNG.choice(a, a.size)) for _ in range(5000)] if a.size > 1 else [med]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"n": int(a.size), "censored": int(censored), "median": float(med),
            "q1": float(q1), "q3": float(q3), "p95": float(p95),
            "median_ci": [float(lo), float(hi)],
            "median_is_bound": censored * 2 > a.size}


def paired_ratio(pairs):
    """Geometric-mean ratio S/N over blocks, bootstrap CI, Wilcoxon p."""
    pairs = [(n, s) for n, s in pairs if n and s and n > 0 and s > 0]
    if len(pairs) < 2:
        return {"n": len(pairs)}
    logs = np.log([s / n for n, s in pairs])
    boots = [np.exp(np.mean(RNG.choice(logs, logs.size))) for _ in range(10000)]
    out = {"n": len(pairs), "ratio": float(np.exp(logs.mean())),
           "ci": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]}
    try:
        from scipy.stats import wilcoxon

        out["wilcoxon_p"] = float(wilcoxon(logs).pvalue) if np.any(logs != 0) else 1.0
    except Exception:  # noqa: BLE001
        out["wilcoxon_p"] = None
    return out


def holm(pvalues: dict) -> dict:
    items = sorted((p, k) for k, p in pvalues.items() if p is not None)
    m, out, running = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def km_curve(values_ms, censored_flags, grid_ms):
    """Fraction of steps at full resolution by each time in `grid_ms`
    (1 - Kaplan-Meier survival of 'not yet full'; censoring at the cap)."""
    order = sorted(zip(values_ms, censored_flags))
    at_risk, surv, curve, i = len(order), 1.0, [], 0
    events = []
    for t, c in order:
        events.append((t, c))
    for g in grid_ms:
        while i < len(events) and events[i][0] <= g:
            t, c = events[i]
            if not c and at_risk > 0:
                surv *= (1 - 1 / at_risk)
            at_risk -= 1
            i += 1
        curve.append(1 - surv)
    return curve


# -- figures ---------------------------------------------------------------------------

def _style():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 8, "axes.titlesize": 9,
        "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7,
        "legend.fontsize": 7, "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#888888", "axes.linewidth": 0.6, "grid.color": "#e6e6e6",
        "grid.linewidth": 0.6, "svg.fonttype": "none", "pdf.fonttype": 42,
        "figure.dpi": 100, "savefig.bbox": "tight"})
    return plt


def save(fig, out: Path, name: str):
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("svg", "pdf", "png"):
        fig.savefig(out / f"{name}.{ext}", dpi=300 if ext == "png" else None)


def fig_steps(plt, steps, out, cap_ms, which=("C1", "C2")):
    """Per-step time to full resolution, both arms, by pass (log scale)."""
    order = ["open", "channels", *NAV_STEPS, "volume"]
    present = [s for s in order if any(r["step"] == s for r in steps)]
    fig, axes = plt.subplots(len(which), 1, figsize=(7.2, 2.4 * len(which)), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, pss in zip(axes, which):
        for j, arm in enumerate(("N", "S")):
            color = N_COLOR if arm == "N" else S_COLOR
            for i, step in enumerate(present):
                rows = [r for r in steps if r["pass"] == pss and r["arm"] == arm and r["step"] == step]
                if not rows:
                    continue
                x0 = i + (-0.18 if arm == "N" else 0.18)
                jitter = RNG.uniform(-0.07, 0.07, len(rows))
                done = [(x0 + jt, r["full_ms"] / 1000) for jt, r in zip(jitter, rows) if r["full_ms"] is not None]
                cens = [(x0 + jt, (r.get("cap_ms") or cap_ms) / 1000) for jt, r in zip(jitter, rows) if r["full_ms"] is None]
                if done:
                    ax.scatter(*zip(*done), s=12, color=color, edgecolor="white", linewidth=0.5, zorder=3,
                               label=ARM_NAME[arm] if i == 0 or step == present[0] else None)
                if cens:
                    ax.scatter(*zip(*cens), s=16, marker="^", facecolor="white", edgecolor=color,
                               linewidth=0.9, zorder=3)
                vals, n_c = censored_values(rows, cap_ms)
                med = np.median(vals) / 1000
                ax.hlines(med, x0 - 0.13, x0 + 0.13, color=color, linewidth=2, zorder=4)
        ax.set_yscale("log")
        ax.axhline(cap_ms / 1000, color="#999999", linewidth=0.6, linestyle="-")
        ax.text(len(present) - 0.5, cap_ms / 1000 * 1.08, f"step cap {cap_ms / 1000:.0f} s",
                ha="right", va="bottom", fontsize=6.5, color="#666666")
        ax.set_ylabel("time to full resolution (s)")
        ax.set_title(f"{pss}: {'cold processes' if pss == 'C1' else 'warm rerun' if pss == 'C2' else pss}",
                     loc="left")
        ax.grid(axis="y")
    axes[-1].set_xticks(range(len(present)))
    axes[-1].set_xticklabels([STEP_LABEL.get(s, s) for s in present], rotation=40, ha="right")
    from matplotlib.lines import Line2D

    handles = [Line2D([], [], marker="o", linestyle="", color=N_COLOR, label=ARM_NAME["N"]),
               Line2D([], [], marker="o", linestyle="", color=S_COLOR, label=ARM_NAME["S"]),
               Line2D([], [], marker="^", linestyle="", markerfacecolor="white",
                      markeredgecolor="#555555", color="#555555", label="not full by the cap (censored)"),
               Line2D([], [], color="#555555", linewidth=2, label="median")]
    axes[0].legend(handles=handles, loc="upper left", bbox_to_anchor=(0, 1.32), ncol=4, frameon=False)
    save(fig, out, "fig3_steps_time_to_full")
    plt.close(fig)


def fig_completion(plt, steps, out, cap_ms):
    grid = np.logspace(-1, math.log10(cap_ms / 1000), 200)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.6), sharey=True)
    for ax, pss in zip(axes, ("C1", "C2")):
        for arm in ("N", "S"):
            rows = [r for r in steps if r["pass"] == pss and r["arm"] == arm and r["step"] in NAV_STEPS]
            if not rows:
                continue
            vals = [(r["full_ms"] if r["full_ms"] is not None else r.get("cap_ms") or cap_ms) / 1000 for r in rows]
            cens = [r["full_ms"] is None for r in rows]
            curve = km_curve(vals, cens, grid)
            color = N_COLOR if arm == "N" else S_COLOR
            ax.step(grid, curve, where="post", color=color, linewidth=2)
            ax.text(grid[-1] * 1.05, curve[-1], f"{ARM_NAME[arm].split(' (')[0]}\n{curve[-1] * 100:.0f}%",
                    va="center", fontsize=6.5, color="#333333")
        ax.set_xscale("log")
        ax.set_xlabel("time since the step began (s)")
        ax.set_title(f"{pss}: navigation steps at full resolution", loc="left")
        ax.grid(True)
    axes[0].set_ylabel("fraction of steps complete")
    axes[0].set_ylim(0, 1.02)
    from matplotlib.lines import Line2D

    axes[0].legend(handles=[Line2D([], [], color=N_COLOR, linewidth=2, label=ARM_NAME["N"]),
                            Line2D([], [], color=S_COLOR, linewidth=2, label=ARM_NAME["S"])],
                   loc="upper left", frameon=False)
    save(fig, out, "fig4_completion_curves")
    plt.close(fig)


def fig_bytes(plt, passes, out):
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.6))
    metrics = [("wire_in", "over the network, into the laptop"),
               ("far_read", "read from the file by the far side")]
    for ax, (key, title) in zip(axes, metrics):
        labels = []
        for i, (arm, pss) in enumerate([("N", "C1"), ("S", "C1"), ("N", "C2"), ("S", "C2")]):
            vals = [r[key] / 1e9 for r in passes if r["arm"] == arm and r["pass"] == pss
                    and r.get(key) is not None]
            color = N_COLOR if arm == "N" else S_COLOR
            if vals:
                med = float(np.median(vals))
                ax.bar(i, med, width=0.6, color=color, alpha=0.9)
                ax.scatter(i + RNG.uniform(-0.12, 0.12, len(vals)), vals, s=9, color="#333333", zorder=3)
                ax.text(i, med, f"{med:.2f}", ha="center", va="bottom", fontsize=6.5)
            labels.append(f"{'node' if arm == 'N' else 'SFTP'}\n{pss}")
        ax.set_xticks(range(4))
        ax.set_xticklabels(labels)
        ax.set_ylabel("GB per tour")
        ax.set_title(title, loc="left")
        ax.grid(axis="y")
    save(fig, out, "fig5_bytes_per_tour")
    plt.close(fig)


def fig_open(plt, c0, steps, passes, out):
    """Getting to a usable view: connection set-up and the first full-resolution view."""
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.6))
    ax = axes[0]
    pos = 0
    ticks = []
    for arm in ("N", "S"):
        color = N_COLOR if arm == "N" else S_COLOR
        key = "t_node_up" if arm == "N" else "t_connect"
        vals = [r[key] for r in passes if r["arm"] == arm and r["pass"] == "C1" and r.get(key) is not None]
        if vals:
            ax.scatter(pos + RNG.uniform(-0.1, 0.1, len(vals)), vals, s=12, color=color, edgecolor="white", linewidth=0.5)
            ax.hlines(np.median(vals), pos - 0.2, pos + 0.2, color=color, linewidth=2)
        ticks.append("node start + tunnel" if arm == "N" else "SFTP session")
        pos += 1
    ax.set_xticks(range(len(ticks)))
    ax.set_xticklabels(ticks)
    ax.set_ylabel("seconds")
    ax.set_title("connection set-up (each cold block)", loc="left")
    ax.grid(axis="y")
    ax = axes[1]
    cats = []
    xi = 0
    for state in ("C0", "C1", "C2"):
        for arm in ("N", "S"):
            color = N_COLOR if arm == "N" else S_COLOR
            if state == "C0":
                vals = []
                for r in c0:
                    if r["arm"] != arm:
                        continue
                    imp = (r.get("import") or {}).get("seconds")
                    op = next((s.get("full") for p in r.get("passes", []) for s in p["steps"] if s["step"] == "open"), None)
                    if imp is not None and op is not None:
                        vals.append(imp + op / 1000)
            else:
                vals = [r["full_ms"] / 1000 for r in steps if r["pass"] == state and r["arm"] == arm
                        and r["step"] == "open" and r["full_ms"] is not None]
            if vals:
                ax.scatter(xi + RNG.uniform(-0.1, 0.1, len(vals)), vals, s=12, color=color, edgecolor="white", linewidth=0.5)
                ax.hlines(np.median(vals), xi - 0.2, xi + 0.2, color=color, linewidth=2)
            cats.append(f"{'node' if arm == 'N' else 'SFTP'}\n{state}")
            xi += 1
    ax.set_yscale("log")
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels(cats)
    ax.set_ylabel("seconds to first full-resolution view")
    ax.set_title("open (C0 includes the first import)", loc="left")
    ax.grid(axis="y")
    save(fig, out, "fig2_connect_and_open")
    plt.close(fig)


def fig_frames(plt, steps, out):
    fig, ax = plt.subplots(figsize=(3.6, 2.6))
    for i, step in enumerate(("anim_zoom_L2", "anim_pan_L2")):
        for arm in ("N", "S"):
            vals = [r["frame_p95_ms"] for r in steps if r["step"] == step and r["arm"] == arm
                    and r["frame_p95_ms"] is not None]
            x = i + (-0.18 if arm == "N" else 0.18)
            color = N_COLOR if arm == "N" else S_COLOR
            if vals:
                ax.scatter(x + RNG.uniform(-0.06, 0.06, len(vals)), vals, s=12, color=color, edgecolor="white", linewidth=0.5,
                           label=ARM_NAME[arm] if i == 0 else None)
                ax.hlines(np.median(vals), x - 0.12, x + 0.12, color=color, linewidth=2)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["zoom (animated)", "pan (animated)"])
    ax.set_ylabel("p95 frame interval (ms)")
    ax.set_title("frame pacing while moving", loc="left")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(axis="y")
    save(fig, out, "fig6_frame_pacing")
    plt.close(fig)


def fig_architecture(plt, probes, out):
    """Schematic of the two arms with the measured link figures."""
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    p = probes[-1] if probes else {}
    rtt_t = (p.get("tcp_transfer") or {}).get("median")
    rtt_l = (p.get("tcp_login") or {}).get("median")
    mbps = (p.get("sftp_stream") or {}).get("MBps")
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.axis("off")

    def box(x, y, w, h, text, color="#f3f3f3", edge="#888888"):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                                    facecolor=color, edgecolor=edge, linewidth=0.8))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=7)

    def arrow(x0, y0, x1, y1, color, text=None, dy=0.18):
        ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=8,
                                     color=color, linewidth=1.6))
        if text:
            ax.text((x0 + x1) / 2, (y0 + y1) / 2 + dy, text, ha="center", va="bottom", fontsize=6.5,
                    color="#333333")

    ax.text(0.1, 5.7, "A  Data node (direct access)", fontsize=8, weight="bold")
    box(0.2, 3.6, 2.2, 1.6, "Laptop\nbrowser + Plexora\n(composites tiles)")
    box(5.3, 3.6, 2.2, 1.6, "O2 compute node\nplexora node serve\n(read, decode, encode)", "#e8f1f8", N_COLOR)
    box(8.0, 3.6, 1.8, 1.6, "/n/scratch\n(NFS)\n640 GB .ims")
    arrow(5.3, 4.4, 2.4, 4.4, N_COLOR,
          f"encoded tiles (16-bit PNG)\nssh tunnel via login node\nRTT {rtt_l:.0f} ms" if rtt_l
          else "encoded tiles (16-bit PNG)\nssh tunnel via login node", dy=-0.95)
    arrow(8.0, 4.4, 7.5, 4.4, "#888888")
    ax.text(0.1, 2.9, "B  SFTP streaming (files only)", fontsize=8, weight="bold")
    box(0.2, 0.8, 2.2, 1.6, "Laptop\nbrowser + Plexora\n(read, decode, encode)", "#fdf3e1", S_COLOR)
    box(5.3, 0.8, 2.2, 1.6, "O2 transfer node\nsftp-server only\n(nothing may run)")
    box(8.0, 0.8, 1.8, 1.6, "/n/scratch\n(NFS)\n640 GB .ims")
    label = "raw HDF5 chunk bytes\n64 KiB reads, one ssh channel"
    if rtt_t:
        label += f"\nRTT {rtt_t:.0f} ms"
    if mbps:
        label += f", {mbps:.0f} MB/s peak"
    arrow(5.3, 1.6, 2.4, 1.6, S_COLOR, label, dy=-0.95)
    arrow(8.0, 1.6, 7.5, 1.6, "#888888")
    save(fig, out, "fig1_architecture")
    plt.close(fig)


# -- main ------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results")
    parser.add_argument("--timeout-s", type=float, default=120)
    args = parser.parse_args(argv)
    res = Path(args.results).expanduser()
    cap_ms = args.timeout_s * 1000
    runs = [r for r in load(res / "runs.jsonl") if not r.get("error") and r.get("passes")]
    failed = [r for r in load(res / "runs.jsonl") if r.get("error")]
    c0 = load(res / "c0.jsonl")
    probes = load(res / "probes.jsonl")
    steps = [s for r in runs for s in step_rows(r, cap_ms)]
    passes = [p for r in runs for p in pass_rows(r)]
    c0_steps = [s for r in c0 if not r.get("error") for s in step_rows(r, cap_ms)]
    write_csv(res / "steps.csv", steps + c0_steps)
    write_csv(res / "passes.csv", passes)

    stats = {"blocks": sorted({r["block"] for r in runs}), "failed_runs": len(failed),
             "cap_ms": cap_ms, "per_step": {}, "paired": {}, "completion": {}, "bytes": {},
             "c0": {}}
    for pss in ("C1", "C2"):
        for step in ["open", "channels", *NAV_STEPS, "volume"]:
            for arm in ("N", "S"):
                rows = [s for s in steps if s["pass"] == pss and s["arm"] == arm and s["step"] == step]
                if not rows:
                    continue
                vals, n_c = censored_values(rows, cap_ms)
                stats["per_step"][f"{pss}/{step}/{arm}"] = describe(vals, n_c)
            # Paired S/N over blocks.
            pairs = []
            for b in stats["blocks"]:
                n = [s for s in steps if s["block"] == b and s["pass"] == pss and s["step"] == step and s["arm"] == "N"]
                sv = [s for s in steps if s["block"] == b and s["pass"] == pss and s["step"] == step and s["arm"] == "S"]
                if n and sv:
                    nv, _ = censored_values(n, cap_ms)
                    svv, _ = censored_values(sv, cap_ms)
                    pairs.append((nv[0], svv[0]))
            if pairs:
                stats["paired"][f"{pss}/{step}"] = paired_ratio(pairs)
        for arm in ("N", "S"):
            rows = [s for s in steps if s["pass"] == pss and s["arm"] == arm and s["step"] in NAV_STEPS]
            k = sum(1 for r in rows if r["full_ms"] is not None)
            within = {}
            for t in (1, 2, 5, 10, 30, 60):
                kk = sum(1 for r in rows if r["full_ms"] is not None and r["full_ms"] <= t * 1000)
                within[f"{t}s"] = {"k": kk, "n": len(rows), "frac": kk / len(rows) if rows else None,
                                   "ci": wilson(kk, len(rows))}
            stats["completion"][f"{pss}/{arm}"] = {"k": k, "n": len(rows), "frac": k / len(rows) if rows else None,
                                                   "ci": wilson(k, len(rows)), "within": within}
            # Per-block median nav-step time (censored at the cap).
            per_block = []
            for b in stats["blocks"]:
                rb = [r for r in rows if r["block"] == b]
                if rb:
                    v, _ = censored_values(rb, cap_ms)
                    per_block.append(float(np.median(v)))
            stats["completion"][f"{pss}/{arm}"]["block_median_nav_ms"] = describe(per_block)
            pr = [p for p in passes if p["arm"] == arm and p["pass"] == pss]
            for key in ("wire_in", "wire_out", "far_read", "far_storage_read", "app_data_bytes", "far_cpu_s", "pass_s"):
                vals = [p[key] for p in pr if p.get(key) is not None]
                stats["bytes"][f"{pss}/{arm}/{key}"] = describe(vals)
    # Primary endpoints (pre-specified): open C1, per-block median nav step C1, wire bytes C1, frame p95 anim pan.
    prim = {}
    def block_pairs(fn):
        out = []
        for b in stats["blocks"]:
            n, s = fn(b, "N"), fn(b, "S")
            if n is not None and s is not None:
                out.append((n, s))
        return out
    def open_c1(b, arm):
        r = [s for s in steps if s["block"] == b and s["arm"] == arm and s["pass"] == "C1" and s["step"] == "open"]
        return r[0]["full_ms"] if r and r[0]["full_ms"] is not None else None
    def nav_c1(b, arm):
        r = [s for s in steps if s["block"] == b and s["arm"] == arm and s["pass"] == "C1" and s["step"] in NAV_STEPS]
        if not r:
            return None
        v, _ = censored_values(r, cap_ms)
        return float(np.median(v))
    def wire_c1(b, arm):
        r = [p for p in passes if p["block"] == b and p["arm"] == arm and p["pass"] == "C1"]
        return r[0]["wire_in"] if r and r[0]["wire_in"] else None
    def frames_pan(b, arm):
        r = [s for s in steps if s["block"] == b and s["arm"] == arm and s["pass"] == "C1" and s["step"] == "anim_pan_L2"]
        return r[0]["frame_p95_ms"] if r and r[0]["frame_p95_ms"] else None
    for name, fn in (("open_C1", open_c1), ("nav_step_median_C1", nav_c1),
                     ("wire_bytes_C1", wire_c1), ("frame_p95_anim_pan_C1", frames_pan)):
        prim[name] = paired_ratio(block_pairs(fn))
    adj = holm({k: v.get("wilcoxon_p") for k, v in prim.items()})
    for k, v in prim.items():
        v["holm_p"] = adj.get(k)
    stats["primary"] = prim
    for arm in ("N", "S"):
        rows = [r for r in c0 if r["arm"] == arm and not r.get("error")]
        imports = [(r.get("import") or {}).get("seconds") for r in rows]
        opens = [next((s.get("full") for p in r.get("passes", []) for s in p["steps"]
                       if s["step"] == "open"), None) for r in rows]
        wires = []
        for r in rows:
            a = ((r.get("before") or {}).get("wire") or {}).get("in")
            b = ((r.get("after_open") or {}).get("wire") or {}).get("in")
            if a is not None and b is not None:
                wires.append(b - a)
        stats["c0"][arm] = {"import_s": describe([v for v in imports if v is not None]),
                            "open_ms": describe([v for v in opens if v is not None]),
                            "open_censored": sum(1 for v in opens if v is None),
                            "wire_in_bytes": describe(wires),
                            "connect_s": describe([r.get("t_connect") or (r.get("node") or {}).get("t_node_up")
                                                   for r in rows if r.get("t_connect") or r.get("node")])}
    stats["probes"] = probes
    (res / "stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")

    plt = _style()
    figs = res / "figures"
    fig_architecture(plt, probes, figs)
    if steps:
        fig_open(plt, c0, steps, passes, figs)
        fig_steps(plt, steps, figs, cap_ms)
        fig_completion(plt, steps, figs, cap_ms)
        fig_bytes(plt, passes, figs)
        fig_frames(plt, steps, figs)
    print(json.dumps({"runs": len(runs), "failed": len(failed), "steps": len(steps),
                      "passes": len(passes), "c0": len(c0)}))


if __name__ == "__main__":
    main()
