/**
 * scaleCalibration.js -- what one pixel is worth, when the file never said.
 *
 * A multiplexed TIFF written by a processing script routinely carries no
 * physical size at all: `channelNames.txt` beside it, nothing inside it. The
 * scale bar then counts image pixels (see imageViewer's pixelScaleSizeAndText),
 * which is honest but not what anybody wants to put in a figure. This is the
 * one place a person can supply the missing number.
 *
 * THREE STATES, and the control is deliberately not visible in all of them:
 *
 * - **metadata** -- the file states its own physical size. In the viewer the
 *   control does not render at all: there is nothing in question, and an
 *   affordance offering to contradict the file invites exactly the
 *   scale-bar-disagrees-with-its-source failure the bar exists to prevent.
 * - **manual** -- somebody typed it. The pencil stays, so it can be changed.
 * - neither -- nothing knows. The pencil, and a popup saying the bar is
 *   counting pixels, so the state is legible rather than merely unlabelled.
 *
 * TWO FACES, one controller. In the viewer it is a pencil beside the scale bar
 * with a popup behind it (`_scale_calibration_float.html`); on the project edit
 * page it is an inline field in a list of settings
 * (`_scale_calibration.html`). Parts are found by `data-role` INSIDE the root
 * rather than by document id, so neither face has to know the other exists and
 * a page could carry both.
 *
 * The server, not this file, decides which of the three states applies. Every
 * write is followed by re-reading `/get_ome_metadata` and re-rendering from
 * THAT -- never from the number that was just typed -- because the same payload
 * is what the scale bar reads, and two readers of one fact that update
 * independently are two readers that can disagree.
 */

class ScaleCalibration {

    /**
     * @param datasource - the project name, for the two endpoints
     * @param seaDragonViewer - takes the new calibration via applyPixelSize,
     *        and owns the scale bar this floats beside. Null on the edit page.
     * @param options.root - element id to drive. Defaults to the floating
     *        control when a viewer was given, the inline one otherwise.
     * @param options.alwaysShow - render even when the FILE states the size.
     *        False in the viewer; true on the project edit page, which is
     *        where a wrong PhysicalSizeX is meant to be fixable without
     *        re-importing.
     * @param options.floating - dock beside the scale bar and open in a popup.
     */
    constructor(datasource, seaDragonViewer, options = {}) {
        this.datasource = datasource;
        this.viewer = seaDragonViewer;
        this.floating = options.floating ?? Boolean(seaDragonViewer);
        this.alwaysShow = Boolean(options.alwaysShow);
        this.editing = false;
        this.busy = false;
        this.metadata = {};

        const id = options.root
            || (this.floating ? "scale_calibration_float" : "scale_calibration");
        this.root = document.getElementById(id);
        if (!this.root) return;
        const part = (role) => this.root.querySelector('[data-role="' + role + '"]');
        this.readout = part("readout");
        this.value = part("value");
        this.popup = part("popup");
        this.form = part("form");
        this.input = part("input");
        this.apply = part("apply");
        this.clear = part("clear");
        this.edit = part("edit");
        this.hint = part("hint");
    }

    /** @param metadata - a payload the caller already fetched, so the viewer's
     *  first paint costs no second request. Omitted on a page that has not
     *  read one (the project edit page), which fetches here instead. */
    async init(metadata) {
        if (!this.root) return;
        this.form.addEventListener("submit", (event) => {
            event.preventDefault();
            this.commit(this.input.value);
        });
        this.edit.addEventListener("click", () => this.toggle());
        this.clear.addEventListener("click", () => this.commit(""));
        this.input.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            // Escape backs out without writing. On the inline face that is
            // only meaningful when there is a calibration to go back TO --
            // uncalibrated, the form is the resting state, not a mode.
            if (!this.floating && !this.current) return;
            event.preventDefault();
            this.close();
        });

        if (this.floating) this.mountBesideScalebar();
        this.render(metadata || await this.fetchMetadata());
    }

    // -- the floating face ------------------------------------------------

    /**
     * Dock into the viewer shell and follow the scale bar.
     *
     * The bar is not anchored to an edge -- the plugin puts it at four fifths
     * of the free width, so both of its edges move as its label changes -- and
     * there is no CSS offset that stays beside a thing like that. So the
     * position is recomputed on the same three events that redraw the bar.
     *
     * `#openseadragon_wrapper`, NOT OpenSeadragon's own container: the channel
     * legend is an absolutely-positioned sibling of `#openseadragon` living in
     * the wrapper, so anything parked inside the OSD container is in a nested
     * stacking context that no z-index can lift above it -- the popup opened
     * underneath the legend and lost its Set button to it.
     */
    mountBesideScalebar() {
        const viewer = this.viewer?.viewer;
        const host = document.getElementById("openseadragon_wrapper");
        if (!viewer?.container || !host) return;
        host.appendChild(this.root);
        const follow = () => this.followScalebar();
        viewer.addHandler("open", follow);
        viewer.addHandler("animation", follow);
        viewer.addHandler("resize", follow);
        // The bar is drawn on `open`, which for a viewer already open has been
        // and gone; one sync now covers that.
        follow();

        document.addEventListener("pointerdown", (event) => {
            if (!this.isOpen || this.root.contains(event.target)) return;
            this.close();
        });
    }

    followScalebar() {
        const bar = this.viewer?.viewer?.scalebarInstance?.divElt;
        if (!bar || !this.root || this.root.hidden) return;
        if (bar.style.display === "none") {
            // No bar to sit beside -- the user turned it off. The pencil goes
            // with it rather than floating over the image on its own.
            this.root.classList.add("is-orphaned");
            return;
        }
        this.root.classList.remove("is-orphaned");
        // Rects rather than offsetLeft/offsetTop: the bar and this are no
        // longer children of the same element, so their offsets are measured
        // from different origins.
        const host = this.root.offsetParent;
        if (!host) return;
        const hostRect = host.getBoundingClientRect();
        const barRect = bar.getBoundingClientRect();
        this.root.style.left = (barRect.right - hostRect.left + 8) + "px";
        // Bottom edges aligned: the bar's own bottom border is the line the
        // eye reads the measurement off, so the pencil sits on it.
        this.root.style.top =
            (barRect.bottom - hostRect.top - this.root.offsetHeight) + "px";
    }

    get isOpen() {
        return this.floating ? !this.popup.hidden : this.editing;
    }

    toggle() {
        if (this.isOpen) {
            this.close();
            return;
        }
        this.editing = true;
        this.render(this.metadata);
        this.input.focus();
        this.input.select();
    }

    close() {
        this.editing = false;
        this.render(this.metadata);
    }

    // -- state ------------------------------------------------------------

    /** The payload the scale bar reads, straight from the endpoint that
     *  decides `manual` vs `metadata`. `{}` on any failure -- an unreachable
     *  server is not a calibration of zero. */
    async fetchMetadata() {
        try {
            const response = await fetch(plexoraUrl("get_ome_metadata") + "?"
                + new URLSearchParams({ datasource: this.datasource }));
            if (!response.ok) return {};
            return (await response.json()) || {};
        } catch (error) {
            return {};
        }
    }

    /** `{value, unit}` when something knows, else null. */
    get current() {
        const value = Number(this.metadata?.physical_size_x);
        if (!(value > 0)) return null;
        return { value, unit: this.metadata?.physical_size_x_unit || "µm" };
    }

    get source() {
        return this.metadata?.pixel_size_source || null;
    }

    render(metadata) {
        this.metadata = metadata || {};
        // The one state with no control at all -- see `alwaysShow`.
        if (this.source === "metadata" && !this.alwaysShow) {
            this.root.hidden = true;
            return;
        }
        this.root.hidden = false;

        const calibrated = Boolean(this.current);
        const reading = calibrated
            ? formatPerPixel(this.current.value) + " " + this.current.unit + "/px"
            : "";

        if (this.floating) {
            this.popup.hidden = !this.editing;
            this.edit.setAttribute("aria-expanded", String(this.editing));
            this.edit.classList.toggle("is-set", calibrated);
            this.edit.title = calibrated
                ? "Scale: " + reading
                : "Set what one pixel is worth";
        } else {
            // Inline: uncalibrated, the form IS the resting state, so there is
            // no closed position to return to.
            const showForm = this.editing || !calibrated;
            this.readout.hidden = showForm;
            this.form.hidden = !showForm;
            if (calibrated) this.value.textContent = reading;
        }

        // Only a value somebody typed can be removed. Clearing one the file
        // states would mean nothing -- the next load reads it straight back
        // off the file -- so it is not offered for that.
        this.clear.hidden = this.source !== "manual";
        // Rewritten on every render, not only while the form is on screen: the
        // last thing written here is "Saving...", and leaving that behind means
        // the pencil reopens onto the tail of an action that finished.
        this.setHint(this.restingHint(calibrated));
        if (this.isOpen || !this.floating) {
            this.input.value = calibrated ? String(this.current.value) : "";
        }
        this.setBusy(false);
        if (this.floating) this.followScalebar();
    }

    /** What the block says about itself when nothing is in flight. */
    restingHint(calibrated) {
        if (!calibrated) return "The scale bar is counting pixels.";
        if (this.source === "metadata") {
            return "Read from the image file. Enter a different size to override it.";
        }
        return "";
    }

    setHint(text, tone) {
        this.hint.textContent = text || "";
        this.hint.classList.toggle("is-error", tone === "error");
    }

    setBusy(busy) {
        this.busy = busy;
        this.apply.disabled = busy;
        this.input.disabled = busy;
    }

    /**
     * Write a calibration (or clear it) and take on whatever the server then
     * reports. An empty box means "clear" -- back to a bar counting pixels
     * rather than a number nobody stands behind.
     */
    async commit(raw) {
        if (this.busy) return;
        const text = String(raw ?? "").trim();
        const value = text === "" ? 0 : Number(text);
        if (!Number.isFinite(value) || value < 0) {
            this.setHint("Enter a positive number, like 0.325.", "error");
            this.input.focus();
            return;
        }

        this.setBusy(true);
        this.setHint(text === "" ? "Clearing…" : "Saving…");
        try {
            const response = await fetch(plexoraUrl("set_pixel_size"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ datasource: this.datasource, value: value }),
            });
            const body = await response.json().catch(() => ({}));
            if (!response.ok || !body.success) {
                // 404 is worth naming. A long-running server fixed its route
                // table at import while it re-reads templates from disk, so
                // after an upgrade the control appears and nothing behind it
                // does -- and "could not be saved" sends somebody looking for
                // a bug in the wrong place.
                this.setHint(response.status === 404
                    ? "This Plexora is running an older server. Restart it and try again."
                    : (body.error
                       || "That could not be saved (HTTP " + response.status + ")."),
                    "error");
                this.setBusy(false);
                return;
            }
        } catch (error) {
            this.setHint("That could not be saved — the server did not answer.",
                         "error");
            this.setBusy(false);
            return;
        }

        // Re-read rather than trust the round trip: this payload is what the
        // scale bar reads too, and handing the two of them different objects
        // is how they come to disagree.
        const metadata = await this.fetchMetadata();
        this.editing = false;
        this.viewer?.applyPixelSize(metadata);
        this.render(metadata);
    }
}

/**
 * A per-pixel size, written the way a microscope quotes one.
 *
 * Not `toFixed`: 0.325 must not become "0.33" (a 1.5% error in every length
 * measured from it) and 1 must not become "1.0000". Six significant digits is
 * past any real objective's precision and short of float noise.
 */
function formatPerPixel(value) {
    return Number(value.toPrecision(6)).toString();
}
