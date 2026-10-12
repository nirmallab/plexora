/**
 * videoPrefetch.js - what the next frames will need, asked for early.
 *
 * An export knows every frame in advance, which the viewer never does. Two
 * things are worth asking for before the frame that needs them:
 *
 *   - planes: in plane and slab views the viewer can hold a "standby" copy of
 *     a plane, fetched at the lowest priority (viewerManager.prepareStandby);
 *     the planes of the next two frames are prepared while this one renders,
 *     so a sweep rarely waits on a fetch. The viewer's own neighbour prefetch
 *     guesses by direction of travel and is paused for the session.
 *   - exact contrast and prepared sections: a 3-D frame waits for every
 *     channel's exact window (never a provisional one that would brighten
 *     mid-video), and a frame showing sections asks the server to prepare the
 *     area so the panes are never coarse stand-ins.
 *
 * Classic script: `window.PlexoraVideoPrefetch`.
 */
(function (global) {
    "use strict";

    function create(env) {
        let preparedSections = false;
        let preparing = null;

        function specFor(state) {
            const vs = env.vs;
            if (!vs) return null;
            const current = vs.get();
            const mode = state.view.mode === "sections" ? "plane" : state.view.mode;
            if (mode === "volume") return null;
            const slab = state.view.slab ? { ...current.slab, ...state.view.slab } : current.slab;
            try {
                return vs.spec({ ...current, t: state.view.t, z: state.view.z, mode, slab });
            } catch (error) {
                return null;
            }
        }

        async function exactWindows(names, timeoutMs = 20000) {
            const windows = env.volumeWindows;
            if (!windows?.ensure || !names.length) return;
            await Promise.race([windows.ensure(names),
                                new Promise((resolve) => setTimeout(resolve, timeoutMs))]);
        }

        /** Before the first frame. */
        async function prime(project, states) {
            const needsVolume = states.some((s) => s.view.mode === "volume");
            if (needsVolume) {
                const names = (global.PlexoraVideoSceneState.slots(env) || [])
                    .filter((entry) => entry.slot.enabled).map((entry) => entry.fullName);
                await exactWindows(names);
            }
            if (states.some((s) => s.view.mode === "sections")) await prepareSections();
        }

        /** Ask the server to prepare the image for reading across planes, then
         *  rebuild the panes once it has. Once per session. */
        async function prepareSections() {
            if (preparedSections) return preparing;
            preparedSections = true;
            const ds = encodeURIComponent(env.datasource || "");
            const base = global.PLEXORA_BASE_URL || "";
            preparing = (async () => {
                try {
                    const state = await fetch(`${base}/volume/${ds}/prepare`).then((r) => r.json());
                    if (!state || state.state === "not_needed") return;
                    const [width, height] = env.facts?.image_size || [0, 0];
                    if (!(width > 0 && height > 0)) return;
                    const response = await fetch(`${base}/volume/${ds}/prepare`, {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ box: { x: 0, y: 0, width, height } }) });
                    if (!response.ok) return;
                    for (let i = 0; i < 600; i += 1) {
                        await new Promise((resolve) => setTimeout(resolve, 1000));
                        const now = await fetch(`${base}/volume/${ds}/prepare`).then((r) => r.json());
                        const job = now?.prepare;
                        if (!job || job.status === "done" || job.status === "failed"
                                || job.status === "error") break;
                    }
                    env.panes?.rebuild?.();
                } catch (error) {
                    console.warn("Video Studio: could not prepare sections", error);
                }
            })();
            return preparing;
        }

        /** While frame k renders: warm the planes of the next frames. */
        function ahead(states, k) {
            const manager = env.iv?.viewerManagerVMain;
            if (typeof manager?.prepareStandby !== "function") return;
            const specs = [];
            for (const next of [k + 1, k + 2]) {
                if (next >= states.length) break;
                const spec = specFor(states[next]);
                const current = specFor(states[k]);
                if (spec && spec !== current && !specs.includes(spec)) specs.push(spec);
            }
            if (specs.length) {
                try {
                    manager.prepareStandby(specs);
                } catch (error) {
                    // a standby that cannot be prepared costs a wait, not a frame
                }
            }
        }

        return { prime, ahead, prepareSections, specFor };
    }

    global.PlexoraVideoPrefetch = { create };
})(typeof window !== "undefined" ? window : globalThis);
