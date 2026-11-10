// Google Cloud on the website without the Google Cloud CLI (plan L9), in a
// real Chrome against a stand-in Google (tests/browser/mock_google.py) --
// never Google's real APIs, and no cloud resource is made:
//   1. Settings > Remote servers (the SSH notice on the website) has a Google
//      Cloud button, and it opens the form, not a hand-off notice.
//   2. Sign in with Google: a real click opens the sign-in popup at the
//      stand-in's authorization endpoint, which returns to the site's
//      google-auth.html with a token; the form says who is signed in, as the
//      worker's Python read it from the userinfo endpoint with that token.
//   3. The project list and the project's buckets fill the form's menus (the
//      worker's Python, through the page's fetch, with the token per request).
//   4. Create & Connect: the profile is saved, a node VM is created (the
//      firewall rules first), its startup followed through the Compute API
//      (booting, installing, ready), and the dialog hands over a link to the
//      node's own /app/connect with the node's token in the fragment and the
//      certificate fingerprint beside it.
//   5. The VM verbs from the page: list instances and a bucket's folders,
//      stop, resize while stopped, start; then the Google Cloud button again
//      opens the saved connection's VM and hands over the same link.
//   6. The token was never stored: not in localStorage, sessionStorage,
//      IndexedDB, cookies, the saved profile or the worker's files; and every
//      API call the stand-in saw carried it as a bearer token.
//   7. Google refusing the token (the hour is over) is a sign-in, not an error.
//
//   PLEXORA_BENCH_HOME=<bundle home> node tests/browser/gcloud.mjs [--headed] [--shots DIR]
// Prints JSON with `ok` and `checks`; never a token.
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, seedBrowserStore, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const headed = args.includes("--headed");
const shots = args.includes("--shots") ? args[args.indexOf("--shots") + 1] : null;
const CLIENT_ID = "test-client.apps.googleusercontent.com";

function startMock() {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tests", "browser", "mock_google.py"),
                                     "--client-id", CLIENT_ID, "--ready-after", "2"],
                            { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) {
                try { resolve({ ...JSON.parse(buffer.split("\n")[0]), stop: () => child.kill() }); }
                catch (error) { reject(error); }
            }
        });
        child.on("exit", (code) => reject(new Error(`mock_google.py exited ${code}`)));
    });
}

const out = { checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};
const shoot = async (page, name) => {
    if (!shots) return;
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(shots, `gcloud-${name}.png`) });
};

const mock = await startMock();
const google = `http://127.0.0.1:${mock.port}`;
const mockJson = async (route, init) => (await fetch(`${google}${route}`, init)).json();
const site = mkdtempSync(path.join(tmpdir(), "pxgcp-site-"));
const data = mkdtempSync(path.join(tmpdir(), "pxgcp-data-"));
execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site,
                      "--google-client-id", CLIENT_ID, "--google-auth", `${google}/o/oauth2/v2/auth`],
             { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: data },
               stdio: ["ignore", "ignore", "inherit"] });
const server = await startServer(["--site", site]);
const browser = await launch({ headed });
const origin = `http://127.0.0.1:${server.port}`;

const DIALOG = "dialog.connect-modal[open]";
//: The trigger of the dialog's dropdown whose label is `label`.
const fieldTrigger = (label) => `${DIALOG} .connect-field:has(.connect-field-label:text-is("${label}")) .connect-select-button`;
const optionsOf = (frame, label) => frame.evaluate(([dialog, wanted]) => {
    const field = [...document.querySelectorAll(`${dialog} .connect-field`)]
        .find((one) => one.querySelector(".connect-field-label")?.textContent === wanted);
    return field ? [...field.querySelectorAll(".connect-select-option")].map((row) => row.textContent) : null;
}, [DIALOG, label]);
const identityText = (frame) => frame.evaluate((dialog) =>
    document.querySelector(`${dialog} .connect-gcloud-identity`)?.textContent || "", DIALOG);

try {
    const page = await browser.context.newPage();
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    const popups = [];
    const popupSaid = [];
    browser.context.on("page", (opened) => {
        if (opened === page) return;
        popups.push(opened);
        opened.on("console", (message) => popupSaid.push(message.text().slice(0, 200)));
    });
    await seedBrowserStore(browser.context, origin);
    await page.goto(`${origin}/settings?pydebug#remotes`);
    const frame = await appFrame(page);
    // The worker's Python calls the stand-in instead of googleapis.com.
    await page.evaluate((base) => window.PlexoraPython.run(
        `import os\nos.environ["PLEXORA_GOOGLE_API"] = ${JSON.stringify(base)}\n"ok"`), google);
    await frame.evaluate(() => {
        window.PlexoraConnectNodePollMs = 300;
        try { localStorage.setItem("plexora.gcloud.project", "lab-imaging"); } catch (e) { /* none */ }
    });

    // -- 1. the form, not the notice --------------------------------------------
    // Settings > Remote servers is the SSH notice on the website; its Google
    // Cloud button is the way in (no saved connection yet: the form).
    await frame.click('[data-handoff-action="google_cloud"]', { timeout: 60000 });
    await frame.waitForSelector(`${DIALOG} .connect-gcloud-identity button:text-is("Sign in with Google")`,
                                { timeout: 60000 });
    const first = await frame.evaluate((dialog) => ({
        handoff: Boolean(document.querySelector(`${dialog}.is-handoff`)),
        subtitle: document.querySelector(`${dialog} .connect-modal-subtitle`)?.textContent || "",
    }), DIALOG);
    check("the Google Cloud tab is the form on the website, signing in in the browser",
          !first.handoff && /signs in to Google in this browser/.test(first.subtitle),
          { subtitle: first.subtitle });
    await shoot(page, "1-sign-in");

    // -- 2. sign in ----------------------------------------------------------------
    await frame.click(`${DIALOG} .connect-gcloud-identity button:text-is("Sign in with Google")`);
    await frame.waitForFunction((dialog) => /Signed in as/.test(
        document.querySelector(`${dialog} .connect-gcloud-identity`)?.textContent || ""), DIALOG,
                                { timeout: 30000 });
    const state = await mockJson("/_state");
    const asked = state.auth[0] || {};
    const scopes = String(asked.scope || "").split(" ");
    check("the sign-in popup asked the authorization endpoint for a token with the narrow scopes, "
          + "and came back to the site's own page",
          state.auth.length === 1 && asked.client_id === CLIENT_ID && asked.response_type === "token"
          && asked.redirect_uri === `${origin}/google-auth.html`
          && scopes.includes("https://www.googleapis.com/auth/compute")
          && scopes.includes("https://www.googleapis.com/auth/devstorage.read_only")
          && scopes.includes("https://www.googleapis.com/auth/cloudplatformprojects.readonly")
          && !scopes.some((scope) => scope.endsWith("/auth/cloud-platform")),
          { scopes, redirect: asked.redirect_uri === `${origin}/google-auth.html` });
    const who = await identityText(frame);
    check("the form names the account the worker's Python read with the token",
          /Signed in as researcher@example\.org/.test(who), { identity: who });
    if (popups.length === 1 && !popups[0].isClosed()) {
        await popups[0].waitForEvent("close", { timeout: 5000 }).catch(() => null);
    }
    const popupClosed = popups.length === 1 && popups[0].isClosed();
    const popupText = popupClosed ? "" : await popups[0].evaluate(
        () => document.body.textContent + " | " + location.href).catch(() => "");
    check("the sign-in window closed itself",
          popupClosed, { popups: popups.length, said: popupSaid, text: popupText.slice(0, 200) });

    // -- 3. projects and buckets --------------------------------------------------
    await frame.waitForFunction(([dialog]) => {
        const rows = [...document.querySelectorAll(`${dialog} .connect-select-option`)].map((r) => r.textContent);
        return rows.some((text) => /^tonsil-data/.test(text));
    }, [DIALOG], { timeout: 30000 });
    const projects = await optionsOf(frame, "Google Cloud project");
    check("the project menu lists the account's projects",
          JSON.stringify(projects) === JSON.stringify(["Lab imaging (lab-imaging)", "Teaching (teaching-2026)"]),
          { projects });
    await frame.click(`${DIALOG} button:text-is("Next")`);
    const buckets = await optionsOf(frame, "Cloud Storage bucket");
    check("the bucket menu lists the project's buckets with their locations",
          (buckets || []).includes("tonsil-data — US-EAST1") && (buckets || []).includes("atlas-public — US"),
          { buckets });
    await frame.click(fieldTrigger("Cloud Storage bucket"));
    await frame.click(`${DIALOG} .connect-select-option:text-is("tonsil-data — US-EAST1")`);
    await frame.waitForFunction((dialog) => {
        const next = [...document.querySelectorAll(`${dialog} .connect-modal-actions button`)]
            .find((b) => b.textContent === "Next" && !b.hidden);
        return next && !next.disabled;
    }, DIALOG, { timeout: 20000 });
    await shoot(page, "2-bucket");
    await frame.click(`${DIALOG} button:text-is("Next")`);
    await frame.waitForFunction((dialog) => {
        const text = document.querySelector(`${dialog} .connect-wizard-page:not([hidden])`)?.textContent || "";
        return /us-east1-b/.test(text);
    }, DIALOG, { timeout: 20000 }).catch(() => null);
    await frame.click(`${DIALOG} button:text-is("Next")`);
    await shoot(page, "3-exit");

    // -- 4. create, follow, hand over ---------------------------------------------
    await frame.click(`${DIALOG} button:text-is("Create & Connect")`);
    await frame.waitForSelector(`${DIALOG} a.connect-node-open`, { timeout: 90000 });
    await shoot(page, "4-ready");
    const ready = await frame.evaluate((dialog) => ({
        href: document.querySelector(`${dialog} a.connect-node-open`).href,
        target: document.querySelector(`${dialog} a.connect-node-open`).target,
        rel: document.querySelector(`${dialog} a.connect-node-open`).rel,
        fingerprint: document.querySelector(`${dialog} .connect-node-fingerprint`).textContent,
        line: document.querySelector(`${dialog} .connect-node-state`).textContent,
    }), DIALOG);
    const made = (await mockJson("/_state")).instances;
    const vm = made["plexora-gcloud"] || Object.values(made)[0] || {};
    const meta = Object.fromEntries(((vm.metadata || {}).items || []).map((i) => [i.key, i.value]));
    const link = new URL(ready.href);
    const fragment = new URLSearchParams(link.hash.slice(1));
    const firewalls = Object.keys((await mockJson("/_state")).firewalls);
    check("Create & Connect made a node VM (firewall rules first) and handed over a link to its own app",
          link.origin === "https://203.0.113.10:8443" && link.pathname === "/app/connect"
          && fragment.get("v") === "1" && fragment.get("e") === "https://203.0.113.10:8443"
          && fragment.get("t") === meta["plexora-node-token"] && /^[0-9a-f]{32}$/.test(fragment.get("t") || "")
          && ready.target === "_blank" && /noopener/.test(ready.rel) && /noreferrer/.test(ready.rel)
          && firewalls.includes("plexora-deny-public-ingress") && firewalls.includes("plexora-allow-node-https")
          && (vm.labels || {})["created-by"] === "plexora" && meta["plexora-bucket"] === "tonsil-data"
          && /--tls self-signed/.test(meta["startup-script"] || ""),
          { origin: link.origin, path: link.pathname, vm: vm.name, firewalls });
    check("the certificate fingerprint the VM published is shown beside the link",
          ready.fingerprint === Array(32).fill("AB").join(":"), { fingerprint: ready.fingerprint });

    // -- 5. the VM verbs from the page ---------------------------------------------
    const verbs = await frame.evaluate(async (name) => {
        const json = async (path, init) => {
            const answer = await fetch(path, init);
            return { status: answer.status, body: await answer.json() };
        };
        const instances = await json("/settings/gcloud/instances?project=lab-imaging");
        const folders = await json("/settings/gcloud/objects?project=lab-imaging&bucket=tonsil-data&prefix=images/");
        const stopped = await json(`/settings/remotes/${name}/vm/stop`, { method: "POST" });
        const resized = await json(`/settings/remotes/${name}/vm/resize`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ machine_type: "e2-highmem-8" }) });
        const started = await json(`/settings/remotes/${name}/vm/start`, { method: "POST" });
        const status = await json(`/settings/remotes/${name}/vm`);
        return { instances: instances.body.instances, folders: folders.body.folders,
                 stopped: stopped.status, resized: resized.status, started: started.status,
                 status: status.body.status, machine: status.body.machine_type };
    }, vm.name ? vm.name.replace(/^plexora-/, "") : "gcloud");
    const after = (await mockJson("/_state")).instances[vm.name] || {};
    check("the page lists instances and a bucket's folders, and stops, resizes and starts the VM",
          verbs.instances.length === 1 && verbs.instances[0].name === vm.name
          && JSON.stringify(verbs.folders) === JSON.stringify(["images/masks/"])
          && verbs.stopped === 200 && verbs.resized === 200 && verbs.started === 200
          && verbs.status === "RUNNING" && verbs.machine === "e2-highmem-8"
          && /machineTypes\/e2-highmem-8$/.test(after.machineType || ""), verbs);

    // -- 5b. the saved connection, opened again from Settings ----------------------
    await frame.click(`${DIALOG} .connect-modal-actions button:text-is("Close")`);
    await frame.waitForFunction(() => !document.querySelector("dialog.connect-modal[open]"));
    await frame.click('[data-handoff-action="google_cloud"]');
    await frame.waitForSelector(`${DIALOG} a.connect-node-open`, { timeout: 60000 });
    const again = await frame.evaluate((dialog) => ({
        title: document.querySelector(`${dialog} .connect-modal-title`).textContent,
        href: document.querySelector(`${dialog} a.connect-node-open`).href,
    }), DIALOG);
    check("Google Cloud in Settings opens the saved connection's VM and hands over again",
          again.title === "Opening “gcloud” on Google Cloud" && again.href === ready.href,
          { title: again.title });

    // -- 6. the token was never kept --------------------------------------------------
    const requests = await mockJson("/_requests");
    const kept = await frame.evaluate(async () => {
        const dbs = indexedDB.databases ? await indexedDB.databases() : [];
        const local = JSON.stringify({ ...localStorage });
        const session = JSON.stringify({ ...sessionStorage });
        return { local, session, cookies: document.cookie, dbs: dbs.map((d) => d.name) };
    });
    const token = await page.evaluate(() => window.PlexoraGoogleAuth.token());
    const files = await page.evaluate((secret) => window.PlexoraPython.run(
        `import os\nhits = []\nfor root, _d, names in os.walk("/data"):\n    for n in names:\n        p = os.path.join(root, n)\n        try:\n            if ${JSON.stringify(secret)}.encode() in open(p, "rb").read():\n                hits.append(p)\n        except OSError:\n            pass\nimport json\njson.dumps(hits)`), token);
    const idb = await frame.evaluate(async (secret) => {
        const found = [];
        const dbs = indexedDB.databases ? await indexedDB.databases() : [];
        for (const { name } of dbs) {
            const db = await new Promise((resolve) => {
                const request = indexedDB.open(name);
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => resolve(null);
            });
            if (!db) continue;
            for (const store of db.objectStoreNames) {
                const values = await new Promise((resolve) => {
                    const tx = db.transaction(store, "readonly");
                    const all = tx.objectStore(store).getAll();
                    all.onsuccess = () => resolve(all.result);
                    all.onerror = () => resolve([]);
                });
                const text = JSON.stringify(values, (k, v) => (v instanceof ArrayBuffer || ArrayBuffer.isView(v))
                    ? new TextDecoder().decode(v) : v);
                if (text.includes(secret)) found.push(`${name}/${store}`);
            }
            db.close();
        }
        return found;
    }, token);
    check("the token stayed in the tab's memory: no storage, cookie, saved profile or file holds it",
          token.length > 10 && !kept.local.includes(token) && !kept.session.includes(token)
          && !kept.cookies.includes(token) && JSON.parse(files).length === 0 && idb.length === 0,
          { files: JSON.parse(files), idb, cookies: kept.cookies.length });
    check("every API call the stand-in saw carried the token as a bearer token",
          requests.length > 10 && requests.every((one) => one.bearer), { calls: requests.length });

    // -- 7. the hour is over ------------------------------------------------------------
    await fetch(`${google}/_expire`, { method: "POST" });
    const lapsed = await frame.evaluate(async () => {
        const answer = await fetch("/settings/gcloud/projects");
        return { status: answer.status, body: await answer.json() };
    });
    check("a token Google no longer accepts is a sign-in for the form, not an error",
          lapsed.status === 400 && lapsed.body.reauth === true, { status: lapsed.status });
} catch (error) {
    out.errors.push(String(error && error.stack || error).slice(0, 1500));
} finally {
    await browser.close();
    server.stop();
    mock.stop();
}
out.ok = out.errors.length === 0 && out.checks.length === 13 && out.checks.every((one) => one.ok);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
