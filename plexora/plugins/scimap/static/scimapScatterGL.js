// Every cell of an embedding, drawn on the GPU.
//
// A UMAP of four million cells is four million points, and a plot that drew a
// sample would make a lasso a guess. This draws all of them with WebGL2: the
// coordinates go to the GPU once (two float32 per cell), a colour per cell
// (four bytes) when the colour-by changes, and one byte per cell -- selected,
// in the region under the pointer -- when the selection changes. Panning and
// zooming change two uniforms and redraw; nothing is re-uploaded.
//
// Two passes, so selected cells are never buried under the rest: everything
// unselected first, then the selected (and the region's) on top.
//
// The look of a selection is the caller's (`setStyle`): the notebook's light
// page paints the selected cells in its primary colour over faded others; the
// viewer's glass window paints them white, and its Highlight / Dim others /
// Only follow the tissue.
//
// Also the selection tools' test: `inside(polygonScreen)` (a lasso, a
// polygon) and `stroke(pathScreen, radius)` (a brush, the eraser) paint the
// shape into a mask at the canvas's resolution and look every point up in it
// -- one pass over the cells, exact to the pixel the hand drew at, and fast
// enough for millions on the main thread.
//
// A point whose colour has no alpha is not drawn at all, selected or not: a
// group hidden from the legend disappears, and so cannot be picked.
// `style.scale` is the user's point size, over the density-fitted size.
window.ScimapScatterGL = (function () {
    "use strict";

    const VERT = `#version 300 es
in vec2 a_pos;
in vec4 a_color;
in float a_flag;
uniform vec2 u_center;
uniform vec2 u_scale;
uniform float u_size;
uniform int u_pass;
uniform int u_hasSel;
uniform vec4 u_sel;
uniform vec4 u_gold;
uniform float u_rest;
uniform int u_selOwn;
out vec4 v_color;
void main() {
  int flag = int(a_flag + 0.5);
  bool sel = (flag & 1) != 0;
  bool region = (flag & 2) != 0;
  bool top = sel || region;
  if ((u_pass == 0 && top) || (u_pass == 1 && !top) || a_pos.x != a_pos.x || a_color.a < 0.002) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    gl_PointSize = 0.0;
    return;
  }
  gl_Position = vec4((a_pos - u_center) * u_scale, 0.0, 1.0);
  vec4 c = a_color;
  if (region) {
    c = u_gold;
  } else if (sel) {
    c = u_selOwn == 1 ? vec4(a_color.rgb, 1.0) : u_sel;
  } else if (u_hasSel == 1) {
    c = vec4(a_color.rgb, a_color.a * u_rest);
  }
  v_color = c;
  gl_PointSize = top ? u_size * 1.25 : u_size;
}`;

    const FRAG = `#version 300 es
precision mediump float;
in vec4 v_color;
out vec4 outColor;
void main() {
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = dot(d, d);
  if (r > 0.25) discard;
  float edge = 1.0 - smoothstep(0.16, 0.25, r);
  outColor = vec4(v_color.rgb, v_color.a * edge);
}`;

    function compile(gl, type, source) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            const log = gl.getShaderInfoLog(shader);
            gl.deleteShader(shader);
            throw new Error("WebGL shader: " + log);
        }
        return shader;
    }

    class ScatterGL {
        constructor(canvas) {
            this.canvas = canvas;
            const gl = canvas.getContext("webgl2", { antialias: false, premultipliedAlpha: false, alpha: true,
                                                    preserveDrawingBuffer: true });
            if (!gl) throw new Error("This browser cannot draw WebGL2, which the embedding needs.");
            this.gl = gl;
            const program = gl.createProgram();
            gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT));
            gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG));
            gl.linkProgram(program);
            if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
            this.program = program;
            this.loc = {};
            ["a_pos", "a_color", "a_flag"].forEach((n) => { this.loc[n] = gl.getAttribLocation(program, n); });
            ["u_center", "u_scale", "u_size", "u_pass", "u_hasSel", "u_sel", "u_gold", "u_rest", "u_selOwn"]
                .forEach((n) => { this.loc[n] = gl.getUniformLocation(program, n); });
            this.vao = gl.createVertexArray();
            this.buffers = { pos: gl.createBuffer(), color: gl.createBuffer(), flag: gl.createBuffer() };
            this.n = 0;
            this.xy = null;
            this.extent = [0, 1, 0, 1];
            this.view = { cx: 0.5, cy: 0.5, zoom: 1 };
            this.style = { sel: [43, 50, 75, 242], gold: [242, 201, 76, 255], rest: 0.35, selOwn: false, base: 2.2, scale: 1 };
            this.onView = null;
            this.hasSelection = false;
            this.frame = 0;
            this.flags = null;
        }

        /** Coordinates (interleaved x,y float32) and their extent. */
        setPoints(xy, extent) {
            const gl = this.gl;
            this.xy = xy;
            this.n = xy.length >> 1;
            this.extent = extent;
            gl.bindVertexArray(this.vao);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.pos);
            gl.bufferData(gl.ARRAY_BUFFER, xy, gl.STATIC_DRAW);
            gl.enableVertexAttribArray(this.loc.a_pos);
            gl.vertexAttribPointer(this.loc.a_pos, 2, gl.FLOAT, false, 0, 0);
            this.flags = new Uint8Array(this.n);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.flag);
            gl.bufferData(gl.ARRAY_BUFFER, this.flags, gl.DYNAMIC_DRAW);
            gl.enableVertexAttribArray(this.loc.a_flag);
            gl.vertexAttribPointer(this.loc.a_flag, 1, gl.UNSIGNED_BYTE, false, 0, 0);
            gl.bindVertexArray(null);
            this.fit();
        }

        /** One RGBA (uint8) per point. */
        setColors(rgba) {
            const gl = this.gl;
            gl.bindVertexArray(this.vao);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.color);
            gl.bufferData(gl.ARRAY_BUFFER, rgba, gl.DYNAMIC_DRAW);
            gl.enableVertexAttribArray(this.loc.a_color);
            gl.vertexAttribPointer(this.loc.a_color, 4, gl.UNSIGNED_BYTE, true, 0, 0);
            gl.bindVertexArray(null);
            this.redraw();
        }

        /** One byte per point: bit 0 selected, bit 1 in the region. */
        setFlags(flags, hasSelection) {
            const gl = this.gl;
            this.flags = flags;
            this.hasSelection = Boolean(hasSelection);
            gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.flag);
            gl.bufferSubData(gl.ARRAY_BUFFER, 0, flags);
            this.redraw();
        }

        setStyle(style) {
            Object.assign(this.style, style || {});
            this.redraw();
        }

        // -- the camera: data centre and a zoom over the fitted extent --------

        size() {
            const dpr = window.devicePixelRatio || 1;
            const w = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
            const h = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
            if (this.canvas.width !== w || this.canvas.height !== h) {
                this.canvas.width = w;
                this.canvas.height = h;
            }
            return { w, h, dpr };
        }

        /** Data units per CSS pixel at the current zoom, equal on both axes. */
        unitsPerPixel() {
            const [x0, x1, y0, y1] = this.extent;
            const w = Math.max(1, this.canvas.clientWidth);
            const h = Math.max(1, this.canvas.clientHeight);
            const pad = 0.92;
            const span = Math.max((x1 - x0) / (w * pad), (y1 - y0) / (h * pad)) || 1;
            return span / this.view.zoom;
        }

        fit(animate) {
            const [x0, x1, y0, y1] = this.extent;
            const to = { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, zoom: 1 };
            if (animate) this.animateTo(to);
            else { this.view = to; this.redraw(); }
        }

        /** Zoom by `factor` about the centre of the plot, eased. */
        zoomBy(factor) {
            const zoom = Math.min(400, Math.max(0.5, this.view.zoom * factor));
            this.animateTo({ cx: this.view.cx, cy: this.view.cy, zoom });
        }

        animateTo(to, ms) {
            cancelAnimationFrame(this.tween || 0);
            const from = Object.assign({}, this.view);
            const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            const span = reduce ? 0 : (ms || 180);
            const start = performance.now();
            const tick = (now) => {
                const t = span ? Math.min(1, (now - start) / span) : 1;
                const e = 1 - Math.pow(1 - t, 3);
                // The zoom is eased in log space, so a step in feels even.
                this.view = { cx: from.cx + (to.cx - from.cx) * e, cy: from.cy + (to.cy - from.cy) * e,
                              zoom: Math.exp(Math.log(from.zoom) + (Math.log(to.zoom) - Math.log(from.zoom)) * e) };
                this.draw();
                if (t < 1) this.tween = requestAnimationFrame(tick);
            };
            this.tween = requestAnimationFrame(tick);
        }

        /** CSS pixel (relative to the canvas) -> data. y grows up in data. */
        toData(px, py) {
            const u = this.unitsPerPixel();
            const w = this.canvas.clientWidth;
            const h = this.canvas.clientHeight;
            return [this.view.cx + (px - w / 2) * u, this.view.cy - (py - h / 2) * u];
        }

        toScreen(x, y) {
            const u = this.unitsPerPixel();
            return [(x - this.view.cx) / u + this.canvas.clientWidth / 2,
                    (this.view.cy - y) / u + this.canvas.clientHeight / 2];
        }

        zoomAt(px, py, factor) {
            const before = this.toData(px, py);
            this.view.zoom = Math.min(400, Math.max(0.5, this.view.zoom * factor));
            const after = this.toData(px, py);
            this.view.cx += before[0] - after[0];
            this.view.cy += before[1] - after[1];
            this.redraw();
        }

        panBy(dxPx, dyPx) {
            const u = this.unitsPerPixel();
            this.view.cx -= dxPx * u;
            this.view.cy += dyPx * u;
            this.redraw();
        }

        /** A point size that keeps a dense embedding readable: smaller with
         *  more cells, larger as one zooms in. */
        pointSize(dpr) {
            const density = Math.max(0.6, Math.min(this.style.base * 1.6, this.style.base * Math.sqrt(150000 / Math.max(1, this.n))));
            return Math.min(18, density * Math.sqrt(this.view.zoom) * (this.style.scale || 1)) * dpr;
        }

        redraw() {
            if (this.frame) return;
            this.frame = requestAnimationFrame(() => {
                this.frame = 0;
                this.draw();
            });
        }

        draw() {
            const gl = this.gl;
            const { w, h, dpr } = this.size();
            gl.viewport(0, 0, w, h);
            gl.clearColor(0, 0, 0, 0);
            gl.clear(gl.COLOR_BUFFER_BIT);
            if (!this.n) return;
            gl.useProgram(this.program);
            gl.enable(gl.BLEND);
            gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
            const u = this.unitsPerPixel();
            gl.uniform2f(this.loc.u_center, this.view.cx, this.view.cy);
            gl.uniform2f(this.loc.u_scale, 2 / (u * this.canvas.clientWidth), 2 / (u * this.canvas.clientHeight));
            gl.uniform1f(this.loc.u_size, this.pointSize(dpr));
            const s = this.style;
            gl.uniform4f(this.loc.u_sel, s.sel[0] / 255, s.sel[1] / 255, s.sel[2] / 255, s.sel[3] / 255);
            gl.uniform4f(this.loc.u_gold, s.gold[0] / 255, s.gold[1] / 255, s.gold[2] / 255, s.gold[3] / 255);
            gl.uniform1f(this.loc.u_rest, s.rest);
            gl.uniform1i(this.loc.u_selOwn, s.selOwn ? 1 : 0);
            gl.uniform1i(this.loc.u_hasSel, this.hasSelection ? 1 : 0);
            gl.bindVertexArray(this.vao);
            for (const pass of [0, 1]) {
                gl.uniform1i(this.loc.u_pass, pass);
                gl.drawArrays(gl.POINTS, 0, this.n);
            }
            gl.bindVertexArray(null);
            if (this.onView) this.onView();
        }

        /**
         * A bool per point: inside the polygon `screen` ([[x, y], ...] in CSS
         * pixels of the canvas). The polygon is filled into a mask the size of
         * the canvas and every point is looked up in it.
         */
        inside(screen) {
            if (!screen || screen.length < 3) return new Uint8Array(this.n);
            return this.maskTest((g) => {
                g.beginPath();
                g.moveTo(screen[0][0], screen[0][1]);
                for (let i = 1; i < screen.length; i += 1) g.lineTo(screen[i][0], screen[i][1]);
                g.closePath();
                g.fillStyle = "#fff";
                g.fill();
            });
        }

        /** A bool per point: within `radius` CSS pixels of the path `screen`. */
        stroke(screen, radius) {
            if (!screen || !screen.length) return new Uint8Array(this.n);
            return this.maskTest((g) => {
                g.strokeStyle = "#fff";
                g.fillStyle = "#fff";
                g.lineWidth = radius * 2;
                g.lineCap = "round";
                g.lineJoin = "round";
                g.beginPath();
                g.moveTo(screen[0][0], screen[0][1]);
                if (screen.length === 1) {
                    g.arc(screen[0][0], screen[0][1], radius, 0, Math.PI * 2);
                    g.fill();
                    return;
                }
                for (let i = 1; i < screen.length; i += 1) g.lineTo(screen[i][0], screen[i][1]);
                g.stroke();
            });
        }

        /** Paint a shape with `paint(context)` into a canvas-sized mask; a
         *  bool per point under it. Points not drawn (no alpha) never are. */
        maskTest(paint) {
            const w = Math.max(1, Math.round(this.canvas.clientWidth));
            const h = Math.max(1, Math.round(this.canvas.clientHeight));
            const out = new Uint8Array(this.n);
            if (!this.n) return out;
            const mask = typeof OffscreenCanvas === "function" ? new OffscreenCanvas(w, h)
                : Object.assign(document.createElement("canvas"), { width: w, height: h });
            const g = mask.getContext("2d", { willReadFrequently: true });
            paint(g);
            const alpha = g.getImageData(0, 0, w, h).data;
            const u = this.unitsPerPixel();
            const cx = this.view.cx;
            const cy = this.view.cy;
            const hw = this.canvas.clientWidth / 2;
            const hh = this.canvas.clientHeight / 2;
            const xy = this.xy;
            const shown = this.shown;
            for (let i = 0, p = 0; i < this.n; i += 1, p += 2) {
                if (shown && !shown[i]) continue;
                const x = Math.floor((xy[p] - cx) / u + hw);
                const y = Math.floor((cy - xy[p + 1]) / u + hh);
                if (x < 0 || y < 0 || x >= w || y >= h) continue;
                if (alpha[(y * w + x) * 4 + 3] > 127) out[i] = 1;
            }
            return out;
        }

        /** Which points are drawn (null: all); the tools skip the rest. */
        setShown(shown) {
            this.shown = shown || null;
        }

        destroy() {
            const gl = this.gl;
            if (this.frame) cancelAnimationFrame(this.frame);
            Object.values(this.buffers).forEach((b) => gl.deleteBuffer(b));
            gl.deleteVertexArray(this.vao);
            gl.deleteProgram(this.program);
            gl.getExtension("WEBGL_lose_context")?.loseContext();
        }
    }

    return { ScatterGL, create: (canvas) => new ScatterGL(canvas) };
})();
