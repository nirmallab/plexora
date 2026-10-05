// Read the storyboard's own timeline and list what the soundtrack needs:
// narration lines, clicks (every click cue), camera moves (whooshes) and the
// storyboard's sfx cues. node audio_events.mjs [storyboard.js] > audio.json
import fs from "fs";
const file = process.argv[2] || "storyboard.js";
let cfg = null;
const st = {};
const PV = {
  story: (d) => { cfg = d; }, state: st, clamp01: (x) => Math.max(0, Math.min(1, x)),
  tap: (t, sel, { move = 1.1, pause = 0.25, ring = false } = {}) =>
    [[t, { do: "move", to: sel, dur: move }], [t + move + pause, { do: "click" }]],
};
new Function("PV", "window", "document", "OpenSeadragon", fs.readFileSync(file, "utf8"))(PV, {}, {}, {});
const ev = [];
for (const [t, id] of cfg.voice || []) ev.push({ t, kind: "voice", id });
for (const [t, kind] of cfg.sfx || []) ev.push({ t, kind });
for (const [t, c] of cfg.cues) if (c.do === "click") ev.push({ t, kind: "click" });
// image-camera flights with a bump: a soft whoosh over the flight
const K = cfg.imageCam || [];
for (let i = 1; i < K.length; i++) if ((K[i][2] || 0) >= 1.0) ev.push({ t: K[i - 1][0], kind: "whoosh", dur: K[i][0] - K[i - 1][0] });
// screen-camera moves between different poses: a lighter swish
const S = cfg.screenCam || [];
for (let i = 1; i < S.length; i++) if (S[i][1] !== S[i - 1][1]) ev.push({ t: S[i - 1][0], kind: "swish", dur: S[i][0] - S[i - 1][0] });
ev.sort((a, b) => a.t - b.t);
process.stdout.write(JSON.stringify({ duration: cfg.duration, events: ev }, null, 1));
