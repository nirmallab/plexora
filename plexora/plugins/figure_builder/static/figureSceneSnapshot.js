/**
 * FigureScene - reading the viewer's state, and putting it back.
 *
 * This is the file the whole product claim rests on. A captured panel looks
 * like a screenshot; what makes it something else is that everything affecting
 * what is on screen is written down here in a form that survives the screen.
 *
 * Two rules govern what goes in:
 *
 * **Only what changes the science.** Channels, their colours, their display
 * windows, the overlays, the region of the image. Not the sidebar's scroll
 * position, not the cursor, not which panel was expanded -- storing those would
 * make two identical captures compare unequal and would restore a UI the user
 * has since rearranged.
 *
 * **Nothing in screen units.** The viewport is full-resolution image pixels and
 * the windows are raw 16-bit, whatever the slider happened to be showing. Both
 * are stable under zoom, under HD mode and under a different monitor; the
 * alternatives are indistinguishable from them once written down, and only
 * these two can be re-rendered at an arbitrary DPI years later.
 *
 * A channel is identified by the last segment of its tile URL rather than by
 * its name. A name is something the user renames; the key is generated at
 * import from the file and the channel's position and does not move. A figure
 * that identified channels by name would lose them the first time somebody
 * relabelled "LSP20209_2" as "CD8a" -- which is a thing people do on the way to
 * making a figure.
 */
const FigureScene = {

    /**
     * The stable id of one image channel, from the config entry the viewer was
     * built with.
     */
    channelKey(config, fullname) {
        const entry = (config?.imageData || []).find((channel) => channel.fullname === fullname);
        if (!entry || !entry.src) return "";
        return String(entry.src).replace(/\/+$/, "").split("/").pop();
    },

    /**
     * Every channel currently contributing to the image, as the scene records
     * them.
     *
     * Read from the sidebar's slots rather than from the saved channel list,
     * because the saved list is what was last PERSISTED and the slots are what
     * is on screen -- and the gap between them is exactly the unsaved
     * adjustment the user is capturing.
     */
    channels(ctx) {
        const sidebar = window.__plexora?.viewerSidebar;
        const config = ctx.config;
        if (!sidebar || !Array.isArray(sidebar.channelSlots)) return [];

        const out = [];
        for (const slot of sidebar.channelSlots) {
            if (!slot.name || !slot.enabled) continue;
            const fullname = ctx.dataLayer?.getFullChannelName
                ? ctx.dataLayer.getFullChannelName(slot.name)
                : slot.name;
            const key = this.channelKey(config, fullname);
            if (!key) continue;
            // Raw 16-bit, always. slot.range is byte-domain outside HD mode, so
            // storing it directly would produce a window that means nothing the
            // next time the viewer is in the other mode.
            const window_ = sidebar.toRawRangeForSlot
                ? sidebar.toRawRangeForSlot(slot)
                : slot.range;
            out.push({
                key: key,
                fullname_at_capture: fullname,
                color: { r: slot.color.r, g: slot.color.g, b: slot.color.b },
                window: [Number(window_[0]), Number(window_[1])],
                visible: true,
            });
        }
        return out;
    },

    /**
     * Core's own overlays: the cell layers, HD tiles, the scale bar.
     *
     * The layers are recorded in stacking order with their modes and
     * opacities, but WITHOUT the colours -- those belong to whichever plugin
     * computed them, and that plugin records its own state and its own legend
     * through the capture bridge. Copying a lookup table of half a million
     * cells into every panel would put megabytes of derived data into a
     * document whose whole point is that it holds none.
     */
    coreOverlays(ctx) {
        const viewer = ctx.viewer;
        const layers = (viewer?.cellLayers?.() || []).map((layer, index) => ({
            name: layer.name,
            mode: layer.mode,
            opacity: layer.opacity,
            visible: Boolean(layer.visible),
            z: index,
        }));
        return {
            cell_layers: layers,
            hd_tiles: Boolean(document.getElementById("viewer_controls_hd")?.checked),
            scalebar_visible: Boolean(viewer?.show_scalebar),
        };
    },

    /**
     * Ask every open plugin what it is drawing.
     *
     * A DOM CustomEvent rather than a call, because Figure Builder must not
     * know any plugin's internals -- the same reason ROI and Cell Explorer talk
     * through `plexora:roi-hover` rather than through each other's classes. A
     * plugin that is not loaded contributes nothing and the capture proceeds;
     * that is the honest outcome, and it is what makes a figure built today
     * still openable in a build where that plugin was uninstalled.
     *
     * Each contribution carries a `legend` the plugin computed NOW, which is
     * what lets export draw a legend with no plugin JavaScript running at all.
     */
    pluginStates() {
        const contributions = {};
        try {
            window.dispatchEvent(new CustomEvent("plexora:figure-capture-state", {
                detail: {
                    contribute(name, payload) {
                        if (!name || !payload) return;
                        contributions[String(name)] = {
                            version: String(payload.version || ""),
                            state: payload.state || {},
                            legend: Array.isArray(payload.legend) ? payload.legend : [],
                        };
                    },
                },
            }));
        } catch (error) {
            // A plugin that throws while describing itself must not cost the
            // user their capture. The panel is still worth having without that
            // plugin's overlay recorded, and the omission is visible.
            console.error("figure_builder: a plugin failed to describe its state", error);
        }
        return contributions;
    },

    /**
     * The whole snapshot for a captured region.
     *
     * `viewport` is already in full-resolution image pixels -- converted by the
     * capture tool, which is the only place that touches screen coordinates.
     */
    capture(ctx, sourceId, viewport) {
        return {
            snapshot_version: FigureSchema.SNAPSHOT_VERSION,
            source_id: sourceId,
            viewport: {
                x: viewport.x, y: viewport.y, w: viewport.w, h: viewport.h,
            },
            channels: this.channels(ctx),
            core_overlays: this.coreOverlays(ctx),
            plugins: this.pluginStates(),
            captured_at: new Date().toISOString(),
        };
    },

    /**
     * What the viewer is looking at right now, in full-resolution image pixels.
     *
     * Used to stash the state a panel edit is about to displace, so Cancel puts
     * the user back where they were rather than leaving them somewhere they
     * never chose.
     */
    currentViewport(ctx) {
        const viewer = ctx.viewer?.viewer;
        const item = viewer?.world?.getItemAt(0);
        if (!item) return { x: 0, y: 0, w: 1, h: 1 };
        const bounds = item.viewportToImageRectangle(viewer.viewport.getBounds(true));
        const scale = 2 ** (ctx.config?.extraZoomLevels || 0);
        return {
            x: bounds.x / scale, y: bounds.y / scale,
            w: Math.max(1, bounds.width / scale), h: Math.max(1, bounds.height / scale),
        };
    },

    // -- putting it back -------------------------------------------------

    /**
     * The scene's channels as the rows the sidebar's own restore path takes.
     *
     * Deliberately reuses `applySavedChannels` rather than driving the viewer
     * directly. That function already handles the things a second
     * implementation would get wrong on a project nobody tested it on: it
     * prefetches each channel's statistics in parallel, converts the raw window
     * into whichever domain the slider is currently in, and stands down the
     * auto-level that a marker change would otherwise schedule on top of the
     * range being restored. A parallel path would be a second answer to all
     * three.
     *
     * Returns the rows plus the channels that could not be resolved, which are
     * reported and never guessed at: a figure that silently substituted a
     * different channel would look right and be wrong.
     */
    restoreRows(ctx, scene) {
        const byKey = new Map();
        for (const channel of ctx.config?.imageData || []) {
            const key = String(channel.src || "").replace(/\/+$/, "").split("/").pop();
            if (key) byKey.set(key, channel);
        }

        const rows = [];
        const missing = [];
        for (const channel of scene.channels || []) {
            const entry = byKey.get(channel.key);
            if (!entry) {
                missing.push(channel.fullname_at_capture || channel.key);
                continue;
            }
            rows.push({
                // The SHORT name, which is what the slots are keyed by -- the
                // same field `persistChannelList` writes.
                channel: entry.name || entry.fullname,
                channel_active: true,
                r: channel.color.r, g: channel.color.g, b: channel.color.b,
                opacity: 1,
                // Raw 16-bit both ways: this is the form the saved channel list
                // uses, so the conversion into the live domain is the sidebar's
                // and there is one of it.
                start: channel.window[0],
                end: channel.window[1],
            });
        }
        return { rows: rows, missing: missing };
    },

    /**
     * Put a captured scene back into the live viewer.
     *
     * Returns a report -- what was restored, what could not be, which plugins
     * answered. Nothing here throws on a missing piece: a panel captured with a
     * plugin that is no longer installed still shows its image and its
     * channels, and saying which part is absent is more useful than refusing
     * the whole restore.
     *
     * The project's own saved channel list is NOT touched. That is what
     * `suspendPersistence` is for, and it is the difference between looking at
     * a figure panel and permanently overwriting the settings the user had.
     */
    async restore(ctx, scene) {
        const sidebar = window.__plexora?.viewerSidebar;
        const report = { channels: 0, missing_channels: [], plugins: {}, viewport: false };
        if (!scene) return report;

        const resolved = this.restoreRows(ctx, scene);
        report.missing_channels = resolved.missing;

        sidebar?.suspendPersistence?.();
        try {
            if (sidebar && resolved.rows.length) {
                await sidebar.applySavedChannels(resolved.rows);
                report.channels = resolved.rows.length;
            }
            this.restoreOverlays(ctx, scene.core_overlays);
            report.viewport = this.restoreViewport(ctx, scene.viewport);
            report.plugins = this.restorePlugins(scene.plugins);
        } finally {
            // In a `finally`, because a restore that throws halfway must not
            // leave the project's channel list permanently unsaveable -- the
            // symptom would be a viewer that quietly stops remembering
            // anything, hours later, with no way to connect it to this.
            sidebar?.resumePersistence?.();
        }
        return report;
    },

    /**
     * Move the viewer to the captured region.
     *
     * The inverse of the capture conversion, and new code: nothing in the
     * viewer restored a viewport before this, because nothing had a viewport
     * worth restoring.
     */
    restoreViewport(ctx, viewport) {
        const viewer = ctx.viewer?.viewer;
        const item = viewer?.world?.getItemAt(0);
        if (!item || !viewport) return false;
        const scale = 2 ** (ctx.config?.extraZoomLevels || 0);
        const bounds = item.imageToViewportRectangle(new OpenSeadragon.Rect(
            viewport.x * scale, viewport.y * scale,
            viewport.w * scale, viewport.h * scale));
        // Immediately rather than animated: this is a jump to a recorded place,
        // and a two-second pan across a slide to get there is a two-second wait
        // that tells the user nothing.
        viewer.viewport.fitBounds(bounds, true);
        return true;
    },

    /**
     * Core's overlays, as far as they still exist.
     *
     * A layer belongs to whichever plugin registered it, so a scene captured
     * with Cell Explorer open restores nothing here when it is closed -- and
     * says so, rather than conjuring a layer with no colours in it.
     */
    restoreOverlays(ctx, overlays) {
        if (!overlays) return;
        const viewer = ctx.viewer;
        const controls = window.__plexora?.viewerControls;

        let restored = null;
        for (const layer of overlays.cell_layers || []) {
            if (!viewer?.cellLayer?.(layer.name)) continue;
            // Per layer, because each has its own mode: a phenotype map drawn
            // filled and a gate drawn as outlines are a legitimate pair, and
            // restoring one mode for all of them would flatten that.
            viewer.setCellLayerMode?.(layer.name, layer.mode);
            viewer.setLayerOpacity?.(layer.name, layer.opacity);
            viewer.setCellLayerVisible?.(layer.name, layer.visible);
            if (layer.visible && layer.mode && layer.mode !== "none") restored = layer;
        }
        // setCellLayerMode changes how the mask is DRAWN; it does not fetch a
        // mask that is not loaded. viewerControls.selectMode is the path that
        // does, so it is called once for whichever layer is actually showing --
        // otherwise a panel captured with outlines on reopens with the layer
        // switched on and nothing in it.
        if (restored && controls?.selectMode) controls.selectMode(restored.mode);

        const hd = document.getElementById("viewer_controls_hd");
        if (hd && hd.checked !== overlays.hd_tiles) {
            hd.checked = overlays.hd_tiles;
            // Through the element's own change event, so the reload work stays
            // in viewerControls rather than being reimplemented here.
            hd.dispatchEvent(new Event("change", { bubbles: true }));
        }
        viewer?.setScalebarVisible?.(Boolean(overlays.scalebar_visible));
    },

    /**
     * Hand each plugin its own state back.
     *
     * The mirror of the capture bridge, and equally ignorant: Figure Builder
     * passes the blob to whoever answers to that name and records what they
     * said. A plugin that is not open reports nothing, which becomes a notice
     * on the panel rather than an error -- the image and the channels are
     * restored either way, and that is most of the view.
     */
    restorePlugins(plugins) {
        const report = {};
        if (!plugins || !Object.keys(plugins).length) return report;
        try {
            window.dispatchEvent(new CustomEvent("plexora:figure-restore-state", {
                detail: {
                    plugins: plugins,
                    report(name, outcome) { report[String(name)] = String(outcome); },
                },
            }));
        } catch (error) {
            console.error("figure_builder: a plugin failed to restore its state", error);
        }
        for (const name of Object.keys(plugins)) {
            if (!(name in report)) report[name] = "skipped";
        }
        return report;
    },
};
