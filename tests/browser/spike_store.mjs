// Spike S-3: the project store on OPFS (NativeFS mounted at /data). What a
// write back (syncfs) costs as the store grows, whether sqlite works on it,
// and whether both survive a reload. Prints JSON.
//
//   node tests/browser/spike_store.mjs [--py 0.0.36]
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const py = args.includes("--py") ? args[args.indexOf("--py") + 1] : "0.0.36";
const server = await startServer();
const browser = await launch();
const out = { py, writes: [] };
const url = `http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`;
try {
    let page = await browser.context.newPage();
    await page.goto(url);
    await page.evaluate(() => window.harness.ready);
    out.sqlite = await page.evaluate((code) => window.harness.run(code), `
import sqlite3, time, json
t = time.perf_counter()
db = sqlite3.connect("/data/s3.db")
db.execute("create table if not exists cells (id integer primary key, gate real)")
db.executemany("insert into cells (gate) values (?)", [(i * 0.5,) for i in range(20000)])
db.commit(); db.close()
json.dumps({"ms": round((time.perf_counter() - t) * 1000, 1)})`);
    for (const megabytes of [1, 10, 50]) {
        const wrote = await page.evaluate((code) => window.harness.run(code), `
import os, time, json
os.makedirs("/data/blobs", exist_ok=True)
t = time.perf_counter()
with open("/data/blobs/b${megabytes}.bin", "wb") as f:
    f.write(os.urandom(${megabytes} * 2**20))
json.dumps({"write_ms": round((time.perf_counter() - t) * 1000, 1)})`);
        const syncMs = await page.evaluate(() => window.harness.sync());
        const again = await page.evaluate(() => window.harness.sync());
        out.writes.push({ megabytes, ...JSON.parse(wrote), syncMs: Math.round(syncMs),
                          syncUnchangedMs: Math.round(again) });
    }
    await page.close();
    page = await browser.context.newPage();
    await page.goto(url);
    await page.evaluate(() => window.harness.ready);
    out.afterReload = JSON.parse(await page.evaluate((code) => window.harness.run(code), `
import sqlite3, os, json
db = sqlite3.connect("/data/s3.db")
rows = db.execute("select count(*), sum(gate) from cells").fetchone()
db.close()
json.dumps({"rows": rows[0], "sum": rows[1],
            "blobs": sorted((n, os.path.getsize("/data/blobs/" + n)) for n in os.listdir("/data/blobs"))})`));
    out.bootAfterReloadMs = await page.evaluate(() => window.harness.marks.ready);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
