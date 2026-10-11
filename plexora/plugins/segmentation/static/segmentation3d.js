/**
 * Segment in 3-D (plugins/segmentation): the card that runs Cellpose on the
 * z-stack and makes the result the project's mask.
 *
 * Reads `/plugins/segmentation/api/state` once on open and while a run is
 * going (every second), starts a run with `/api/run`, and asks before
 * replacing a mask the project already has. When the run ends the page is
 * offered a reload: the mask and the cell table it measured are the
 * project's now, and the viewer reads both at load.
 */
class Segmentation3dController {
    constructor(ctx) {
        this.ctx = ctx;
        this.state = null;
        this.mode = "3d";
        this.timer = null;
    }

    el(id) {
        return document.getElementById(id);
    }

    url(path, params) {
        const base = this.ctx.url(`plugins/segmentation/api/${path}`);
        return params ? `${base}?${new URLSearchParams(params)}` : base;
    }

    async init() {
        for (const button of document.querySelectorAll("[data-seg3d-mode]")) {
            button.addEventListener("click", () => this.chooseMode(button.dataset.seg3dMode));
        }
        this.el("seg3d_run")?.addEventListener("click", () => this.run(false));
        this.ctx.onCleanup?.(() => clearTimeout(this.timer));
        await this.load();
    }

    chooseMode(mode) {
        this.mode = mode === "stitch" ? "stitch" : "3d";
        for (const button of document.querySelectorAll("[data-seg3d-mode]")) {
            const on = button.dataset.seg3dMode === this.mode;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", on ? "true" : "false");
        }
        this.paint();
    }

    async load() {
        clearTimeout(this.timer);
        try {
            const response = await fetch(this.url("state", { datasource: this.ctx.datasource }));
            this.state = response.ok ? await response.json() : null;
        } catch (error) {
            this.state = null;
        }
        this.paint();
        if (this.state?.task?.status === "pending") this.timer = setTimeout(() => this.load(), 1000);
    }

    fillChannels(select, names, keep, blank) {
        if (!select || select.options.length > (blank ? 1 : 0)) return;
        for (const name of names) {
            const option = document.createElement("option");
            option.value = name;
            option.textContent = name;
            select.appendChild(option);
        }
        if (keep) select.value = keep;
    }

    paint() {
        const state = this.state;
        const status = this.el("seg3d_status");
        const form = this.el("seg3d_form");
        const actions = this.el("seg3d_actions");
        const result = this.el("seg3d_result");
        const licence = this.el("seg3d_licence");
        if (!state) {
            if (status) status.textContent = "Could not read what this card needs from the server.";
            return;
        }
        const cellpose = state.cellpose || {};
        if (!cellpose.installed) {
            status.textContent = cellpose.problem
                ? `${cellpose.problem}. Install: ${cellpose.install}`
                : `Segmenting in 3-D uses Cellpose, which is not installed here. Install: ${cellpose.install}`;
            form.hidden = actions.hidden = licence.hidden = true;
            return;
        }
        const names = state.channels || [];
        const nuclear = this.el("seg3d_nuclear");
        const guess = names.find((name) => /dapi|hoechst|dna|nuc/i.test(name)) || names[0];
        this.fillChannels(nuclear, names, state.last?.parameters?.nuclear || guess, false);
        this.fillChannels(this.el("seg3d_cytoplasm"), names, state.last?.parameters?.cytoplasm, true);
        form.hidden = false;
        actions.hidden = false;
        licence.hidden = false;
        licence.textContent = `${cellpose.licence} Model page: ${cellpose.weights_page}`;
        const task = state.task || {};
        const pending = task.status === "pending";
        const run = this.el("seg3d_run");
        const needsStep = this.mode === "3d" && !state.z_step_known;
        if (run) run.disabled = pending || needsStep;
        const first = cellpose.weights_cached ? ""
            : " The first run downloads Cellpose-SAM's weights (about 1.2 GB).";
        const how = this.mode === "3d"
            ? " In 3-D follows each cell through the planes and takes about ten times as long "
              + "as plane by plane, which can split a cell between planes."
            : " Plane by plane is quicker and can split a cell between planes.";
        status.textContent = needsStep
            ? "In 3-D needs the distance between planes: set the z step in the Depth card, "
              + "or segment plane by plane."
            : `Cellpose ${cellpose.version} (Cellpose-SAM), on ${state.depth} planes.${how}${first}`;
        result.hidden = false;
        if (pending) {
            const percent = Number.isFinite(task.progress) ? ` ${Math.round(task.progress)} %` : "";
            result.textContent = `${task.message || "Segmenting"}…${percent}`;
        } else if (task.status === "failed") {
            result.textContent = `Could not segment: ${task.error || "see the server log"}`;
        } else if (state.last) {
            const last = state.last;
            const measured = last.objects ? `, measured into the cell table` : "";
            result.textContent = `${Number(last.cells).toLocaleString()} cells found`
                + ` (${last.parameters?.mode === "stitch" ? "plane by plane" : "in 3-D"},`
                + ` ${last.seconds} s)${measured}. They are this project's mask.`;
            if (task.status === "ready") this.offerReload();
        } else {
            result.hidden = true;
        }
    }

    offerReload() {
        if (this._offered) return;
        this._offered = true;
        window.PlexoraToast?.show?.({
            title: "The new mask is ready.",
            note: "Reload to draw it and use its cells.",
            actions: [{ label: "Reload", primary: true, onSelect: () => window.location.reload() }],
        });
    }

    async run(replace) {
        const body = {
            datasource: this.ctx.datasource,
            nuclear: this.el("seg3d_nuclear")?.value,
            cytoplasm: this.el("seg3d_cytoplasm")?.value || null,
            mode: this.mode,
            diameter_um: Number(this.el("seg3d_diameter")?.value) || null,
            level: this.el("seg3d_level")?.value || "auto",
            t: this.ctx.view?.frame?.() || 0,
            replace_mask: Boolean(replace),
        };
        let answer = {};
        let response = null;
        try {
            response = await fetch(this.url("run"), {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body) });
            answer = await response.json().catch(() => ({}));
        } catch (error) {
            answer = { error: String(error.message || error) };
        }
        if (response?.status === 409 && answer.needs === "replace_mask") {
            const ok = await (window.PlexoraConfirm?.ask?.({
                title: "Replace this project's mask?",
                body: [answer.error],
                confirm: "Replace", cancel: "Keep it" }) ?? Promise.resolve(false));
            if (ok) return this.run(true);
            return;
        }
        if (!response?.ok) {
            window.PlexoraToast?.show?.({ title: "Could not start segmenting.",
                                          note: answer.error || "", tone: "warning" });
            return;
        }
        this._offered = false;
        this.load();
    }
}

if (typeof window !== "undefined") {
    window.Segmentation3dController = Segmentation3dController;
    window.Plexora?.registerPlugin?.({
        name: "segmentation",
        help: {
            summary: "Finds every cell of the z-stack in 3-D with Cellpose and makes the "
                + "result this project's mask, measured into the cell table.",
        },
        createSidebarController: (ctx) => {
            const controller = new Segmentation3dController(ctx);
            controller.init();
            return controller;
        },
    });
}
