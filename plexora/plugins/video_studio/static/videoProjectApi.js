/**
 * videoProjectApi.js - Video Studio's REST surface, from the browser.
 *
 * Thin wrappers over `/plugins/video_studio/api/...` (server/routes.py) that
 * throw an Error carrying `status` and the server's body, so callers can tell
 * a 409 (someone saved first) from a 422 (the edit would interpolate acquired
 * data) from a 403 (the licence does not include Video Studio).
 *
 * Classic script: `window.PlexoraVideoProjectApi`.
 */
(function (global) {
    "use strict";

    const base = () => `${global.PLEXORA_BASE_URL || ""}/plugins/video_studio/api`;

    async function request(path, { method = "GET", body, headers = {}, raw = false, signal } = {}) {
        const init = { method, headers: { ...headers }, signal };
        if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)
            && !ArrayBuffer.isView(body)) {
            init.headers["Content-Type"] = "application/json";
            init.body = JSON.stringify(body);
        } else if (body !== undefined) {
            init.body = body;
        }
        const response = await fetch(`${base()}${path}`, init);
        if (raw) return response;
        let payload = null;
        try {
            payload = await response.json();
        } catch (error) {
            payload = null;
        }
        if (!response.ok || (payload && payload.success === false)) {
            const error = new Error(payload?.error || `${method} ${path} answered ${response.status}`);
            error.status = response.status;
            error.payload = payload;
            error.retryAfter = Number(response.headers.get("Retry-After")) || 0;
            throw error;
        }
        return payload;
    }

    const enc = encodeURIComponent;

    const api = {
        request,
        state: (datasource) => request(`/state?datasource=${enc(datasource || "")}`),
        encoders: (refresh = false) => request(`/encoders${refresh ? "?refresh=1" : ""}`),
        build: (body, signal) => request("/presets/build", { method: "POST", body, signal }),
        list: (datasource) => request(`/videos${datasource ? `?datasource=${enc(datasource)}` : ""}`),
        create: (body) => request("/videos", { method: "POST", body }),
        get: (id) => request(`/videos/${enc(id)}`),
        replace: (id, baseRevision, document) => request(`/videos/${enc(id)}`, {
            method: "PUT", body: { base_revision: baseRevision, document } }),
        remove: (id) => request(`/videos/${enc(id)}`, { method: "DELETE" }),
        ops: (id, baseRevision, operations, origin = "ui") => request(`/videos/${enc(id)}/ops`, {
            method: "POST", body: { base_revision: baseRevision, operations, origin } }),
        duplicate: (id, title) => request(`/videos/${enc(id)}/duplicate`, {
            method: "POST", body: { title } }),
        documentUrl: (id) => `${base()}/videos/${enc(id)}/document`,
        putPoster: (id, kfId, blob) => request(`/videos/${enc(id)}/posters/${enc(kfId)}`, {
            method: "PUT", body: blob, headers: { "Content-Type": blob.type || "image/webp" } }),
        posterUrl: (id, kfId) => `${base()}/videos/${enc(id)}/posters/${enc(kfId)}`,
        exports: (id) => request(`/videos/${enc(id)}/exports`),
        startExport: (id, output) => request(`/videos/${enc(id)}/exports`, {
            method: "POST", body: { output } }),
        pushFrames: (id, exportId, first, count, body, format = "rgba", signal) => request(
            `/videos/${enc(id)}/exports/${enc(exportId)}/frames`, {
                method: "POST", body, signal,
                headers: { "Content-Type": "application/octet-stream",
                           "X-Plexora-First-Frame": String(first),
                           "X-Plexora-Frames": String(count),
                           "X-Plexora-Frame-Format": format } }),
        finishExport: (id, exportId, manifest) => request(
            `/videos/${enc(id)}/exports/${enc(exportId)}/finish`, { method: "POST", body: { manifest } }),
        exportStatus: (id, exportId) => request(`/videos/${enc(id)}/exports/${enc(exportId)}`),
        cancelExport: (id, exportId) => request(`/videos/${enc(id)}/exports/${enc(exportId)}`, {
            method: "DELETE" }),
        downloadUrl: (id, exportId) => `${base()}/videos/${enc(id)}/exports/${enc(exportId)}/download`,
        manifestUrl: (id, exportId) => `${base()}/videos/${enc(id)}/exports/${enc(exportId)}/manifest`,
        recordExport: (id, body) => request(`/videos/${enc(id)}/exports/record`, {
            method: "POST", body }),
    };

    global.PlexoraVideoProjectApi = api;
})(typeof window !== "undefined" ? window : globalThis);
