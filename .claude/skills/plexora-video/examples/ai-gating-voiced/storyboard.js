// Plexora AI · Auto-Thresholding -- voiced promo on lsp11385 (v4, 2026-10-04).
// Replays the real MCP gating session gs_20261004T041222_d3e818: every gate,
// proposal, confidence, outcome and evidence sheet below is from its
// session.json / decisions.jsonl. The slider starts at Plexora's own scored
// proposal (decisions.jsonl "scored"), then moves only where the AI moved it.
(() => {
  // ---- the real session ----------------------------------------------------------
  // prop: Plexora's scored proposal; final: the written gate.
  const G = {
    CD45: { prop: 7.28, final: 7.27, conf: "moderate", state: "accepted", method: "ai_accepted" },
    SOX10: { prop: 6.58, final: 6.58, conf: "high", state: "accepted", method: "gmm" },
    Ecad: { prop: 6.61, final: 6.53, conf: "moderate", state: "accepted", method: "ai_refined" },
    CD3e: { prop: 6.63, final: 6.62, conf: "low", state: "accepted_low_confidence", method: "ai_refined" },
    CD20L: { prop: 6.40, final: 9.79, conf: "low", state: "no_positive_population", method: "no_positive_population" },
    CD68: { prop: 5.48, final: 5.47, conf: "manual_review", state: "manual_review_recommended", method: "needs_review" },
    CD8a: { prop: 5.83, final: 5.83, conf: "manual_review", state: "manual_review_recommended", method: "needs_review" },
    Ki67: { prop: 6.47, final: 6.35, conf: "moderate", state: "accepted", method: "ai_refined" },
  };
  const RUN_S = 181;                     // 04:12:22.7 -> 04:15:23.6
  const CELLS = 202049;
  const SUMMARY = { units_total: 8, units_done: 8, accepted: 5, accepted_low_confidence: 1, review: 2,
                    empty: 1, failed: 0, skipped: 0, written: 8, replayed: 0 };

  // ---- looks (tuned per combination for the v3 promo; outlines at 100 %) ----------
  const RED = "#ff2b2b", GREEN = "#2ee86b", NUCC = "#3a5596", CYAN = "#22d3ee";
  const OPEN = [["Nucleus", NUCC, [400, 3500]], ["CD45", RED, [700, 3500]],
                ["SOX10", GREEN, [350, 1100]], ["Ecad", CYAN, [600, 9500]]];
  const NUCW = [800, 10000];
  const LOOK = {
    CD45: { nuc: NUCW, m: [900, 5075], ref: ["SOX10", [489, 2900]] },
    SOX10: { nuc: NUCW, m: [489, 1714], ref: ["CD45", [900, 9124]] },
    CD3e: { nuc: NUCW, m: [436, 2408], ref: ["CD45", [1400, 20000]] },
    Ecad: { nuc: NUCW, m: [355, 3184] },
    Ki67: { nuc: NUCW, m: [384, 1046] },
    CD8a: { nuc: NUCW, m: [260, 875] },
    CD68: { nuc: NUCW, m: [260, 560] },
    CD20L: { nuc: NUCW, m: [395, 1400] },
  };
  const WIDE = {   // tissue level: the pyramid averages dilute membranes
    CD45: { nuc: [600, 7000], m: [700, 3500] },
    SOX10: { nuc: [600, 7000], m: [400, 1400] },
    Ecad: { nuc: [600, 7000], m: [300, 2400] },
  };

  // ---- gate slider moves: [marker, from, to, start, dur]; "min" = full range ------
  const SLIDE = [
    ["CD45", "min", G.CD45.prop, 24.0, 2.0],
    ["CD45", G.CD45.prop, G.CD45.final, 31.4, 0.6],
    ["SOX10", "min", G.SOX10.final, 39.6, 1.8],
    ["CD3e", "min", G.CD3e.prop, 46.4, 1.8],
    ["CD3e", G.CD3e.prop, G.CD3e.final, 53.6, 1.0],
    ["Ecad", "min", G.Ecad.prop, 60.6, 0.8], ["Ecad", G.Ecad.prop, G.Ecad.final, 61.6, 0.7],
    ["Ki67", "min", G.Ki67.prop, 64.0, 0.7], ["Ki67", G.Ki67.prop, G.Ki67.final, 64.9, 0.6],
    ["CD8a", "min", G.CD8a.final, 67.6, 0.8],
    ["CD68", "min", G.CD68.prop, 71.4, 0.8], ["CD68", G.CD68.prop, G.CD68.final, 72.4, 0.4],
    ["CD20L", "min", G.CD20L.prop, 76.6, 0.9], ["CD20L", G.CD20L.prop, G.CD20L.final, 78.4, 1.2],
  ];
  const OUTLINES_AT = 22.8;

  // ---- handles ----------------------------------------------------------------------
  const H = () => PV.H();
  const EV = () => (typeof CSVGatingList !== "undefined" ? CSVGatingList : window.CSVGatingList).events;
  const vid = () => window.PlexoraAgentBridge?.sessionId?.() || null;
  const progress = (done) => ({ units_done: done, units_total: 8 });
  const ev = (event, extra = {}) => ({ do: "call", fn: () => {
    const payload = Object.assign({ session_id: "gs_demo", event }, extra);
    if (event === "started") { payload.view_id = vid(); payload.control = {}; }
    window.dispatchEvent(new CustomEvent("plexora:agent-state-changed",
      { detail: { kind: "gating.session", plugin: "gating", payload } }));
  } });
  const gateFull = {};
  function fullRange(m) {
    const { ctrl } = H();
    if (!gateFull[m]) gateFull[m] = ctrl.getGateRange(m).slice();
    return gateFull[m];
  }
  // Switching a slot off deactivates its channel BY NAME: only switch off
  // slots showing some other marker, or the gated marker's layer goes too.
  function offUnless(k, keep) {
    const { sb } = H();
    const slot = sb.channelSlots[k];
    if (!slot || !slot.enabled) return;
    if (slot.name === keep) { slot.enabled = false; sb.syncSlotDom?.(slot); return; }
    sb.setSlotEnabled(k, false);
  }
  // window tweens: {n0, n1, from, to}, played in tracks
  let tw = null;
  function tween(targets, n, dur) {
    const { sb } = H();
    const from = {}, to = {};
    for (const k of Object.keys(targets)) {
      const slot = sb.channelSlots[k];
      from[k] = targets[k].start || (slot?.range ? [slot.range[0], slot.range[1]] : targets[k].to);
      to[k] = targets[k].to;
    }
    tw = { n0: n, n1: n + PV.F(dur), from, to };
  }
  function doMarker(c, n) {
    const { ctrl, sb } = H();
    const L = (c.wide && WIDE[c.name]) || LOOK[c.name];
    ctrl.setGateMarker(c.name, { force: true });
    sb.setSlotColor(1, RED, true);
    // the channel comes in a little dim and is brought up
    const t = { 0: { to: L.nuc }, 1: { to: L.m, start: [L.m[0], L.m[1] * 1.9] } };
    if (c.ref && LOOK[c.name].ref) {
      const [rname, rwin] = LOOK[c.name].ref;
      sb.setSlotMarker(2, rname, { keepColor: true, enable: true, force: true });
      sb.setSlotColor(2, GREEN, true);
      t[2] = { to: rwin, start: [rwin[0], rwin[1] * 1.6] };
    } else offUnless(2, c.name);
    offUnless(3, c.name);
    for (const k of Object.keys(t)) sb.setSlotWindow(Number(k), t[k].start || t[k].to);
    tween(t, n, c.quiet ? 0.6 : 1.0);
    const full = fullRange(c.name);
    ctrl.setGateRange([c.quiet ? G[c.name].final : full[0], full[1]], EV().SELECTION_CHANGED);
  }
  const marker = (name, o = {}) => ({ do: "call", fn: (h, n) => doMarker({ name, ...o }, n) });
  const prov = (name) => ({ do: "call", fn: () => {
    const { ctrl, X } = H();
    const g = G[name];
    ctrl.provenance = ctrl.provenance || {};
    ctrl.provenance[X.dataLayer.getFullChannelName(name)] = { marker: name, status: "accepted", method: g.method,
                                                            confidence: g.conf, state: g.state };
    ctrl.paintProvenance();
  } });
  const issued = (name, narration, done, evidence) => ev("issued", { phase: "inspecting", subject: name, marker: name,
    narration, progress: progress(done), ...(evidence ? { evidence } : {}) });
  const closed = (name) => ev("unit_closed", { marker: name, state: G[name].state, confidence: G[name].conf });
  const say = (phase, narration, done, evidence) => ev("answered", { phase, narration, progress: progress(done),
    ...(evidence ? { evidence } : {}) });

  // ring targets in the gating panel
  const T = {
    marker: () => { const i = document.querySelector("#gate_marker_select, #gate_marker, [id^=gate_marker]");
                    return i?.closest(".plx-combobox, .combobox, .select-wrap") || i; },
    gate: () => { const g = document.getElementById("gate_slider");
                  return g?.closest(".gate-slider-row, .gate-row, .plx-slider-row") || g?.parentElement; },
    legend: () => document.querySelector(".viewer-legend, #viewer_legend, .channel-legend, [class*=legend]"),
    contrast: () => document.querySelector(".agent-contrast, #gating_contrast, [class*=contrast-overlay], [class*=viewer-contrast]"),
  };
  const ring = (sel, dur) => ({ do: "ring", sel, dur });

  // ---- timing anchors -------------------------------------------------------------------
  const T_START = 13.6, T_DONE = 82.6;
  const CLOSES = [33.6, 41.8, 55.4, 62.4, 65.7, 69.6, 73.6, 80.4];

  PV.story({
    duration: 103.0,
    hd: true,
    async setup(h) {
      if (!window.PlexoraPaid?.allows?.("ai")) throw new Error("the AI licence is not active");
      await h.sb.applyLaunchChannels(OPEN.map(([name, color, range]) => ({ name, color, range })), { silent: true });
    },

    imagePoses: {
      ov0: [22700, 26600, 47500], ov1: [22700, 26800, 42000],
      agg: [10450, 13300, 1750], agg2: [10450, 13300, 1550],
      cd45b: [11450, 18700, 1900], cd45b2: [11450, 18700, 1700],
      nest: [40600, 19666, 2000], nest2: [40600, 19666, 1800],
      aggC: [43692, 32000, 1250], aggC2: [43692, 32000, 950],
      ecad: [9550, 12566, 2300], ki67: [11692, 16000, 2300], cd8: [11077, 16000, 1700],
      cd68: [10400, 12466, 2000], cd20: [12307, 16615, 3200], cd20b: [12307, 16615, 2900],
      revA: [10450, 13300, 1500], revB: [11500, 18000, 9000], revC: [22700, 26800, 44000], revD: [22700, 26800, 41000],
    },
    imageCam: [
      [0, "ov0"], [12.0, "ov1"], [18.6, "ov1"],
      [21.0, "agg", 0.6], [27.6, "agg2"],
      [28.8, "cd45b", 0.25], [33.8, "cd45b2"],
      [34.8, "nest", 3.0], [38.8, "nest2"], [42.2, "nest2"],
      [42.6, "aggC", 0.5], [45.4, "aggC"], [57.4, "aggC2"],
      [58.0, "ecad", 3.0], [60.4, "ecad"], [62.4, "ecad"],
      [62.6, "ki67", 1.2], [63.8, "ki67"], [66.0, "ki67"],
      [66.2, "cd8", 0], [67.4, "cd8"], [69.8, "cd8"],
      [70.0, "cd68", 0.3], [71.2, "cd68"], [74.6, "cd68"],
      [74.8, "cd20", 0.2], [76.2, "cd20"], [82.0, "cd20b"],
      [84.4, "revA", 0.3], [87.4, "revA"], [90.4, "revB"], [93.4, "revC"], [103.0, "revD"],
    ],

    // interface poses (page px), from the v3 promo; checked on the draft
    screenPoses: {
      AIB: [300, 150, 2.6], LAUN: [960, 545, 2.0], CTX: [960, 560, 2.3],
      GATE: [480, 390, 2.0], GATE2: [470, 400, 1.75], CARD: [480, 700, 2.0], DONE: [470, 690, 1.9],
      SIDE: [640, 580, 1.5], SIDE2: [700, 560, 1.4],
    },
    screenCam: [
      [0, "F"], [5.4, "F"], [6.6, "AIB"], [7.6, "AIB"], [8.4, "LAUN"], [9.9, "LAUN"], [10.6, "CTX"], [13.6, "CTX"],
      [14.6, "F"], [17.0, "F"], [18.0, "CARD"], [20.4, "CARD"],
      [21.2, "GATE"], [27.0, "GATE"], [27.8, "CARD"], [30.4, "CARD"], [31.0, "GATE2"], [34.0, "GATE2"], [34.8, "F"],
      [38.6, "F"], [39.4, "GATE"], [42.0, "GATE"], [42.6, "F"],
      [45.4, "F"], [46.2, "GATE"], [48.4, "GATE"], [49.2, "CARD"], [52.8, "CARD"], [53.4, "GATE2"], [57.0, "GATE2"],
      [57.8, "F"], [58.6, "SIDE"], [66.0, "SIDE2"], [74.0, "SIDE"], [81.0, "SIDE"], [81.8, "DONE"], [88.4, "DONE"],
      [89.4, "F"],
    ],

    cues: [
      // the launcher: real clicks, real typing
      [5.6, { do: "cursor", show: true }],
      ...PV.tap(5.8, "#plexora_ai_button", { move: 1.3, pause: 0.25 }),
      ...PV.tap(8.4, '[data-action="ai-gating"]', { move: 1.2, pause: 0.25, ring: true }),
      [10.4, { do: "type", text: "Melanoma, skin.", cps: 14 }],
      ...PV.tap(11.8, '[data-action="ai-context-start"]', { move: 1.0, pause: 0.3 }),
      [13.6, { do: "move", to: [1180, 760], dur: 1.4 }],
      [14.4, { do: "cursor", show: false }],
      // planning
      [T_START, ev("started", { phase: "planning", progress: progress(0), viewer_attached: true })],
      [14.2, say("planning", "Planning the order: CD45 and SOX10 first", 0)],
      [14.6, { do: "call", fn: () => window.PlexoraToolLoader.openTool("gating", null, { quiet: true }).then(() => {
        const h = H();
        // opening picks a default marker and shows outlines at 70 %: the shot's own state instead
        h.ctrl.setGateMarker("CD45", { force: true });
        h.sb.setSlotMarker(1, "CD45", { keepColor: true, enable: true, force: true });
        h.sb.setSlotColor(1, RED, true);
        h.sb.setSlotMarker(2, "SOX10", { keepColor: true, enable: true, force: true });
        h.sb.setSlotColor(2, GREEN, true);
        offUnless(3, "CD45");
        h.vc.userChose = true;
        const lyr = h.vc.activeLayer?.();
        if (lyr) h.iv.setLayerOpacity(lyr.name, 0); else h.iv.setCellDisplayOpacity?.(0);
        h.vc.opacitySlider?.set(0, { silent: true });
      }) }],
      // CD45
      [21.0, issued("CD45", "Inspecting CD45 expression…", 0,
                    [{ artifact_id: "art_50ab7fc919b95d9032a9", caption: "CD45 · the tissue at three scales" }])],
      [21.1, marker("CD45", { ref: true })],
      [21.3, ring(T.marker, 1.6)],
      [22.2, ring(T.contrast, 1.6)],
      [OUTLINES_AT, { do: "call", fn: (h) => h.vc.selectMode("outlines") }],
      [23.9, ring(T.gate, 2.4)],
      [26.6, say("thinking", "Checking cells below, at and above the gate…", 0)],
      [29.2, say("validating", "Checking SOX10 as the exclusion partner…", 0)],
      [33.6, closed("CD45")], [33.65, prov("CD45")], [33.7, say("planning", "", 1)],
      // SOX10
      [35.0, issued("SOX10", "Inspecting SOX10 in the tumour nests…", 1)],
      [35.1, marker("SOX10", { ref: true })],
      [39.4, ring(T.gate, 2.2)],
      [40.6, say("validating", "Clear nuclear stain; consistent with melanoma", 1)],
      [41.8, closed("SOX10")], [41.85, prov("SOX10")], [41.9, say("planning", "", 2)],
      // CD3e
      [42.6, issued("CD3e", "Checking co-expression with CD45…", 2,
                    [{ artifact_id: "art_e4e9ffb102f73dfcc4c7", caption: "CD3e · cells below, at and above the gate" }])],
      [42.7, marker("CD3e", { ref: true })],
      [46.2, ring(T.gate, 2.0)],
      [48.6, ev("phase", { phase: "thinking" })],
      [48.7, say("thinking", "Comparing candidate thresholds…", 2,
                 [{ artifact_id: "art_d1e12b82ddd4dd276ce7", caption: "CD3e · cells between neighbouring thresholds" }])],
      [53.2, say("validating", "Some stain spills across outlines from neighbours", 2)],
      [53.4, ring(T.gate, 1.4)],
      [55.4, closed("CD3e")], [55.45, prov("CD3e")], [55.5, say("planning", "", 3)],
      // the rest, briskly
      [58.2, issued("Ecad", "Inspecting Ecad in the epidermis…", 3)], [58.3, marker("Ecad")],
      [62.4, closed("Ecad")], [62.45, prov("Ecad")],
      [62.6, issued("Ki67", "Inspecting Ki67…", 4)], [62.7, marker("Ki67")],
      [65.7, closed("Ki67")], [65.75, prov("Ki67")],
      [66.2, issued("CD8a", "Inspecting CD8a beside CD45…", 5)], [66.3, marker("CD8a")],
      [69.6, closed("CD8a")], [69.65, prov("CD8a")],
      [70.0, issued("CD68", "Inspecting CD68 in the stroma…", 6)], [70.1, marker("CD68")],
      [73.6, closed("CD68")], [73.65, prov("CD68")],
      [74.8, issued("CD20L", "Checking whether CD20L staining is real…", 7)], [74.9, marker("CD20L")],
      [80.4, closed("CD20L")], [80.45, prov("CD20L")],
      [81.0, say("summarizing", "Writing the gates and the report", 8)],
      [T_DONE, ev("finished", { reason: "closed", summary: SUMMARY })],
      // the reveal: each marker's own gated cells
      [84.2, marker("CD45", { quiet: true })],
      [89.4, { do: "call", fn: (h, n) => tween({ 0: { to: WIDE.CD45.nuc }, 1: { to: WIDE.CD45.m } }, n, 2.4) }],
      [92.4, marker("SOX10", { quiet: true, wide: true })],
      [95.0, marker("Ecad", { quiet: true, wide: true })],
    ],

    tracks(s, n, h) {
      // the launcher is appended at once; this is its entrance
      const L = document.querySelector(".plx-ai-launcher"), B = document.querySelector(".plx-ai-backdrop");
      PV.state.launchAt ??= null;
      if (L && PV.state.launchAt === null) PV.state.launchAt = n;
      if (L) {
        const e = PV.easeOut(PV.clamp01((n - PV.state.launchAt) / PV.F(0.45)));
        L.style.opacity = String(e);
        L.style.transform = `translate(-50%, -50%) translateY(${(1 - e) * 10}px) scale(${0.975 + 0.025 * e})`;
        if (B) B.style.opacity = String(e);
      }
      const C = document.querySelector(".plx-ai-context");
      if (C) {
        PV.state.ctxAt ??= n;
        const e = PV.easeOut(PV.clamp01((n - PV.state.ctxAt) / PV.F(0.35)));
        C.style.opacity = String(e);
        C.style.transform = `translateY(${(1 - e) * 6}px)`;
      }
      // the rollback hint under Done is for the user's own runs, not a film
      if (s >= T_DONE) {
        const hint = document.querySelector("#plexora_ai_dock .plx-agent-hint");
        if (hint) hint.style.display = "none";
        document.querySelectorAll("#plexora_ai_dock *").forEach((el) => {
          if (el.children.length === 0 && /rollback/.test(el.textContent || "")) el.hidden = true;
        });
      }
      const { ctrl, sb, vc, iv } = h;
      // gate slides
      if (ctrl) {
        for (const [m, from, to, t0, d] of SLIDE) {
          const n0 = PV.F(t0), n1 = n0 + PV.F(d);
          if (n < n0 || n > n1 || ctrl.gateMarker !== m) continue;
          const full = fullRange(m);
          const a = from === "min" ? full[0] : from;
          const k = PV.clamp01((n - n0) / (n1 - n0));
          const v = PV.lerp(a, to, from === "min" ? PV.easeOut(k) : PV.ease(k));
          ctrl.setGateRange([v, full[1]], n === n1 ? EV().SELECTION_CHANGED : EV().GATING_BRUSH_MOVE);
        }
      }
      // channel windows
      if (tw && n <= tw.n1 + 1) {
        const e = PV.ease(PV.clamp01((n - tw.n0) / Math.max(1, tw.n1 - tw.n0)));
        for (const k of Object.keys(tw.to)) {
          const a = tw.from[k], b = tw.to[k];
          sb.setSlotWindow(Number(k), [PV.lerp(a[0], b[0], e), PV.lerp(a[1], b[1], e)]);
        }
      }
      // outlines ease 0 -> 100 % through the viewer's own opacity control
      const o0 = PV.F(OUTLINES_AT + 0.2), o1 = o0 + PV.F(0.8);
      if (n >= o0 && n <= o1 + 1) {
        const v = Math.round(PV.lerp(0, 100, PV.ease(PV.clamp01((n - o0) / (o1 - o0)))));
        const lyr = vc.activeLayer?.();
        if (lyr) iv.setLayerOpacity(lyr.name, v / 100); else iv.setCellDisplayOpacity?.(v / 100);
        vc.opacitySlider?.set(v, { silent: true });
      }
    },

    // the voice says the rest; captions carry the numbers
    captions: [
      [22.0, 27.4, `CD45 · proposed gate ${G.CD45.prop.toFixed(2)}`],
      [30.0, 33.8, "SOX10 dark in CD45+ cells"],
      [44.6, 50.4, "CD3e · candidate gates compared"],
      [55.6, 57.6, "Accepted · low confidence"],
      [67.0, 74.2, "CD8a, CD68 · for manual review"],
      [76.4, 81.6, "CD20L · no positive population"],
    ],
    hud: {
      label: "Plexora AI · Gating", from: T_START, to: 88.6, idle: "",
      chapters: [[T_START, "Planning"], [21.0, "CD45"], [35.0, "SOX10"], [42.6, "CD3e"], [58.2, "Ecad"],
                 [62.6, "Ki67"], [66.2, "CD8a"], [70.0, "CD68"], [74.8, "CD20L"], [T_DONE, "Done"]],
      total: 8,
      progress: (s) => CLOSES.filter((t) => s >= t).length,
      clock: { start: T_START, end: T_DONE, seconds: RUN_S },
    },
    endCard: { at: 97.6, dim: 0.84, brand: "Plexora AI", main: "Auto-Thresholding", sub: "Visual AI for spatial biology.",
               stats: [[8, "markers gated", "int"], [CELLS, "cells", "int"], [RUN_S, "start to finish", "time"]] },

    voice: [[1.2, "v01"], [6.2, "v02"], [14.0, "v03"], [21.4, "v04"], [29.4, "v05"], [35.6, "v06"],
            [43.0, "v07"], [51.4, "v08"], [58.6, "v09"], [66.4, "v10"], [75.2, "v11"], [83.0, "v12"],
            [89.4, "v13"], [99.0, "v14"]],
    sfx: [[21.0, "card"], [35.0, "card"], [42.6, "card"], [48.7, "card"], [58.2, "card"],
          [T_DONE, "done"], [89.4, "reveal_all"], [97.6, "end"]],
  });
})();
