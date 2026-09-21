/**
 * A registered layer's channel controls: the reference image's, not a copy.
 *
 * THE POINT OF THIS FILE IS THAT IT IS SMALL. A layer added with **+ Add
 * Layer** used to get one `<select>`, one colour swatch and two number boxes,
 * with every change refetching the viewport -- while the reference image, two
 * cards below it, had channel slots, a colour each, a logarithmic contrast
 * slider each, Auto and Add Channel, all of them free repaints. Two widgets
 * for one job is two things to learn and two things to fix.
 *
 * So this mounts a SECOND `ViewerSidebar` instance over the same markup, which
 * that class has supported since Figure Builder's Quick Edit
 * (`{root, idPrefix, persist, channelIndex}` -- see its class comment). What is
 * written here is only what a layer answers differently from the project:
 *
 *   - where the stats and the GaussianMixture fit come from (the layer's own
 *     routes, beside its tiles, rather than the datasource-wide ones);
 *   - where the saved channels come from and go (`render.channels` on the
 *     layer record, through the Layers panel's own PATCH);
 *   - what a change repaints (the layer's `LayerChannelSet`, rather than the
 *     reference image's world items).
 *
 * Everything else -- what a slot looks like, what Auto does, how a window is
 * stored, how the byte and raw-16-bit domains convert -- is the class's, and
 * stays the class's.
 *
 * Served as a classic script (see base.html) and mounted at runtime by
 * views/layerManager.js.
 */
(function () {
    "use strict";

    //: The most slots any one layer may open at once. The same ceiling the
    //: viewer's own sidebar has, and for the same reason: every enabled slot
    //: is a world item fetching, decoding and compositing its own tiles.
    const MAX_SLOTS = 15;

    /**
     * The id prefix this layer's copy of the markup uses.
     *
     * A layer id comes from the import and can hold anything a filename can,
     * so it is reduced to what an element id may contain. Two layers whose ids
     * differ only in punctuation would collide; they would also be two layers
     * the user cannot tell apart, so that is not the interesting case -- what
     * matters is that the prefix is stable for a given layer across rebuilds
     * of its card.
     */
    function prefixFor(layerId) {
        return `layer_${String(layerId).replace(/[^\w-]/g, "_")}_`;
    }

    /**
     * `render.channels` as the saved rows `ViewerSidebar.applySavedChannels`
     * reads -- the project's own saved-channel shape, per layer.
     *
     * Ranges are RAW 16-BIT UNITS on both sides of this, which is what makes a
     * saved window mean the same thing across a session, a reload and an HD
     * toggle (see `toRawRangeForSlot`).
     *
     * A layer saved before this existed named ONE channel, one colour and one
     * window, applied server-side. That is read as a single row, so an
     * existing project opens showing what it showed before.
     */
    function savedRowsFor(spec, channels) {
        const render = (spec || {}).render || {};
        const list = channels || spec?.channels || [];
        const rowFor = (name, colour, range) => {
            const rgb = hexToRgb(colour) || { r: 255, g: 255, b: 255 };
            const [start, end] = Array.isArray(range) && range.length === 2
                ? range : [0, 65535];
            return { channel: name, start, end, r: rgb.r, g: rgb.g, b: rgb.b,
                     channel_active: true };
        };
        if (Array.isArray(render.channels) && render.channels.length) {
            return render.channels.map((row) => {
                // Resolved AGAINST THE LAYER, by index first and by name
                // second -- never taken on trust. A saved row can outlive the
                // channel it names (a layer rebuilt from a different export),
                // and a slot naming a channel that is not there asks the
                // server for one and gets a 404 per activation.
                const found = list[row?.index ?? -1]
                    || (row?.name ? list.find((c) => c?.name === row.name) : null);
                if (!found?.name) return null;
                return rowFor(found.name, row.color, row.range);
            }).filter(Boolean);
        }
        const legacy = list[Math.min(render.channelIndex ?? 0,
                                     Math.max(0, list.length - 1))];
        if (!legacy?.name || !render.color) return [];
        return [rowFor(legacy.name, render.color, render.range)];
    }

    /**
     * The slots, as `render.channels` stores them.
     *
     * The WHOLE list every time, because that is what is stored -- the PATCH
     * merges `render` one key deep, so a partial list would be the new list.
     * Ranges go out in raw 16-bit units for the reason above.
     */
    function slotsToChannels(sidebar, nameToIndex) {
        return sidebar.channelSlots
            .filter((slot) => slot.enabled && slot.name)
            .map((slot) => {
                const raw = sidebar.toRawRangeForSlot(slot);
                return {
                    index: nameToIndex[slot.name],
                    name: slot.name,
                    color: slot.colorHex,
                    range: [Math.round(raw[0]), Math.round(raw[1])],
                };
            });
    }

    /**
     * The slots, as `LayerChannelSet.setChannels` draws them.
     *
     * The range is converted into the same fractional units the reference
     * image's `rangeConnector` holds (see `imageViewer.updateChannelRange`),
     * because the shader reads one `u_tile_range` and does not know or care
     * which kind of layer a tile came from.
     */
    function slotsToDrawn(sidebar) {
        return sidebar.channelSlots
            .filter((slot) => slot.enabled && slot.name)
            .map((slot) => ({
                name: slot.name,
                color: { ...slot.color },
                range: sidebar.toImageConnectorRange(slot.range),
            }));
    }

    function hexToRgb(hex) {
        const cleaned = String(hex || "").trim().replace(/^#/, "");
        const full = cleaned.length === 3
            ? cleaned.split("").map((c) => c + c).join("") : cleaned;
        if (!/^[0-9a-fA-F]{6}$/.test(full)) return null;
        const value = parseInt(full, 16);
        return { r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255 };
    }

    /** The prefixed copy of the base card's channel markup (index.html). */
    function buildMarkup(prefix, withUpload) {
        const node = document.createElement("div");
        node.className = "layer-channel-panel";

        // First, as it is on the base card: ONE LINE FOR THE TWO THINGS THAT
        // ACT ON THE WHOLE LAYER. This is the reference card's
        // `.layer-card-actions` from index.html, built for a layer, and
        // marked as index.html marks it -- layerManager finds it by
        // `data-layer-opacity-slot` and puts the compact `Opacity 100%`
        // control first on it, the same way for both cards. Without the mark
        // a layer got a label, a full track and a number box on a row of its
        // own above the panel, and a multiplex imported as a layer looked like
        // a different widget from one imported as the reference image. The
        // line exists even with nothing to rename, because the opacity still
        // has to go somewhere.
        //
        // The rename button is built only when the caller can actually carry
        // a rename through -- a button that opens a dialog nothing applies
        // would be worse than no button. A multiplex registered as a layer
        // arrives called Channel_0 … Channel_n exactly as often as one opened
        // as the reference image does, and until the real names are in, every
        // slot's marker select is a list of numbers.
        const actions = document.createElement("div");
        actions.className = "layer-card-actions";
        actions.setAttribute("data-layer-opacity-slot", "");
        node.appendChild(actions);
        let upload = null;
        if (withUpload) {
            upload = document.createElement("button");
            upload.type = "button";
            upload.id = `${prefix}channels_upload_icon`;
            upload.className = "layer-card-action";
            upload.title = "Name these channels from a CSV or a spreadsheet";
            upload.innerHTML = '<span class="fas fa-file-arrow-up"></span>'
                + '<span class="layer-card-action-text">Upload channel names</span>';
            actions.appendChild(upload);
        }

        const list = document.createElement("div");
        list.id = `${prefix}channel_slot_list`;
        list.className = "channel-slot-list";
        node.appendChild(list);

        const add = document.createElement("button");
        add.type = "button";
        add.id = `${prefix}add_channel_button`;
        add.className = "sidebar-action secondary";
        add.innerHTML = '<span class="fas fa-plus"></span> Add Channel';
        node.appendChild(add);

        // The card's last line, and this panel builds it rather than the card:
        // the counter has to stay inside `root` or `updateSelectedCount`
        // cannot find it. The alignment note is put in beside it by
        // layerManager, which is the one thing on this line that is not the
        // panel's.
        const footer = document.createElement("div");
        footer.className = "layer-card-footer";
        const count = document.createElement("div");
        count.className = "selected-count";
        count.setAttribute("data-layer-count", "");
        count.innerHTML = `<span id="${prefix}num-selected-channels">0</span>`
            + '<span>active</span>'
            + `<span class="selected-count-max">· <span id="${prefix}max-channels">${MAX_SLOTS}</span> max</span>`;
        footer.appendChild(count);
        node.appendChild(footer);

        return { node, footer, upload };
    }

    /**
     * Mount this layer's channel controls and start drawing what they say.
     *
     * @param layer   - the LayerStack record, whose `.spec` carries the
     *                  server's channel list and `render`
     * @param draw    - `(list) => void`, which reaches this layer's
     *                  `LayerChannelSet`. A CALLBACK rather than the viewer
     *                  manager itself, because a card can be built before
     *                  `window.__plexora.seaDragonViewer` is assigned -- a
     *                  handle taken now would be undefined for the life of
     *                  the panel, and the controls would move nothing.
     * @param persist - `(patch) => void`, layerManager's debounced PATCH
     *                  bound to this layer's id
     * @param rename  - `() => void`, called by "Upload channel names". Owned
     *                  by layerManager rather than here: naming the channels
     *                  is a change to the PROJECT's record of this layer and
     *                  what follows it is a rebuild of the card and the world
     *                  items, neither of which is this panel's to do. Absent
     *                  (a build without the modal, the layer-manager probe)
     *                  and the button is not drawn at all.
     * @returns `{node, footer, sidebar, destroy}`, or null for a layer with no
     *   channels to control.
     */
    function mount({ layer, draw, persist, rename }) {
        const spec = layer?.spec || {};
        const channels = (spec.channels || []).filter((c) => c?.name && c?.src);
        if (!channels.length) return null;

        const prefix = prefixFor(layer.id);
        const { node, footer, upload } = buildMarkup(prefix, typeof rename === "function");
        if (upload) upload.addEventListener("click", () => rename());
        const names = channels.map((channel) => channel.name);
        const nameToIndex = {};
        const srcByName = {};
        channels.forEach((channel, index) => {
            nameToIndex[channel.name] = index;
            srcByName[channel.name] = channel.src;
        });

        // ONE object, shared by the shim and handed to `init`, which keeps it
        // by reference -- so a packet fetched later is visible to
        // `quantWindow` without anything being told about it. The same
        // arrangement the viewer's own sidebar has with `dd`.
        const described = {};
        const hasChannelGMM = {};
        const pending = new Map();

        //: The layer's own routes, beside its tiles: the packets are the same
        //: shape the datasource-wide ones return, because they feed the same
        //: widget. `src` is never rewritten by `applyRouting` (that rewrites
        //: `imageData`), so this is always an address on this server.
        function packet(name, what) {
            const key = `${what}:${name}`;
            if (pending.has(key)) return pending.get(key);
            const src = srcByName[name];
            if (!src) return Promise.resolve(null);
            const request = fetch(plexoraUrl(`${src}${what}`))
                .then((response) => (response.ok ? response.json() : null))
                .catch(() => null);
            pending.set(key, request);
            return request;
        }

        const channelList = {
            selections: [],
            sel: {},
            image_channels: {},
            rangeConnector: {},
            colorConnector: {},
            hasChannelGMM,
            databaseDescription: described,
            // Never delegated to the real ChannelList: its own
            // `ensureChannelStats` writes a d3 curve into `#channel_list`,
            // which belongs to the reference image's panel.
            ensureChannelStats(name) {
                if (described[name]) return Promise.resolve(described[name]);
                return packet(name, "stats").then((stats) => {
                    if (stats) described[name] = stats;
                    return stats;
                });
            },
            getAndDrawChannelGMM(name) {
                if (name in hasChannelGMM) return Promise.resolve(hasChannelGMM[name]);
                return packet(name, "gmm").then((fit) => {
                    if (fit) hasChannelGMM[name] = fit;
                    return fit;
                });
            },
        };

        const dataLayer = {
            imageBitRange: [0, 65536],
            // A layer's channels have no marker/feature-column distinction:
            // the names ARE the full names.
            getFullChannelName: (name) => name,
            getSavedChannelList: () => Promise.resolve(savedRowsFor(spec, channels)),
            // Never: this instance persists through `persistChannelList`
            // below, into the layer record, not into the project's channel
            // list. See the override.
            saveChannelList: () => Promise.resolve(null),
        };

        // A bus of this panel's own. `SimpleEventHandler` has no unbind, so a
        // handler bound to a shared node would outlive every card removal and
        // fire into a dead panel. A detached element dies with this closure.
        const bus = document.createElement("div");
        const sidebar = new ViewerSidebar(
            {}, names, dataLayer, new SimpleEventHandler(bus), channelList,
            {
                root: node,
                idPrefix: prefix,
                // The project's saved channel list is the REFERENCE image's.
                // This instance writes the layer record instead, through the
                // override below -- so the guard stays on.
                persist: false,
                channelIndex: nameToIndex,
                // Undefined, not pinned: a layer's tiles are the same quantized
                // WebP the reference image's are, so this panel's slider domain
                // must follow the viewer's HD toggle exactly as the reference's
                // does. `destroy()` is what makes the listener that costs safe.
                hdMode: undefined,
            });
        sidebar.maxChannelSlots = Math.min(MAX_SLOTS, names.length);
        sidebar.initialChannelSlots = Math.max(1, Math.min(4, names.length));

        /**
         * WHERE THIS PANEL'S CHANGES ARE SAVED -- overridden on the instance
         * rather than bound to the bus.
         *
         * `scheduleSaveChannels` is what every setter calls, it is already
         * debounced at 400 ms, it is already suppressed during `init`'s
         * restore (`_restoring`), and `init` already calls this once for a
         * layer that has never been saved so a brand-new layer keeps its
         * auto-levelled default. Binding three events instead would
         * reimplement all four of those and get the cadence wrong.
         *
         * Note that `scheduleSaveChannels` does NOT check `persist` -- only
         * this method does -- which is exactly why replacing this method is
         * the whole of the change.
         */
        sidebar.persistChannelList = () => {
            persist({ render: { channels: slotsToChannels(sidebar, nameToIndex) } });
            return Promise.resolve(null);
        };

        /** What the layer draws, from what the slots say. */
        function reconcile() {
            draw?.(slotsToDrawn(sidebar));
        }
        for (const event of [ChannelList.events.CHANNELS_CHANGE,
                             ChannelList.events.COLOR_TRANSFER_CHANGE,
                             ChannelList.events.BRUSH_MOVE]) {
            sidebar.eventHandler.bind(event, reconcile);
        }

        // Unawaited: the card is built synchronously and the slots fill in as
        // the restore resolves, exactly as the viewer's own sidebar does.
        const ready = sidebar.init(described).then(reconcile).catch((error) => {
            console.error("layerChannelPanel: mounting the channel controls failed", error);
        });

        return {
            node,
            footer,
            sidebar,
            ready,
            /** Put the alignment note on the panel's last line, replacing any
             *  note a previous render left there. */
            setAlignment(note) {
                const held = footer.querySelector(".layer-card-alignment");
                if (held) held.remove();
                if (note) footer.insertBefore(note, footer.firstChild);
            },
            destroy() {
                sidebar.destroy();
            },
        };
    }

    const api = { mount, prefixFor, savedRowsFor, slotsToChannels, slotsToDrawn,
                  MAX_SLOTS };
    if (typeof window !== "undefined") window.PlexoraLayerChannels = api;
    if (typeof globalThis !== "undefined" && !globalThis.PlexoraLayerChannels) {
        globalThis.PlexoraLayerChannels = api;
    }
}());
