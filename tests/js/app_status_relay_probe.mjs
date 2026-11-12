/**
 * The status indicator (services/appStatus.js) and the website's relay to a
 * data node.
 *
 * On the website a data node is answered at the site's own origin, under
 * `/__plexora/relay/<room>/` (services/webRemote.js), and a dropped
 * connection is answered 502 or 504 there. The indicator speaks for the app,
 * so those failures are a data node's -- reported per resource, as a direct
 * node's are on the desktop -- and never "Server error". Its own routes'
 * failures still are.
 *
 * Run directly:  node tests/js/app_status_relay_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/appStatus.js");
const ROOM = "RoomRoomRoomRoomRoom1";

const failures = [];
function check(name, ok, detail) {
    console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok || detail === undefined ? "" : ` -- ${detail}`}`);
    if (!ok) failures.push(name);
}

// -- a page whose requests answer whatever this test says --------------------

const answers = new Map();
const label = { textContent: "" };
const root = {
    dataset: {}, innerHTML: "", classList: { add: () => {} }, setAttribute: () => {},
    querySelector: () => label,
};
const window = {
    location: { href: "https://viewer.example/", origin: "https://viewer.example" },
    fetch: async (input) => {
        const answer = answers.get(String(input));
        if (answer === "unreachable") throw new TypeError("Failed to fetch");
        return { status: answer ?? 200, ok: (answer ?? 200) < 400 };
    },
    addEventListener: () => {},
    setInterval: () => 0,
};
const document = {
    readyState: "complete", visibilityState: "visible",
    getElementById: (id) => (id === "app_status" ? root : null), addEventListener: () => {},
};
const context = createContext({ window, document, console, URL, setTimeout, clearTimeout, Date });
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });

const shown = () => `${root.dataset.state}: ${label.textContent}`;

async function main() {
    check("the indicator starts live", root.dataset.state === "live", shown());

    const health = `/__plexora/relay/${ROOM}/node/v1/health?t=x`;
    answers.set(health, 502);
    await window.fetch(health);
    check("a relayed node's 502 is not the app's error", root.dataset.state === "live", shown());

    const tile = `https://viewer.example/__plexora/relay/${ROOM}/node/v1/image/r1/tile/0/0_0.png`;
    answers.set(tile, 504);
    await window.fetch(tile);
    check("...nor its 504, asked for by full address", root.dataset.state === "live", shown());

    const based = `/site/__plexora/relay/${ROOM}/node/v1/health?t=x`;
    answers.set(based, "unreachable");
    await window.fetch(based).catch(() => null);
    check("...nor one that never answered, under a site base", root.dataset.state === "live", shown());

    answers.set("/get_channel_names", 502);
    await window.fetch("/get_channel_names");
    check("the app's own 502 still is", root.dataset.state === "error" && label.textContent === "Server error 502",
          shown());

    console.log(failures.length ? `\n${failures.length} failed` : "\nall status checks passed");
    if (failures.length) process.exitCode = 1;
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
