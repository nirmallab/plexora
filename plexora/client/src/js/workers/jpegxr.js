/**
 * JPEG XR for the website's Python (imagecodecs_shim's `set_jpegxr`): what
 * czifile decodes most Zeiss Axioscan slide scans with, and tifffile a JPEG XR
 * TIFF. jxrlib, Microsoft's reference codec, compiled to WebAssembly
 * (client/external/jpegxr-0.3.0: the decoder Brion Vibber's npm package
 * `jpegxr` 0.3.0 carries inline, taken out as it is).
 *
 * The package's own Emscripten glue is not used: it starts the module only
 * asynchronously, which Python without JSPI cannot wait for, and the module
 * needs little of it -- five imports (heap growth, a large copy, a failed
 * assertion, stdout, a 64-bit result's high half) and the C functions of the
 * package's wasm/wasm_api.c. Nothing here evaluates code: the site's CSP
 * allows WebAssembly ('wasm-unsafe-eval') and not eval or `new Function`.
 *
 * Fetched and compiled synchronously the first time a tile needs it (a
 * worker may), never at boot, so with or without JSPI.
 *
 *   jpegxrDecode(bytes: Uint8Array) -> {width, height, channels, colorFormat,
 *       bitDepth, bitsPerPixel, alpha, bgr, data: Uint8Array}
 *   (`data` is the pixels as jxrlib writes them in the stream's own pixel
 *   format, rows packed; `bitDepth` is jxrlib's name: "8", "16", "16Float",
 *   "32Float", "16Fixed", ...; `bgr` that its colour samples are B, G, R.
 *   An alpha plane is never decoded -- the build's wasm_api.c does not ask
 *   jxrlib for it -- so a stream with alpha has no valid alpha samples.)
 *   jpegxrDecoder(wasm: Uint8Array) -> that function, over a module of its own
 */

export const JPEGXR_WASM = new URL("../../../external/jpegxr-0.3.0/jpegxr.wasm", import.meta.url).href;
//: The build's ceiling (ALLOW_MEMORY_GROWTH; the module's maximum is 32768 pages).
const MAX_HEAP = 2147483648;
const PAGE = 65536;

let decode = null;

export function jpegxrDecode(bytes) {
    if (!decode) decode = jpegxrDecoder(readSync(JPEGXR_WASM));
    try {
        return decode(bytes);
    } catch (error) {
        // A failed assertion leaves jxrlib's state unknown: the next tile
        // gets a fresh module.
        if (error && error.jpegxrAbort) decode = null;
        throw error;
    }
}

function readSync(url) {
    const request = new XMLHttpRequest();
    request.open("GET", url, false);
    request.responseType = "arraybuffer";
    request.send();
    if (request.status !== 200 || !request.response) {
        throw new Error(`the JPEG XR decoder could not be loaded (${url}: ${request.status})`);
    }
    return new Uint8Array(request.response);
}

export function jpegxrDecoder(wasm) {
    let memory = null;
    let heap = null;
    const said = [];
    const bytes = () => (heap && heap.buffer === memory.buffer ? heap : (heap = new Uint8Array(memory.buffer)));
    const text = (pointer) => {
        if (!pointer) return "";
        const all = bytes();
        let end = pointer;
        while (all[end]) end += 1;
        return new TextDecoder().decode(all.subarray(pointer, end));
    };
    // Emscripten's own growth policy (emscripten_resize_heap): a fifth more
    // than now, at most 96 MB past the request, smaller steps if refused.
    const grow = (requested) => {
        const old = memory.buffer.byteLength;
        if (requested > MAX_HEAP) return 0;
        for (let cut = 1; cut <= 4; cut *= 2) {
            const over = Math.min(old * (1 + 0.2 / cut), requested + 100663296);
            const wanted = Math.max(requested, over);
            const size = Math.min(MAX_HEAP, Math.ceil(wanted / PAGE) * PAGE);
            try {
                memory.grow((size - old + PAGE - 1) >>> 16);
                return 1;
            } catch (error) {
                // refused: try a smaller step
            }
        }
        return 0;
    };
    const imports = {
        env: {
            __assert_fail(condition, file, line) {
                const error = new Error(`jxrlib: assertion failed: ${text(condition)} (${text(file)}:${line})`);
                error.jpegxrAbort = true;
                throw error;
            },
            emscripten_memcpy_big(dest, src, num) {
                bytes().copyWithin(dest >>> 0, src >>> 0, (src >>> 0) + (num >>> 0));
            },
            emscripten_resize_heap: (requested) => grow(requested >>> 0),
            setTempRet0() {},
        },
        wasi_snapshot_preview1: {
            // jxrlib's messages (stderr, stdout): kept for the error they explain.
            fd_write(fd, iov, count, written) {
                const view = new DataView(memory.buffer);
                let total = 0;
                for (let i = 0; i < count; i += 1) {
                    const at = view.getUint32(iov + i * 8, true);
                    const length = view.getUint32(iov + i * 8 + 4, true);
                    said.push(new TextDecoder().decode(bytes().subarray(at, at + length)));
                    total += length;
                }
                view.setUint32(written, total, true);
                return 0;
            },
        },
    };
    const c = new WebAssembly.Instance(new WebAssembly.Module(wasm), imports).exports;
    memory = c.memory;
    c.__wasm_call_ctors();

    const failed = (what) => {
        const detail = said.join("").trim().split("\n").slice(-3).join("; ");
        said.length = 0;
        return new Error(`JPEG XR: ${what}${detail ? ` (${detail})` : ""}`);
    };

    return function decodeJpegxr(input) {
        const stack = c.stackSave();
        said.length = 0;
        const source = c.malloc(input.length);
        if (!source) throw failed("out of memory");
        try {
            bytes().set(input, source);
            const image = c.image_decode_with_memory(source, input.length);
            if (!image) throw failed("not a JPEG XR image");
            try {
                const width = c.image_decode_width(image);
                const height = c.image_decode_height(image);
                if (width <= 0 || height <= 0) throw failed("no image size");
                const info = c.image_decode_pixel_info(image);
                if (!info) throw failed("an unknown pixel format");
                let format;
                try {
                    format = {
                        channels: c.pixel_info_channels(info) >>> 0,
                        colorFormat: text(c.pixel_info_color_format(info)),
                        bitDepth: text(c.pixel_info_bit_depth(info)),
                        bitsPerPixel: c.pixel_info_bits_per_pixel(info) >>> 0,
                        alpha: Boolean(c.pixel_info_has_alpha(info)),
                        bgr: Boolean(c.pixel_info_bgr(info)),
                    };
                } finally {
                    c.pixel_info_free(info);
                }
                if (!format.bitsPerPixel || format.bitsPerPixel % 8) {
                    throw failed(`${format.bitsPerPixel}-bit pixels are not supported`);
                }
                const stride = width * (format.bitsPerPixel / 8);
                const size = stride * height;
                const target = c.malloc(size);
                if (!target) throw failed("out of memory");
                try {
                    if (c.image_decode_copy_all(image, target, stride) < 0) throw failed("the image could not be decoded");
                    return { width, height, ...format, data: bytes().slice(target, target + size) };
                } finally {
                    c.free(target);
                }
            } finally {
                c.image_decode_free(image);
            }
        } finally {
            c.free(source);
            c.stackRestore(stack);
        }
    };
}
