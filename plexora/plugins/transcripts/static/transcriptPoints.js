/**
 * Millions of molecules, drawn as point sprites on their own WebGL2 canvas.
 *
 * WHY NOT THE OVERLAY CANVAS. Core's `addOverlay` gives a plugin a 2-D context
 * with the image transform already applied, and that is the right tool for a
 * few hundred things: a selection polygon, a handful of ROI shapes. A
 * transcript view is three to five hundred thousand dots per frame, and in
 * canvas2d each one is an `arc` -- a path, a fill, a rasterizer set-up -- so
 * the frame budget is gone an order of magnitude before the data is. One
 * `drawArrays` per tile costs the same whether the tile holds ten points or
 * fifty thousand.
 *
 * IT IS NOT A SECOND VIEW OF THE DATA. The canvas is inserted into
 * OpenSeadragon's own `viewer.canvas`, directly above the tile canvas and
 * directly below the 2-D overlay, so the stacking is: image, transcripts,
 * then everything core draws over the image -- centroids, ROI, the selection
 * polygon. Transcripts belong under those: they are what the tissue IS, and
 * the cell work is what somebody is doing to it.
 *
 * THE TRANSFORM IS OPENSEADRAGON'S OWN, READ BACK. Every frame the anchor
 * world item is asked where image (0,0) landed and how many screen pixels an
 * image pixel is worth -- the identical two questions `canvas-overlay-hd.js`
 * asks to set up its 2-D transform. Deriving it from the viewport bounds
 * instead would drift from the tiles by a fraction of a pixel at high zoom,
 * which on a dot two pixels wide is visible.
 *
 * THREE THINGS ARE DECIDED IN THE SHADER, AND THAT IS THE POINT.
 *
 *   Which genes are visible, and in what colour. A 2x N RGBA texture: one
 *   texel per gene carrying its colour and whether it is on, another carrying
 *   its icon. Toggling a gene is a two-byte texture write and a redraw -- no
 *   refetch, no re-upload, no rebuild of anything.
 *
 *   The quality threshold. Each point carries its own score, so moving the Q
 *   slider is a uniform change. Filtering server side would have meant a
 *   round trip per drag tick over 19 million rows.
 *
 *   The glyph. `gl_PointCoord` plus a shape function is eight icons for the
 *   cost of one; an icon atlas would be a texture to build, bind and keep in
 *   step with a palette that the user can change.
 *
 * Falls back to nothing at all when WebGL2 is unavailable: `isSupported()` is
 * false and the layer draws through core's 2-D overlay instead. A headless
 * boot probe takes that path, which is also what keeps this file testable.
 */
class TranscriptPointRenderer {

    /**
     * The glyph set, and the arithmetic that keeps them one size.
     *
     * A SHAPE IS AS BIG AS THE DOT IT REPLACES. Every glyph is scaled so it
     * covers the same AREA as the circle at the same point size, which is what
     * makes one slider mean one thing: switching a gene from a dot to a
     * triangle changes what it is, not how much of the picture it takes.
     *
     * The radii used to be literal numbers chosen by eye, and they were not
     * close. An equal-area triangle reaches 1.55x as far as the dot and the
     * one written here reached 0.66x, so turning Icons on made every molecule
     * visibly smaller and dragging the size slider never brought them level --
     * which is exactly what "the slider changes the points but not the icons"
     * looks like from the outside.
     *
     * `reach` is how far a shape goes from its centre, in units where the
     * dot's radius is 0.95. `span` turns that into the sprite's own half-width
     * -- keeping the 5% margin the dot has always had -- and IS the point-size
     * multiplier the vertex shader applies, so a triangle is drawn into a
     * sprite 1.55x the dot's and fills the same number of pixels inside it.
     *
     * The GLSL below is GENERATED from this table (see `glyphSource`). One set
     * of numbers, so the shape the shader draws and the size the sprite is
     * made cannot drift apart.
     */
    static GEOMETRY = (function () {
        //: The dot, unchanged. Everything else is sized against it, and
        //: `styleMode: circles` still draws exactly this.
        const RADIUS = 0.95;
        const area = Math.PI * RADIUS * RADIUS;
        const root = Math.sqrt;
        //: The proportions kept from the shapes as they were first drawn: how
        //: thick a plus's arms are against their length, how wide a ring's
        //: wall is, how deep a star's points cut. Only the SIZE is recomputed
        //: here -- the character of each glyph is the one that was chosen.
        const arm = 0.28 / 0.95;
        const hole = 0.55 / 0.95;
        const body = 0.55;
        const lobe = 0.45;

        //: Each solved for the radius that makes its area equal the dot's.
        const square = root(area / 4);                          // 4a^2
        const diamond = root(area / 2);                         // 2d^2
        const inradius = root(area / (3 * root(3)));            // 3*sqrt(3)*r^2
        const plus = root(area / (8 * arm - 4 * arm * arm));    // 8tL - 4t^2
        const ring = root(area / (Math.PI * (1 - hole * hole)));
        //: r = k*(body + lobe*|cos(2.5 t)|), integrated: k^2 (pi b^2 + 4ab + pi a^2 / 2)
        const star = root(area / (Math.PI * body * body + 4 * lobe * body
                                  + Math.PI * lobe * lobe / 2));

        const f = (value) => value.toFixed(6);
        const entries = [
            { name: "circle", reach: RADIUS,
              coverage: `band(length(p) - ${f(RADIUS)})` },
            { name: "square", reach: square,
              coverage: `band(max(abs(p.x), abs(p.y)) - ${f(square)})` },
            { name: "diamond", reach: diamond,
              coverage: `band(abs(p.x) + abs(p.y) - ${f(diamond)})` },
            //: Three half-planes the same distance from the centre, which is
            //: what makes it equilateral. The apex is at -2r, so the sprite
            //: has to hold twice the inradius.
            { name: "triangle", reach: 2 * inradius,
              coverage: `band(max(p.y - ${f(inradius)},`
                  + ` max(-0.866025 * p.x - 0.5 * p.y - ${f(inradius)},`
                  + ` 0.866025 * p.x - 0.5 * p.y - ${f(inradius)})))` },
            { name: "plus", reach: plus,
              coverage: `band(min(abs(p.x), abs(p.y)) - ${f(plus * arm)})`
                  + ` * band(max(abs(p.x), abs(p.y)) - ${f(plus)})` },
            //: The plus turned 45 degrees. The rotation is orthonormal, so the
            //: area is the plus's -- but the arm ENDS move out to (L+t)/sqrt2
            //: on each axis, and the sprite has to hold that and not L.
            { name: "cross", reach: plus * (1 + arm) / Math.SQRT2,
              coverage: `band(min(abs(r.x), abs(r.y)) - ${f(plus * arm)})`
                  + ` * band(max(abs(r.x), abs(r.y)) - ${f(plus)})` },
            { name: "ring", reach: ring,
              coverage: `band(length(p) - ${f(ring)})`
                  + ` * (1.0 - band(length(p) - ${f(ring * hole)}))` },
            { name: "star", reach: star,
              coverage: `band(length(p) - (${f(star * body)}`
                  + ` + ${f(star * lobe)} * abs(cos(2.5 * atan(p.y, p.x)))))` },
        ];
        return entries.map((entry) => ({ ...entry, span: entry.reach / RADIUS }));
    })();

    /** The glyphs, in the order an icon index means. */
    static get ICONS() {
        return TranscriptPointRenderer.GEOMETRY.map((entry) => entry.name);
    }

    /** How much bigger than the dot each glyph's sprite is, by icon index. */
    static get SPANS() {
        return TranscriptPointRenderer.GEOMETRY.map((entry) => entry.span);
    }

    /**
     * `glyph()` for the fragment shader, written out of GEOMETRY.
     *
     * Generated rather than typed, because the size of a shape and the sprite
     * it is drawn into are two halves of one number: an equal-area triangle
     * that the vertex shader still sizes as a dot is a triangle with its
     * corners cut off, and nothing on screen says which of the two was wrong.
     */
    static glyphSource() {
        const entries = TranscriptPointRenderer.GEOMETRY;
        const branches = entries.slice(1).map(
            (entry, index) => `    if (icon == ${index + 1}) `
                + `return ${entry.coverage};   // ${entry.name}`);
        return `float glyph(int icon, vec2 p) {
    vec2 r = vec2(p.x + p.y, p.x - p.y) * 0.7071068;
${branches.join("\n")}
    return ${entries[0].coverage};   // ${entries[0].name}
}`;
    }

    //: Bytes per point once repacked for the GPU: x f4, y f4, gene u2, q u1,
    //: a pad byte, count f4. A float attribute has to sit on a 4-byte
    //: boundary, so the layout is reordered here rather than the server
    //: writing it padded and paying the extra bytes on disk and on the wire
    //: for every one of 19 million records.
    //:
    //: ONE LAYOUT FOR BOTH KINDS OF RECORD. A molecule and an aggregate of
    //: molecules differ in two fields -- a molecule has a quality score and
    //: an aggregate has a count -- and carrying both costs four bytes of GPU
    //: memory per point against a second vertex layout, a second shader path
    //: and a rule about which tile is which. A molecule's count is 1 and an
    //: aggregate's score is "keeps whatever threshold you set", so every
    //: point answers both questions and the shader never asks which it is.
    static get STRIDE() { return 16; }

    /**
     * How much a dot grows per DOUBLING of the molecules it stands for.
     *
     * SIZE IS NOT THE QUANTITATIVE CHANNEL HERE, and that is a decision
     * rather than a limitation. The first version of this made the radius
     * the square root of the count, so area read as quantity -- correct as
     * an encoding, and at whole-slide zoom on an abundant gene it turned
     * the section into a field of overlapping bubbles with the tissue
     * invisible underneath. What the picture is FOR at that zoom is the
     * spatial distribution, and a dot that covers its neighbours destroys
     * exactly that. So the count moves the size by a tenth per doubling and
     * stops at MAX_GROWTH: a bin of 1000 molecules is 1.6x the radius of a
     * bin of one, which reads as "more here" without taking the ground.
     * Quantity, when it is the question, is what Density map answers.
     *
     * It also makes the zoom transitions calm. A level boundary quadruples
     * the count, which used to double the radius -- a visible jump in dot
     * size at every step of a zoom. Now it is a fifth, and clamped.
     */
    static get AGGREGATE_GROWTH() { return 0.10; }

    /** The most an aggregate may grow for its count, whatever the count. */
    static get MAX_GROWTH() { return 1.6; }

    /**
     * The smallest a RESTING dot is drawn, in CSS pixels.
     *
     * One pixel, because that is the whole-slide picture this overlay is
     * for: a faint grain of gene colour laid over the morphology, not a
     * field of discs sitting on top of it. A one-pixel dot is also what
     * makes it affordable to draw far more of them, which is what turns
     * the zoomed-out overlay from a pegboard into a texture.
     */
    static get MIN_DOT() { return 1; }

    /**
     * How fast a dot shrinks as the view pulls back, as a power of the zoom.
     *
     * THE RESTING SIZE IS A FUNCTION OF THE ZOOM, and this is what makes a
     * zoomed-out view read as tissue again. A point size fixed in SCREEN
     * pixels means a dot is as prominent on a whole section as it is on one
     * cell, so pulling back turned the section into a mat of circles -- and
     * merging harder did not fix it, because what was wrong was the ink per
     * dot rather than the number of them.
     *
     * So the slider sets the size at the zoom where a dot stands for one
     * molecule (`fullZoom`), and below that the dot fades towards MIN_DOT.
     * A half power and not a whole one: shrinking in proportion to the zoom
     * reaches the floor after about one screenful of zooming out and then
     * stays there, which throws away the whole progression from a cell to a
     * section. At 0.5 a four-fold zoom out halves the dot -- 6 px at the
     * top, 3 px four steps out, 1.5 px sixteen, the floor near a whole
     * slide -- so the dots become distinct gradually as the user comes in.
     */
    static get ZOOM_FADE() { return 0.5; }

    /**
     * How much bigger a hovered gene's points are drawn.
     *
     * A MULTIPLIER ON THE USER'S POINT SIZE, not a pixel count: somebody
     * who set the size to 2 to see a dense panel and somebody who set it to
     * 14 both need the same RELATIVE jump for the hovered gene to separate
     * from the rest.
     *
     * ON THE SIZE THE SLIDER SAYS, NOT ON THE FADED ONE, which is the part
     * that matters now that resting is a pixel or two at low zoom: 1.8x of
     * one pixel is two pixels and nobody could find it. Hovering lifts a
     * gene out of the fade rather than scaling within it, so one gene's
     * distribution is readable across a whole section while everything
     * else stays a texture -- which is the entire point of the gesture.
     *
     * The bin it stands for is still the ceiling, so the lift can never
     * produce the overlap the level of detail was chosen to avoid: a
     * hovered dot fills the patch of slide it speaks for and no more. That
     * ceiling is what 1.8 used to have to do by being small -- unbounded at
     * 2.2, an abundant gene's dots merged into chains that covered the
     * tissue, which is the thing this redesign is against. How much of the
     * bin it may take is `u_emphFill`, which the layer pulls back for a
     * gene that has a dot in nearly every bin -- see `emphasisFill`.
     */
    static get EMPHASIS_SCALE() { return 1.8; }

    //: Time constant of the hover ease, in milliseconds. An exponential
    //: approach, so it is settled in about three of these -- long enough to
    //: read as motion, short enough that running down a gene list does not
    //: feel like waiting for anything.
    static get EMPHASIS_TAU() { return 55; }


    constructor(viewer, options = {}) {
        this.viewer = viewer;
        //: Which world item the coordinates are anchored to. The same guard
        //: core's overlay uses -- item 0 is whichever channel happened to be
        //: added first, and the anchor is the one whose transform the
        //: reference pixel grid belongs to.
        this.anchorIndex = options.anchorIndex || (() => 0);
        this.gl = null;
        this.canvas = null;
        this.tiles = new Map();
        //: Which tag `draw` is currently showing; null draws everything,
        //: which is what a caller that never sets one gets.
        this.activeTag = null;
        this.geneCount = 1;
        this.visible = true;
        this.pointSize = 6;
        this.opacity = 1;
        this.minQ = 0;
        //: Image pixels one aggregation bin covers, and therefore how big a
        //: dot standing for a whole bin is allowed to get. Zero at level 0,
        //: where a point is one molecule and stands for nothing but itself.
        this.binPixels = 0;
        //: The zoom -- CSS pixels per image pixel -- at which a dot is
        //: drawn at the full size the slider asks for. Below it the dot
        //: fades towards MIN_DOT; at and above it the slider is taken
        //: literally. Zero until the layer says otherwise, meaning no fade.
        this.fullZoom = 0;
        //: How much of its bin a lifted dot may fill. 1 unless the layer
        //: says the hovered gene has a dot in nearly every bin, in which
        //: case filling them is not a highlight but a flood fill.
        this.emphasisFill = 1;
        //: Where the hover ease is now and where it is heading, as a
        //: FRACTION of the way from the resting size to the lifted one. 0
        //: is "no gene is emphasised"; the mask of WHICH genes lives in the
        //: gene table, so moving from one gene to the next is a texture
        //: write of a few hundred bytes and everything else is this number.
        this.emphasis = 0;
        this.emphasisTarget = 0;
        this._eased = 0;
        this.styleMode = 0;          // 0 circles, 1 icons
        this._frame = null;
        this._table = null;
        this._tableData = null;
        this.mount();
    }

    isSupported() { return Boolean(this.gl); }

    // -- the canvas ------------------------------------------------------

    mount() {
        const host = this.viewer?.canvas;
        if (!host) return;
        const canvas = document.createElement("canvas");
        canvas.className = "transcripts-gl";
        canvas.style.position = "absolute";
        canvas.style.left = "0";
        canvas.style.top = "0";
        // Never a pointer target. Everything clickable on this viewer lives
        // on the layer above or the tiles below, and a canvas that swallowed
        // events would break both.
        canvas.style.pointerEvents = "none";

        // Above the tiles, below the 2-D overlay. `_canvasdiv` is
        // CanvasOverlayHd's own wrapper; inserting before it is what puts the
        // molecules under the centroids and the ROI rather than over them.
        const overlay = this.viewer.canvas.querySelector("div");
        if (overlay) host.insertBefore(canvas, overlay);
        else host.appendChild(canvas);
        this.canvas = canvas;

        this.gl = canvas.getContext("webgl2", {
            alpha: true,
            antialias: false,
            depth: false,
            stencil: false,
            premultipliedAlpha: false,
            preserveDrawingBuffer: false,
        });
        if (!this.gl) {
            canvas.remove();
            this.canvas = null;
            return;
        }
        this.initProgram();
        this.resize();
    }

    initProgram() {
        const gl = this.gl;
        //: The driver's own ceiling on a point sprite -- 255 on some, 1024
        //: on others. A sprite past it is silently clamped, so the cap is
        //: read and applied here rather than discovered as "the biggest
        //: aggregates are all the same size on this machine".
        const range = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE);
        this.maxSprite = (range && range[1]) || 64;
        const glyphs = TranscriptPointRenderer.GEOMETRY.length;
        const vertex = `#version 300 es
precision highp float;
// AND highp int, in BOTH stages. u_styleMode is an int uniform the vertex
// shader reads to size the sprite and the fragment shader reads to pick the
// glyph, and GLSL ES defaults int to highp in a vertex shader and mediump in
// a fragment one -- so declaring neither is a link error ("precisions of
// uniform differ"), and a link error here is a viewer that silently falls
// back to drawing every molecule through a 2-D canvas.
precision highp int;
in vec2 a_pos;
in float a_gene;
in float a_q;
in float a_count;
uniform vec2 u_origin;
uniform float u_scale;
uniform vec2 u_viewport;
// The view transform (core's Rotate and Flip), as OpenSeadragon's canvas
// drawer applies it: turn about the canvas centre by (cos, sin), then mirror
// about the vertical centre line when u_flip is 1. All in device pixels.
uniform vec2 u_center;
uniform vec2 u_rotation;
uniform float u_flip;
uniform float u_pointSize;
uniform float u_minQ;
uniform float u_binPixels;
uniform float u_maxSprite;
uniform float u_growth;
uniform float u_maxGrowth;
uniform float u_emphasis;
uniform float u_emphSize;
uniform float u_emphFill;
// -1 draws every point in one pass. 0 and 1 split the emphasised genes out
// so they can be drawn LAST and land on top of what they are being compared
// against -- an enlarged dot with a neighbour's small one punched out of its
// middle reads as a ring, not as a highlight.
uniform int u_pass;
uniform float u_tableWidth;
uniform int u_styleMode;
uniform float u_span[${glyphs}];
uniform sampler2D u_table;
out vec3 v_color;
out float v_icon;
out float v_span;
void main() {
    float u = (a_gene + 0.5) / u_tableWidth;
    vec4 style = texture(u_table, vec2(u, 0.25));
    vec4 meta = texture(u_table, vec2(u, 0.75));
    float mark = meta.g;
    bool mine = (u_pass < 0) || ((u_pass == 1) == (mark > 0.5));
    if (style.a < 0.5 || a_q < u_minQ || !mine) {
        // Off clip and zero-sized: cheaper than a branch in the fragment
        // shader, and it is what makes toggling a gene free.
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
        gl_PointSize = 0.0;
        v_color = vec3(0.0);
        v_icon = 0.0;
        v_span = 1.0;
        return;
    }
    int icon = int(floor(meta.r * 255.0 + 0.5));
    // THE SPRITE IS SIZED FOR THE SHAPE IT HOLDS. Every glyph covers the same
    // area as the dot (see GEOMETRY), and the ones that are spikier reach
    // further to do it -- a triangle 1.55x as far. Drawing them all into a
    // dot-sized sprite is what cut the corners off and made Icons look like a
    // smaller point size that the slider could not fix.
    v_span = (u_styleMode == 0) ? 1.0 : u_span[clamp(icon, 0, ${glyphs - 1})];
    vec2 device = u_origin + a_pos * u_scale;
    vec2 offset = device - u_center;
    device = u_center + vec2(offset.x * u_rotation.x - offset.y * u_rotation.y,
                             offset.x * u_rotation.y + offset.y * u_rotation.x);
    if (u_flip > 0.5) device.x = 2.0 * u_center.x - device.x;
    gl_Position = vec4((device.x / u_viewport.x) * 2.0 - 1.0,
                       1.0 - (device.y / u_viewport.y) * 2.0,
                       0.0, 1.0);
    // A TENTH PER DOUBLING, CLAMPED. Size is deliberately a weak channel
    // here: crowding is controlled by how coarsely the level merges, not by
    // how big the dots get, so that the tissue stays visible under them.
    // See AGGREGATE_GROWTH for why this is not the square root of the count.
    //
    // A dot may not outgrow the patch of slide it stands for -- u_binPixels
    // is the bin in IMAGE pixels and u_scale turns it into screen ones, zero
    // at level 0 where every count is 1. With growth this gentle the cap
    // almost never binds at rest; it is here so that a large point size at a
    // coarse level cannot produce overlap the level was chosen to avoid.
    // Capped on the DOT the glyph is equal in area to and not on the sprite
    // that holds it, or a triangle and a circle standing for the same count
    // would stop covering the same pixels.
    //
    // u_pointSize arrives ALREADY FADED for the zoom (see ZOOM_FADE) and
    // u_emphSize does not: a hovered gene is lifted out of the fade rather
    // than multiplied inside it, which is the only way to trace one gene
    // across a section where the resting dot is a single pixel. The lift
    // still answers to the ceiling, so it fills the patch it stands for and
    // stops. u_emphasis is how far along that lift the ease has got.
    float grow = min(1.0 + u_growth * log2(max(a_count, 1.0)), u_maxGrowth);
    float ceiling = (u_binPixels > 0.0) ? u_binPixels * u_scale : u_maxSprite;
    float rest = min(u_pointSize * grow, ceiling);
    float lifted = max(rest, min(u_emphSize, ceiling * u_emphFill));
    float dot = mix(rest, lifted, mark * u_emphasis);
    gl_PointSize = clamp(dot * v_span, 1.0, u_maxSprite);
    v_color = style.rgb;
    v_icon = float(icon);
}`;

        const fragment = `#version 300 es
precision highp float;
precision highp int;
in vec3 v_color;
in float v_icon;
in float v_span;
uniform float u_opacity;
uniform int u_styleMode;
out vec4 fragColor;

// Every glyph as a coverage in [0,1] over the sprite's own square.
// Antialiased with fwidth so a dot two pixels across still has an edge.
float band(float d) {
    float w = max(fwidth(d), 0.001);
    return 1.0 - smoothstep(-w, w, d);
}

${TranscriptPointRenderer.glyphSource()}

void main() {
    // The sprite spans [-v_span, v_span] rather than [-1, 1], which is what
    // puts every shape in the same units: a radius written in glyph() means
    // the same distance whatever sprite it is drawn into.
    vec2 p = (gl_PointCoord * 2.0 - 1.0) * v_span;
    p.y = -p.y;
    float mask = (u_styleMode == 0) ? band(length(p) - 0.95)
                                    : glyph(int(v_icon), p);
    if (mask <= 0.01) discard;
    fragColor = vec4(v_color, mask * u_opacity);
}`;

        const program = this.link(vertex, fragment);
        if (!program) {
            this.gl = null;
            this.canvas?.remove();
            this.canvas = null;
            return;
        }
        this.program = program;
        gl.useProgram(program);
        this.attr = {
            pos: gl.getAttribLocation(program, "a_pos"),
            gene: gl.getAttribLocation(program, "a_gene"),
            q: gl.getAttribLocation(program, "a_q"),
            count: gl.getAttribLocation(program, "a_count"),
        };
        this.uniform = {};
        for (const name of ["u_origin", "u_scale", "u_viewport", "u_pointSize",
                            "u_center", "u_rotation", "u_flip",
                            "u_minQ", "u_tableWidth", "u_table", "u_opacity",
                            "u_styleMode", "u_binPixels", "u_maxSprite",
                            "u_growth", "u_maxGrowth", "u_emphasis",
                            "u_emphSize", "u_emphFill", "u_pass",
                            "u_span[0]"]) {
            this.uniform[name] = gl.getUniformLocation(program, name);
        }
        // Constant for the life of the program: the shapes do not change, only
        // which gene wears which.
        gl.uniform1fv(this.uniform["u_span[0]"],
                      new Float32Array(TranscriptPointRenderer.SPANS));
        this.vao = gl.createVertexArray();
        this._table = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, this._table);
        for (const [key, value] of [
            [gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST],
            [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE],
        ]) gl.texParameteri(gl.TEXTURE_2D, key, value);
        this.setGeneTable([]);
    }

    link(vertexSource, fragmentSource) {
        const gl = this.gl;
        const compile = (type, source) => {
            const shader = gl.createShader(type);
            gl.shaderSource(shader, source);
            gl.compileShader(shader);
            if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
            console.error("transcripts: shader failed to compile",
                          gl.getShaderInfoLog(shader));
            gl.deleteShader(shader);
            return null;
        };
        const vs = compile(gl.VERTEX_SHADER, vertexSource);
        const fs = compile(gl.FRAGMENT_SHADER, fragmentSource);
        if (!vs || !fs) return null;
        const program = gl.createProgram();
        gl.attachShader(program, vs);
        gl.attachShader(program, fs);
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            console.error("transcripts: program failed to link",
                          gl.getProgramInfoLog(program));
            return null;
        }
        return program;
    }

    // -- the gene table --------------------------------------------------

    /**
     * Colour, visibility and icon for every gene in the panel.
     *
     * @param entries - `[{color: [r,g,b], visible, icon, emphasis}]`,
     *   indexed by the gene index the records carry. One texture write, so a
     *   panel of five hundred genes costs the same as one of five -- which
     *   is what makes hovering down a gene list free: the mask of which
     *   genes are emphasised is here, the amount is a uniform, and neither
     *   touches the geometry.
     */
    setGeneTable(entries) {
        const gl = this.gl;
        if (!gl) return;
        const list = entries || [];
        const width = Math.max(1, list.length);
        const data = new Uint8Array(width * 2 * 4);
        for (let index = 0; index < width; index += 1) {
            const entry = list[index] || {};
            const [r, g, b] = entry.color || [154, 160, 170];
            data[index * 4 + 0] = r;
            data[index * 4 + 1] = g;
            data[index * 4 + 2] = b;
            data[index * 4 + 3] = entry.visible ? 255 : 0;
            data[(width + index) * 4 + 0] = (entry.icon || 0) & 0xff;
            data[(width + index) * 4 + 1] = entry.emphasis ? 255 : 0;
        }
        this.geneCount = width;
        this._tableData = data;
        gl.bindTexture(gl.TEXTURE_2D, this._table);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, 2, 0, gl.RGBA,
                      gl.UNSIGNED_BYTE, data);
    }

    // -- tiles -------------------------------------------------------------

    //: Bytes per record on the wire. A molecule is u2 gene, f4 x, f4 y, u1
    //: q; an aggregate is u2 gene, f4 x, f4 y, u4 count. Both packed, both
    //: unpadded, and both mirrored from `transcript_tiles` -- POINT_DTYPE
    //: and AGGREGATE_DTYPE respectively.
    static get WIRE_STRIDE() { return 11; }
    static get WIRE_STRIDE_AGGREGATE() { return 14; }

    /**
     * The wire format, repacked for the GPU.
     *
     * 11 or 14 bytes in and 16 out, with x and y first so they land on a
     * 4-byte boundary. WebGL refuses a float attribute at an unaligned
     * offset, and the alternative (padding the record on disk) is another 19
     * megabytes per ten million molecules for bytes the CPU throws away.
     *
     * The two kinds converge here, which is what lets one shader draw both:
     * a molecule is given a count of 1, and an aggregate is given a quality
     * score of 255 because the server applied the threshold before merging
     * and what came back has already passed it.
     */
    static repack(buffer, aggregated = false) {
        const view = new DataView(buffer);
        const stride = aggregated
            ? TranscriptPointRenderer.WIRE_STRIDE_AGGREGATE
            : TranscriptPointRenderer.WIRE_STRIDE;
        const count = Math.floor(view.byteLength / stride);
        const out = new ArrayBuffer(count * TranscriptPointRenderer.STRIDE);
        const floats = new Float32Array(out);
        const bytes = new Uint8Array(out);
        const shorts = new Uint16Array(out);
        for (let index = 0; index < count; index += 1) {
            const at = index * stride;
            floats[index * 4 + 0] = view.getFloat32(at + 2, true);
            floats[index * 4 + 1] = view.getFloat32(at + 6, true);
            floats[index * 4 + 3] = aggregated ? view.getUint32(at + 10, true) : 1;
            shorts[index * 8 + 4] = view.getUint16(at, true);
            bytes[index * 16 + 10] = aggregated ? 255 : view.getUint8(at + 10);
        }
        return { data: out, count };
    }

    /**
     * Upload one tile's points, replacing whatever was there.
     *
     * `tag` says which picture this tile belongs to -- a level of detail and
     * a gene selection -- and only the tiles of the ACTIVE tag are drawn.
     * That is what makes a change of level seamless: the new level's tiles
     * are uploaded while the old level's are still on screen, and the switch
     * is one assignment once they have all arrived. Without it the choice is
     * between a blank frame and a frame with both levels drawn over each
     * other, and the second is worse.
     */
    setTile(key, packed, tag = "") {
        const gl = this.gl;
        if (!gl || !packed) return;
        let entry = this.tiles.get(key);
        if (!entry) {
            entry = { buffer: gl.createBuffer(), count: 0 };
            this.tiles.set(key, entry);
        }
        entry.tag = tag;
        entry.count = packed.count;
        gl.bindBuffer(gl.ARRAY_BUFFER, entry.buffer);
        gl.bufferData(gl.ARRAY_BUFFER, packed.data, gl.STATIC_DRAW);
    }

    hasTile(key) { return this.tiles.has(key); }

    /** Draw only the tiles carrying this tag. Null draws every tile. */
    setActive(tag) {
        if (this.activeTag === tag) return;
        this.activeTag = tag;
        this.invalidate();
    }

    dropTile(key) {
        const entry = this.tiles.get(key);
        if (!entry) return;
        this.gl?.deleteBuffer(entry.buffer);
        this.tiles.delete(key);
    }

    /** Every tile key currently on the GPU. */
    keys() { return [...this.tiles.keys()]; }

    clearTiles() {
        for (const key of this.keys()) this.dropTile(key);
    }

    // -- drawing -----------------------------------------------------------

    setVisible(on) {
        this.visible = Boolean(on);
        if (this.canvas) this.canvas.style.display = this.visible ? "" : "none";
        this.invalidate();
    }

    set(values = {}) {
        if (values.pointSize !== undefined) this.pointSize = Number(values.pointSize);
        if (values.opacity !== undefined) this.opacity = Number(values.opacity);
        if (values.minQ !== undefined) this.minQ = Number(values.minQ);
        if (values.binPixels !== undefined) {
            this.binPixels = Math.max(0, Number(values.binPixels) || 0);
        }
        if (values.emphasis !== undefined) {
            this.emphasisTarget = Math.min(
                1, Math.max(0, Number(values.emphasis) || 0));
            this._eased = 0;
        }
        if (values.fullZoom !== undefined) {
            this.fullZoom = Math.max(0, Number(values.fullZoom) || 0);
        }
        if (values.emphasisFill !== undefined) {
            this.emphasisFill = Math.min(
                1, Math.max(0.1, Number(values.emphasisFill) || 1));
        }
        if (values.styleMode !== undefined) this.styleMode = values.styleMode ? 1 : 0;
        this.invalidate();
    }

    resize() {
        const canvas = this.canvas;
        const container = this.viewer?.container;
        if (!canvas || !container) return;
        const ratio = window.devicePixelRatio || 1;
        const width = container.clientWidth;
        const height = container.clientHeight;
        if (canvas.width !== Math.round(width * ratio)
            || canvas.height !== Math.round(height * ratio)) {
            canvas.width = Math.round(width * ratio);
            canvas.height = Math.round(height * ratio);
        }
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
    }

    /** Redraw on the next frame. Several calls in one frame cost one draw. */
    invalidate() {
        if (this._frame || !this.gl) return;
        this._frame = window.requestAnimationFrame(() => {
            this._frame = null;
            this.draw();
        });
    }

    /**
     * Where image (0,0) landed and what an image pixel is worth, in DEVICE
     * pixels -- read back off OpenSeadragon rather than recomputed.
     */
    placement() {
        const viewer = this.viewer;
        const world = viewer?.world;
        if (!world || !world.getItemCount()) return null;
        const index = Math.min(Math.max(0, this.anchorIndex()),
                               world.getItemCount() - 1);
        const item = world.getItemAt(index);
        if (!item) return null;
        const ratio = window.devicePixelRatio || 1;
        const viewport = viewer.viewport;
        const zoom = item.viewportToImageZoom(viewport.getZoom(true));
        // UNROTATED, because the shader turns and mirrors the result itself --
        // the same split OpenSeadragon's drawer makes: tiles are placed
        // unrotated and the whole context is turned about the canvas centre.
        const corner = viewport.pixelFromPointNoRotate(
            item.imageToViewportCoordinates(0, 0, true), true);
        const size = viewport.getContainerSize();
        const radians = (viewport.getRotation(true) || 0) * Math.PI / 180;
        return {
            originX: corner.x * ratio,
            originY: corner.y * ratio,
            scale: zoom * ratio,
            centerX: size.x * ratio / 2,
            centerY: size.y * ratio / 2,
            cos: Math.cos(radians),
            sin: Math.sin(radians),
            flipped: !!viewport.getFlip?.(),
        };
    }

    /**
     * How big one dot is at rest, in CSS pixels, at this zoom.
     *
     * The slider's value at `fullZoom` and fading below it -- see ZOOM_FADE
     * for why the size is a function of the zoom at all. Static as well as
     * an instance method because `TranscriptLayer` has to answer the same
     * question to choose a level of detail, and a level chosen for one size
     * while another is drawn is a view that either crowds or wastes.
     */
    static restingSize(pointSize, cssZoom, fullZoom) {
        const base = Math.max(1, Number(pointSize) || 6);
        if (!(cssZoom > 0) || !(fullZoom > 0)) return base;
        const fade = Math.min(
            1, (cssZoom / fullZoom) ** TranscriptPointRenderer.ZOOM_FADE);
        return Math.max(TranscriptPointRenderer.MIN_DOT, base * fade);
    }

    restingSize(cssZoom) {
        return TranscriptPointRenderer.restingSize(
            this.pointSize, cssZoom, this.fullZoom);
    }

    draw() {
        const gl = this.gl;
        if (!gl) return;
        this.resize();
        gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        if (!this.visible || !this.tiles.size) return;
        const place = this.placement();
        if (!place) return;

        gl.useProgram(this.program);
        gl.bindVertexArray(this.vao);
        gl.enable(gl.BLEND);
        gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA,
                             gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

        gl.uniform2f(this.uniform.u_origin, place.originX, place.originY);
        gl.uniform1f(this.uniform.u_scale, place.scale);
        gl.uniform2f(this.uniform.u_center, place.centerX, place.centerY);
        gl.uniform2f(this.uniform.u_rotation, place.cos, place.sin);
        gl.uniform1f(this.uniform.u_flip, place.flipped ? 1 : 0);
        gl.uniform2f(this.uniform.u_viewport,
                     gl.drawingBufferWidth, gl.drawingBufferHeight);
        // RECOMPUTED EVERY FRAME, which is what makes the shrink continuous
        // through a zoom rather than a step whenever the level of detail
        // changes. It costs one square root a frame.
        const ratio = window.devicePixelRatio || 1;
        gl.uniform1f(this.uniform.u_pointSize,
                     this.restingSize(place.scale / ratio) * ratio);
        gl.uniform1f(this.uniform.u_emphSize,
                     Math.max(1, this.pointSize)
                     * TranscriptPointRenderer.EMPHASIS_SCALE * ratio);
        gl.uniform1f(this.uniform.u_emphFill, this.emphasisFill);
        gl.uniform1f(this.uniform.u_minQ, this.minQ);
        gl.uniform1f(this.uniform.u_binPixels, this.binPixels);
        gl.uniform1f(this.uniform.u_maxSprite, this.maxSprite || 64);
        gl.uniform1f(this.uniform.u_growth,
                     TranscriptPointRenderer.AGGREGATE_GROWTH);
        gl.uniform1f(this.uniform.u_maxGrowth,
                     TranscriptPointRenderer.MAX_GROWTH);
        gl.uniform1f(this.uniform.u_emphasis, this.emphasis);
        gl.uniform1f(this.uniform.u_tableWidth, this.geneCount);
        gl.uniform1f(this.uniform.u_opacity, this.opacity);
        gl.uniform1i(this.uniform.u_styleMode, this.styleMode);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this._table);
        gl.uniform1i(this.uniform.u_table, 0);

        const stride = TranscriptPointRenderer.STRIDE;
        // One pass while nothing is emphasised, which is nearly always; two
        // while a gene is hovered, so its points are drawn over the ones
        // they are being picked out from rather than under them.
        const passes = this.emphasis > 0.001 ? [0, 1] : [-1];
        for (const pass of passes) {
            gl.uniform1i(this.uniform.u_pass, pass);
            for (const entry of this.tiles.values()) {
                if (!entry.count) continue;
                if (this.activeTag !== null && entry.tag !== this.activeTag) continue;
                gl.bindBuffer(gl.ARRAY_BUFFER, entry.buffer);
                gl.enableVertexAttribArray(this.attr.pos);
                gl.vertexAttribPointer(this.attr.pos, 2, gl.FLOAT, false, stride, 0);
                gl.enableVertexAttribArray(this.attr.gene);
                gl.vertexAttribPointer(this.attr.gene, 1, gl.UNSIGNED_SHORT, false, stride, 8);
                gl.enableVertexAttribArray(this.attr.q);
                gl.vertexAttribPointer(this.attr.q, 1, gl.UNSIGNED_BYTE, false, stride, 10);
                gl.enableVertexAttribArray(this.attr.count);
                gl.vertexAttribPointer(this.attr.count, 1, gl.FLOAT, false, stride, 12);
                gl.drawArrays(gl.POINTS, 0, entry.count);
            }
        }
        gl.bindVertexArray(null);
        this.ease();
    }

    /**
     * Step the hover ease, and ask for another frame while it is moving.
     *
     * Exponential rather than linear, and driven by elapsed time rather
     * than by frame count, so the animation lasts the same fraction of a
     * second on a machine dropping frames as on one that is not.
     */
    ease() {
        if (this.emphasis === this.emphasisTarget) {
            this._eased = 0;
            return;
        }
        const now = (typeof performance !== "undefined" && performance.now)
            ? performance.now() : Date.now();
        const elapsed = this._eased ? Math.min(64, now - this._eased) : 16;
        this._eased = now;
        const step = 1 - Math.exp(-elapsed / TranscriptPointRenderer.EMPHASIS_TAU);
        this.emphasis += (this.emphasisTarget - this.emphasis) * step;
        if (Math.abs(this.emphasisTarget - this.emphasis) < 0.01) {
            this.emphasis = this.emphasisTarget;
        }
        this.invalidate();
    }

    destroy() {
        if (this._frame) window.cancelAnimationFrame(this._frame);
        this._frame = null;
        this.clearTiles();
        if (this.gl) {
            this.gl.deleteTexture(this._table);
            this.gl.deleteVertexArray(this.vao);
            this.gl.deleteProgram(this.program);
        }
        this.canvas?.remove();
        this.canvas = null;
        this.gl = null;
    }
}

if (typeof window !== "undefined") window.TranscriptPointRenderer = TranscriptPointRenderer;
if (typeof globalThis !== "undefined") globalThis.TranscriptPointRenderer = TranscriptPointRenderer;
