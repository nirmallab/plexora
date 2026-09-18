/**
 * The gene selector, and the wiring between it and the layer.
 *
 * The whole of this plugin's UI. What it deliberately does NOT do is as much a
 * part of the design as what it does: no counts per region, no differential
 * expression, no co-expression, no clustering. Those interpret the data, and the
 * moment one of them appears here the viewer's Transcripts panel has become an
 * analysis application -- which is precisely what the layer architecture exists
 * to keep it from becoming.
 *
 * Two existing components do the work: `searchableSelect` for a 300-gene list
 * and `colorSwatchPicker` for the colours. No new widget, so a long gene list
 * behaves the way every other long list in Plexora does.
 */
class TranscriptsSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        this.layer = null;
        this.select = null;
        this.poll = null;
    }

    //: The starting colours, in assignment order. Chosen to stay distinguishable
    //: on the black ground a fluorescence composite is drawn on and on the pale
    //: one an H&E is -- which rules out the darkest end of most palettes.
    static get PALETTE() {
        return ["#ff4d4d", "#4dd2ff", "#7cff4d", "#ffd24d", "#c77dff", "#ff8c42",
                "#4dffd2", "#ff6ec7", "#9be564", "#6d9eff", "#ffe066", "#e0e0e0"];
    }

    el(id) { return document.getElementById(id); }

    async setup() {
        this.el("transcripts_panel_close")?.addEventListener(
            "click", () => window.PlexoraToolLoader?.closeTool?.("transcripts"));

        const layerId = this.firstTranscriptLayer();
        if (!layerId) {
            this.el("transcripts_empty").hidden = false;
            return;
        }

        this.layer = new TranscriptLayer(this.ctx, layerId);
        const handle = await this.layer.attach();
        if (!handle) {
            // Registered but not yet tiled. The build job is the thing to watch.
            this.el("transcripts_building").hidden = false;
            this.watchBuild(layerId);
            return;
        }
        this.el("transcripts_body").hidden = false;
        this.fill();
        this.bind();
        this.ctx.onCleanup?.(() => this.destroy());
    }

    /**
     * The project's first points layer.
     *
     * First rather than a chooser, because a run has one transcript table. A
     * project with two is possible and is not a case worth a control until
     * somebody has one.
     */
    firstTranscriptLayer() {
        const layers = this.ctx.layers?.list?.() || [];
        return layers.find((layer) => layer.kind === "points"
            && layer.id !== "__centroids__")?.id || null;
    }

    watchBuild(layerId) {
        const url = this.ctx.url(
            `plugins/transcripts/status?datasource=${encodeURIComponent(this.ctx.datasource)}`
            + `&layer=${encodeURIComponent(layerId)}`);
        this.poll = setInterval(async () => {
            try {
                const state = await (await fetch(url)).json();
                const label = this.el("transcripts_building_label");
                if (state.status === "running" && label) {
                    label.textContent = state.total
                        ? `Preparing transcripts… ${state.done} / ${state.total}`
                        : `Preparing transcripts… (${state.stage})`;
                }
                if (state.status === "ready") {
                    clearInterval(this.poll);
                    this.poll = null;
                    this.el("transcripts_building").hidden = true;
                    await this.setup();
                }
                if (state.status === "error") {
                    clearInterval(this.poll);
                    this.poll = null;
                    const label2 = this.el("transcripts_building_label");
                    // The install line, not the stack trace: a missing pyarrow
                    // is something the user can act on, and "ModuleNotFoundError"
                    // is not what to hand a biologist.
                    if (label2) label2.textContent = state.install
                        ? `${state.error}`
                        : `Transcripts could not be prepared: ${state.error}`;
                }
            } catch (error) {
                clearInterval(this.poll);
                this.poll = null;
            }
        }, 1500);
    }

    fill() {
        const genes = this.layer.genes();
        this.el("transcripts_count").textContent =
            (this.layer.manifest.point_count || 0).toLocaleString();
        this.el("transcripts_gene_count").textContent = genes.length.toLocaleString();

        const mount = this.el("transcripts_gene_select");
        if (mount && window.PlexoraSearchableSelect) {
            this.select = window.PlexoraSearchableSelect.create(mount, {
                options: genes.map((name) => ({ value: name, label: name })),
                multiple: true,
                placeholder: "Search genes…",
                onChange: (values) => this.onGenesChanged(values),
            });
        }
    }

    onGenesChanged(values) {
        this.layer.setGenes(values);
        for (const [index, gene] of values.entries()) {
            if (!this.layer.colors.has(gene)) {
                this.layer.colors.set(
                    gene,
                    TranscriptsSidebarController.PALETTE[
                        index % TranscriptsSidebarController.PALETTE.length]);
            }
        }
        this.paintGeneRows();
    }

    /** One row per selected gene: its swatch, its name, and an eye. */
    paintGeneRows() {
        const list = this.el("transcripts_gene_rows");
        if (!list) return;
        list.innerHTML = "";
        this.layer.selected.forEach((gene, index) => {
            const row = document.createElement("div");
            row.className = "transcripts-gene-row";

            const swatch = document.createElement("button");
            swatch.type = "button";
            swatch.className = "transcripts-swatch";
            swatch.style.background = this.layer.colorFor(gene);
            swatch.title = `Colour for ${gene}`;
            swatch.addEventListener("click", () => {
                window.PlexoraColorSwatchPicker?.open(swatch, {
                    color: this.layer.colorFor(gene),
                    onPick: (color) => {
                        this.layer.setColor(gene, color);
                        swatch.style.background = color;
                    },
                });
            });
            row.appendChild(swatch);

            const name = document.createElement("span");
            name.className = "transcripts-gene-name";
            name.textContent = gene;
            row.appendChild(name);

            // Past the twelfth, the extras share one grey rather than being
            // refused or given a thirteenth colour nobody can tell from the
            // fourth. Said on the row, where the user can see which are which.
            if (index >= TranscriptLayer.MAX_DISTINCT_GENES) {
                const note = document.createElement("span");
                note.className = "transcripts-gene-note";
                note.textContent = "other";
                note.title = "Past twelve genes the colours stop being "
                    + "distinguishable, so the rest share one";
                row.appendChild(note);
            }

            list.appendChild(row);
        });
    }

    bind() {
        this.el("transcripts_clear")?.addEventListener("click", () => {
            this.select?.setValue?.([]);
            this.layer.setGenes([]);
            this.paintGeneRows();
        });

        this.el("transcripts_mode")?.addEventListener("change", (event) => {
            this.layer.setMode(event.target.value);
        });

        const size = this.el("transcripts_size");
        size?.addEventListener("input", () => {
            this.layer.pointRadius = Number(size.value);
            this.el("transcripts_size_value").textContent = Number(size.value).toFixed(1);
            this.layer.handle?.invalidate();
        });

        const opacity = this.el("transcripts_opacity");
        opacity?.addEventListener("input", () => {
            this.layer.opacity = Number(opacity.value);
            this.el("transcripts_opacity_value").textContent =
                Number(opacity.value).toFixed(2);
            this.layer.handle?.invalidate();
        });
    }

    setVisible(on) {
        this.layer?.handle?.setVisible(on);
    }

    destroy() {
        if (this.poll) clearInterval(this.poll);
        this.poll = null;
        this.layer?.destroy();
        this.layer = null;
    }
}

if (typeof window !== "undefined") {
    window.TranscriptsSidebarController = TranscriptsSidebarController;
    window.Plexora?.registerPlugin?.({
        name: "transcripts",
        createSidebarController: (ctx) => new TranscriptsSidebarController(ctx),
    });
}
if (typeof globalThis !== "undefined") {
    globalThis.TranscriptsSidebarController = TranscriptsSidebarController;
}
