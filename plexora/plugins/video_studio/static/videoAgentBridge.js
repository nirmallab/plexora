/**
 * videoAgentBridge.js - Video Studio's part of the agent's view of the page.
 *
 * Core's agent bridge (services/agentBridge.js) names no plugin; plugins
 * answer through three events, and this is Video Studio's answer:
 *
 *   plexora:agent-state          contributes `video_studio: {scene, video,
 *                                playing}` -- the viewer as a SceneState
 *                                (with the 3-D camera, which nothing else
 *                                reports), and the video open in the bar
 *   plexora:agent-command        claims `video_studio:preview`, `:export`,
 *                                `:cancel_export`, `:load`, carried by core's
 *                                generic `plugin_command`
 *   plexora:agent-state-changed  an agent edited a video in another process:
 *                                the open one is reloaded
 *
 * Classic script: `window.PlexoraVideoAgentBridge`.
 */
(function (global) {
    "use strict";

    function attach(controller) {
        const onState = (event) => {
            try {
                event.detail?.contribute?.("video_studio", {
                    scene: global.PlexoraVideoSceneState.capture(controller.env),
                    video: controller.saved?.videoId ? { video_id: controller.saved.videoId,
                                                        revision: controller.saved.revision } : null,
                    preset: controller.presetName || null,
                    playing: Boolean(controller.preview?.playing),
                    exporting: Boolean(controller.exporting),
                });
            } catch (error) {
                console.warn("Video Studio: could not describe the viewer for the agent", error);
            }
        };
        const onCommand = (event) => {
            const detail = event.detail;
            const type = detail?.type || "";
            if (!type.startsWith("video_studio:")) return;
            const command = type.slice("video_studio:".length);
            const args = detail.arguments || {};
            const handlers = {
                preview: () => controller.agentPreview(args),
                export: () => controller.agentExport(args),
                cancel_export: () => controller.cancelExport(),
                load: () => controller.loadVideo(args.video_id),
            };
            if (!handlers[command]) return;
            detail.claim(Promise.resolve().then(handlers[command]), "video_studio");
        };
        const onChanged = (event) => {
            const detail = event.detail || {};
            if (detail.plugin !== "video_studio") return;
            const id = detail.payload?.after?.video_id;
            if (id && id === controller.saved?.videoId) controller.loadVideo(id, { quiet: true });
        };
        global.addEventListener("plexora:agent-state", onState);
        global.addEventListener("plexora:agent-command", onCommand);
        global.addEventListener("plexora:agent-state-changed", onChanged);
        return () => {
            global.removeEventListener("plexora:agent-state", onState);
            global.removeEventListener("plexora:agent-command", onCommand);
            global.removeEventListener("plexora:agent-state-changed", onChanged);
        };
    }

    global.PlexoraVideoAgentBridge = { attach };
})(typeof window !== "undefined" ? window : globalThis);
