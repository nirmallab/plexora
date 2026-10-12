/**
 * videoStudioSidebarController.js - Video Studio's controller: the bar, the
 * presets, preview, saving and export, wired together.
 *
 * Card-less, like Figure Builder: the plugin declares no panel, so the tool
 * loader gives it no sidebar card, and this mounts the Quick Capture bar over
 * the image instead (videoStudioBar.js), with its own close button.
 *
 * THE FLOW. Opening the tool reads `/api/state` (presets for this image,
 * encoders, saved videos), captures the viewer as the video's starting scene,
 * picks a preset for what is on screen (a turn in 3-D, a sweep on a stack, a
 * time-lapse on a series, a zoom on a flat image) and builds it on the server
 * (`/api/presets/build`) -- one implementation of every preset, shared with
 * the agent. Every parameter change rebuilds and keeps playing from the same
 * fraction of the way through. Export saves the video (a document in
 * `data_path/.videos/`), gives the viewer back, and renders it
 * (videoExportLoop.js).
 *
 * THE STARTING SCENE follows the viewer: while no preview is showing, moving
 * the image (or turning the 3-D view) and then playing starts the video from
 * there. "Set end view" is the exception: it records where the user moved to
 * as the END, and keeps the start where it was.
 *
 * Classic script; registers itself with `window.Plexora.registerPlugin`.
 */
(function (global) {
    "use strict";

    const TOOL = "video_studio";
    const PRESET_KEY = "plexora.video_studio.preset";
    const PARAMS_KEY = "plexora.video_studio.params";

    function readJson(key, fallback) {
        try {
            const raw = global.localStorage?.getItem(key);
            return raw ? JSON.parse(raw) : fallback;
        } catch (error) {
            return fallback;
        }
    }

    function writeJson(key, value) {
        try {
            global.localStorage?.setItem(key, JSON.stringify(value));
        } catch (error) {
            // a private window remembers nothing; the defaults still work
        }
    }

    function near(a, b, eps = 1e-4) {
        if (a === b) return true;
        if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) <= eps * Math.max(1, Math.abs(a));
        if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => near(v, b[i], eps));
        if (a && b && typeof a === "object" && typeof b === "object") {
            const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
            return [...keys].every((k) => near(a[k], b[k], eps));
        }
        return false;
    }

    /** Whether two scenes start the same video (where the camera is, which
     *  plane, which channels look how). */
    function sameStart(a, b) {
        if (!a || !b) return false;
        return near(a.view, b.view) && near(a.camera3d, b.camera3d) && near(a.viewport, b.viewport)
            && near((a.channels || []).map((c) => [c.key, c.color, c.window, c.visible]),
                    (b.channels || []).map((c) => [c.key, c.color, c.window, c.visible]));
    }

    class VideoStudioController {
        constructor(ctx) {
            this.ctx = ctx;
            this.api = global.PlexoraVideoProjectApi;
            this.state = null;
            this.presetName = null;
            this.params = readJson(PARAMS_KEY, {});
            this.project = null;
            this.warnings = [];
            this.saved = { videoId: null, revision: null, digest: null };
            this.baseScene = null;
            this.facts = null;
            this.exporting = false;
            this.mounted = false;
            this.building = null;
            this.buildSeq = 0;
            this.env = this.environment();
            this.bar = new global.PlexoraVideoStudioBar({
                onPresetMenu: (anchor) => this.openPresetMenu(anchor),
                onStart: () => this.preview.seek(0),
                onPlay: () => this.togglePlay(),
                onLoop: (on) => { this.preview.loop = on; },
                onSeek: (t) => { this.preview.pause(); this.preview.seek(t); },
                onScrubStart: () => this.preview.pause(),
                onExport: () => this.exportNow(),
                onMore: (anchor) => this.openMoreMenu(anchor),
                onClose: () => this.close(),
            });
            this.preview = global.PlexoraVideoPreview.create(this.env, {
                getProject: () => this.project,
                getAspect: () => this.aspect(),
                onTime: (t) => {
                    this.bar.setTime(t, this.project?.duration_s || 0);
                    this.timelineTick?.(t);
                },
                onPlayState: (playing) => this.bar.setPlaying(playing),
                onSessionChange: (active) => this.bar.root?.classList.toggle("is-previewing", active),
            });
            this.history = global.PlexoraVideoHistory.create({
                onChange: (status) => this.timeline?.setHistory?.(status) });
            this.onKey = (event) => this.handleKey(event);
            ctx.onCleanup?.(() => this.destroy());
            // A handle for scripts and the integration test: the same paths the
            // bar takes, without the dialog.
            global.PlexoraVideoStudio = {
                controller: this,
                ready: () => this.ready,
                choosePreset: (name) => this.choose(name),
                setParams: (params) => {
                    this.params[this.presetName] = { ...(this.params[this.presetName] || {}), ...params };
                    for (const [name, value] of Object.entries(params)) this.fields?.set(name, value);
                    return this.rebuild({ keepBase: true });
                },
                state: () => ({ preset: this.presetName, project: this.project, saved: { ...this.saved },
                                playing: this.preview.playing, time: this.preview.time,
                                exporting: this.exporting, progress: this.progress || null }),
                exportNow: (settings, route) => this.runExport({ settings, route }),
                routes: async (settings) => global.PlexoraVideoEncoders.available(
                    settings, await this.api.encoders()),
                cancel: () => this.cancelExport(),
            };
        }

        /** The controller's handle on the page, passed to every module. */
        environment() {
            const ctx = this.ctx;
            const core = () => global.__plexora || {};
            const controller = this;
            return {
                ctx,
                get config() { return ctx.config || core().config; },
                get datasource() { return ctx.datasource || global.flaskVariables?.datasource || ""; },
                get iv() { return ctx.viewer || core().seaDragonViewer; },
                get vs() { return core().viewState || null; },
                get renderer() { return core().volumeView?.renderer || null; },
                get volumeView() { return core().volumeView || null; },
                get sidebar() { return core().viewerSidebar || null; },
                get panes() { return core().sectionPanes || null; },
                get planeMotion() { return core().planeMotion || null; },
                get volumeWindows() { return core().volumeWindows || null; },
                get depthStrip() { return core().depthStrip || null; },
                get wrapper() { return document.getElementById("openseadragon_wrapper"); },
                get facts() { return controller.facts; },
                network: null,
                touchedWindows: false,
            };
        }

        aspect() {
            const out = this.project?.output;
            return out && out.width && out.height ? out.width / out.height : 16 / 9;
        }

        // -- lifecycle (core's plugin hooks) -------------------------------------

        setup() {
            this.mount();
        }

        fetchSaved() {
            return null;
        }

        applyOrDefault() {
            return undefined;
        }

        onShow() {
            this.mount();
            global.addEventListener("keydown", this.onKey, true);
        }

        onHide() {
            global.removeEventListener("keydown", this.onKey, true);
            this.preview.pause();
        }

        async close() {
            await this.preview.end();
            if (global.PlexoraToolLoader?.removeTool) global.PlexoraToolLoader.removeTool(TOOL);
            else this.destroy();
        }

        destroy() {
            global.removeEventListener("keydown", this.onKey, true);
            this.abort?.abort();
            this.preview.end();
            this.detachAgent?.();
            this.detachFigure?.();
            this.pathEditor?.destroy?.();
            this.timeline?.destroy?.();
            this.bar.destroy();
            this.mounted = false;
            if (global.PlexoraVideoStudio?.controller === this) delete global.PlexoraVideoStudio;
        }

        async mount() {
            if (this.mounted) return;
            this.mounted = true;
            const wrapper = this.env.wrapper;
            if (!wrapper) return;
            this.bar.mount(wrapper);
            this.bar.setHint("Loading…");
            this.detachAgent = global.PlexoraVideoAgentBridge.attach(this);
            this.detachFigure = global.PlexoraVideoFigureBridge.attach(this);
            try {
                this.state = await this.api.state(this.env.datasource);
            } catch (error) {
                if (error.status === 403 && error.payload?.license) {
                    // Not offered without its licence (plugins.unlocked); a
                    // licence that lapsed while the page was open lands here.
                    this.bar.showError("Video Studio is not included in this licence.");
                } else {
                    this.bar.showError(`Video Studio could not start: ${error.message}`);
                }
                return;
            }
            this.facts = { ...this.state.facts, ...this.liveFacts() };
            this.baseScene = global.PlexoraVideoSceneState.capture(this.env);
            const note = global.PlexoraVideoFigureBridge.takeAnimateNote(this.env.datasource);
            if (note) {
                const { scene, defaulted } = global.PlexoraVideoSceneState.fromFigureScene(
                    note.scene, this.baseScene);
                this.baseScene = { ...this.baseScene, ...scene };
                if (defaulted.length) this.bar.setHint(`From the figure panel; ${defaulted.join(" and ")} from the viewer.`);
            }
            this.presetName = this.choosePreset();
            this.showPreset();
            await this.rebuild({ play: false });
            this.ready = true;
        }

        liveFacts() {
            try {
                const facts = global.PlexoraVideoSceneState.facts(this.env);
                // Keep only what the live viewer actually knows.
                return Object.fromEntries(Object.entries(facts).filter(([, v]) =>
                    v !== null && v !== undefined && !(Array.isArray(v) && (!v.length || v.every((x) => !x)))));
            } catch (error) {
                return {};
            }
        }

        choosePreset() {
            const available = new Set((this.state.presets || []).filter((p) => p.available).map((p) => p.name));
            const mode = this.env.vs?.get?.().mode;
            const facts = this.facts || {};
            let preferred = "zoom";
            if ((facts.depth || 1) > 1) preferred = mode === "volume" ? "orbit" : "z_sweep";
            else if ((facts.frames || 1) > 1) preferred = "time_lapse";
            const remembered = readJson(PRESET_KEY, null);
            // The remembered preset only while it suits what is on screen: a
            // turn remembered from a 3-D stack is no use on a flat slide.
            if (remembered && available.has(remembered) && this.presetFits(remembered, mode)) return remembered;
            return available.has(preferred) ? preferred : [...available][0] || "zoom";
        }

        presetFits(name, mode) {
            const preset = this.preset(name);
            if (!preset) return false;
            if (preset.group === "3d") return mode === "volume";
            return true;
        }

        preset(name = this.presetName) {
            return (this.state?.presets || []).find((p) => p.name === name) || null;
        }

        showPreset() {
            const preset = this.preset();
            this.bar.setPreset(preset);
            const values = { ...(this.params[this.presetName] || {}) };
            this.fields = global.PlexoraVideoParamFields.render(this.bar.fields, preset?.fields || [], values, {
                channels: (this.facts?.channels || []).filter((c) => c.color),
                onChange: async (name, value, all) => {
                    if (this.customized && !(await this.confirmRebuild())) {
                        this.fields?.set(name, (this.params[this.presetName] || {})[name]);
                        return;
                    }
                    this.params[this.presetName] = all;
                    writeJson(PARAMS_KEY, this.params);
                    this.building = this.rebuild({ keepBase: name === "end_view" });
                    if (name === "end_view" && value) this.afterEndView();
                },
                captureView: () => this.captureEndView(),
            });
        }

        // -- building ----------------------------------------------------------------

        /** Re-capture the starting scene when the user moved the viewer since
         *  (and no preview frame is what is on screen). */
        ensureBase() {
            if (this.preview.active || this.exporting) return;
            const now = global.PlexoraVideoSceneState.capture(this.env);
            if (!sameStart(now, this.baseScene)) this.baseScene = now;
        }

        async rebuild({ play = null, keepBase = false } = {}) {
            if (!this.presetName || !this.state) return;
            if (!keepBase) this.ensureBase();
            const seq = ++this.buildSeq;
            this.buildAbort?.abort();
            const abort = typeof AbortController === "function" ? new AbortController() : null;
            this.buildAbort = abort;
            const wasPlaying = this.preview.playing;
            const params = { ...(this.params[this.presetName] || {}) };
            let answer;
            try {
                answer = await this.api.build({
                    preset: this.presetName, params, datasource: this.env.datasource,
                    facts: this.facts, scene: this.baseScene }, abort?.signal);
            } catch (error) {
                if (error?.name === "AbortError" || seq !== this.buildSeq) return;
                this.bar.showError(error.message);
                return;
            }
            if (seq !== this.buildSeq) return;
            const previous = this.project;
            this.project = this.withOutput(answer.document, previous);
            this.warnings = answer.warnings || [];
            this.preview.invalidate();
            if (previous) this.preview.rescale(previous.duration_s, this.project.duration_s);
            else this.bar.setTime(0, this.project.duration_s);
            this.bar.showError("");
            const notes = [...(this.project.notes || []), ...this.warnings.map((w) => w.message)];
            this.bar.setHint(notes.join(" ") || this.preset()?.summary || "");
            this.bar.setMarks(this.marks());
            this.timeline?.setProject?.(this.project);
            this.pathEditor?.setProject?.(this.project);
            if (play === true || (play === null && wasPlaying)) this.preview.play();
        }

        /** Keep the export settings the user chose when a rebuild replaces the
         *  document (the preset knows nothing about them). */
        withOutput(document, previous) {
            const output = this.outputChoice || previous?.output;
            return output ? { ...document, output: { ...document.output, ...output } } : document;
        }

        marks() {
            const out = [];
            for (const track of this.project?.tracks || []) {
                if (track.mode === "procedural") {
                    out.push({ t: track.range[0], kind: "range" }, { t: track.range[1], kind: "range" });
                } else {
                    for (const kf of track.keyframes) out.push({ t: kf.t, kind: "key" });
                }
            }
            return out;
        }

        captureEndView() {
            this.preview.pause();
            const scene = global.PlexoraVideoSceneState.capture(this.env);
            if (scene.view.mode === "volume") return { camera3d: scene.camera3d };
            return { viewport: scene.viewport };
        }

        async afterEndView() {
            // Show where the move now starts, so the end the user just set is
            // the thing they see the move travel to.
            await this.building;
            this.preview.seek(0);
        }

        // -- playback ------------------------------------------------------------------

        togglePlay() {
            if (this.preview.playing) {
                this.preview.pause();
                return;
            }
            if (!this.preview.active) {
                const before = this.baseScene;
                this.ensureBase();
                if (before !== this.baseScene) {
                    this.rebuild({ play: true, keepBase: true });
                    return;
                }
            }
            this.preview.play();
        }

        currentFrameState() {
            if (!this.project) return null;
            return global.PlexoraVideoAnimation.evaluate(this.project, this.preview.time);
        }

        restoreFigurePart(part) {
            if (!part?.view) return false;
            try {
                global.PlexoraVideoSceneState.applyView(this.env, part.view);
                if (part.camera3d && part.view.mode === "volume") {
                    this.env.vs?.settled?.().then(() =>
                        global.PlexoraVideoSceneState.applyCamera(this.env, part.camera3d));
                }
                return true;
            } catch (error) {
                return false;
            }
        }

        // -- menus -------------------------------------------------------------------------

        openPresetMenu(anchor) {
            const presets = this.state?.presets || [];
            const items = [];
            for (const group of this.state?.groups || []) {
                const inGroup = presets.filter((p) => p.group === group.name);
                if (!inGroup.length) continue;
                if (items.length) items.push({ separator: true });
                items.push({ label: group.label, disabled: true, className: "vs-menu-heading" });
                for (const preset of inGroup) {
                    items.push({
                        label: preset.label,
                        checked: preset.name === this.presetName,
                        disabled: !preset.available,
                        title: preset.available ? preset.summary : preset.reason,
                        onSelect: () => this.choose(preset.name),
                    });
                }
            }
            const videos = (this.state?.videos || []).filter((v) => v.readable).slice(0, 8);
            if (videos.length) {
                items.push({ separator: true });
                items.push({ label: "Saved for this image", disabled: true, className: "vs-menu-heading" });
                for (const video of videos) {
                    items.push({ label: video.title, checked: video.video_id === this.saved.videoId,
                                 onSelect: () => this.loadVideo(video.video_id) });
                }
            }
            global.PlexoraMenu?.open(anchor, items, { align: "left" });
        }

        openMoreMenu(anchor) {
            const items = [
                { label: "Camera path editor", title: "Keyframe the camera (P)",
                  checked: Boolean(this.pathEditor?.active), onSelect: () => this.togglePathEditor() },
                { label: "Timeline", title: "Every track on one timeline (T)",
                  checked: Boolean(this.timeline?.active), onSelect: () => this.toggleTimeline() },
                { separator: true },
                { label: this.recorder?.active ? "Stop recording" : "Record an exploration",
                  title: "Record where you take the view, as an editable video",
                  onSelect: () => this.toggleRecording() },
                { label: "Start from the current view", onSelect: () => this.restartFromView() },
                { label: "Back to my view", disabled: !this.preview.active,
                  onSelect: () => this.preview.end() },
                { separator: true },
                { label: this.saved.videoId ? "Save changes" : "Save video", onSelect: () => this.save(true) },
                { label: "Download project (.plexvid)", disabled: !this.saved.videoId,
                  onSelect: () => { global.location.href = this.api.documentUrl(this.saved.videoId); } },
                { label: "Send frame to Figure Builder", onSelect: () => this.sendFrameToFigure() },
            ];
            global.PlexoraMenu?.open(anchor, items, { align: "right" });
        }

        /** A preset rebuild replaces edits made in the path editor or the
         *  timeline; asked first. */
        async confirmRebuild() {
            const ask = global.PlexoraConfirm?.ask;
            const ok = ask ? await ask({ title: "Start again from the preset?",
                                         body: "This video has edits from the camera path editor or the timeline. "
                                             + "Changing the preset's settings rebuilds it and discards them.",
                                         confirm: "Rebuild", cancel: "Keep my edits" }) : true;
            if (ok) {
                this.customized = false;
                this.history.clear();
            }
            return ok;
        }

        async choose(name) {
            if (name === this.presetName) return;
            if (this.customized && !(await this.confirmRebuild())) return;
            this.preview.pause();
            await this.preview.end();
            this.presetName = name;
            writeJson(PRESET_KEY, name);
            this.saved = { videoId: null, revision: null, digest: null };
            this.history.clear();
            this.showPreset();
            await this.rebuild({ play: true });
        }

        async restartFromView() {
            await this.preview.end();
            this.baseScene = global.PlexoraVideoSceneState.capture(this.env);
            await this.rebuild({ keepBase: true });
        }

        // -- saving --------------------------------------------------------------------------

        digest(project) {
            return JSON.stringify({ tracks: project.tracks, base: project.base, output: project.output,
                                    duration: project.duration_s, loop: project.loop, title: project.title });
        }

        /** The project as a saved video; created on first save, replaced
         *  after. Resolves the video id. */
        async save(announce = false) {
            if (!this.project) return null;
            const digest = this.digest(this.project);
            if (this.saved.videoId && this.saved.digest === digest) return this.saved.videoId;
            try {
                if (!this.saved.videoId) {
                    const answer = await this.api.create({ document: this.project });
                    this.saved = { videoId: answer.video_id, revision: answer.document.revision, digest };
                } else {
                    const answer = await this.api.replace(this.saved.videoId, this.saved.revision, this.project);
                    this.saved = { ...this.saved, revision: answer.revision, digest };
                }
                this.project = { ...this.project, video_id: this.saved.videoId, revision: this.saved.revision };
                if (announce) global.PlexoraToast?.show({ title: "Video saved", note: this.project.title, timeout: 4000 });
                this.state.videos = (await this.api.list(this.env.datasource)).videos;
                return this.saved.videoId;
            } catch (error) {
                if (error.status === 409) {
                    this.bar.showError("This video changed in another tab. Reload it from the preset menu.");
                } else {
                    this.bar.showError(`Could not save: ${error.message}`);
                }
                throw error;
            }
        }

        async loadVideo(videoId, { quiet = false } = {}) {
            if (!videoId) return null;
            const answer = await this.api.get(videoId);
            const document = answer.document;
            await this.preview.end();
            this.project = document;
            this.saved = { videoId, revision: document.revision, digest: this.digest(document) };
            this.presetName = document.provenance?.preset?.name || this.presetName;
            if (document.provenance?.preset?.params) {
                this.params[this.presetName] = document.provenance.preset.params;
            }
            this.baseScene = document.base;
            this.showPreset();
            this.bar.setTime(0, document.duration_s);
            this.bar.setMarks(this.marks());
            this.timeline?.setProject?.(document);
            this.pathEditor?.setProject?.(document);
            if (!quiet) this.bar.setHint(`Opened “${document.title}”.`);
            return { video_id: videoId, revision: document.revision };
        }

        /** Edits from the path editor or timeline: through the server's ops
         *  route (saved first if need be), onto the undo stack. */
        async edit(operations, label = "Edit") {
            const videoId = await this.save();
            if (!videoId) return null;
            const answer = await this.api.ops(videoId, this.saved.revision, operations);
            this.customized = true;
            this.applyEdited(answer);
            this.history.push({ label, forward: operations, inverse: answer.inverse });
            return answer;
        }

        applyEdited(answer) {
            const previous = this.project;
            this.project = answer.document;
            this.saved = { ...this.saved, revision: answer.revision, digest: this.digest(answer.document) };
            this.preview.invalidate();
            if (previous && previous.duration_s !== this.project.duration_s) {
                this.preview.rescale(previous.duration_s, this.project.duration_s);
            }
            this.bar.setTime(this.preview.time, this.project.duration_s);
            this.bar.setMarks(this.marks());
            this.timeline?.setProject?.(this.project);
            this.pathEditor?.setProject?.(this.project);
            if (this.preview.active && !this.preview.playing) this.preview.seek(this.preview.time);
        }

        async undo() {
            const entry = this.history.undo();
            if (!entry) return;
            try {
                const answer = await this.api.ops(this.saved.videoId, this.saved.revision, entry.inverse);
                // What the server says undoes the undo is the exact redo.
                this.history.settle(entry, { forward: answer.inverse });
                this.applyEdited(answer);
            } catch (error) {
                this.history.restore(entry, "undo");
                this.bar.showError(`Could not undo: ${error.message}`);
            }
        }

        async redo() {
            const entry = this.history.redo();
            if (!entry) return;
            try {
                const answer = await this.api.ops(this.saved.videoId, this.saved.revision, entry.forward);
                this.history.settle(entry, { inverse: answer.inverse });
                this.applyEdited(answer);
            } catch (error) {
                this.history.restore(entry, "redo");
                this.bar.showError(`Could not redo: ${error.message}`);
            }
        }

        // -- recording ---------------------------------------------------------------

        async toggleRecording() {
            if (!this.recorder) this.recorder = global.PlexoraVideoRecorder.create(this);
            if (!this.recorder.active) {
                await this.preview.end();
                this.recorder.start();
                this.bar.root?.classList.add("is-recording");
                this.bar.setHint("Recording: explore the image, then More > Stop recording.");
                return;
            }
            const body = this.recorder.stop();
            this.bar.root?.classList.remove("is-recording");
            if (!body || !body.tracks.length) {
                this.bar.setHint("Nothing moved while recording.");
                return;
            }
            try {
                const answer = await this.api.create({ document: body });
                await this.loadVideo(answer.video_id);
                this.customized = true;
                this.bar.setHint(`Recorded ${body.duration_s.toFixed(1)} s as an editable video.`);
                if (!this.timeline?.active) this.toggleTimeline();
            } catch (error) {
                this.bar.showError(`Could not keep the recording: ${error.message}`);
            }
        }

        // -- path editor and timeline (Phase 2 modules) --------------------------------

        togglePathEditor() {
            if (!global.PlexoraVideoPathEditor) return;
            if (!this.pathEditor) this.pathEditor = global.PlexoraVideoPathEditor.create(this);
            this.pathEditor.toggle();
        }

        toggleTimeline() {
            if (!global.PlexoraVideoTimeline) return;
            if (!this.timeline) this.timeline = global.PlexoraVideoTimeline.create(this);
            this.timeline.toggle();
        }

        // -- export ---------------------------------------------------------------------------

        isRemote() {
            const host = global.location?.hostname || "";
            return !(host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "");
        }

        async exportNow(options = {}) {
            if (this.exporting || !this.project) return null;
            this.preview.pause();
            const volume = this.project.base?.view?.mode === "volume"
                || this.project.tracks.some((t) => t.kind === "camera3d");
            let report = this.state?.encoders;
            try {
                report = await this.api.encoders();
            } catch (error) {
                // the cached report from opening will do
            }
            let choice = options.choice || null;
            if (!choice) {
                choice = await global.PlexoraVideoExportUi.open({
                    project: this.project, output: this.project.output, report,
                    prefer: options.prefer || "browser",
                    volume, remote: this.isRemote() });
            }
            if (!choice) return null;
            this.outputChoice = { width: choice.settings.width, height: choice.settings.height,
                                  fps: choice.settings.fps, format: choice.settings.format,
                                  quality: choice.settings.quality,
                                  transparent: choice.settings.transparent,
                                  overlays: choice.settings.overlays,
                                  attribution: choice.settings.attribution };
            this.project = { ...this.project, output: { ...this.project.output, ...this.outputChoice } };
            return this.runExport(choice, options);
        }

        async runExport(choice, { onStarted = null } = {}) {
            this.exporting = true;
            this.progress = { done: 0, total: 0 };
            this.bar.setBusy(true);
            this.bar.showError("");
            const abort = new AbortController();
            this.abort = abort;
            try {
                const videoId = await this.save();
                // The user's own view back before the render borrows the
                // viewer: what the render gives back at the end is that.
                await this.preview.end();
                const plugin = global.Plexora?.plugins?.get?.(TOOL);
                const run = global.PlexoraVideoExportLoop.run(this.env, this.project, {
                    settings: { ...choice.settings }, route: choice.route, api: this.api, videoId,
                    signal: abort.signal, remote: this.isRemote(),
                    pluginVersion: plugin?.version || "",
                    onCancel: () => abort.abort(),
                    extra: { datasetName: this.env.datasource, attribution: choice.settings.attribution,
                             disclose: true },
                    onProgress: ({ done, total }) => {
                        this.bar.setHint(`Rendering frame ${done} of ${total}`);
                        this.progress = { done, total };
                    },
                });
                onStarted?.({ video_id: videoId });
                const { result, saved } = await run;
                this.announceDone(result, saved);
                this.state.videos = (await this.api.list(this.env.datasource)).videos;
                return result;
            } catch (error) {
                if (error?.name === "AbortError") {
                    this.bar.setHint("Export stopped. Nothing was saved.");
                    return null;
                }
                const message = error?.name === "ReadinessTimeout"
                    ? `Stopped: ${error.message}. Nothing unfinished was written into the video.`
                    : `Export failed: ${error.message}`;
                this.bar.showError(message);
                global.PlexoraToast?.show({ title: "Export failed", note: message, tone: "warning",
                                            timeout: 0 });
                throw error;
            } finally {
                this.exporting = false;
                this.abort = null;
                this.bar.setBusy(false);
            }
        }

        announceDone(result, saved) {
            const size = result.bytes ? ` (${(result.bytes / 1e6).toFixed(result.bytes > 1e7 ? 0 : 1)} MB)` : "";
            const actions = [];
            if (result.kind === "download" && result.url) {
                actions.push({ label: "Download", primary: true, onSelect: () => {
                    // An attachment link: the browser saves it in place, and
                    // the desktop app stages it through its own save dialog.
                    const link = document.createElement("a");
                    link.href = result.url;
                    link.download = result.name || "";
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                } });
            }
            const title = result.kind === "blob"
                ? (saved?.saved ? `Saved ${result.name}${size}` : `${result.name} was not saved`)
                : `${result.name}${size} is ready`;
            global.PlexoraToast?.show({ title, note: "A manifest of every frame is kept with the video.",
                                        actions, timeout: 20000 });
            global.PlexoraDesktop?.notifyIfAway?.({ title: "Video ready", body: result.name || "" });
            this.bar.setHint(title);
        }

        cancelExport() {
            this.abort?.abort();
            return { cancelled: Boolean(this.exporting) };
        }

        // -- agent ---------------------------------------------------------------------------

        async agentPreview({ video_id: videoId, at_s: at, play = true }) {
            if (videoId && videoId !== this.saved.videoId) await this.loadVideo(videoId);
            if (Number.isFinite(at)) this.preview.seek(at);
            if (play) this.preview.play();
            else this.preview.pause();
            return { video_id: this.saved.videoId, at_s: this.preview.time,
                     duration_s: this.project?.duration_s ?? null };
        }

        async agentExport({ video_id: videoId, output = {}, prefer = "server" }) {
            if (videoId && videoId !== this.saved.videoId) await this.loadVideo(videoId);
            if (!this.project) throw new Error("no video is open");
            const settings = {
                width: output.width || this.project.output.width, height: output.height || this.project.output.height,
                fps: output.fps || this.project.output.fps, format: output.format || this.project.output.format,
                quality: output.quality || this.project.output.quality,
                transparent: Boolean(output.transparent),
                overlays: { ...this.project.output.overlays, ...(output.overlays || {}) },
                attribution: output.attribution || this.project.output.attribution || "",
            };
            let report = this.state?.encoders;
            try {
                report = await this.api.encoders();
            } catch (error) {
                // the cached report will do
            }
            const rows = await global.PlexoraVideoEncoders.available(settings, report, { prefer });
            const route = rows.find((r) => r.format === settings.format && r.available)
                || global.PlexoraVideoEncoders.defaultRoute(rows);
            if (!route) throw new Error("no encoder can write this format here");
            settings.format = route.format;
            const started = new Promise((resolve) => {
                this.runExport({ settings, route }, { onStarted: resolve }).catch(() => resolve(null));
            });
            await started;
            // The export id appears once the server writer has been opened.
            for (let i = 0; i < 100; i += 1) {
                const list = await this.api.exports(this.saved.videoId).catch(() => null);
                const running = list?.exports?.find((e) => e.status === "running");
                if (running) return { export_id: running.export_id, route: route.route };
                if (!this.exporting) break;
                await new Promise((resolve) => setTimeout(resolve, 200));
            }
            return { export_id: null, route: route.route };
        }

        async sendFrameToFigure() {
            const state = this.currentFrameState();
            if (!state) return;
            try {
                const out = this.project.output;
                const ratio = Math.min(1, 1600 / out.width);
                const width = Math.round(out.width * ratio / 2) * 2;
                const height = Math.round(out.height * ratio / 2) * 2;
                await this.preview.end();
                const session = await global.PlexoraVideoRenderCoordinator.open(this.env, {
                    width, height, overlays: { scalebar: true } });
                let canvas = null;
                try {
                    const frame = await global.PlexoraVideoRenderCoordinator.frame(session, state, {});
                    canvas = document.createElement("canvas");
                    canvas.width = frame.canvas.width;
                    canvas.height = frame.canvas.height;
                    canvas.getContext("2d").drawImage(frame.canvas, 0, 0);
                } finally {
                    await global.PlexoraVideoRenderCoordinator.close(session);
                }
                await global.PlexoraVideoFigureBridge.sendFrame(this.env, state, canvas, {
                    videoId: this.saved.videoId, revision: this.saved.revision, aspect: this.aspect() });
                global.PlexoraToast?.show({ title: "Frame sent to Figure Builder",
                                            note: "It is in the captures bin.", timeout: 5000 });
            } catch (error) {
                this.bar.showError(error.message);
            }
        }

        // -- keyboard ------------------------------------------------------------------------

        handleKey(event) {
            if (global.PlexoraShortcuts?.isTyping?.()) return;
            if (document.querySelector("dialog[open]")) return;
            if (this.exporting) return;
            if (global.PlexoraToolLoader?.activeTool?.() !== TOOL) return;
            const mod = event.metaKey || event.ctrlKey;
            const key = event.key;
            const take = () => {
                event.preventDefault();
                event.stopImmediatePropagation();
            };
            if (mod && !event.altKey && (key === "z" || key === "Z")) {
                if (!this.timeline?.active && !this.pathEditor?.active) return;
                take();
                if (event.shiftKey) this.redo();
                else this.undo();
                return;
            }
            if (mod || event.altKey) return;
            switch (key) {
                case " ":
                    take();
                    this.togglePlay();
                    break;
                case "Home":
                    take();
                    this.preview.pause();
                    this.preview.seek(0);
                    break;
                case "End":
                    take();
                    this.preview.pause();
                    this.preview.seek(this.project?.duration_s || 0);
                    break;
                case "ArrowLeft":
                case "ArrowRight": {
                    take();
                    const fps = this.project?.output?.fps || 30;
                    const step = (event.shiftKey ? 1 : 1 / fps) * (key === "ArrowLeft" ? -1 : 1);
                    this.preview.step(step);
                    break;
                }
                case "l":
                case "L":
                    take();
                    this.preview.loop = !this.preview.loop;
                    this.bar.setLoop(this.preview.loop);
                    break;
                case "e":
                case "E":
                    take();
                    this.exportNow();
                    break;
                case "p":
                case "P":
                    take();
                    this.togglePathEditor();
                    break;
                case "t":
                case "T":
                    take();
                    this.toggleTimeline();
                    break;
                case "k":
                case "K":
                    if (!this.pathEditor?.active && !this.timeline?.active) return;
                    take();
                    (this.pathEditor?.active ? this.pathEditor : this.timeline).addKey?.();
                    break;
                case "Delete":
                case "Backspace":
                    if (!this.timeline?.active) return;
                    take();
                    this.timeline.deleteSelected?.();
                    break;
                case "Escape":
                    if (this.preview.playing) {
                        take();
                        this.preview.pause();
                    } else if (this.pathEditor?.active) {
                        take();
                        this.pathEditor.toggle(false);
                    } else if (this.preview.active) {
                        take();
                        this.preview.end();
                    }
                    break;
                default:
                    break;
            }
        }
    }

    VideoStudioController.TOOL = TOOL;
    global.PlexoraVideoStudioController = VideoStudioController;

    global.Plexora?.registerPlugin?.({
        name: TOOL,
        help: {
            summary: "Make videos from the viewer: a turning 3-D stack, a sweep through its planes, "
                + "a time-lapse, a zoom, channels revealed one by one -- then refine the camera "
                + "path and timeline and export MP4 with a manifest of every frame.",
            shortcuts: [["Space", "Play or pause"], ["← →", "Previous or next frame (Shift: 1 s)"],
                        ["Home / End", "Start or end"], ["L", "Repeat playback"], ["E", "Export"],
                        ["P", "Camera path editor"], ["T", "Timeline"], ["K", "Add a camera position"],
                        ["Esc", "Pause, then back to your own view"]],
            docs: "plugins/video-studio",
        },
        createSidebarController: (ctx) => new VideoStudioController(ctx),
        ownsCellLayer: false,
    });
})(typeof window !== "undefined" ? window : globalThis);
