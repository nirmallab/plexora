// Promo choreography: injected with addInitScript, driven one frame at a time
// by record.mjs. step(n) is the only thing that moves anything; every value it
// sets is a function of n. Numbers below come from the real MCP gating
// session gs_20261004T041222_d3e818 on lsp11385 (gates, GMM proposals,
// confidence, inspected fields).
(() => {
  "use strict";
  const FPS = 30;
  const F = (s) => Math.round(s * FPS);

  // ---- network/promise accounting for settle() (must exist before the bundle)
  const acct = { fetch: 0, xhr: 0, gate: 0, cue: 0 };
  const _fetch = window.fetch;
  window.fetch = function (...args) {
    acct.fetch++;
    return _fetch.apply(this, args).finally(() => { acct.fetch--; });
  };
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    acct.xhr++;
    let done = false;
    const fin = () => { if (!done) { done = true; acct.xhr--; } };
    this.addEventListener("loadend", fin);
    return _send.apply(this, args);
  };

  // ---- the real session ----------------------------------------------------
  const G = {
    CD45: { final: 7.27, gmm: 7.50, win: [499, 9000], conf: "moderate", state: "accepted", method: "ai_accepted" },
    SOX10: { final: 6.58, gmm: 6.58, win: [294, 3761], conf: "high", state: "accepted", method: "gmm" },
    Ecad: { final: 6.53, gmm: 6.98, win: [357, 7086], conf: "moderate", state: "accepted", method: "ai_refined" },
    CD3e: { final: 6.62, gmm: 6.73, win: [322, 6210], conf: "low", state: "accepted_low_confidence", method: "ai_refined" },
    CD20L: { final: 9.79, gmm: 5.85, win: [274, 822], conf: "low", state: "no_positive_population", method: "no_positive_population" },
    CD68: { final: 5.47, gmm: 5.42, win: [152, 2307], conf: "manual_review", state: "manual_review_recommended", method: "needs_review" },
    CD8a: { final: 5.83, gmm: 5.87, win: [223, 2915], conf: "manual_review", state: "manual_review_recommended", method: "needs_review" },
    Ki67: { final: 6.35, gmm: 6.35, win: [284, 1405], conf: "moderate", state: "accepted", method: "ai_refined" },
  };
  // Display, tuned per combination on its own field (outlines at 100 %):
  // the gated marker red, its reference green, the nucleus a dim blue.
  const RED = "#ff2b2b", GREEN = "#2ee86b", NUCC = "#3a5596", ECADC = "#22d3ee";
  // Windows from each marker's own cells (lsp11385 table): low end at its
  // negatives' 90th percentile, high end where its median positive draws at
  // ~65 %, so a cell just over the gate is visibly lit; then checked on the
  // marker's borderline field with outlines at 100 %.
  const OPEN = [["Nucleus", NUCC, [400, 3500]], ["CD45", RED, [700, 3500]],
                ["SOX10", GREEN, [350, 1100]], ["Ecad", ECADC, [600, 9500]]];
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
  // tissue level: the pyramid's averages dilute membranes, so tighter
  const WIDE = {
    CD45: { nuc: [600, 7000], m: [700, 3500] },
    SOX10: { nuc: [600, 7000], m: [400, 1400] },
    Ecad: { nuc: [600, 7000], m: [300, 2400] },
  };

  const SUMMARY = { units_total: 8, units_done: 8, accepted: 5, accepted_low_confidence: 1, review: 2,
                    empty: 1, failed: 0, skipped: 0, written: 8, replayed: 0 };

  // ---- camera poses (full-res image px: centre and visible width) ----------
  // Fields chosen from the cell table: tissue edge to edge, a clear mix of
  // gated and ungated cells, few cells near the threshold (regions.py).
  const P = {
    ov0: [22700, 26600, 47500], ov1: [22700, 26800, 42000],
    agg: [10450, 13300, 1750], agg2: [10450, 13300, 1550],
    cd45b: [11450, 18700, 1900], cd45b2: [11450, 18700, 1700],
    nest: [40600, 19666, 2000], nest2: [40600, 19666, 1800],
    aggC: [43692, 32000, 1250], aggC2: [43692, 32000, 950],
    ecad: [9550, 12566, 2300], ki67: [11692, 16000, 2300], cd8: [11077, 16000, 1700],
    cd68: [10400, 12466, 2000], cd20: [12307, 16615, 3200],
    revA: [10450, 13300, 1500], revB: [11500, 18000, 9000], revC: [22700, 26800, 44000], revD: [22700, 26800, 41000],
  };

  // [seconds, pose, bump]: the camera eases from each key to the next; bump>0
  // lifts the zoom mid-flight for long pans.
  const CAM = [
    [0, "ov0"], [6.5, "ov1"], [13.4, "ov1"],
    [16.2, "agg", 0.6], [19.6, "agg2"],
    [22.0, "cd45b", 0.25], [25.8, "cd45b2"],
    [28.0, "nest", 3.0], [32.6, "nest2"],
    [35.2, "aggC", 0.5], [42.8, "aggC2"],
    [44.6, "ecad", 3.0], [46.2, "ecad"],
    [47.6, "ki67", 1.2], [48.6, "ki67"],
    [49.8, "cd8", 0], [50.8, "cd8"],
    [52.0, "cd68", 0.3], [53.0, "cd68"],
    [54.2, "cd20", 0.2], [56.8, "cd20"],
    [58.6, "revA", 0.3], [60.2, "revA"],
    [62.6, "revB"], [65.2, "revC"], [72.5, "revD"],
  ];

  // ---- the screen camera: a crop of the whole page (CSS px centre, zoom).
  // Interface poses measured off the draft frames.
  const SP = {
    F: [960, 540, 1], AIB: [300, 150, 2.6], LAUN: [960, 545, 2.0], CTX: [960, 560, 2.3],
    GATE: [480, 390, 2.0], GATE2: [470, 400, 1.75], CARD: [480, 700, 2.0], DONE: [470, 690, 1.9],
    SIDE: [640, 580, 1.5], SIDE2: [700, 560, 1.4],
  };
  // The sidebar (panel and agent card) is where the camera lives during gating;
  // the full viewer only for the long flights, the reveal and the title.
  const SCAM = [
    [0, "F"], [2.4, "F"], [4.6, "AIB"], [5.3, "AIB"], [6.3, "LAUN"], [7.5, "LAUN"], [8.2, "CTX"], [11.2, "CTX"],
    [12.9, "F"], [16.4, "F"], [17.3, "GATE"], [19.5, "GATE"], [20.3, "CARD"], [23.2, "CARD"],
    [23.9, "GATE2"], [25.4, "GATE2"], [26.0, "CARD"], [26.6, "CARD"], [27.4, "F"],
    [28.3, "F"], [29.0, "GATE"], [30.8, "GATE"], [31.4, "CARD"], [33.4, "CARD"], [34.2, "F"],
    [35.4, "F"], [36.2, "GATE"], [38.0, "GATE"], [38.6, "CARD"], [39.7, "CARD"], [40.2, "GATE2"], [41.4, "GATE2"],
    [42.0, "CARD"], [43.3, "CARD"], [44.1, "F"],
    [45.4, "F"], [46.3, "SIDE"], [50.5, "SIDE2"], [55.0, "SIDE"], [55.8, "DONE"], [59.0, "DONE"], [60.2, "F"],
  ];
  function scamAt(s) {
    let i = 0;
    while (i < SCAM.length - 1 && s >= SCAM[i + 1][0]) i++;
    if (i >= SCAM.length - 1) return SP[SCAM[SCAM.length - 1][1]];
    const [t0, a] = SCAM[i], [t1, b] = SCAM[i + 1];
    const A = SP[a], B = SP[b];
    const e = ease(clamp01((s - t0) / (t1 - t0)));
    const z = Math.exp(lerp(Math.log(A[2]), Math.log(B[2]), e));
    // the centre moves in the zoomed frame's terms, so a zoom-in heads straight for its target
    const wa = 1 / A[2], wb = 1 / B[2], w = 1 / z;
    const k = Math.abs(wb - wa) > 1e-6 ? (w - wa) / (wb - wa) : e;
    return [lerp(A[0], B[0], k), lerp(A[1], B[1], k), z];
  }

  // ---- gate slider moves: [marker, from, to, startS, durS] -----------------
  // `from: "min"` is the column's own minimum (the full-range gate).
  const SLIDE = [
    ["CD45", "min", G.CD45.gmm, 17.4, 1.8],
    ["CD45", G.CD45.gmm, G.CD45.final, 23.6, 1.3],
    ["SOX10", "min", G.SOX10.final, 29.0, 1.8],
    ["CD3e", "min", G.CD3e.gmm, 36.6, 1.6],
    ["CD3e", G.CD3e.gmm, G.CD3e.final, 40.0, 1.2],
    ["Ecad", "min", G.Ecad.gmm, 45.0, 0.8], ["Ecad", G.Ecad.gmm, G.Ecad.final, 45.9, 0.6],
    ["Ki67", "min", G.Ki67.final, 47.9, 0.8],
    ["CD8a", "min", G.CD8a.final, 50.0, 0.8],
    ["CD68", "min", G.CD68.final, 52.2, 0.8],
    ["CD20L", "min", G.CD20L.final, 54.4, 0.9],
  ];

  // ---- discrete cues ---------------------------------------------------------
  const vid = () => window.PlexoraAgentBridge?.sessionId?.() || null;
  const progress = (done) => ({ units_done: done, units_total: 8 });
  const ev = (event, extra = {}) => ({ do: "event", event, extra });
  const CUES = [
    // the launcher
    [3.0, { do: "cursor", show: true }],
    [3.2, { do: "move", to: "#plexora_ai_button", dur: 1.7 }],
    [5.2, { do: "click" }],
    [5.25, { do: "launcherIn" }],
    [6.0, { do: "move", to: '[data-action="ai-gating"]', dur: 1.2 }],
    [7.5, { do: "click" }],
    [7.55, { do: "launcherStep" }],
    [8.0, { do: "type", text: "Melanoma, skin.", cps: 14 }],
    [9.6, { do: "move", to: '[data-action="ai-context-start"]', dur: 1.0 }],
    [10.9, { do: "click" }],
    [11.4, { do: "move", to: [1180, 760], dur: 1.6 }],
    [12.4, { do: "cursor", show: false }],
    // the session
    [12.2, ev("started", { phase: "planning", progress: progress(0), viewer_attached: true })],
    [12.7, ev("answered", { phase: "planning", narration: "Planning the order: CD45 and SOX10 first", progress: progress(0) })],
    [13.2, { do: "gatingTool" }],

    [14.4, ev("issued", { phase: "inspecting", subject: "CD45", marker: "CD45", narration: "Inspecting CD45 expression…",
                          progress: progress(0), evidence: [{ artifact_id: "art_50ab7fc919b95d9032a9", caption: "CD45 · the tissue at three scales" }] })],
    [14.5, { do: "marker", name: "CD45", ref: true }],
    [14.6, { do: "ring", sel: "#gate_marker_select_wrap", dur: 1.6 }],
    [15.5, { do: "ring", sel: "@contrast", dur: 1.6 }],
    [16.0, { do: "outlines", on: true }],
    [17.3, { do: "ring", sel: "@gate", dur: 2.2 }],
    [19.9, ev("phase", { phase: "thinking" })],
    [20.0, ev("answered", { phase: "thinking", narration: "Comparing regions across the tissue…", progress: progress(0) })],
    [22.4, ev("answered", { phase: "validating", narration: "Checking SOX10 as the exclusion partner…", progress: progress(0) })],
    [23.4, ev("answered", { phase: "validating", narration: "Refining threshold…", progress: progress(0) })],
    [23.5, { do: "ring", sel: "@gate", dur: 1.7 }],
    [25.2, ev("unit_closed", { marker: "CD45", state: "accepted", confidence: "moderate" })],
    [25.25, { do: "prov", name: "CD45" }],
    [25.3, ev("answered", { phase: "planning", progress: progress(1) })],
    // SOX10
    [26.4, ev("issued", { phase: "inspecting", subject: "SOX10", marker: "SOX10", narration: "Inspecting SOX10 in the tumour nests…", progress: progress(1) })],
    [26.6, { do: "marker", name: "SOX10", ref: true }],
    [26.7, { do: "ring", sel: "#gate_marker_select_wrap", dur: 1.4 }],
    [26.9, { do: "ring", sel: "@legend", dur: 1.6 }],
    [28.9, { do: "ring", sel: "@gate", dur: 2.0 }],
    [31.0, ev("answered", { phase: "validating", narration: "Two clean populations; CD45 stays out", progress: progress(1) })],
    [32.2, ev("unit_closed", { marker: "SOX10", state: "accepted", confidence: "high" })],
    [32.25, { do: "prov", name: "SOX10" }],
    [32.3, ev("answered", { phase: "planning", progress: progress(2) })],
    // CD3e (Ecad's unit closes off screen in the real order; it is shown in the montage)
    [33.6, ev("issued", { phase: "inspecting", subject: "CD3e", marker: "CD3e", narration: "Checking co-expression with CD45…", progress: progress(2),
                          evidence: [{ artifact_id: "art_e4e9ffb102f73dfcc4c7", caption: "CD3e · cells below, at and above the gate" }] })],
    [33.8, { do: "marker", name: "CD3e", ref: true }],
    [33.9, { do: "ring", sel: "#gate_marker_select_wrap", dur: 1.4 }],
    [34.1, { do: "ring", sel: "@legend", dur: 1.6 }],
    [36.5, { do: "ring", sel: "@gate", dur: 1.9 }],
    [38.4, ev("phase", { phase: "thinking" })],
    [38.5, ev("answered", { phase: "thinking", narration: "Comparing candidate thresholds…", progress: progress(2),
                            evidence: [{ artifact_id: "art_d1e12b82ddd4dd276ce7", caption: "CD3e · cells between neighbouring thresholds" }] })],
    [39.8, ev("answered", { phase: "validating", narration: "Refining threshold…", progress: progress(2) })],
    [39.9, { do: "ring", sel: "@gate", dur: 1.6 }],
    [42.2, ev("unit_closed", { marker: "CD3e", state: "accepted_low_confidence", confidence: "low" })],
    [42.25, { do: "prov", name: "CD3e" }],
    [42.3, ev("answered", { phase: "planning", progress: progress(3) })],
    // montage
    [43.9, ev("issued", { phase: "inspecting", subject: "Ecad", marker: "Ecad", narration: "Inspecting Ecad in the epidermis…", progress: progress(3) })],
    [44.0, { do: "marker", name: "Ecad", ref: false }],
    [46.7, ev("unit_closed", { marker: "Ecad", state: "accepted", confidence: "moderate" })],
    [46.75, { do: "prov", name: "Ecad" }],
    [46.9, ev("issued", { phase: "inspecting", subject: "Ki67", marker: "Ki67", narration: "Inspecting Ki67…", progress: progress(4) })],
    [47.0, { do: "marker", name: "Ki67", ref: false }],
    [49.0, ev("unit_closed", { marker: "Ki67", state: "accepted", confidence: "moderate" })],
    [49.05, { do: "prov", name: "Ki67" }],
    [49.1, ev("issued", { phase: "inspecting", subject: "CD8a", marker: "CD8a", narration: "Inspecting CD8a beside CD3e…", progress: progress(5) })],
    [49.2, { do: "marker", name: "CD8a", ref: false }],
    [51.2, ev("unit_closed", { marker: "CD8a", state: "manual_review_recommended", confidence: "manual_review" })],
    [51.25, { do: "prov", name: "CD8a" }],
    [51.3, ev("issued", { phase: "inspecting", subject: "CD68", marker: "CD68", narration: "Inspecting CD68 in the stroma…", progress: progress(6) })],
    [51.4, { do: "marker", name: "CD68", ref: false }],
    [53.4, ev("unit_closed", { marker: "CD68", state: "manual_review_recommended", confidence: "manual_review" })],
    [53.45, { do: "prov", name: "CD68" }],
    [53.5, ev("issued", { phase: "inspecting", subject: "CD20L", marker: "CD20L", narration: "Checking whether CD20L staining is real…", progress: progress(7) })],
    [53.6, { do: "marker", name: "CD20L", ref: false }],
    [55.6, ev("unit_closed", { marker: "CD20L", state: "no_positive_population", confidence: "low" })],
    [55.65, { do: "prov", name: "CD20L" }],
    [55.8, ev("answered", { phase: "summarizing", narration: "Writing the gates and the report", progress: progress(8) })],
    [57.0, ev("finished", { reason: "closed", summary: SUMMARY })],
    // reveal: each marker's own gated cells
    [57.4, { do: "marker", name: "CD45", ref: false, quiet: true }],
    [59.4, { do: "wide", name: "CD45", dur: 2.4 }],
    [60.8, { do: "marker", name: "SOX10", ref: false, quiet: true, wide: true }],
    [63.2, { do: "marker", name: "Ecad", ref: false, quiet: true, wide: true }],
  ];

  // ---- overlay tracks: [start, dur, from, to] ------------------------------
  const BLACK = [[0, 1.2, 1, 0], [70.6, 1.4, 0, 1]];
  const DIM = [[66.0, 1.6, 0, 0.84]];
  const TITLE = [[66.6, 1.4, 0, 1]];

  // ---- helpers -----------------------------------------------------------------
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
  const easeOut = (x) => 1 - Math.pow(1 - x, 3);
  const lerp = (a, b, t) => a + (b - a) * t;
  function track(list, s, base) {
    let v = base;
    for (const [t0, d, a, b] of list) {
      if (s >= t0) v = lerp(a, b, ease(clamp01((s - t0) / d)));
    }
    return v;
  }
  function camAt(s) {
    let i = 0;
    while (i < CAM.length - 1 && s >= CAM[i + 1][0]) i++;
    if (i >= CAM.length - 1) return P[CAM[CAM.length - 1][1]];
    const [t0, a] = CAM[i], [t1, b, bump = 0] = CAM[i + 1];
    const A = P[a], B = P[b];
    const e = ease(clamp01((s - t0) / (t1 - t0)));
    const lw = lerp(Math.log(A[2]), Math.log(B[2]), e) + bump * Math.sin(Math.PI * e);
    return [lerp(A[0], B[0], e), lerp(A[1], B[1], e), Math.exp(lw)];
  }

  const H = () => {
    const X = window.__plexora;
    return { X, sb: X.viewerSidebar, vc: X.viewerControls, iv: X.seaDragonViewer, osd: X.seaDragonViewer.viewer,
             ctrl: X.plugins.get("gating")?.sidebarController };
  };
  const EV = () => (typeof CSVGatingList !== "undefined" ? CSVGatingList : window.CSVGatingList).events;

  function emit(event, extra) {
    const payload = Object.assign({ session_id: "gs_demo", event }, extra);
    if (event === "started") {
      payload.view_id = vid();
      payload.control = {};
    }
    window.dispatchEvent(new CustomEvent("plexora:agent-state-changed",
      { detail: { kind: "gating.session", plugin: "gating", payload } }));
  }

  // ---- overlay DOM ------------------------------------------------------------
  let layer, cursor, dim, title, black;
  const rings = [];
  function mountOverlay() {
    layer = document.createElement("div");
    layer.id = "promo-layer";
    cursor = document.createElement("div");
    cursor.id = "promo-cursor";
    cursor.innerHTML = '<svg width="22" height="30" viewBox="0 0 22 30" xmlns="http://www.w3.org/2000/svg">'
      + '<path d="M2.2 1.6 L2.2 23.4 L7.6 18.3 L11.2 26.9 L14.7 25.4 L11.1 17.0 L18.6 17.0 Z" fill="#ffffff" stroke="#111418" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    dim = document.createElement("div"); dim.id = "promo-dim";
    title = document.createElement("div"); title.id = "promo-title";
    title.innerHTML = '<div class="t-brand">Plexora AI</div><div class="t-main">Auto-Thresholding</div>'
      + '<div class="t-rule"></div><div class="t-sub">Visual AI for spatial biology.</div>';
    black = document.createElement("div"); black.id = "promo-black";
    layer.append(dim, title, cursor, black);
    document.body.appendChild(layer);
  }
  function ringEl(i) {
    while (rings.length <= i) {
      const r = document.createElement("div");
      r.className = "promo-ring";
      layer.insertBefore(r, cursor);
      rings.push(r);
    }
    return rings[i];
  }
  function resolveTarget(sel) {
    if (sel === "@gate") return document.getElementById("gate_slider")?.closest(".gate-slider-row, .gate-row, .plx-slider-row") || document.getElementById("gate_slider")?.parentElement;
    if (sel === "@legend") return document.querySelector(".viewer-legend, #viewer_legend, .channel-legend, [class*=legend]");
    if (sel === "@contrast") return document.querySelector(".agent-contrast, #gating_contrast, [class*=contrast-overlay], [class*=viewer-contrast]");
    if (sel === "#gate_marker_select_wrap") {
      const input = document.querySelector("#gate_marker_select, #gate_marker, [id^=gate_marker]");
      return input?.closest(".plx-combobox, .combobox, .select-wrap") || input;
    }
    return document.querySelector(sel);
  }

  // ---- state machine ----------------------------------------------------------
  const st = { cursorOn: false, cur: [1300, 720], move: null, clickAt: -1, typing: null, launcherIn: -1, stepIn: -1,
               rings: [], contrast: null, outlines: null, ready: false, firedUpTo: -1 };
  let gateFull = {};

  function fullRange(m) {
    const { ctrl } = H();
    if (!gateFull[m]) gateFull[m] = ctrl.getGateRange(m).slice();
    return gateFull[m];
  }

  // Windows ease from where they are to the shot's (the contrast moving is part of the shot).
  function tweenWindows(targets, n, dur) {
    const { sb } = H();
    const from = {};
    for (const k of Object.keys(targets)) {
      const slot = sb.channelSlots[k];
      from[k] = targets[k].start || (slot && slot.range ? [slot.range[0], slot.range[1]] : targets[k].to);
    }
    st.win = { n0: n, n1: n + F(dur), from, to: Object.fromEntries(Object.entries(targets).map(([k, v]) => [k, v.to])) };
  }

  // A slot is switched off only when it shows some other marker: switching
  // one off deactivates its channel BY NAME, which would take the gated
  // marker's own layer with it.
  function offUnless(k, keep) {
    const { sb } = H();
    const slot = sb.channelSlots[k];
    if (!slot || !slot.enabled) return;
    if (slot.name === keep) { slot.enabled = false; sb.syncSlotDom?.(slot); return; }
    sb.setSlotEnabled(k, false);
  }

  function setChannels(n) {
    // the first gating shot: CD45 red, SOX10 green, Ecad off
    const { sb } = H();
    const L = LOOK.CD45;
    sb.setSlotMarker(1, "CD45", { keepColor: true, enable: true, force: true });
    sb.setSlotColor(1, RED, true);
    sb.setSlotMarker(2, "SOX10", { keepColor: true, enable: true, force: true });
    sb.setSlotColor(2, GREEN, true);
    offUnless(3, "CD45");
    tweenWindows({ 0: { to: L.nuc }, 1: { to: L.m }, 2: { to: L.ref[1] } }, n, 0.8);
  }

  function doMarker(c, n) {
    const { ctrl, sb } = H();
    const L = (c.wide && WIDE[c.name]) || LOOK[c.name];
    ctrl.setGateMarker(c.name, { force: true });
    sb.setSlotColor(1, RED, true);
    // the new channel comes in a little dim and is brought up: "adjusting contrast"
    const t = { 0: { to: L.nuc }, 1: { to: L.m, start: [L.m[0], L.m[1] * 1.9] } };
    if (c.ref && LOOK[c.name].ref) {
      const [rname, rwin] = LOOK[c.name].ref;
      sb.setSlotMarker(2, rname, { keepColor: true, enable: true, force: true });
      sb.setSlotColor(2, GREEN, true);
      t[2] = { to: rwin, start: [rwin[0], rwin[1] * 1.6] };
    } else {
      offUnless(2, c.name);
    }
    offUnless(3, c.name);
    for (const k of Object.keys(t)) sb.setSlotWindow(Number(k), t[k].start || t[k].to);
    tweenWindows(t, n, c.quiet ? 0.6 : 1.0);
    // The reveal shows the written gate; before a slider move the gate is the full range.
    const full = fullRange(c.name);
    const start = c.quiet ? G[c.name].final : full[0];
    ctrl.setGateRange([start, full[1]], EV().SELECTION_CHANGED);
  }

  function doProv(name) {
    const { ctrl, X } = H();
    const g = G[name];
    const full = X.dataLayer.getFullChannelName(name);
    ctrl.provenance = ctrl.provenance || {};
    ctrl.provenance[full] = { marker: name, status: "accepted", method: g.method, confidence: g.conf, state: g.state };
    ctrl.paintProvenance();
  }

  function cueAction(c, n, out) {
    const { X, sb, vc, ctrl } = H();
    switch (c.do) {
      case "cursor": st.cursorOn = c.show; st.cursorFade = { at: n, show: c.show }; break;
      case "move": {
        let to = c.to;
        if (typeof to === "string") {
          const el = document.querySelector(to);
          if (el) { const r = el.getBoundingClientRect(); to = [r.left + r.width * 0.5, r.top + r.height * 0.55]; }
          else to = st.cur;
        }
        st.move = { from: st.cur.slice(), to, n0: n, n1: n + F(c.dur) };
        break;
      }
      case "click": st.clickAt = n; out.click = st.cur.slice(); break;
      case "launcherIn": st.launcherIn = n; break;
      case "launcherStep": st.stepIn = n; break;
      case "type": st.typing = { text: c.text, n0: n, cps: c.cps, sent: 0 }; break;
      case "event":
        emit(c.event, c.extra);
        if (c.event === "finished") {
          document.querySelectorAll("#plexora_ai_dock *").forEach((el) => {
            if (el.children.length === 0 && /rollback/.test(el.textContent || "")) el.hidden = true;
          });
        }
        break;
      case "gatingTool":
        out.pending.push(window.PlexoraToolLoader.openTool("gating", null, { quiet: true }).then(() => {
          const h = H();
          // Opening picks a default gate marker and mirrors it into slot 1; put the shot's channels back.
          h.ctrl = h.X.plugins.get("gating").sidebarController;
          h.ctrl.setGateMarker("CD45", { force: true });
          setChannels(st.lastN);
          h.vc.userChose = true;
          const lyr = h.vc.activeLayer?.();
          if (lyr) h.iv.setLayerOpacity(lyr.name, 0); else h.iv.setCellDisplayOpacity?.(0);
          h.vc.opacitySlider?.set(0, { silent: true });
        }));
        break;
      case "marker": doMarker(c, n); break;
      case "wide": tweenWindows({ 0: { to: WIDE[c.name].nuc }, 1: { to: WIDE[c.name].m } }, n, c.dur); break;
      case "prov": doProv(c.name); break;
      case "ring": st.rings.push({ sel: c.sel, n0: n, n1: n + F(c.dur) }); break;
      case "contrast": st.contrast = { name: c.name, from: c.from, to: c.to, n0: n, n1: n + F(c.dur) }; break;
      case "outlines":
        out.pending.push(Promise.resolve(vc.selectMode("outlines")).then(() => {
          st.outlines = { n0: n, n1: n + F(0.8) };
        }));
        break;
    }
  }

  function applyTracks(n, out) {
    const s = n / FPS;
    const { iv, osd, ctrl, sb, vc } = H();
    // camera
    const [cx, cy, w] = camAt(s);
    const cs = osd.viewport.getContainerSize();
    const h = w * cs.y / cs.x;
    window.PlexoraViewerScene.fitRegion(iv, { x: cx - w / 2, y: cy - h / 2, width: w, height: h }, { immediately: true });
    // gate slides
    if (ctrl) {
      for (const [m, from, to, t0, d] of SLIDE) {
        const n0 = F(t0), n1 = n0 + F(d);
        if (n < n0 || n > n1 || ctrl.gateMarker !== m) continue;
        const full = fullRange(m);
        const a = from === "min" ? full[0] : from;
        const e = from === "min" ? easeOut(clamp01((n - n0) / (n1 - n0))) : ease(clamp01((n - n0) / (n1 - n0)));
        const v = lerp(a, to, e);
        ctrl.setGateRange([v, full[1]], n === n1 ? EV().SELECTION_CHANGED : EV().GATING_BRUSH_MOVE);
      }
    }
    // channel windows
    if (st.win && n >= st.win.n0 && n <= st.win.n1 + 1) {
      const e = ease(clamp01((n - st.win.n0) / Math.max(1, st.win.n1 - st.win.n0)));
      for (const k of Object.keys(st.win.to)) {
        const a = st.win.from[k], b = st.win.to[k];
        sb.setSlotWindow(Number(k), [lerp(a[0], b[0], e), lerp(a[1], b[1], e)]);
      }
    }
    // outline fade-in through the overlay's own opacity control
    if (st.outlines && n >= st.outlines.n0 && n <= st.outlines.n1 + 1) {
      const e = ease(clamp01((n - st.outlines.n0) / (st.outlines.n1 - st.outlines.n0)));
      const lyr = vc.activeLayer?.();
      const v = Math.round(lerp(0, 100, e));
      if (lyr) iv.setLayerOpacity(lyr.name, v / 100); else iv.setCellDisplayOpacity?.(v / 100);
      vc.opacitySlider?.set(v, { silent: true });
    }
    // launcher entrance (it is appended instantly; this is the motion)
    const L = document.querySelector(".plx-ai-launcher"), B = document.querySelector(".plx-ai-backdrop");
    if (L && st.launcherIn >= 0) {
      const e = easeOut(clamp01((n - st.launcherIn) / F(0.45)));
      L.style.opacity = String(e);
      L.style.transform = `translate(-50%, -50%) translateY(${(1 - e) * 10}px) scale(${0.975 + 0.025 * e})`;
      if (B) B.style.opacity = String(e);
    }
    const C = document.querySelector(".plx-ai-context");
    if (C && st.stepIn >= 0) {
      const e = easeOut(clamp01((n - st.stepIn) / F(0.35)));
      C.style.opacity = String(e);
      C.style.transform = `translateY(${(1 - e) * 6}px)`;
    }
    // cursor
    if (st.move) {
      const { from, to, n0, n1 } = st.move;
      const e = ease(clamp01((n - n0) / (n1 - n0)));
      // a slight arc, as a hand moves
      const arc = Math.sin(Math.PI * e) * Math.min(60, Math.hypot(to[0] - from[0], to[1] - from[1]) * 0.08);
      st.cur = [lerp(from[0], to[0], e) - arc * 0.3, lerp(from[1], to[1], e) - arc];
      if (n >= n1) st.move = null;
    }
    let co = st.cursorOn ? 1 : 0;
    if (st.cursorFade) {
      const e = clamp01((n - st.cursorFade.at) / F(0.35));
      co = st.cursorFade.show ? e : 1 - e;
    }
    const press = st.clickAt >= 0 && n - st.clickAt >= 0 && n - st.clickAt < 5 ? 0.86 + 0.14 * ((n - st.clickAt) / 5) : 1;
    cursor.style.opacity = String(co);
    cursor.style.transform = `translate(${st.cur[0] - 3}px, ${st.cur[1] - 2}px) scale(${press})`;
    out.mouse = co > 0.01 ? st.cur.slice() : null;
    // typing
    if (st.typing) {
      const want = Math.min(st.typing.text.length, Math.floor((n - st.typing.n0) * st.typing.cps / FPS) + 1);
      if (want > st.typing.sent) {
        out.type = st.typing.text.slice(st.typing.sent, want);
        st.typing.sent = want;
      }
      if (st.typing.sent >= st.typing.text.length) st.typing = null;
    }
    // rings
    let k = 0;
    for (const r of st.rings) {
      if (n < r.n0 || n > r.n1 + F(0.3)) continue;
      const el = resolveTarget(r.sel);
      if (!el) continue;
      const b = el.getBoundingClientRect();
      if (!b.width) continue;
      const fin = clamp01((n - r.n0) / F(0.3)), fout = 1 - clamp01((n - r.n1) / F(0.3));
      const ring = ringEl(k++);
      ring.style.left = `${b.left - 4}px`; ring.style.top = `${b.top - 4}px`;
      ring.style.width = `${b.width + 8}px`; ring.style.height = `${b.height + 8}px`;
      ring.style.opacity = String(0.85 * Math.min(fin, fout));
    }
    for (; k < rings.length; k++) rings[k].style.opacity = "0";
    // screen camera (applied by the recorder as the screenshot's crop)
    out.scam = scamAt(s);
    {
      const [cx, cy, z] = out.scam;
      const w = 1920 / z, h = 1080 / z;
      const x = Math.min(Math.max(cx - w / 2, 0), 1920 - w), y = Math.min(Math.max(cy - h / 2, 0), 1080 - h);
      out.crop = [x, y, z];
      paintHud(n, s, x, y, z);
    }
    // dim, title, black
    dim.style.opacity = String(track(DIM, s, 0));
    title.style.opacity = String(track(TITLE, s, 0));
    paintStats(s);
    title.style.transform = `translateY(${(1 - track(TITLE, s, 0)) * 8}px)`;
    black.style.opacity = String(track(BLACK, s, 1));
  }

  // ---- the HUD: marker being gated, progress, elapsed (real session clock) ----
  // The real run took 3:01 from start to close (gs_20261004T041222_d3e818,
  // 04:12:22.8 -> 04:15:23.6); the clock maps the video's started..finished
  // onto that, so the number it ends on is true.
  const RUN_S = 181, T_START = 12.2, T_DONE = 57.0;
  const HUD_MARKERS = [[14.4, "CD45", 0], [26.4, "SOX10", 1], [33.6, "CD3e", 2], [43.9, "Ecad", 3],
                       [46.9, "Ki67", 4], [49.1, "CD8a", 5], [51.3, "CD68", 6], [53.5, "CD20L", 7]];
  const CLOSES = [25.2, 32.2, 42.2, 46.7, 49.0, 51.2, 53.4, 55.6];
  const fmt = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  let hud;
  function mountHud() {
    hud = document.createElement("div");
    hud.id = "promo-hud";
    hud.innerHTML = '<div class="h-label">Plexora AI · <span class="h-mode">Gating</span></div>'
      + '<div class="h-row"><span class="h-marker"></span><span class="h-sep"></span><span class="h-prog"></span>'
      + '<span class="h-sep"></span><span class="h-time"></span></div><div class="h-bar"><i></i></div>';
    layer.insertBefore(hud, cursor);
  }
  function paintHud(n, s, x, y, z) {
    if (!hud) mountHud();
    const vis = clamp01((s - T_START) / 0.5) * (1 - clamp01((s - 60.4) / 0.6));
    hud.style.opacity = String(vis);
    if (vis <= 0) return;
    // fixed at the output's top right, constant size, whatever the camera does
    hud.style.left = `${x + (1920 - 28) / z}px`;
    hud.style.top = `${y + 74 / z}px`;
    hud.style.transform = `scale(${1.3 / z}) translateX(-100%)`;
    const done = s >= T_DONE;
    const el = Math.min(RUN_S, Math.max(0, (s - T_START) / (T_DONE - T_START) * RUN_S));
    let cur = null;
    for (const m of HUD_MARKERS) if (s >= m[0]) cur = m;
    const closed = CLOSES.filter((t) => s >= t).length;
    hud.querySelector(".h-mode").textContent = done ? "Done" : (cur ? "Gating" : "Planning");
    const mk = hud.querySelector(".h-marker");
    const name = done ? "8 markers" : (cur ? cur[1] : "Reading the panel");
    if (mk.textContent !== name) { mk.textContent = name; st.hudSwap = n; }
    const k = st.hudSwap === undefined ? 1 : clamp01((n - st.hudSwap) / 8);
    mk.style.opacity = String(easeOut(k));
    mk.style.transform = `translateY(${(1 - easeOut(k)) * 6}px)`;
    hud.querySelector(".h-prog").textContent = `${closed} of 8`;
    hud.querySelector(".h-time").textContent = fmt(el);
    hud.querySelector(".h-bar i").style.width = `${(closed / 8) * 100}%`;
  }

  // title stats count up
  const STATS = [[8, "markers gated"], [202049, "cells"], [RUN_S, "start to finish"]];
  let statsEl;
  function paintStats(s) {
    if (!statsEl) {
      statsEl = document.createElement("div");
      statsEl.className = "t-stats";
      statsEl.innerHTML = STATS.map(() => '<div class="t-stat"><b></b><span></span></div>').join("");
      title.appendChild(statsEl);
    }
    [...statsEl.children].forEach((node, i) => {
      const t0 = 67.6 + i * 0.25;
      const e = easeOut(clamp01((s - t0) / 1.3));
      const [v, label] = STATS[i];
      const val = v * e;
      node.querySelector("b").textContent = i === 2 ? fmt(val) : Math.round(val).toLocaleString("en-US");
      node.querySelector("span").textContent = label;
      node.style.opacity = String(clamp01((s - t0) / 0.5));
      node.style.transform = `translateY(${(1 - clamp01((s - t0) / 0.6)) * 10}px)`;
    });
  }

  // ---- public ------------------------------------------------------------------
  async function prepare() {
    const X = window.__plexora;
    mountOverlay();
    // HD: through the viewer's own checkbox, as a user would
    const box = document.querySelector("#viewer_controls_hd");
    if (box && !box.checked) { box.checked = true; box.dispatchEvent(new Event("change")); }
    await X.seaDragonViewer.viewerManagerVMain.setHdMode(true).catch?.(() => {});
    if (!X.seaDragonViewer.viewerManagerVMain.isHdMode()) throw new Error("HD did not turn on");
    await X.viewerSidebar.applyLaunchChannels(OPEN.map(([name, color, range]) => ({ name, color, range })), { silent: true });
    // Keep the Image card open with its channels showing for the opening.
    const osd = X.seaDragonViewer.viewer;
    const tune = (item) => { try { item.maxTilesPerFrame = 64; } catch (e) {} };
    for (let i = 0; i < osd.world.getItemCount(); i++) tune(osd.world.getItemAt(i));
    osd.world.addHandler("add-item", (e) => tune(e.item));
    osd.imageLoader.jobLimit = 16;
    // wrap the gate overlay's async work so settle() can wait for it
    const iv = X.seaDragonViewer;
    for (const fn of ["updateSegmentationFilter", "ensureSegmentationReady", "evaluateGateLocally"]) {
      const orig = iv[fn];
      if (typeof orig !== "function") continue;
      iv[fn] = function (...args) {
        acct.gate++;
        let r;
        try { r = orig.apply(this, args); } catch (e) { acct.gate--; throw e; }
        Promise.resolve(r).finally(() => { acct.gate--; });
        return r;
      };
    }
    st.ready = true;
    return { hd: X.seaDragonViewer.viewerManagerVMain.isHdMode(), items: osd.world.getItemCount() };
  }

  // Cues for frame n fire exactly once, in order; continuous tracks are set every frame.
  async function step(n) {
    st.lastN = n;
    const out = { pending: [] };
    for (const [t, c] of CUES) {
      const cn = F(t);
      if (cn === n || (cn < n && cn > st.firedUpTo)) cueAction(c, n, out);
    }
    st.firedUpTo = n;
    applyTracks(n, out);
    // Async cue work is not awaited here (it may wait on the faked clock);
    // settle() waits for the counter instead.
    for (const p of out.pending) { acct.cue++; Promise.resolve(p).catch((e) => console.error("promo cue", e)).finally(() => { acct.cue--; }); }
    delete out.pending;
    return out;
  }

  // settle probe; with pump=true also updates/draws the world without moving the clock
  function probe(pump) {
    const X = window.__plexora;
    const osd = X.seaDragonViewer.viewer;
    if (pump) {
      try { osd.world.update(false); if (osd.world.needsDraw()) osd.world.draw(); } catch (e) {}
    }
    let fully = true, n = osd.world.getItemCount();
    for (let i = 0; i < n; i++) {
      const it = osd.world.getItemAt(i);
      if (it.opacity > 0 && it.getFullyLoaded && !it.getFullyLoaded()) fully = false;
    }
    const sb = X.viewerSidebar;
    return {
      fully, items: n, jobs: osd.imageLoader.jobsInProgress, queue: osd.imageLoader.jobQueue.length,
      needsDraw: osd.world.needsDraw(), fetch: acct.fetch, xhr: acct.xhr, gate: acct.gate, cue: acct.cue,
      autoLeveling: (sb.channelSlots || []).some((s) => s.autoLeveling),
    };
  }

  const TOTAL = F(72);
  window.__promo = { prepare, step, probe, TOTAL, FPS, acct };
})();
