#version 300 es
// The cell layer, one label tile, one layer: the GPU twin of renderLabelTile
// (views/labelTile.js), which stays the reference. Every rule below mirrors a
// line there; when one changes, both change, and tests/test_label_gpu_parity.py
// renders both and compares.
//
// Drawn 1:1 into a viewport exactly the tile's size, so gl_FragCoord IS the
// label pixel: x across, and y counted from the bottom (the tile was uploaded
// without a flip, so its first row is image row 0 and the flip is here).
//
// Nothing is blended and nothing is discarded: a pixel the CPU path skips is
// written as transparent black, so the quad overwrites the viewport and no
// previous tile can show through.
precision highp float;
precision highp int;
precision highp usampler2D;

uniform usampler2D u_tile;   // RGBA8UI: the label id in its four bytes, low first
uniform usampler2D u_gate;   // R8UI, id-indexed: 1 = the gate passes this cell
uniform usampler2D u_lut;    // RGBA8UI, id-indexed: the layer's colour per cell
uniform usampler2D u_alpha;  // R8UI 256 x 2: row 0 tint[a], row 1 edge[a]

uniform ivec2 u_size;        // the tile, in label pixels
uniform int u_table_width;   // texels per row of u_gate and u_lut
uniform int u_gate_present;  // 0: no gate, every cell passes
uniform uint u_gate_max;     // highest id u_gate describes
uniform int u_lut_present;   // 0: plain white at 220
uniform uint u_lut_max;      // highest id u_lut describes
uniform int u_derive;        // 1: boundaries are derived here (labels stored whole)
uniform int u_fill_on;       // 1: this tile's small-cell fill weight is non-zero

out vec4 color;

uint idAt(ivec2 p) {
    uvec4 b = texelFetch(u_tile, p, 0);
    return b.r | (b.g << 8u) | (b.b << 16u) | (b.a << 24u);
}

ivec2 tableTexel(uint id) {
    int i = int(id);
    return ivec2(i % u_table_width, i / u_table_width);
}

// Eight-neighbour, on raw ids; the tile's own border counts as "same"
// (labelTile.js isBoundary, and the comment at its call site).
bool isBoundary(ivec2 p, uint id) {
    for (int dy = -1; dy <= 1; dy++) {
        for (int dx = -1; dx <= 1; dx++) {
            if (dx == 0 && dy == 0) continue;
            ivec2 q = p + ivec2(dx, dy);
            if (q.x < 0 || q.y < 0 || q.x >= u_size.x || q.y >= u_size.y) continue;
            if (idAt(q) != id) return true;
        }
    }
    return false;
}

void main() {
    ivec2 p = ivec2(int(gl_FragCoord.x), u_size.y - 1 - int(gl_FragCoord.y));
    color = vec4(0.0);
    uint id = idAt(p);
    if (id == 0u) return;
    if (u_gate_present == 1) {
        if (id > u_gate_max) return;
        if (texelFetch(u_gate, tableTexel(id), 0).r == 0u) return;
    }
    uvec4 c = uvec4(255u, 255u, 255u, 220u);
    if (u_lut_present == 1) {
        if (id > u_lut_max) return;
        c = texelFetch(u_lut, tableTexel(id), 0);
        if (c.a == 0u) return;
    }
    uint a = c.a;
    if (u_derive == 1) {
        // The alpha arithmetic (rounding included) was done on the CPU, in
        // the same expressions renderLabelTile uses; this only looks it up.
        uint tint = u_fill_on == 1 ? texelFetch(u_alpha, ivec2(int(a), 0), 0).r : 0u;
        if (!isBoundary(p, id)) {
            if (tint == 0u) return;
            a = tint;
        } else if (u_fill_on == 1) {
            a = texelFetch(u_alpha, ivec2(int(a), 1), 0).r;
        }
        if (a == 0u) return;
    }
    // The context is premultiplied (the WebGL default), and so is this.
    float alpha = float(a) / 255.0;
    color = vec4(vec3(c.rgb) / 255.0 * alpha, alpha);
}
