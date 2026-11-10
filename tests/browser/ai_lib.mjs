// What the AI drivers share (tests/browser/ai_chat.mjs, jobs.mjs): the
// stand-in platform and gateway (mock_platform.py), a site built to trust its
// key, a browser licensed through Settings > Account as a user connects one,
// and a project made inside the website's own Python from the test suite's
// fixture modules (the same code, the same seeds, that the CPython half of
// each test runs).
import { execFileSync, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { PYTHON, REPO, appFrame, seedBrowserStore } from "./lib.mjs";

//: `extra`: more of mock_platform.py's options (`--roots ai,volume,plugin:video_studio`).
export function startMock(home, extra = []) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tests", "browser", "mock_platform.py"), "--bind-version", "2",
                                     "--home", home, ...extra], { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) {
                try {
                    const info = JSON.parse(buffer.split("\n")[0]);
                    const api = `http://127.0.0.1:${info.port}`;
                    resolve({ ...info, api, stop: () => child.kill(),
                              requests: async () => (await (await fetch(`${api}/_requests`)).json()).requests,
                              brain: (replies) => fetch(`${api}/_brain`, { method: "POST",
                                                                          headers: { "Content-Type": "application/json" },
                                                                          body: JSON.stringify({ replies }) }) });
                } catch (error) { reject(error); }
            }
        });
        child.on("exit", (code) => reject(new Error(`mock_platform.py exited ${code}`)));
    });
}

//: The static site, built into `site` to call the mock and trust its key.
export function buildSite(site, data, mock) {
    execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site, "--licence-api", `${mock.api}/v1`,
                          "--licence-test-key", `${mock.kid}=${mock.public_key}`],
                 { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: data },
                   stdio: ["ignore", "ignore", "inherit"] });
}

//: Settings > Account > Connect this browser, through the mock: Pro.
export async function connectLicence(page, origin) {
    page.on("popup", (opened) => { opened.close().catch(() => null); });
    await seedBrowserStore(page.context(), origin);
    await page.goto(`${origin}/settings?pydebug#license`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#settings_license_connect", { timeout: 60000 });
    await frame.click("#settings_license_connect");
    await frame.waitForFunction(() => document.getElementById("settings_license_plan")?.textContent.trim() === "Pro",
                                null, { timeout: 60000 });
    return frame;
}

//: The fixture modules the CPython tests use, written into the worker's
//: files and importable there as `tests.*`.
const FIXTURES = ["helpers.py", "agent_fixtures.py", "autogate_fixtures.py", "qc_fixtures.py"];

export async function shipFixtures(page) {
    const files = Object.fromEntries(FIXTURES.map((name) => [name, readFileSync(path.join(REPO, "tests", name), "utf8")]));
    files["__init__.py"] = "";
    const code = [
        "import json, os, sys",
        `files = json.loads(${JSON.stringify(JSON.stringify(files))})`,
        "os.makedirs('/tmp/pxtests/tests', exist_ok=True)",
        "for name, text in files.items():",
        "    with open('/tmp/pxtests/tests/' + name, 'w', encoding='utf-8', newline='\\n') as handle:",
        "        handle.write(text)",
        "sys.path.insert(0, '/tmp/pxtests')",
        "'shipped'",
    ].join("\n");
    return page.evaluate((c) => window.PlexoraPython.run(c), code);
}

//: Python's last expression in the worker, parsed as JSON.
export async function py(page, code) {
    const text = await page.evaluate((c) => window.PlexoraPython.run(c), code);
    return JSON.parse(text);
}
