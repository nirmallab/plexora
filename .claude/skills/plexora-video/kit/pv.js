// Plexora video runtime. Injected with addInitScript BEFORE the app bundle
// (the fetch/XHR counters must wrap the app's own calls), followed by the
// storyboard script, which calls PV.story({...}). record.mjs drives it one
// frame at a time: step(n) is the only thing that moves anything, and every
// value it sets is a function of n. See ../SKILL.md for the storyboard format.
(() => {
  "use strict";
  const FPS = 30;
  const OW = 1920, OH = 1080;           // output frame, CSS px
  const F = (s) => Math.round(s * FPS);

  // ---- accounting for settle() ------------------------------------------------
  const acct = { fetch: 0, xhr: 0, app: 0, cue: 0 };
  const _fetch = window.fetch;
  window.fetch = function (...args) {
    acct.fetch++;
    return _fetch.apply(this, args).finally(() => { acct.fetch--; });
  };
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    acct.xhr++;
    let done = false;
    this.addEventListener("loadend", () => { if (!done) { done = true; acct.xhr--; } });
    return _send.apply(this, args);
  };
  // Count any promise the shot waits on (gate overlay, a tool opening...).
  const count = (p) => { acct.cue++; Promise.resolve(p).catch((e) => console.error("pv cue", e)).finally(() => { acct.cue--; }); return p; };

  // ---- maths --------------------------------------------------------------------
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
  const easeOut = (x) => 1 - Math.pow(1 - x, 3);
  const lerp = (a, b, t) => a + (b - a) * t;
  // [[start, dur, from, to], ...] -> value at s (later entries win once started)
  function track(list, s, base) {
    let v = base;
    for (const [t0, d, a, b] of list || []) if (s >= t0) v = lerp(a, b, ease(clamp01((s - t0) / d)));
    return v;
  }

  // ---- handles into the app (null-safe: the project list has no viewer) -------
  function H() {
    const X = window.__plexora;
    const iv = X?.seaDragonViewer;
    return { X, sb: X?.viewerSidebar, vc: X?.viewerControls, iv, osd: iv?.viewer,
             ctrl: X?.plugins?.get?.("gating")?.sidebarController };
  }

  // ---- storyboard -------------------------------------------------------------------
  let S = null;
  const st = { cur: [1300, 720], cursorOn: false, cursorFade: null, move: null, clickAt: -1, typing: null,
               rings: [], win: null, firedUpTo: -1, hold: null, held: [], lastN: 0, swaps: {} };

  function story(def) {
    S = Object.assign({ duration: 30, hd: true, fadeIn: 1.0, fadeOut: 1.2, cues: [], captions: [],
                        screenPoses: {}, imagePoses: {} }, def);
    S.screenPoses = Object.assign({ F: [OW / 2, OH / 2, 1] }, S.screenPoses);
    // keep cues in time order; a cue list may be built from PV.tap() spreads
    S.cues = S.cues.slice().sort((a, b) => a[0] - b[0]);
  }

  // ---- cameras ------------------------------------------------------------------------
  // Screen camera: [cx, cy, zoom] in page CSS px. Zoom interpolates in log space
  // and the centre in 1/zoom terms, so a zoom-in heads straight for its target.
  function scamAt(s) {
    const K = S.screenCam;
    if (!K || !K.length) return S.screenPoses.F;
    const P = S.screenPoses;
    let i = 0;
    while (i < K.length - 1 && s >= K[i + 1][0]) i++;
    if (i >= K.length - 1 || s < K[0][0]) return P[K[i][1]];
    const [t0, a] = K[i], [t1, b] = K[i + 1];
    const A = P[a], B = P[b];
    const e = ease(clamp01((s - t0) / (t1 - t0)));
    const z = Math.exp(lerp(Math.log(A[2]), Math.log(B[2]), e));
    const wa = 1 / A[2], wb = 1 / B[2], w = 1 / z;
    const k = Math.abs(wb - wa) > 1e-6 ? (w - wa) / (wb - wa) : e;
    return [lerp(A[0], B[0], k), lerp(A[1], B[1], k), z];
  }
  function cropOf([cx, cy, z]) {
    const w = OW / z, h = OH / z;
    return [Math.min(Math.max(cx - w / 2, 0), OW - w), Math.min(Math.max(cy - h / 2, 0), OH - h), z];
  }
  // Image camera: [cx, cy, visibleWidth] in full-resolution image px; the third
  // key entry ("bump") lifts the zoom mid-flight so long pans pass over tissue
  // at a coarse level instead of streaming black tiles.
  function icamAt(s) {
    const K = S.imageCam, P = S.imagePoses;
    let i = 0;
    while (i < K.length - 1 && s >= K[i + 1][0]) i++;
    if (i >= K.length - 1 || s < K[0][0]) return P[K[i][1]];
    const [t0, a] = K[i], [t1, b, bump = 0] = K[i + 1];
    const A = P[a], B = P[b];
    const e = ease(clamp01((s - t0) / (t1 - t0)));
    const lw = lerp(Math.log(A[2]), Math.log(B[2]), e) + bump * Math.sin(Math.PI * e);
    return [lerp(A[0], B[0], e), lerp(A[1], B[1], e), Math.exp(lw)];
  }

  // ---- overlay DOM --------------------------------------------------------------------
  // #pv-page lives in page coordinates (cursor, rings: part of the scene, they
  // zoom with it). #pv-out is a 1920x1080 box laid over the current crop and
  // scaled by 1/zoom, so everything in it is in OUTPUT pixels: captions, HUD,
  // title, end card and fades stay put whatever the screen camera does.
  let pageL, outL, cursor, dim, black, cap, hud, title, endc;
  const rings = [];
  const div = (id, cls, parent, html = "") => {
    const d = document.createElement("div");
    if (id) d.id = id;
    if (cls) d.className = cls;
    d.innerHTML = html;
    parent.appendChild(d);
    return d;
  };
  function card(parent, id, c) {
    const el = div(id, "pv-card", parent);
    el.innerHTML = (c.brand ? `<div class="c-brand">${c.brand}</div>` : "")
      + `<div class="c-main">${c.main || ""}</div>`
      + (c.sub ? `<div class="c-rule"></div><div class="c-sub">${c.sub}</div>` : "")
      + (c.stats ? `<div class="c-stats">${c.stats.map(() => '<div class="c-stat"><b></b><span></span></div>').join("")}</div>` : "");
    return el;
  }
  function mount() {
    pageL = div("pv-page", null, document.body);
    outL = div("pv-out", null, document.body);
    cursor = div("pv-cursor", null, pageL, '<svg width="22" height="30" viewBox="0 0 22 30" xmlns="http://www.w3.org/2000/svg">'
      + '<path d="M2.2 1.6 L2.2 23.4 L7.6 18.3 L11.2 26.9 L14.7 25.4 L11.1 17.0 L18.6 17.0 Z" fill="#ffffff" stroke="#111418" stroke-width="1.4" stroke-linejoin="round"/></svg>');
    dim = div("pv-dim", null, outL);
    cap = div("pv-caption", null, outL, '<div class="k-step"></div><div class="k-text"></div>');
    hud = div("pv-hud", null, outL, '<div class="h-label"></div><div class="h-row"><span class="h-main"></span>'
      + '<span class="h-sep"></span><span class="h-prog"></span><span class="h-sep h-tsep"></span><span class="h-time"></span></div><div class="h-bar"><i></i></div>');
    if (S.titleCard) title = card(outL, "pv-title", S.titleCard);
    if (S.endCard) endc = card(outL, "pv-end", S.endCard);
    black = div("pv-black", null, outL);
  }
  function ringEl(i) {
    while (rings.length <= i) { const r = div(null, "pv-ring", pageL); pageL.insertBefore(r, cursor); rings.push(r); }
    return rings[i];
  }
  const target = (sel) => (typeof sel === "function" ? sel() : document.querySelector(sel));
  function centreOf(sel) {
    const el = target(sel);
    if (Array.isArray(el)) return el;             // a function may return a page point [x, y]
    if (!el) { console.warn("pv: no element for", sel); return null; }
    const r = el.getBoundingClientRect();
    return [r.left + r.width * 0.5, r.top + r.height * 0.55];
  }

  // ---- cues ------------------------------------------------------------------------------
  function cue(c, n, out) {
    const h = H();
    switch (c.do) {
      case "cursor": st.cursorOn = c.show; st.cursorFade = { at: n, show: c.show }; break;
      case "move": {
        const to = Array.isArray(c.to) ? c.to : centreOf(c.to) || st.cur;
        st.move = { from: st.cur.slice(), to, n0: n, n1: n + Math.max(1, F(c.dur ?? 1.0)) };
        break;
      }
      case "click": st.clickAt = n; out.click = { at: st.cur.slice(), button: c.button || "left", count: c.count || 1 }; break;
      case "type": st.typing = { text: c.text, n0: n, cps: c.cps || 15, sent: 0 }; break;
      case "key": out.key = c.key; break;                       // e.g. "Enter", "Escape"
      case "files": out.files = { sel: c.sel, paths: c.paths }; break;   // <input type=file>: no native dialog
      case "ring": st.rings.push({ sel: c.sel, n0: n, n1: n + F(c.dur ?? 1.6) }); break;
      case "call": { const r = c.fn(h, n); if (r && typeof r.then === "function") out.pending.push(r); break; }
      case "wait": st.hold = c; out.hold = { max: c.max ?? 600, label: c.label || "" }; break;
      case "channels":
        out.pending.push(h.sb.applyLaunchChannels(c.list.map(([name, color, range]) => ({ name, color, range })), { silent: true }));
        break;
      case "windows": windows(c.slots, n, c.dur ?? 0.8, c.from); break;
      default: console.warn("pv: unknown cue", c.do);
    }
  }
  // Contrast moves on screen: each slot's window eases from where it is (or
  // `from`) to the target. Slot-domain units (raw 16-bit in HD).
  function windows(slots, n, dur, from = {}) {
    const { sb } = H();
    const f = {}, t = {};
    for (const k of Object.keys(slots)) {
      const slot = sb.channelSlots[k];
      f[k] = from[k] || (slot?.range ? [slot.range[0], slot.range[1]] : slots[k]);
      t[k] = slots[k];
      if (from[k]) sb.setSlotWindow(Number(k), from[k]);
    }
    st.win = { n0: n, n1: n + Math.max(1, F(dur)), from: f, to: t };
  }

  // ---- per-frame tracks ------------------------------------------------------------------
  function frame(n, out) {
    const s = n / FPS;
    const h = H();
    if (S.imageCam && h.osd) {
      const [cx, cy, w] = icamAt(s);
      const cs = h.osd.viewport.getContainerSize();
      const hh = w * cs.y / cs.x;
      window.PlexoraViewerScene.fitRegion(h.iv, { x: cx - w / 2, y: cy - hh / 2, width: w, height: hh }, { immediately: true });
    }
    if (st.win && n <= st.win.n1 + 1) {
      const e = ease(clamp01((n - st.win.n0) / (st.win.n1 - st.win.n0)));
      for (const k of Object.keys(st.win.to)) {
        const a = st.win.from[k], b = st.win.to[k];
        h.sb.setSlotWindow(Number(k), [lerp(a[0], b[0], e), lerp(a[1], b[1], e)]);
      }
    }
    if (S.tracks) S.tracks(s, n, h, out);               // storyboard's own continuous moves
    // cursor: eased with a slight arc, as a hand moves
    if (st.move) {
      const { from, to, n0, n1 } = st.move;
      const e = ease(clamp01((n - n0) / (n1 - n0)));
      const arc = Math.sin(Math.PI * e) * Math.min(60, Math.hypot(to[0] - from[0], to[1] - from[1]) * 0.08);
      st.cur = [lerp(from[0], to[0], e) - arc * 0.3, lerp(from[1], to[1], e) - arc];
      if (n >= n1) st.move = null;
    }
    let co = st.cursorOn ? 1 : 0;
    if (st.cursorFade) {
      const e = clamp01((n - st.cursorFade.at) / F(0.35));
      co = st.cursorFade.show ? e : 1 - e;
    }
    const press = st.clickAt >= 0 && n - st.clickAt < 5 ? 0.86 + 0.14 * ((n - st.clickAt) / 5) : 1;
    cursor.style.opacity = String(co);
    cursor.style.transform = `translate(${st.cur[0] - 3}px, ${st.cur[1] - 2}px) scale(${press})`;
    out.mouse = co > 0.01 ? st.cur.slice() : null;
    if (st.typing) {
      const want = Math.min(st.typing.text.length, Math.floor((n - st.typing.n0) * st.typing.cps / FPS) + 1);
      if (want > st.typing.sent) { out.type = st.typing.text.slice(st.typing.sent, want); st.typing.sent = want; }
      if (st.typing.sent >= st.typing.text.length) st.typing = null;
    }
    let k = 0;
    for (const r of st.rings) {
      if (n < r.n0 || n > r.n1 + F(0.3)) continue;
      const el = target(r.sel);
      const b = el?.getBoundingClientRect();
      if (!b || !b.width) continue;
      const fade = Math.min(clamp01((n - r.n0) / F(0.3)), 1 - clamp01((n - r.n1) / F(0.3)));
      const ring = ringEl(k++);
      Object.assign(ring.style, { left: `${b.left - 4}px`, top: `${b.top - 4}px`, width: `${b.width + 8}px`,
                                  height: `${b.height + 8}px`, opacity: String(0.85 * fade) });
    }
    for (; k < rings.length; k++) rings[k].style.opacity = "0";

    // screen camera -> the recorder's crop; the output layer rides on it
    out.scam = scamAt(s);
    const [x, y, z] = cropOf(out.scam);
    outL.style.transform = `translate(${x}px, ${y}px) scale(${1 / z})`;
    paintCaption(s, n);
    paintHud(s, n);
    paintCard(title, S.titleCard, s);
    paintCard(endc, S.endCard, s);
    let dv = 0;
    const tc = S.titleCard, ec = S.endCard;
    if (tc && tc.dim) dv = Math.min(track([[tc.at, 0.8, 0, tc.dim]], s, 0), tc.dim * (1 - clamp01((s - (tc.at + tc.dur)) / 0.8)));
    if (ec) dv = Math.max(dv, track([[ec.at - 0.6, 1.4, 0, ec.dim ?? 0.84]], s, 0));
    dim.style.opacity = String(dv);
    black.style.opacity = String(track([[0, S.fadeIn, 1, 0], [S.duration - S.fadeOut, S.fadeOut, 0, 1]], s, 1));
  }

  // Lower-third caption: [t0, t1, text, step?]. One at a time; crossfades.
  function paintCaption(s, n) {
    const c = S.captions.find(([t0, t1]) => s >= t0 - 0.35 && s < t1 + 0.35);
    if (!c) { cap.style.opacity = "0"; return; }
    const [t0, t1, text, step] = c;
    const v = Math.min(clamp01((s - (t0 - 0.35)) / 0.35), 1 - clamp01((s - t1) / 0.35));
    const txt = typeof text === "function" ? text(st) : text;
    cap.querySelector(".k-text").textContent = txt;
    cap.querySelector(".k-step").textContent = step || "";
    cap.querySelector(".k-step").style.display = step ? "" : "none";
    cap.style.opacity = String(easeOut(v));
    cap.style.transform = `translate(-50%, ${(1 - easeOut(v)) * 10}px)`;
  }

  // HUD (top right): label, the current chapter (crossfades on change),
  // "k of N", optional clock. hud: {label, from, to, chapters:[[t, name]],
  // total, clock:{start, end, seconds} | {segments}} -- the clock maps video time
  // onto the REAL time of what was filmed, so the number it ends on is true.
  const fmt = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  // Real seconds shown at video time s: {start, end, seconds} maps the span
  // linearly; {segments: [[videoT, realS], ...]} piecewise, so the clock is
  // true at every key (a 10 s bulk beat that took 6 min reads 6 min there).
  function clockAt(c, s) {
    const K = c.segments || [[c.start, 0], [c.end, c.seconds]];
    if (s <= K[0][0]) return K[0][1];
    for (let i = 1; i < K.length; i++) {
      if (s <= K[i][0]) return lerp(K[i - 1][1], K[i][1], (s - K[i - 1][0]) / (K[i][0] - K[i - 1][0]));
    }
    return K[K.length - 1][1];
  }
  function paintHud(s, n) {
    const H_ = S.hud;
    if (!H_) { hud.style.opacity = "0"; return; }
    const vis = clamp01((s - H_.from) / 0.5) * (1 - clamp01((s - H_.to) / 0.6));
    hud.style.opacity = String(vis);
    if (vis <= 0) return;
    let idx = -1;
    (H_.chapters || []).forEach(([t], i) => { if (s >= t) idx = i; });
    const name = idx >= 0 ? H_.chapters[idx][1] : (H_.idle || "");
    hud.querySelector(".h-label").textContent = typeof H_.label === "function" ? H_.label(s) : H_.label;
    const main = hud.querySelector(".h-main");
    if (main.textContent !== name) { main.textContent = name; st.swaps.hud = n; }
    const e = easeOut(st.swaps.hud === undefined ? 1 : clamp01((n - st.swaps.hud) / 8));
    main.style.opacity = String(e);
    main.style.transform = `translateY(${(1 - e) * 6}px)`;
    const total = H_.total || (H_.chapters || []).length;
    const done = H_.progress ? H_.progress(s) : idx + 1;
    hud.querySelector(".h-prog").textContent = total ? `${Math.max(0, done)} of ${total}` : "";
    hud.querySelector(".h-bar i").style.width = `${total ? (Math.max(0, done) / total) * 100 : 0}%`;
    const tEl = hud.querySelector(".h-time");
    if (H_.clock) tEl.textContent = fmt(clockAt(H_.clock, s));
    tEl.style.display = hud.querySelector(".h-tsep").style.display = H_.clock ? "" : "none";
  }

  // Title / end card: {at, dur, brand, main, sub, stats:[[value, label, "time"|"int"]]}
  function paintCard(el, c, s) {
    if (!el) return;
    const v = c.dur ? Math.min(track([[c.at, 1.2, 0, 1]], s, 0), 1 - clamp01((s - (c.at + c.dur)) / 0.8)) : track([[c.at, 1.4, 0, 1]], s, 0);
    el.style.opacity = String(v);
    el.style.transform = `translateY(${(1 - v) * 8}px)`;
    if (!c.stats) return;
    [...el.querySelector(".c-stats").children].forEach((node, i) => {
      const t0 = c.at + 1.0 + i * 0.25;
      const [val, label, kind] = c.stats[i];
      const x = val * easeOut(clamp01((s - t0) / 1.3));
      node.querySelector("b").textContent = kind === "time" ? fmt(x) : Math.round(x).toLocaleString("en-US");
      node.querySelector("span").textContent = label;
      node.style.opacity = String(clamp01((s - t0) / 0.5));
      node.style.transform = `translateY(${(1 - clamp01((s - t0) / 0.6)) * 10}px)`;
    });
  }

  // ---- recorder API --------------------------------------------------------------------------
  async function prepare() {
    if (!S) throw new Error("no storyboard: the storyboard script must call PV.story({...})");
    mount();
    const h = H();
    const res = { viewer: !!h.osd, hd: null };
    if (h.osd) {
      if (S.hd) {
        // HD through the viewer's own checkbox, as a user would; then insist.
        const box = document.querySelector("#viewer_controls_hd");
        if (box && !box.checked) { box.checked = true; box.dispatchEvent(new Event("change")); }
        await h.iv.viewerManagerVMain.setHdMode(true)?.catch?.(() => {});
        if (!h.iv.viewerManagerVMain.isHdMode()) throw new Error("HD mode did not turn on");
      }
      res.hd = h.iv.viewerManagerVMain.isHdMode();
      // OSD's default of 1 new tile per frame makes every settle take dozens of ticks
      const tune = (item) => { try { item.maxTilesPerFrame = 64; } catch (e) {} };
      for (let i = 0; i < h.osd.world.getItemCount(); i++) tune(h.osd.world.getItemAt(i));
      h.osd.world.addHandler("add-item", (e) => tune(e.item));
      h.osd.imageLoader.jobLimit = 16;
      for (const fn of ["updateSegmentationFilter", "ensureSegmentationReady", "evaluateGateLocally"]) {
        const orig = h.iv[fn];
        if (typeof orig !== "function") continue;
        h.iv[fn] = function (...args) {
          acct.app++;
          let r;
          try { r = orig.apply(this, args); } catch (e) { acct.app--; throw e; }
          Promise.resolve(r).finally(() => { acct.app--; });
          return r;
        };
      }
    }
    if (S.setup) await S.setup(h);
    return res;
  }

  // Cues for frame n fire exactly once, in order; continuous tracks every frame.
  function step(n) {
    st.lastN = n;
    const out = { pending: [] };
    for (const [t, c] of S.cues) {
      const cn = F(t);
      if (cn === n || (cn < n && cn > st.firedUpTo)) cue(c, n, out);
    }
    st.firedUpTo = n;
    frame(n, out);
    // not awaited here (it may wait on the faked clock); settle() waits on the counter
    for (const p of out.pending) count(p);
    delete out.pending;
    return out;
  }

  // A `wait` cue holds the timeline: the recorder runs the clock in real time,
  // capturing nothing, until this returns true; then reports the real wait.
  function holdCheck() { try { return !!st.hold?.until(H()); } catch (e) { return false; } }
  function held(ms) { st.held.push({ label: st.hold?.label, ms }); st.lastHeld = ms; st.hold = null; }

  function probe(pump) {
    const h = H();
    let fully = true, items = 0, jobs = 0, queue = 0;
    if (h.osd) {
      if (pump) { try { h.osd.world.update(false); if (h.osd.world.needsDraw()) h.osd.world.draw(); } catch (e) {} }
      items = h.osd.world.getItemCount();
      for (let i = 0; i < items; i++) {
        const it = h.osd.world.getItemAt(i);
        if (it.opacity > 0 && it.getFullyLoaded && !it.getFullyLoaded()) fully = false;
      }
      jobs = h.osd.imageLoader.jobsInProgress;
      queue = h.osd.imageLoader.jobQueue.length;
    }
    return { fully, items, jobs, queue, fetch: acct.fetch, xhr: acct.xhr, app: acct.app, cue: acct.cue,
             autoLeveling: (h.sb?.channelSlots || []).some((s) => s.autoLeveling) };
  }

  // Storyboard sugar: move to a control, pause, click it.
  function tap(t, sel, { move = 1.1, pause = 0.25, ring = false } = {}) {
    const c = [[t, { do: "move", to: sel, dur: move }], [t + move + pause, { do: "click" }]];
    if (ring) c.push([t + move * 0.4, { do: "ring", sel, dur: move * 0.6 + pause + 0.4 }]);
    return c;
  }

  window.PV = { story, tap, H, F, FPS, ease, easeOut, lerp, clamp01, track, count, fmt, state: st };
  window.__promo = {
    prepare, step, probe, holdCheck, held, acct,
    get TOTAL() { return F(S.duration); }, FPS,
  };
})();
