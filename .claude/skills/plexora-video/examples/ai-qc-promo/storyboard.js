// Plexora AI · Automatic QC -- promo on lsp11385 (2026-10-04).
// Every number, narration line, evidence thumbnail and region below comes from
// the real QC session qs_20261004T184838_c6518e (story.json, harvested from
// its session record); the card replays its events in their real order.
(() => {
  // ---- facts from the run (story.json) -----------------------------------------
  const RUN_S = 538.0;              // created -> finished, seconds
  const FIRST_PACKET_S = 11.2;      // the bulk pass (a reused scan) was done by then
  const UNITS = 207;
  const SUMMARY = { text: "40 channels (19 clean) · 7 regions excluded · 73 flagged for a look", written: 80 };
  const STATS = { channels: 40, regions: 80, excluded: 9185, warned: 9717 };
  const LABELS = { unit_noun: "item", subject_noun: "image", finish_tool: "qc_session_finish",
    outcomes: { clean: "clean", flagged: "artifact found", confirmed_exclude: "region excluded",
                confirmed_warn: "region warned", dismissed: "not an artifact",
                manual_review_recommended: "for manual review", reviewed: "reviewed" } };
  const R = {   // featured regions (roi ids from the run)
    debris: ["qcroi_02e6199cb8"],                                   // c48 debris, exclude
    loss: ["qcroi_5e9cc9b88e", "qcroi_d2a3b7c3be"],                // c20, c50 cycle-2 loss, exclude
    auto: ["qcroi_adfb2a766b"],                                     // c33 CTLA4 autofluorescence, exclude
    review: ["qcroi_cd6357f109", "qcroi_110e1dc4d6"],              // c32, c34 CTLA4, manual review
    seg: ["qcroi_0c7ce15a0c", "qcroi_466472849c", "qcroi_d9bd17ddbf", "qcroi_cb88714e56",
          "qcroi_8eaf653937", "qcroi_b185f344a8", "qcroi_f2c15545cf", "qcroi_40aee05b96",
          "qcroi_5530ed1cb2"],
  };

  // ---- looks ----------------------------------------------------------------------
  const RED = "#ff2b2b", GREEN = "#2ee86b", NUCC = "#3a5596", CYAN = "#22d3ee";
  const OPEN = [["Nucleus", NUCC, [400, 3500]], ["CD45", RED, [700, 3500]],
                ["SOX10", GREEN, [350, 1100]], ["Ecad", CYAN, [600, 9500]]];
  // the two cycles' nuclear stains at matched windows (tissue medians 429 / 399)
  const CYCLES = [["Nucleus", RED, [600, 6000]], ["Nucleus2", GREEN, [558, 5580]]];
  const CTLA4 = [["Nucleus", NUCC, [500, 5000]], ["CTLA4", RED, [350, 2200]]];
  const SEG = [["Nucleus", "#5b7bd6", [500, 5000]]];

  // ---- session events (the card's own feed) ---------------------------------------
  const vid = () => window.PlexoraAgentBridge?.sessionId?.() || null;
  const prog = (done, bulk) => ({ units_done: done, units_total: UNITS, ...(bulk ? { bulk } : {}) });
  const scan = (stage, message, done, total) => prog(0, { state: "bulk_running", stage, message, done, total });
  const emit = (event, extra) => ({ do: "call", fn: () => {
    const payload = Object.assign({ session_id: "qs_demo", event,
      control: { url: "plugins/qc/agent_session/qs_demo/control",
                 actions: ["pause", "resume", "stop", "take_over", "limit", "detach_viewer", "attach_viewer"] } }, extra);
    if (event === "started") Object.assign(payload, { view_id: vid(), viewer_attached: true, labels: LABELS });
    window.dispatchEvent(new CustomEvent("plexora:agent-state-changed",
      { detail: { kind: "qc.session", plugin: "qc", payload } }));
  } });
  const issued = (phase, subject, narration, done, art, caption) => emit("issued", {
    phase, subject, narration, progress: prog(done, { state: "deciding" }),
    evidence: art ? [{ artifact_id: art, caption }] : [] });
  const closed = (marker, state, outcome_text) => emit("unit_closed", { marker, state, outcome_text });

  // ---- handles ------------------------------------------------------------------------
  const qc = () => window.__plexora?.plugins?.get?.("qc")?.sidebarController || null;
  const osd = () => window.__plexora?.seaDragonViewer?.viewer;
  function pageOf(ix, iy) {
    const v = osd();
    // the first tiled image's own conversion: with HD's several items in the
    // world, the viewport's is "not accurate with multi-image"
    const item = v.world.getItemAt(0);
    const p = item ? item.imageToViewerElementCoordinates(new OpenSeadragon.Point(ix, iy))
      : v.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(ix, iy));
    const r = v.canvas.getBoundingClientRect();
    return [r.left + p.x, r.top + p.y];
  }
  // A point inside a region's own traced outline (not its box), found once.
  const probes = {};
  function probe(roi, fx = 0.5, fy = 0.5) {
    if (probes[roi]) return probes[roi];
    const q = qc(); const r = q?.regionData?.regions?.find((x) => x.roi_id === roi);
    if (!r) return null;
    const b = r.bbox, cands = [];
    for (let j = 1; j < 24; j++) for (let i = 1; i < 24; i++) {
      const ix = b[0] + (b[2] - b[0]) * i / 24, iy = b[1] + (b[3] - b[1]) * j / 24;
      const was = q.hidden.has("r:" + roi); q.hidden.delete("r:" + roi);
      const h = q.overlay.hitTest(ix, iy, { tolerance: 0 });
      if (was) q.hidden.add("r:" + roi);
      if (h && h.roi_id === roi && !h.edge) cands.push([ix, iy, Math.hypot(i / 24 - fx, j / 24 - fy)]);
    }
    cands.sort((a, b2) => a[2] - b2[2]);
    probes[roi] = cands.length ? [cands[0][0], cands[0][1]] : [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
    return probes[roi];
  }
  const at = (roi, fx, fy) => () => { const p = probe(roi, fx, fy); return p ? pageOf(p[0], p[1]) : [1150, 560]; };

  // ---- timing ---------------------------------------------------------------------------
  // [t, roi ids] -- each region eases in over FADE seconds from t
  const FADE = 0.9;
  const REVEAL = [[40.0, R.debris], [51.4, R.loss], [61.8, R.auto], [69.0, R.review], [77.8, R.seg]];
  const ALL_AT = 92.0;                                   // every region, the reveal
  // [t0, t1, roi, fx, fy] -- the hover card held on a region
  const HOVER = [[41.6, 47.3, R.debris[0], 0.45, 0.55], [53.4, 57.7, R.loss[0], 0.4, 0.5],
                 [63.8, 68.6, R.auto[0], 0.4, 0.45], [70.9, 73.8, R.review[0], 0.35, 0.5],
                 [79.8, 82.8, "qcroi_cb88714e56", 0.5, 0.5]];

  PV.story({
    duration: 106.0,
    hd: true,
    async setup(h) {
      if (!window.PlexoraPaid?.allows?.("ai")) throw new Error("the AI licence is not active");
      await h.sb.applyLaunchChannels(OPEN.map(([name, color, range]) => ({ name, color, range })), { silent: true });
      // cells: none (outlines come in for the segmentation shot only)
      document.querySelector('[data-cell-mode="none"], #cell_mode_none')?.click?.();
      PV.state.fade = {};
    },

    imagePoses: {
      ov0: [22700, 26460, 79000], ov1: [22700, 26460, 70000],
      sweepA: [12500, 24000, 34000], sweepB: [36000, 30000, 34000],
      debris: [28590, 4560, 5600], debris2: [28590, 4560, 4900],
      loss: [44100, 50200, 9600], loss2: [44200, 50200, 8600],
      ctla4: [32700, 27300, 9800], ctla42: [32700, 27400, 8800],
      seg: [3900, 29400, 7000], seg2: [3900, 29400, 6200],
      ov2: [22700, 26460, 72000],
    },
    imageCam: [
      [0, "ov0"], [11.0, "ov1"], [14.0, "ov1"], [19.0, "sweepA", 0.6], [27.0, "sweepB", 1.6],
      [35.0, "ov1", 0.4], [37.0, "ov1"], [39.6, "debris", 2.2], [47.6, "debris2"],
      [48.0, "debris2"], [51.2, "loss", 2.6], [58.0, "loss2"],
      [58.4, "loss2"], [61.6, "ctla4", 1.8], [74.0, "ctla42"],
      [74.4, "ctla42"], [77.6, "seg", 2.6], [83.4, "seg2"],
      [90.6, "seg2"], [94.0, "ov2", 0.5],
    ],

    screenPoses: {
      AIB: [430, 260, 2.0], LAUN: [960, 540, 1.55], MID: [700, 560, 1.32],
      CARD: [450, 700, 1.75], THUMB: [330, 760, 2.25], HOV: [1160, 560, 1.32],
    },
    screenCam: [
      [0, "F"], [5.2, "F"], [6.4, "AIB"], [7.6, "AIB"], [8.6, "LAUN"], [10.6, "LAUN"], [11.8, "MID"],
      [14.4, "MID"], [15.6, "CARD"], [22.6, "CARD"], [23.6, "THUMB"], [35.6, "THUMB"], [36.8, "F"],
      [40.4, "F"], [41.4, "HOV"], [47.6, "HOV"], [48.6, "F"],
      [51.8, "F"], [52.8, "HOV"], [58.0, "HOV"], [59.0, "F"],
      [62.2, "F"], [63.2, "HOV"], [74.0, "HOV"], [75.0, "F"],
      [78.4, "F"], [79.4, "HOV"], [83.0, "HOV"], [84.0, "CARD"], [90.4, "CARD"], [91.6, "F"],
    ],

    cues: [
      // the launcher
      [5.4, { do: "cursor", show: true }],
      ...PV.tap(5.6, "#plexora_ai_button", { move: 1.4, pause: 0.3 }),
      ...PV.tap(8.6, '[data-action="ai-qc"]', { move: 1.3, pause: 0.3, ring: true }),
      [10.9, { do: "move", to: [1200, 760], dur: 1.0 }],
      [11.4, { do: "cursor", show: false }],
      [11.2, { do: "call", fn: () => window.PlexoraToolLoader.openTool("qc", null, { quiet: true }) }],
      ...HOVER.flatMap(([t0, t1, roi, fx, fy]) => [
        [t0 - 1.5, { do: "cursor", show: true }],
        [t0 - 1.4, { do: "move", to: at(roi, fx, fy), dur: 1.3 }],
        [t1, { do: "cursor", show: false }]]),
      [11.6, { do: "call", fn: () => {               // the panel's own check layers stay out of the way
        const q = qc(); for (const k of ["blur", "segmentation", "artifacts", "registration"]) q?.[k]?.setMuted?.(true);
      } }],
      // the bulk pass
      [11.8, emit("started", { phase: "analyzing", progress: scan("calibrating", "calibrating display") })],
      [13.6, emit("phase", { phase: "analyzing", progress: scan("scanning", "scanned Nucleus", 1, 40) })],
      [14.8, emit("phase", { phase: "analyzing", progress: scan("scanning", "scanned CD68", 16, 40) })],
      [16.0, emit("phase", { phase: "analyzing", progress: scan("scanning", "scanned TIGIT", 34, 40) })],
      [17.2, emit("phase", { phase: "analyzing", progress: scan("detectors", "folds", 2, 9) })],
      [18.4, emit("phase", { phase: "analyzing", progress: scan("detectors", "aggregates", 7, 9) })],
      [19.4, emit("phase", { phase: "analyzing", progress: scan("candidates", "ranking candidates") })],
      [20.4, emit("phase", { phase: "analyzing", progress: scan("checks", "blur check: Nucleus", 1, 4) })],
      [21.6, emit("phase", { phase: "analyzing", progress: scan("checks", "registration check: Nucleus2", 3, 4) })],
      // the channel audit
      [23.2, issued("auditing", "16 channels", "I'm reviewing 16 channels for staining and imaging problems.", 32,
                    "art_b930073f1ebaa4fdd0e5", "channel audit 1/3")],
      [26.0, issued("auditing", "16 channels", "I'm reviewing 16 channels for staining and imaging problems.", 35,
                    "art_3b1e5dc3c276dd561ee7", "channel audit 2/3")],
      [27.6, issued("auditing", "8 channels", "I'm reviewing 8 channels for staining and imaging problems.", 45,
                    "art_b967c90bb630c9e6f492", "channel audit 3/3")],
      // the checks' score reviews
      [29.4, issued("reviewing", "blur · Nucleus", "I'm checking tiles across the blur score in Nucleus.", 59,
                    "art_8cc7c00377b6e6c2374b", "places across the blur score")],
      [31.8, issued("reviewing", "registration mismatch · Nucleus2",
                    "I'm checking tiles across the registration mismatch score in Nucleus2.", 65,
                    "art_810885990968bdbde38e", "Nucleus red, Nucleus2 green")],
      [34.0, issued("reviewing", "segmentation problems · Nucleus",
                    "I'm checking tiles across the segmentation problems score in Nucleus.", 74,
                    "art_ae456afdda87969904a9", "the mask's outlines on the DNA")],
      // debris
      [36.6, issued("inspecting", "c45, c46, c47, c48 · tissue loss in a cycle",
                    "I'm checking 4 suspected artifacts side by side.", 112, "art_14701804868472745b0f",
                    "four candidates, one row each")],
      [37.4, { do: "channels", list: CYCLES }],
      [42.6, closed("c48 debris or foreign object", "confirmed_exclude", "region excluded")],
      // cycle-2 loss
      [48.2, issued("inspecting", "c56, c18, c19, c20 · tissue loss in a cycle",
                    "I'm checking 4 suspected artifacts side by side.", 144, "art_66bcb419f8ad77a482f7",
                    "four candidates, one row each")],
      [54.2, closed("c20 tissue loss in a cycle", "confirmed_exclude", "region excluded")],
      // CTLA4
      [58.6, issued("inspecting", "c33 · antibody aggregate · CTLA4",
                    "I'm checking a suspected antibody aggregate in CTLA4.", 186, "art_9b64b1e342e4ecf34ac7",
                    "CTLA4 beside the nuclear stain")],
      [59.4, { do: "channels", list: CTLA4 }],
      [64.6, closed("c33 autofluorescence", "confirmed_exclude", "region excluded")],
      [71.4, closed("c32 antibody aggregate", "manual_review_recommended", "for manual review")],
      // segmentation, cells
      [74.6, issued("inspecting", "seg_under, seg_over, seg_size, seg_shape",
                    "Checking the cells beside 4 cell-QC cutoffs at once.", 194, "art_bac2b11e60dc8d5de294",
                    "cells beside each cutoff")],
      [75.4, { do: "channels", list: SEG }],
      // the self-check
      [83.6, issued("reviewing", "the whole image", "Reviewing the whole QC picture before I close.", 206,
                    "art_14ec09122c4d651f9280", "every region on the tissue")],
      [85.4, issued("inspecting", "tile seam · CD16", "I'm checking a suspected tile seam in CD16.", 203,
                    "art_0130bf1a7c82cc7c6710", "a second look")],
      [86.6, issued("inspecting", "c35 · antibody aggregate · TIGIT",
                    "I'm checking a suspected antibody aggregate in TIGIT.", 204, "art_5316a306169ea89ff7bd", "a second look")],
      [87.8, issued("inspecting", "c40 · antibody aggregate · CXCL10",
                    "I'm checking a suspected antibody aggregate in CXCL10.", 205, "art_817f095d3c7264486781", "a second look")],
      [88.6, issued("reviewing", "the whole image", "Reviewing the whole QC picture before I close.", 206,
                    "art_9bdfb5547484f28ceb4a", "every region on the tissue")],
      [89.6, emit("finished", { reason: "closed", state: "done", phase: "summarizing",
                                 progress: prog(UNITS), summary: SUMMARY })],
      // the reveal
      [91.0, { do: "channels", list: OPEN }],
    ],

    tracks(s, n, h) {
      const q = qc();
      // the rollback hint under Done is for the user's own runs, not a film
      const hint = document.querySelector("#plexora_ai_dock .plx-agent-hint");
      if (hint) hint.style.display = "none";
      if (!q || !q.regionData) return;
      // -- which regions are on: a pure function of time, re-asserted every frame
      //    (the panel reloads after each answer and would show its own choice)
      const fade = {};
      for (const [t0, ids] of REVEAL) for (const id of ids) fade[id] = PV.clamp01((s - t0) / FADE);
      const all = PV.clamp01((s - ALL_AT) / 1.2);
      const regions = q.regionData.regions || [];
      const want = new Set();
      let any = false;
      for (const r of regions) {
        const a = Math.max(fade[r.roi_id] || 0, all);
        PV.state.fade[r.roi_id] = a;
        if (a > 0) any = true; else want.add("r:" + r.roi_id);
      }
      if (!any) want.add("g:regions");
      const cats = new Set(regions.map((r) => "c:" + r.category));
      for (const c of cats) {
        const on = regions.some((r) => "c:" + r.category === c && (PV.state.fade[r.roi_id] || 0) > 0);
        if (!on) want.add(c);
      }
      const keep = [...q.hidden].filter((k) => !k.startsWith("r:") && !k.startsWith("c:") && k !== "g:regions");
      const next = new Set([...keep, ...want]);
      const same = next.size === q.hidden.size && [...next].every((k) => q.hidden.has(k));
      if (!same) { q.hidden = next; q.redraw(); }
      // -- fades: the overlay draws each region at its fade (wrapped once)
      if (!q.overlay.__pvWrapped) {
        const orig = q.overlay.drawRegion.bind(q.overlay);
        const rgba = (hex, a) => {
          const v = parseInt(String(hex).replace("#", ""), 16);
          return `rgba(${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}, ${a})`;
        };
        q.overlay.drawRegion = (ctx, region, zoom) => {
          const a = PV.state.fade[region.roi_id] ?? 1;
          if (a <= 0) return;
          orig(ctx, a >= 1 ? region : { ...region, color: rgba(region.color || "#9ca3af", a) }, zoom);
        };
        q.overlay.__pvWrapped = true;
      }
      if (Object.values(PV.state.fade).some((a) => a > 0 && a < 1)) q.overlay.schedule();
      // -- the hover card: the real card for the region under the pointer
      const hv = q.hover;
      const cur = HOVER.find(([t0, t1]) => s >= t0 && s < t1);
      if (cur && hv) {
        // OSD reports a pointer "leave" for a still pointer over the canvas,
        // and the card's clear() would take it down between frames: held here.
        if (!hv.__pvClear) { hv.__pvClear = hv.clear; hv.clear = () => {}; }
        const p = probe(cur[2], cur[3], cur[4]);
        if (p) {
          const [px, py] = pageOf(p[0], p[1]);
          const cr = osd().canvas.getBoundingClientRect();
          hv.position = new OpenSeadragon.Point(px - cr.left, py - cr.top);
          hv.resolve();
          if (!PV.state.move) PV.state.cur = [px, py];   // the pointer rides with the tissue
        }
        PV.state.hovering = true;
      } else if (PV.state.hovering && hv) {
        if (hv.__pvClear) { hv.clear = hv.__pvClear; delete hv.__pvClear; }
        hv.clear();
        PV.state.hovering = false;
      }
      // -- cell outlines for the segmentation shot, eased 0 -> 100 %
      const mode = s >= 75.6 && s < 90.4 ? "outlines" : "none";
      const btn = document.querySelector(`[data-cell-mode="${mode}"]`);
      if (btn && btn.getAttribute("aria-checked") !== "true" && !btn.disabled) btn.click();
    },

    captions: [
      [13.0, 22.4, "Every channel scored first: focus, registration, segmentation."],
      [24.6, 29.0, "19 of 40 channels clean."],
      [85.0, 89.4, "Self-check: three second looks"],
      [94.0, 99.4, `${STATS.excluded.toLocaleString("en-US")} cells excluded · ${STATS.warned.toLocaleString("en-US")} to review`],
    ],
    hud: {
      label: "Plexora AI · QC", from: 12.0, to: 99.6,
      chapters: [[12.0, "Calibrating"], [13.6, "Scanning channels"], [17.2, "Looking for artifacts"],
                 [20.4, "Running checks"], [23.2, "Channel audit"], [29.4, "Score review"],
                 [36.6, "Artifacts"], [74.6, "Cells"], [83.6, "Final review"], [89.6, "Done"]],
      total: UNITS,
      progress: (s) => {
        const steps = [[23.2, 32], [26.0, 35], [27.6, 45], [29.4, 59], [31.8, 65], [34.0, 74], [36.6, 112],
                       [48.2, 144], [58.6, 186], [74.6, 194], [83.6, 202], [85.4, 203], [86.6, 204],
                       [87.8, 205], [88.6, 206], [89.6, UNITS]];
        let v = 0; for (const [t, d] of steps) if (s >= t) v = d; return v;
      },
      clock: { segments: [[11.8, 0], [23.2, FIRST_PACKET_S], [89.6, RUN_S]] },
    },
    endCard: { at: 100.0, dim: 0.84, brand: "Plexora AI", main: "Automatic QC", sub: "Visual AI for spatial biology.",
               stats: [[STATS.channels, "channels audited", "int"], [STATS.regions, "regions flagged", "int"],
                       [STATS.excluded, "cells excluded", "int"], [RUN_S, "start to finish", "time"]] },

    // -- sound: narration lines (vo/<id>.wav) and effects, read by mix.py
    voice: [[1.2, "v01"], [6.0, "v02"], [12.8, "v03"], [24.0, "v04"], [29.8, "v05"], [40.0, "v06"],
            [51.6, "v07"], [62.0, "v08"], [69.4, "v09"], [78.0, "v10"], [84.0, "v11"], [93.0, "v12"],
            [101.2, "v13"]],
    sfx: [[40.0, "reveal_exclude"], [51.4, "reveal_exclude"], [61.8, "reveal_exclude"], [69.0, "reveal_warn"],
          [77.8, "reveal_warn"], [92.0, "reveal_all"], [89.6, "done"], [100.0, "end"],
          [23.2, "card"], [29.4, "card"], [36.6, "card"], [48.2, "card"], [58.6, "card"], [74.6, "card"],
          [83.6, "card"]],
  });
})();
