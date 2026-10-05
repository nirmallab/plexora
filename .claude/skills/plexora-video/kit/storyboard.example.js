// Example storyboard: a 16 s micro-tutorial, "Bring up a channel's contrast".
// Shows every storyboard feature once; copy it to storyboard.js and replace.
// Times are seconds. Pose numbers are for lsp11385 -- measure your own.
PV.story({
  duration: 16,
  hd: true,                                   // always; prepare() throws if HD will not turn on

  // Before frame 0 (not filmed): the opening state. h = {X, sb, vc, iv, osd, ctrl}.
  async setup(h) {
    await h.sb.applyLaunchChannels([
      { name: "Nucleus", color: "#3a5596", range: [400, 3500] },
      { name: "CD45", color: "#ff2b2b", range: [700, 3500] },
    ], { silent: true });
  },

  // Image camera: [cx, cy, visible width] in full-resolution image px.
  imagePoses: { wide: [22700, 26800, 42000], agg: [10450, 13300, 1750], agg2: [10450, 13300, 1550] },
  imageCam: [[0, "wide"], [8.6, "wide"], [11.4, "agg", 0.8], [14.5, "agg2"]],

  // Screen camera: [cx, cy, zoom] in page CSS px; "F" (full window) is built in.
  screenPoses: { SLOT: [300, 300, 2.2] },
  screenCam: [[0, "F"], [3.2, "F"], [4.2, "SLOT"], [8.0, "SLOT"], [8.9, "F"]],

  titleCard: { at: 0.4, dur: 2.4, dim: 0.75, brand: "Plexora", main: "Channel contrast", sub: "Two steps, ten seconds." },
  captions: [
    [4.4, 7.8, "Each channel has its own window.", "Step 1"],
    [9.2, 13.6, () => `Waited ${(PV.state.lastHeld / 1000).toFixed(1)} s for real work; the cut hides it.`, "Step 2"],
  ],
  hud: { label: "Tutorial · Contrast", from: 3.4, to: 14.4, chapters: [[4.4, "Pick the window"], [9.2, "Look closer"]] },
  endCard: { at: 14.2, brand: "Plexora", main: "plexoraapp.com" },

  cues: [
    [3.6, { do: "cursor", show: true }],
    // point, don't click, when the click is not the lesson: a real click opens the
    // marker menu, and Escape does not close it. PV.tap(t, sel) when it is.
    [3.8, { do: "move", to: () => document.querySelector('[id$="channel_slot_1"]'), dur: 1.0 }],
    [4.2, { do: "ring", sel: () => document.querySelector('[id$="channel_slot_1"]'), dur: 3.2 }],
    [5.6, { do: "windows", slots: { 1: [900, 5075] }, dur: 1.4, from: { 1: [900, 9600] } }],
    [8.2, { do: "cursor", show: false }],
    // a hold: the timeline stops, the clock runs in real time, nothing is filmed
    [8.8, { do: "wait", label: "demo work", max: 30,
            until: () => (PV.state.waitT0 ??= Date.now()) && Date.now() - PV.state.waitT0 > 2000 }],
    // cell scale needs its own windows: the tissue-level nucleus window sums to white up close
    [10.2, { do: "windows", slots: { 0: [800, 10000], 1: [900, 5075] }, dur: 1.2 }],
  ],
});
