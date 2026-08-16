/**
 * WebGL2 tile-colorization engine.
 *
 * Deliberately OpenSeadragon-independent: this module is only the WebGL
 * rendering engine, so the app can run against upstream OpenSeadragon rather
 * than a fork pinned to a patched commit. The OpenSeadragon integration
 * (tile-loaded/tile-drawing wiring) lives in imageViewer.js rather than a
 * separate glue class, since imageViewer.js already overrides most of that
 * layer's behavior.
 */
export class GLRenderer {
    constructor(incoming) {
        // Custom hook points, callable via `this['gl-drawing']`/`this['gl-loaded']`
        this['gl-drawing'] = function (e) { return e; };
        this['gl-loaded'] = function (e) { return e; };

        // Vertex input buffer describing a full-canvas quad
        this.one_point_size = 2 * Float32Array.BYTES_PER_ELEMENT;
        this.points_list_size = 4 * this.one_point_size;
        this.points_buffer = new Float32Array([
            0, 1, 0, 0, 1, 1, 1, 0,
        ]);

        // Offscreen WebGL2 context and texture/buffer handles
        this.gl = document.createElement('canvas').getContext('webgl2');
        this.texture = this.gl.createTexture();
        this.buffer = this.gl.createBuffer();

        // Placeholder defaults, overwritten by the caller before init()
        this.vShader = 'vShader.glsl';
        this.fShader = 'fShader.glsl';
        this.height = 0;
        this.width = 0;

        for (const key in incoming) {
            this[key] = incoming[key];
        }
    }

    init() {
        const step = [[this.vShader, this.fShader].map(this.getter)];
        step.push(this.toProgram.bind(this), this.toBuffers.bind(this));
        return Promise.all(step[0]).then(step[1]).then(step[2]);
    }

    updateShape(width, height) {
        this.width = width;
        this.height = height;
        this.gl.canvas.width = width;
        this.gl.canvas.height = height;
        this.gl.viewport(0, 0, width, height);
    }

    // Fetch a shader source file (or pass through a non-.glsl value unchanged)
    getter(where) {
        return new Promise((done) => {
            if (where.slice(-4) != 'glsl') {
                return done(where);
            }
            const bid = new XMLHttpRequest();
            const win = function () {
                if (bid.status == 200) {
                    return done(bid.response);
                }
                return done(where);
            };
            bid.open('GET', where, true);
            bid.setRequestHeader('Cache-Control', 'no-cache, no-store, max-age=0');
            bid.onerror = bid.onload = win;
            bid.send();
        });
    }

    // Compile and link the vertex/fragment shader pair
    toProgram(files) {
        const gl = this.gl;
        const program = gl.createProgram();
        const ok = function (kind, status, value, sh) {
            if (!gl['get' + kind + 'Parameter'](value, gl[status + '_STATUS'])) {
                console.log((sh || 'LINK') + ':\n' + gl['get' + kind + 'InfoLog'](value));
            }
            return value;
        };
        files.map((given, i) => {
            const sh = ['VERTEX_SHADER', 'FRAGMENT_SHADER'][i];
            const shader = gl.createShader(gl[sh]);
            gl.shaderSource(shader, given);
            gl.compileShader(shader);
            gl.attachShader(program, shader);
            ok('Shader', 'COMPILE', shader, sh);
        });
        gl.linkProgram(program);
        return ok('Program', 'LINK', program);
    }

    // Bind the quad buffer, texture parameters, and uniforms
    toBuffers(program) {
        const gl = this.gl;
        gl.useProgram(program);
        this['gl-loaded'].call(this, program);

        const u_tile = gl.getUniformLocation(program, 'u_tile');
        const a_uv = gl.getAttribLocation(program, 'a_uv');
        const u8 = gl.getUniformLocation(program, 'u8');

        gl.uniform1ui(u8, 255);
        gl.uniform1i(u_tile, 0);

        gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
        gl.bufferData(gl.ARRAY_BUFFER, this.points_buffer, gl.STATIC_DRAW);

        gl.enableVertexAttribArray(a_uv);
        gl.vertexAttribPointer(a_uv, 2, gl.FLOAT, 0, this.one_point_size, 0 * this.points_list_size);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.texture);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);

        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    }

    // Default single-texture draw path. imageViewer.js overrides this with
    // its own multi-texture-cache implementation; kept here for fidelity/reuse.
    loadArray(width, height, pixels, format = 'u16') {
        this['gl-drawing'].call(this);
        const gl = this.gl;

        if (format == 'u16') {
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG8UI, width, height, 0, gl.RG_INTEGER, gl.UNSIGNED_BYTE, pixels);
        } else if (format == 'u32') {
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8UI, width, height, 0, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE, pixels);
        } else if (format == 'u8') {
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8UI, width, height, 0, gl.RED_INTEGER, gl.UNSIGNED_BYTE, pixels);
        }

        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        return this.gl.canvas;
    }
}
