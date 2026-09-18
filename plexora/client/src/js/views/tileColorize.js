/**
 * The per-tile colorize pass: one decoded tile plus one channel's colour and
 * range, through the fragment shader and onto the tile's own 2D context.
 *
 * Extracted verbatim from imageViewer.js, where both functions lived as closures
 * in the constructor. What each drew is unchanged; the values they read off `this`
 * are now named dependencies.
 *
 * `tileDrawingCustom` is deliberately NOT async here, though it was in its old
 * home. It never awaited anything -- it only worked at all because it ran to
 * completion synchronously, and OSD raises `tile-drawing` with raiseEvent (which
 * ignores the return value) rather than raiseEventAwaiting. Declaring it sync
 * states that requirement instead of hiding it behind a promise nobody waits for.
 *
 * Served as a classic script (see base.html) and must load BEFORE imageViewer.js.
 */

/**
 * @param renderer             - the GLRenderer from createGLRenderer
 * @param floatRange           - numericData.floatRange, the fallback channel range
 * @param findCurrentChannel   - ImageViewer.findCurrentChannel, bound
 * @param selectCenterProps    - ImageViewer.selectCenterProps, bound
 * @param labelOutlinesEnabled - () => whether the mask is being drawn at all
 * @param modeFlags            - () => ImageViewer.modeFlags
 * @param maskDrawList         - () => the cell layers to blit, bottom of the stack
 *                               first. BLIT ORDER IS Z-ORDER; see maskDrawList.
 */
function createTileDrawing({
    renderer,
    floatRange,
    findCurrentChannel,
    selectCenterProps,
    labelOutlinesEnabled,
    modeFlags,
    maskDrawList,
}) {
    // Default tile-drawing behavior, invoked as the "callback" from the
    // custom handler below.
    function tileDrawingDefault(e) {
        var w = e.rendered.canvas.width;
        var h = e.rendered.canvas.height;
        var gl_w = renderer.width;
        var gl_h = renderer.height;

        // Render a webGL canvas to an input canvas
        var output = renderer.loadArray(e, w, h);
        e.rendered.drawImage(output, 0, 0, gl_w, gl_h, 0, 0, w, h);
    }

    // Custom tile-drawing handler
    const tileDrawingCustom = async (callback, e) => {
        // Read parameters from each tile
        const { source } = e.tiledImage;
        const { tileFormat } = source;

        // A brightfield tile is already the picture. `e.rendered` holds
        // the decoded RGB OSD loaded, and OSD blits it the moment this
        // handler returns -- so doing nothing here is what draws it. The
        // GL pass below exists to turn a grey plane into a coloured one,
        // which is the opposite of what this image needs.
        if (tileFormat === RGB_TILE_FORMAT) return;

        const w = e.rendered.canvas.width;
        const h = e.rendered.canvas.height;

        if (tileFormat != 32) {
            // A tile's URL never changes, so derive sub_url once and keep it
            // on the tile. This was a getUrl() + String.split() allocation
            // per tile per channel per frame.
            let sub_url = e.tile._subUrl;
            if (sub_url === undefined) {
                const group = e.tile.getUrl().split("/");
                sub_url = group[group.length - 3];
                e.tile._subUrl = sub_url;
            }
            const channel = findCurrentChannel(sub_url);
            const range = _.get(channel, "range", floatRange);
            const color = _.get(channel, "color", d3.color("white"));
            const floatColor = toFloatColor(color);
            // The fast/default tile path quantizes 16-bit -> 8-bit
            // server-side, linear against the channel's true max (see
            // get_channel_quantization_window). The shader works directly
            // in that same [0, 255] byte domain -- u_tile_range is
            // expressed in byte units too in this mode (see
            // viewerSidebar.js's getImageRange/toImageConnectorRange),
            // so no reconstruction back into 16-bit units is needed here.
            const tileFmt = e.tile._format === "u8" ? 8 : 16;
            const modes = modeFlags();

            // `e.rendered` is this tile's OWN persistent 2D context -- OSD
            // resolves it per tile via DrawerBase.getDataToDraw() (the tile
            // cache) and blits it onto the canvas right after this handler
            // returns. So when it already holds exactly this colorization
            // there is nothing to do.
            //
            // This early return is the single biggest win available on the
            // client. A CPU profile of a 7-channel pan attributed 81.9% of
            // all wall time to one call: the WebGL-canvas -> 2D-canvas blit
            // at the end of this path, running ~103 times per frame at
            // ~2.3 ms each, because OSD re-raises tile-drawing for every
            // visible tile of every channel on every frame. The pixels were
            // almost always identical to the previous frame's.
            //
            // The signature covers everything the draw below depends on:
            // tile identity (cacheKey also changes when HD swaps the pixel
            // data), the pixel format, and the channel's colour/range/mode.
            const sig = `${e.tile.cacheKey}|${tileFmt}|${floatColor}|${range}|${modes.edge},${modes.or}`;
            if (e.rendered._plexoraSig === sig) {
                return;
            }

            if (!e.tile._array) {
                // Not loaded yet -- e.g. right after the HD toggle forces
                // every visible tile to redraw immediately, before the
                // freshly-invalidated tile's fetch/decode has finished.
                // Falling through with pixels=undefined would still reach
                // gl.texImage2D, which allocates the texture with whatever
                // GPU memory happened to be there -- rendered as solid
                // static instead of skipping this frame.
                e.rendered._plexoraSig = null;
                e.rendered.fillStyle = "black";
                e.rendered.fillRect(0, 0, w, h);
                console.warn("Missing Array for tile:", e.tile.getUrl(), "- skipping rendering");
                return;
            }

            // The black fill is load-bearing, not redundant: the shader
            // emits alpha 0.9, so the GL output composites over whatever is
            // already in this reused canvas.
            e.rendered.fillStyle = "black";
            e.rendered.fillRect(0, 0, w, h);

            // Store channel color and range to send to shader
            renderer.gl_arguments = {
                ...selectCenterProps(e.tile, source),
                centers: [],
                id_end_1i: 0,
                picked_end_1i: 0,
                color_3fv: new Float32Array(floatColor),
                range_2fv: new Float32Array(range),
                fmt_1i: tileFmt,
            };
            callback(e);
            e.rendered._plexoraSig = sig;
            return;
        }

        // Label/segmentation tiles must stay transparent outside outlines
        // so image channels underneath remain visible. Not signature-cached:
        // the label tiled image is a single item (so this is not multiplied
        // by the channel count), and what it draws also depends on each
        // layer's gate and colours, which rerenderSegmentationTiles()
        // rebuilds out of band.
        e.rendered.clearRect(0, 0, w, h);

        if (e.tile._layerContexts) {
            if (labelOutlinesEnabled()) {
                // BLIT ORDER IS Z-ORDER, and it is the whole of the stacking
                // model for mask layers: bottom first, each one painting over
                // whatever the layers below it left. Reordering the sidebar
                // therefore costs a redraw and never a re-render -- the tile
                // canvases already hold the right pixels.
                //
                // Opacity is applied HERE rather than baked into a layer's
                // pixels for the same reason. This branch already re-runs
                // every frame, so a slider drag costs one extra blit
                // argument; folding it into renderLabelTile would instead
                // re-derive every visible tile's boundaries on every pointer
                // move.
                const previous = e.rendered.globalAlpha;
                let alpha = previous;
                for (const layer of maskDrawList()) {
                    const context = e.tile._layerContexts.get(layer.name);
                    if (!context) continue;
                    // A layer with no colours is the plain white cell layer,
                    // which predates the opacity control and must keep
                    // compositing at full strength -- see layerAlpha.
                    const next = layer.lut ? layer.opacity : 1;
                    if (next !== alpha) {
                        e.rendered.globalAlpha = next;
                        alpha = next;
                    }
                    e.rendered.drawImage(context.canvas, 0, 0, w, h);
                }
                if (alpha !== previous) e.rendered.globalAlpha = previous;
            }
            return;
        }

        if (!e.tile._array) {
            console.warn("Missing Array for tile:", e.tile.getUrl(), "- skipping rendering");
            return;
        }

        // Use new parameters for this tile
        renderer.gl_arguments = {
            ...selectCenterProps(e.tile, source),
            color_3fv: new Float32Array([1, 1, 1]),
            range_2fv: new Float32Array([0, 1]),
            fmt_1i: 32,
        };

        // Start webGL rendering
        callback(e);
    };

    return { tileDrawingDefault, tileDrawingCustom };
}


if (typeof window !== "undefined") {
    window.PlexoraTileColorize = { createTileDrawing };
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraTileColorize) {
    globalThis.PlexoraTileColorize = { createTileDrawing };
}
