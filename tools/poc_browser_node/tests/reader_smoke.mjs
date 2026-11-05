// node tests/reader_smoke.mjs <tiff> [decoder]  -- open, geometry, a few tiles, timings.
import { fileSource } from "./fileSource.mjs";
import { createTiffReader } from "../page/src/lib/tiffReader.js";

const [path, decoder = "native"] = process.argv.slice(2);
const reader = createTiffReader();
const opened = await reader.open({ source: await fileSource(path), decoder });
console.log(JSON.stringify(opened.geometry).slice(0, 600));
console.log("open", JSON.stringify(opened.timing));
const { levels } = opened.geometry;
for (const level of [levels.length - 1, Math.floor(levels.length / 2), 0]) {
    const t = await reader.tile({ channel: 0, level, tx: 0, ty: 0, tw: 1024, th: 1024 });
    console.log("tile L" + level, t.width, t.height, t.format, JSON.stringify(t.timing));
}
