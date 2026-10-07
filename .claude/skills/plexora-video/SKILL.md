---
name: plexora-video
description: Make a Plexora tutorial or promo video (screen capture of the real app, frame by frame, with camera moves, captions, HUD and title cards in one house style). Use for "make a video / tutorial / demo / promo of ...", "record how to add an image / set up a remote / gate a marker", or any request for an MP4 of Plexora in use.
---

# Plexora videos

Every Plexora video is the **real app**, filmed **one frame at a time** in a
browser whose clock is frozen. A storyboard sets the state of each frame;
the recorder waits until everything has drawn, then takes a screenshot. So:
no dropped frames, no half-loaded tiles, no "the spinner was still there",
and a re-render gives the same video. The look, pacing and overlays come
from one kit, so a 40 s tutorial on adding an image and a 70 s AI promo look
like the same product.

`kit/` is the tooling (copy it per video). `examples/ai-gating-promo/` is
the first video, kept as shipped.

---

## 1. The brief (the prompt)

Start every video from this brief. When the user gives less, fill the gaps
from the defaults below and say which defaults you used. Do not ask about
anything a default covers.

```
Video:        <one line: what the viewer will be able to do / see afterwards>
Type:         tutorial | promo | feature short
Project/data: <project name, or "none -- starts from an empty install">
Real work:    <what must really happen first, e.g. a gating session via the MCP; "none">
Must show:    <the moments that matter, in order>
Spend time on:<the part that is the point -- the camera lives there>
Do not show:  <real hostnames, user names, accounts, keys, other projects ...>
Length:       <target, or "what the task needs">
Ending:       <end card text; stats if any>
Output:       1080p (default) | + 4K | + 720p preview
```

Defaults: tutorial; 1080p30 H.264 plus a 720p preview under 30 MB; silent
(captions carry the story) unless the user asks for a voice-over, then
section 7a (Kokoro US voice `af_heart` at 0.95, interface sounds, no music);
house style (section 3); end card "Plexora" + product line.

Before rendering anything final, show the user **a timing sheet** (a table:
time, screen camera, image camera, action, caption/HUD) and a **contact
sheet** from a quick draft. A final render costs ~25 s of wall clock per
second of video; a draft costs a fifth of that.

---

## 2. Workflow

| # | Step | Output |
|---|---|---|
| 0 | Brief → timing sheet (section 4 rules) | table the user agrees |
| 1 | Real work first, through the Claude Code `plexora` MCP, against the real install (gating session, QC, ...). Harvest what it decided: values, order, narration, evidence artefact ids, real start/finish times. Every number on screen comes from here. | `story` facts |
| 2 | Work dir + isolation + scratch server (section 6) | server on :8791 |
| 3 | Look development: channels, colours, windows, fields (section 5) | stills you have checked |
| 4 | Storyboard (`storyboard.js`) + interface poses measured off a draft frame | |
| 5 | Draft: `node record.mjs --dsf 1 --preview 15` → contact sheet → fix → repeat | |
| 6 | Final: `node record.mjs --dsf 3 --preview 30` (add `--4k` only for promos) | mp4 |
| 6a | Voice-over + sound, if asked (section 7a): script → `tts.py` → retime the storyboard → `audio_events.mjs` → `mix.py` → `mux.sh` | mp4 with audio |
| 7 | QA (section 8) | 0 unsettled, flagged frames looked at |
| 8 | Deliver + clean up (section 9) | files in `~/Downloads/plexora-videos/<slug>/` |

### Setup

```bash
W=<scratchpad>/video-<slug>; mkdir -p "$W"
cp -R .claude/skills/plexora-video/kit/. "$W"/
cd "$W" && mv storyboard.example.js storyboard.js && mv video.example.json video.json
npm init -y >/dev/null && npm i playwright          # uses the installed Chrome (channel "chrome")
python -m pip install imageio-ffmpeg --target pylib # an ffmpeg WITH libx264; nothing system-wide
pylib/imageio_ffmpeg/binaries/ffmpeg* -hide_banner -encoders | grep libx264
python scratch_root.py "$W/data" <project>          # or no project: an empty install
PLEXORA_DATA_PATH="$W/data" python -m plexora <project> --port 8791 --no-browser > server.log 2>&1 &
echo $! > server.pid; until curl -sf http://127.0.0.1:8791/health >/dev/null; do sleep 1; done
```

Use the `plexora` conda environment's python. Port 8791 keeps clear of a
running Plexora (8765) and the test suite.

---

## 3. House style (the same in every video)

**Frame.** 1920×1080 page at 30 fps, dark theme, sRGB, rendered at device
scale 3 (DSF ≥ the largest screen zoom, or zoomed text goes soft). H.264
yuv420p bt709, CRF 15, `+faststart`. 4K is honest only where screen zoom ≤
DSF/2; offer it for promos, not tutorials.

**Colour (microscopy).** Featured/gated marker **red `#ff2b2b`**; reference
marker **green `#2ee86b`**; nucleus **dim blue `#3a5596`**; a fourth context
channel at tissue level only, **cyan `#22d3ee`**. Never yellow under white
outlines (they sum to white). Cell outlines at **100 % opacity**, white.
At most nucleus + marker + one reference in a close-up.

**Overlays** (all in `pv.css`; change them there, never per video):
- gold `#e9c25a` is the only accent: rings, step labels, HUD label, progress bar, card brand line;
- no glow, no shadows beyond the cursor's, no zoom-blur, no spinning, no bounce;
- **caption**: lower third, one short sentence (≤ 12 words), optional gold step label ("Step 2");
- **HUD**: top right, fixed size in the output whatever the camera does: label · current chapter · "k of N" · optional real clock · progress bar;
- **title card** (tutorials, at the start, over the dimmed app) and **end card** (always): brand line, main line, rule, sub line, optional counting stats;
- **ring**: 1.5 px gold rounded rectangle around the control being used, 0.3 s fade in/out;
- **cursor**: white arrow with a dark edge, eased moves on a slight arc, a 5-frame press on click.

**Motion.** Cubic ease-in-out for everything. Screen-camera zoom moves in log
space; long image-camera flights lift ("bump") over coarse tissue. Fade from
black 1.0 s; fade to black 1.2 s. Nothing pops: channels, windows, outlines
and cards all ease in.

---

## 4. Timing rules

The task decides the length; these rules decide the rhythm, so videos feel
the same.

| Beat | Duration |
|---|---|
| Fade in | 1.0 s |
| Title card (tutorial) | 2.4–3.0 s over the app dimmed to 0.75 |
| Establishing full-window shot before the first zoom | 1.5–2.5 s |
| Screen-camera move (zoom/pan) | 0.7–1.2 s; zoom 1.4–2.6 |
| Settle after arriving, before anything happens | ≥ 0.4 s |
| Cursor move | 0.9–1.4 s; 0.25 s pause before the click |
| After a click, before the camera moves on | 0.5–0.8 s |
| Typing | 14–16 characters/s |
| Caption on screen | max(2.5 s, 0.35 s × words + 1.0 s); one at a time; 0.35 s crossfade |
| Result held after it appears | ≥ 1.5 s (2.5 s if it is the point of the step) |
| A real wait (import, connection, AI run) | cut it with a `wait` cue; say the real time in the caption or HUD clock |
| End card | 3–4 s, then 1.2 s fade (≥ 5 s when a voice line plays over it) |
| **With a voice-over** | each beat as long as its line + 0.5–1.0 s; holds on a finding 6–11 s; captions cut to 2–6 words (the voice says the rest), none while a hover card is up |

The shape of a tutorial step: **zoom to the control → caption → cursor → action
→ hold on the result → zoom out**. One task per video; past ~90 s, split it.
A promo spends at least a third of its length zoomed on "the point" (for AI
gating: the sidebar where gates move and the agent explains itself), and
goes wide only for flights across the tissue, the reveal and the title.

**Pace by voice, not by the silent rules.** The silent AI-gating promo ran
72 s; the user asked for the voiced Automatic QC promo to be *slower* than
that, and the agreed cut is 106 s (voice 71 s of it, Kokoro `af_heart` at
speed 0.95). Speed 0.95 is the default; under 0.9 sounds drawn out, at 1.0
it feels hurried over dense visuals. Write the script first, synthesise,
then lay the timeline on `vo/durations.json`.

---

## 5. Look development (do this before the storyboard)

1. **HD mode is on** in every take that shows the image. `prepare()` ticks
   the viewer's own `#viewer_controls_hd` box, calls `setHdMode(true)` and
   throws if it did not take. HD slider windows are **raw 16-bit units**.
2. **Windows from the data, not by eye.** For gated markers:
   `python windows.py <cells.csv> CD45=7.27 SOX10=6.58 ...` (gates are
   `log1p(raw)`, what the Thresholding panel shows): low end at the
   negatives' 90th percentile, high end so the median positive draws at
   ~65 %, references at ~40 %. Without gates, the MCP's `calibrate_display`.
3. **Tune each combination on its own field.** A marker's window beside its
   reference is not its window alone; never copy one setting to all markers.
   Co-expressed references (CD3e over CD45) go much dimmer or they sum to white.
4. **Two scales, two windows.** The pyramid averages pixels at tissue level,
   so wide shots need a tighter high end; the nucleus window that suits the
   whole tissue is too hot at cell scale (lsp11385: nucleus [600,7000] wide,
   [800,10000] close). Ease between them with a `windows` cue as the camera arrives.
5. **Positives must look positive.** A cell just over the gate must be
   visibly lit; if gated cells read as negative, the high end is too high.
   Over-correcting saturation is the usual way to get there.
6. **Fields.** `python regions.py <cells.csv> CD45=7.27 --width 2000 [--ref SOX10=6.58]`
   finds fields with tissue edge to edge, a clear gated/ungated mix and few
   cells on the gate. Avoid background; plan flights over tissue.
7. **Stills.** Render one frame at time *t* with `node record.mjs --from N --to N --preview 1`
   (N = t × 30) and look at it at full size with outlines at 100 %. Check:
   background black, positives lit, references quieter than the marker, no
   white sums. `check_frames.py` flags hot frames to look at.

---

## 6. Isolation and real interaction

- **Scratch data root, always.** `scratch_root.py` copies the project's config
  entry (image paths are absolute, read in place) and small derived files,
  **without the .db**: no gates, no saved channels, nothing written back.
  With no project it is an empty install ("add an image").
- **Settings-changing tutorials** (remotes, preferences, AI keys): settings
  live under the platform config dir, not the data root, so also run the
  server with `HOME=$W/home BIOCOGNIA_DIR="<real config dir>/biocognia"`
  (the licence must survive or paid features vanish). `remotes.json` and
  `nodes.json` are in the data root, already isolated.
- **Stub what must not really happen** in `stubs.mjs` (`page.route`): the AI
  gateway (no credits spent, no run started), remote hosts, accounts. Tell the
  user which on-screen numbers are stubbed placeholders.
- **Never show** real hostnames, user names, IPs, keys, emails or other
  projects. Use `demo-cluster`, `alex`, `example.org`.
- **Real clicks are real.** `click`/`type`/`key` cues drive Chrome's real
  mouse and keyboard, so hovers and focus are genuine -- and a click on a
  combobox opens its menu for the rest of the take, and Escape does not
  close Plexora's marker menu. Click only when the click is the lesson;
  otherwise point and ring. Check every opened menu is closed in the draft.
- **Files** go through `{do:"files", sel:"input[type=file]", paths:[...]}`;
  a native file dialog cannot be filmed. If the app uses a path field,
  type the path.
- **Long real work** (conversion, upload, connection, AI run): a
  `{do:"wait", until:(h)=>..., label, max}` cue stops the timeline, runs the
  clock in real time, films nothing, then carries on: a clean cut.
  `PV.state.lastHeld` is the real wait in ms for the caption.
- **One document per take.** A navigation (project list → viewer) reloads
  the page and wipes the runtime; the recorder stops with an error. Split
  into takes (one `video.json` + storyboard each) and join them with
  ffmpeg's concat demuxer (same encode settings, so `-c copy` works).
- **Real AI work happens before filming**, through the MCP against the real
  install, and the video replays its decisions. MCP `viewer_*` calls are
  instant jumps with no duration, so a live MCP session cannot be filmed
  smoothly. Check which server the MCP is attached to (`server_info`).

---

## 7. Storyboard reference (`storyboard.js`)

`PV.story({...})`, times in seconds. See `kit/storyboard.example.js`.

| Field | Meaning |
|---|---|
| `duration`, `hd` | length; HD on (default `true`) |
| `setup(h)` | async, before frame 0, not filmed: opening channels, tool, state. `h = {X, sb, vc, iv, osd, ctrl}` |
| `screenPoses`, `screenCam` | `{NAME: [cx, cy, zoom]}` in page px (`F` = full window is built in); keys `[[t, "NAME"], ...]` -- hold = two keys on the same pose |
| `imagePoses`, `imageCam` | `{name: [cx, cy, visibleWidth]}` in full-res image px; keys `[[t, "name", bump?]]`; bump ≈ 3 for flights across empty slide |
| `cues` | `[[t, cue]]`, fired once in order: `cursor {show}`, `move {to: sel|fn (an element, or a page point [x, y])|[x,y], dur}`, `click`, `type {text, cps}`, `key {key}`, `files {sel, paths}`, `ring {sel, dur}`, `channels {list:[[name,color,[lo,hi]]]}`, `windows {slots:{k:[lo,hi]}, dur, from?}`, `wait {until, label, max}`, `call {fn(h, n)}` (may return a promise; it is counted, never awaited) |
| `PV.tap(t, sel, {move, pause, ring})` | spread into `cues`: move, pause, click (+ ring) |
| `tracks(s, n, h, out)` | anything continuous the kit does not do (a gate slider sweep) -- a pure function of time |
| `captions` | `[[t0, t1, text or fn(state), stepLabel?]]` |
| `hud` | `{label, from, to, chapters:[[t, name]], total?, progress?(s), clock?:{start, end, seconds} or {segments:[[videoT, realS], ...]}}` -- the clock maps video time onto real time, so its last number is true; `segments` keeps it true at every key (a compressed bulk beat) |
| `titleCard`, `endCard` | `{at, dur?, dim?, brand, main, sub?, stats?:[[value, label, "int"|"time"]]}` |
| `voice`, `sfx` | soundtrack only (the recorder ignores them; section 7a): `[[t, "v01"], ...]` narration line ids; `[[t, "reveal_exclude"|"reveal_warn"|"reveal_all"|"card"|"done"|"end"], ...]` |

Interface poses: render a draft, open a preview frame, read the control's
centre in page px. Selectors beat coordinates for cursor targets (`move.to`
accepts a CSS selector or a function returning an element).

`record.mjs` flags: `--config`, `--dsf`, `--from/--to` (frames; earlier frames
are stepped, not captured), `--name`, `--preview K` (every Kth frame as PNG),
`--4k`.

### 7a. Voice-over and sound (when asked for)

Narration is synthesised locally with Kokoro (no account, no licence fee);
every effect is synthesised from sines and noise in `mix.py`. The video is
rendered silent and the soundtrack muxed on afterwards, so a voice or level
change never costs a re-render.

1. `bash audio_setup.sh` (venv + ~350 MB of model files, in the work dir).
2. `vo_script.json`: `[["v01", "one or two short sentences"], ...]`; spell
   numbers as words ("two hundred thousand"); one idea per line.
3. `tts-venv/bin/python tts.py af_heart 0.95` → `vo/*.wav` + `durations.json`.
   Offer a voice sample (`af_heart`, `am_michael` US; `bf_emma`, `bm_george` UK)
   when the user has no preference.
4. Time the storyboard to the voice, not the other way round: each line
   starts on its beat and ends ≥ 0.5 s before the next; captions shorten to
   a few words where the voice says the rest. A voiced video runs ~1.4× the
   silent pacing.
5. `node audio_events.mjs > audio.json` (voice + `sfx` cues, a click on every
   `click` cue, a whoosh on every bumped flight, a swish on every screen-camera
   move), then `python mix.py audio.json vo soundtrack.wav` (plexora env;
   voice peaks -3 dBFS, effects ducked 5 dB under speech).
6. `bash mux.sh out/<name>-1080p.mp4 soundtrack.wav <final>.mp4` (video
   copied, AAC 192k); add `preview` as a 4th argument for the 720p send copy.

Trap: espeak-ng keeps its data path in a short buffer, so a venv under a long
scratchpad path fails with "phontab not found"; `tts.py` copies the data to a
short temp path (or set `PV_ESPEAK_DATA`).

---

## 8. QA before anything goes to the user

1. `python check_frames.py out/<name>.frames.jsonl` → **0 unsettled**; look at
   every `dark`/`hot` preview yourself. Dark is expected under the title
   card and fades, nowhere else. An unsettled frame mid-pan is usually tiles
   still loading: re-render that range.
2. Contact sheet: `magick montage out/preview/f*.png -tile 6x -geometry 320x180+4+4 -background '#111' contact.png`, read it.
3. `ffmpeg -i out.mp4` → duration, 1920x1080, 30 fps, yuv420p bt709.
4. Every number on screen traces to the real run, or is called out as a placeholder.
5. Isolation: the real project's `.db` mtime is unchanged by the capture;
   `git status` shows nothing new in the repo.

---

## 9. Delivery and clean-up

- `~/Downloads/plexora-videos/<slug>/<Title>-1080p.mp4` (+ `-4k.mp4`); the
  previous version moves to `v<N>/` beside it.
- A 720p preview for sending (SendUserFile caps at 30 MiB): two-pass x264 at
  ~2900 kbit/s for ~70 s; scale the bitrate to the length.
- Stop the scratch server (`kill $(cat server.pid)`); close nothing else.
  The work dir stays in the scratchpad.
- Report: where the files are, what is real and what is stubbed, render
  time, anything imperfect you chose to leave.

---

## 10. Lessons learned (each cost a re-render)

**Capture**
- Install the fake clock **before** `goto`: OpenSeadragon captures
  `requestAnimationFrame`/`Date.now` at load. CSS animations are not on that
  clock -- the recorder seeks them through CDP every frame.
- Never `await` a timer inside a cue (`Promise.race` with `setTimeout`, a
  debounce): the clock is frozen, it never fires. Return the promise; the
  settle step waits on a counter while ticking the clock 16 ms at a time.
- OSD loads one new tile per frame by default; `prepare()` raises
  `maxTilesPerFrame` to 64 or every settle takes dozens of ticks.
- A static camera reads as a slideshow. The screen camera (a crop of the
  page) is what makes it a video: zoom to what is changing, go wide to travel.
- Long image-camera pans across empty slide stream black tiles mid-flight:
  bump the zoom out on the way (bump 3.0 for half-slide flights).
- The HUD, captions and cards ride on the crop (`#pv-out`) so they stay
  fixed and sharp; the cursor and rings belong to the page and zoom with it.

**The app**
- `PlexoraViewerScene.fitRegion(iv, rect, {immediately:true})` takes the
  Plexora viewer (`__plexora.seaDragonViewer`), not the OSD viewer.
- `setSlotEnabled(k, false)` deactivates a channel **by name**: if another
  slot shows the same marker, its layer goes too. Switch off only slots
  showing some other marker (see `offUnless` in the example).
- Opening the gating tool picks a default marker and shows its outlines at
  70 %: set the shot's marker at once, outlines at opacity 0, then ease to 100.
- Script-level classes (`CSVGatingList`) are not on `window`: use
  `typeof X !== "undefined" ? X : window.X`.
- `setGateMarker(name)` is a no-op for the current marker: pass `{force: true}`.
- The project registry name is case-sensitive (`lsp11385`, not `LSP11385`).
- **HD mode puts several items in the OSD world**: convert image → page
  points with `osd.world.getItemAt(0).imageToViewerElementCoordinates`, not
  `osd.viewport` (wrong by the item's offset; the hover never hit).
- **QC hover cards**: OSD's MouseTracker fires a leave for a pointer that
  has not moved and the card closes. While a hover beat is on, swap
  `qc.hover.clear` for a no-op, set `qc.hover.position` and call
  `resolve()` every frame (a track); restore `clear` after. Pick the point
  inside the outline with `qc.overlay.hitTest` (image coordinates).
- **QC turns cell outlines on** whenever the panel reloads; assert
  `data-cell-mode` from a track every frame, not once in a cue.
- **Agent-card replay**: dispatch `plexora:agent-state-changed` with
  `kind: "qc.session"` (`started`, `phase` with `progress.bulk`, `issued`
  with an evidence `artifact_id`, `unit_closed`, `finished`); stub
  `/agent/v1/captures/<id>` to serve the real run's sheets read-only.
- **Region fade-ins**: keep regions hidden with `qc.hidden` (`r:<roi_id>`)
  recomputed per frame, and wrap `overlay.drawRegion` once to scale its
  colour alpha; never click region rows (they refit the view).
- Opening a tool does a `pushState`; the recorder now tolerates
  same-document navigations and fails only on a real reload.

**Look** (what the user corrected)
- Over-saturated first: yellow marker + white outlines + bright nucleus sum to white.
- Then over-corrected: gated cells so dim they looked negative. Both are wrong;
  the data-derived windows in section 5 sit between them.
- Outline opacity stays at 100 %: it is the most important thing on screen in a gating video.
- Pick fields with little background and a clear gated/ungated split.

**Process**
- Show a draft contact sheet before a final render; the user's notes came
  from looking (static camera, too little time on the AI sidebar, wanting a
  marker/progress/elapsed indicator, stats on the title card).
- The HUD clock shows **real** elapsed time compressed onto the video, so
  the number it ends on is true. Say so when asked.
- zsh splits words differently and has no `timeout`: drive multi-step
  shell work from Python or `bash -c`.
- A `wait` hold advances the page clock with the wall clock, so the app's
  own polling keeps pace with the real work (the first version ticked a fixed
  200 ms per loop and a 2 s wait took 25 s).
- A final at DSF 3 runs ~0.7–0.8 s/frame (a 70 s video ≈ 30 min; the 106 s
  QC promo with `--4k` took 36 min). Draft at DSF 1 (~0.25 s/frame) and
  render the final in the background.
- Render the picture silent and mux audio afterwards: a voice, wording or
  level change is a 1-minute re-mix, not a 36-minute re-render. Send a
  short voice sample (4 voices, one line each) before the first draft.
- Captions over a hover card collide: drop the caption for that beat;
  the voice carries it.
