/**
 * The website's licence client (services/licenseClient.js), run in node
 * against a stand-in platform that signs real Ed25519 certificates with a
 * throwaway key (the shapes of biocognia.testing.FakeService and of the
 * platform's /v1 routes).
 *
 * What it holds the client to is biocognia/state.py's behaviour, ported: the
 * device flow and the key, the binding (both forms), expiry and grace, the
 * clock's high-water mark, the heartbeat's schedule and failing open, renewed
 * and revoked answers, two tabs sharing one licence under one lock, Forget,
 * the AI token kept in memory, the routes' shapes, and https only.
 *
 * Run directly:  node tests/js/license_client_probe.mjs
 */

import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const L = await import(pathToFileURL(join(REPO, "plexora/client/src/js/services/licenseClient.js")).href);
const V = await import(pathToFileURL(join(REPO, "plexora/client/src/js/services/licenseVerify.js")).href);

async function check(label, fn) {
    await fn();
    console.log("  ok  " + label);
}

const DAY = 86400;
const API = "https://api.biocognia.test/v1";
const SITE = "https://viewer.plexoraapp.test";

// -- a stand-in platform ---------------------------------------------------------

async function keyPair() {
    const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    const raw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
    return { pair, publicKey: V.base64url(raw) };
}

const ISSUER = await keyPair();
const ROGUE = await keyPair();

async function sign(payload, key = ISSUER) {
    const body = new TextEncoder().encode(V.canonicalJson(payload));
    const signature = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, key.pair.privateKey, body));
    return `BIOC1.${payload.kid}.${V.base64url(body)}.${V.base64url(signature)}`;
}

function platform(clock, { bindVersion = 1, pendingPolls = 1 } = {}) {
    const requests = [];
    const scripts = new Map();
    let started = null;
    let down = false;
    let pending = pendingPolls;
    const self = {
        requests, bindVersion,
        of: (path) => requests.filter((r) => r.path === path),
        script: (path, status, body) => {
            if (!scripts.has(path)) scripts.set(path, []);
            scripts.get(path).push([status, body]);
        },
        down: (value) => { down = value; },
        async certificate(env, overrides = {}, key = ISSUER) {
            const t = Math.floor(clock());
            let bind = env.binding;
            const extra = {};
            if (self.bindVersion === 2) {
                bind = await V.commitmentOf(bind);
                extra.bv = 2;
            }
            return sign({
                v: 1, aud: "plexora", kid: "bct", cid: "crt_web0001", typ: "env", org: "org_web0001",
                usr: "usr_web0001", seat: "seat_web0001", lic: "lic_web0001", env: "env_web0001",
                envt: env.kind, bind, dlg: null, plan: "plexora.pro", trial: false, use_class: "academic",
                ent: ["ai", "volume"], lim: {}, iat: t, exp: t + 90 * DAY, lic_exp: t + 365 * DAY,
                grace_days: 14, offline_until: null, ...extra, ...overrides,
            }, key);
        },
        async happy(path, body) {
            const t = Math.floor(clock());
            if (path === "/activate/start") {
                started = body.environment;
                return [200, { code: "BIOC-TEST-0001", verify_url: "https://account.biocognia.test/activate",
                               expires_in: 900, interval: 1 }];
            }
            if (path === "/activate/poll") {
                if (pending > 0) {
                    pending -= 1;
                    return [428, { error: { code: "activation_pending", message: "waiting", retry_after: 0 } }];
                }
                return [200, { certificates: { plexora: await self.certificate(started) }, server_time: t,
                               environment: { id: "env_web0001", name: started.display_name, type: started.kind } }];
            }
            if (path === "/activate") {
                return [200, { certificates: { plexora: await self.certificate(body.environment) }, server_time: t,
                               environment: { id: "env_web0001", name: body.environment.display_name,
                                              type: body.environment.kind } }];
            }
            if (path === "/refresh") return [200, { status: "ok", server_time: t }];
            if (path === "/deactivate") return [200, { status: "released", server_time: t }];
            if (path === "/account") {
                return [200, { email: "ada@lab.example.org", name: "Ada Lovelace",
                               org: { name: "Example Lab", kind: "institution" }, server_time: t }];
            }
            if (path === "/ai/token") {
                return [200, { token: `BIOCAI1.test.${requests.length}`, expires_at: t + 1800, server_time: t,
                               gateway: "https://ai.biocognia.test", mode: "credits", mods: ["chat"] }];
            }
            return [404, { error: { code: "not_found", message: "not scripted" } }];
        },
        fetch: async (url, init = {}) => {
            const path = String(url).replace(API, "");
            const body = init.body ? JSON.parse(init.body) : null;
            requests.push({ path, body, headers: init.headers, url: String(url) });
            if (down) throw new TypeError("Failed to fetch");
            const queue = scripts.get(path) || [];
            const [status, payload] = queue.length ? queue.shift() : await self.happy(path, body);
            return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
        },
    };
    return self;
}

// -- the page's surroundings -------------------------------------------------------

//: navigator.locks for one name, as the spec has it: a request is held or
//: queued from the moment it is made, and `ifAvailable` is refused while any is.
function fakeLocks() {
    let chain = Promise.resolve();
    let outstanding = 0;
    return {
        request(name, options, fn) {
            if (typeof options === "function") { fn = options; options = {}; }
            if (options.ifAvailable && outstanding > 0) return Promise.resolve().then(() => fn(null));
            outstanding += 1;
            const run = chain.then(() => fn({ name })).finally(() => { outstanding -= 1; });
            chain = run.catch(() => null);
            return run;
        },
    };
}

function hub() {
    const members = new Set();
    return () => {
        const channel = {
            onmessage: null,
            postMessage(data) {
                for (const other of members) {
                    if (other !== channel && other.onmessage) setTimeout(() => other.onmessage({ data }), 0);
                }
            },
        };
        members.add(channel);
        return channel;
    };
}

function tab({ clock, service, store = L.memoryStore(), locks = fakeLocks(), channel = null, api = API,
               keys = { bct: ISSUER.publicKey } } = {}) {
    const adopted = [];
    const client = L.createLicenseClient({
        store, fetch: service.fetch, api, version: "0.0.36", keys, clock, locks, channel,
        label: { name: "Chrome on Windows", platform: "web-windows" },
        adopt: async (files) => {
            adopted.push(JSON.parse(JSON.stringify(files)));
            const held = client.state();
            return { plan: held && held.licensed ? "pro" : "free", state: held ? held.state : "free",
                     paid: Boolean(held && held.licensed), entitlements: held ? held.entitlements : [] };
        },
    });
    return { client, adopted, store };
}

const request = (method, path, body) => new Request(`${SITE}${path}`, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
});

async function ask(client, method, path, body) {
    const response = await client.answer(request(method, path, method === "GET" ? undefined : (body || {})));
    return response ? { status: response.status, body: await response.json() } : null;
}

async function until(test, ms = 3000) {
    const end = Date.now() + ms;
    while (!test()) {
        if (Date.now() > end) return false;
        await new Promise((resolve) => setTimeout(resolve, 10));
    }
    return true;
}

async function connected(options = {}) {
    let t = options.at || 1_800_000_000;
    const clock = () => t;
    const service = platform(clock, options);
    const page = tab({ clock, service, ...(options.tab || {}) });
    await page.client.restore({ heartbeatEveryMs: 0 });
    await ask(page.client, "POST", "/settings/license/connect", { name: "Lab browser" });
    let reply;
    do { reply = await ask(page.client, "POST", "/settings/license/connect/poll", { code: "BIOC-TEST-0001" }); }
    while (reply.body.pending);
    return { ...page, service, clock, set: (value) => { t = value; }, get t() { return t; } };
}

// -- the checks ------------------------------------------------------------------------

await check("the device flow saves the certificate, writes the worker's files, and keeps no code, key or email", async () => {
    let t = 1_800_000_000;
    const clock = () => t;
    const service = platform(clock);
    const { client, adopted, store } = tab({ clock, service });
    await client.restore({ heartbeatEveryMs: 0 });
    assert.equal(adopted.length, 0, "the free tier writes nothing and asks nothing");
    assert.equal(service.requests.length, 0);
    const started = await ask(client, "POST", "/settings/license/connect", { name: "Lab browser" });
    assert.equal(started.status, 200);
    assert.equal(started.body.code, "BIOC-TEST-0001");
    assert.equal(started.body.verify_url, "https://account.biocognia.test/activate");
    const env = service.of("/activate/start")[0].body.environment;
    assert.equal(env.kind, "browser");
    assert.equal(env.display_name, "Lab browser");
    assert.match(env.binding, /^[0-9a-f]{64}$/);
    assert.equal(env.delegation_pubkey, null);
    assert.equal(service.requests[0].headers["BioCognia-Client"], "plexora/0.0.36");
    const first = await ask(client, "POST", "/settings/license/connect/poll", { code: "biocx-test" });
    assert.equal(first.status, 400, "not a connection code");
    const waiting = await ask(client, "POST", "/settings/license/connect/poll", { code: "bioc-test-0001" });
    assert.deepEqual([waiting.status, waiting.body.pending], [200, true]);
    const done = await ask(client, "POST", "/settings/license/connect/poll", { code: "BIOC-TEST-0001" });
    assert.deepEqual([done.status, done.body.pending, done.body.license.plan], [200, false, "pro"]);
    assert.equal(service.of("/activate/poll")[1].body.binding, env.binding);
    const held = client.state();
    assert.deepEqual([held.state, held.entitlements, held.environment.type], ["active", ["ai", "volume"], "browser"]);
    const files = adopted[adopted.length - 1];
    assert.ok(files.license.certificate.startsWith("BIOC1.bct."));
    assert.equal(await V.bindingOf(files.environment.secret), env.binding);
    assert.equal(files.license.credential_hint, null);
    const kept = JSON.stringify(store.dump());
    assert.deepEqual(Object.keys(store.dump()).sort(), ["environment", "licence"]);
    for (const secret of ["BIOC-TEST-0001", "ada@lab.example.org", "BIOCAI1"]) assert.ok(!kept.includes(secret), secret);
});

await check("a licence key activates, and the key itself is never kept", async () => {
    let t = 1_800_000_000;
    const clock = () => t;
    const service = platform(clock);
    const { client, store } = tab({ clock, service });
    await client.restore({ heartbeatEveryMs: 0 });
    const empty = await ask(client, "POST", "/settings/license/activate", { credential: "  " });
    assert.equal(empty.status, 400);
    const key = "BIOC-ABCD-EFGH-JKLM-NPQR";
    const done = await ask(client, "POST", "/settings/license/activate", { credential: key, name: "" });
    assert.equal(done.status, 200);
    assert.equal(service.of("/activate")[0].body.credential, key);
    assert.equal(service.of("/activate")[0].body.environment.display_name, "Chrome on Windows");
    assert.equal(client.state().state, "active");
    assert.ok(!JSON.stringify(store.dump()).includes(key));
    service.script("/activate", 409, { error: { code: "device_limit", message: "full" } });
    const full = await ask(client, "POST", "/settings/license/activate", { credential: key });
    assert.equal(full.status, 409);
    assert.equal(full.body.code, "device_limit");
    assert.match(full.body.error, /^Activation was refused: This seat already has as many devices/);
});

await check("a bv 2 certificate binds by its commitment, and another browser's certificate is invalid", async () => {
    const page = await connected({ bindVersion: 2 });
    assert.equal(page.client.state().state, "active");
    assert.equal(V.parse(page.client.files().license.certificate).payload.bv, 2);
    const stolen = L.memoryStore({ licence: page.store.dump().licence,
                                   environment: { schema: 1, secret: "x".repeat(43), created_at: 1 } });
    const other = tab({ clock: page.clock, service: page.service, store: stolen });
    await other.client.restore({ heartbeatEveryMs: 0 });
    assert.deepEqual([other.client.state().state, other.client.state().reason], ["invalid", "environment_mismatch"]);
    const untrusted = tab({ clock: page.clock, service: page.service, store: L.memoryStore(page.store.dump()), keys: {} });
    await untrusted.client.restore({ heartbeatEveryMs: 0 });
    assert.deepEqual([untrusted.client.state().state, untrusted.client.state().reason], ["invalid", "unknown_key"]);
    // A test key never replaces a production kid.
    assert.deepEqual(L.testKeys({ bc1: ISSUER.publicKey, bct2: ISSUER.publicKey, evil: ISSUER.publicKey }),
                     { bct2: ISSUER.publicKey });
});

await check("expiry moves to grace, then expired, and a clock set back does not undo it", async () => {
    const page = await connected();
    const payload = V.parse(page.client.files().license.certificate).payload;
    page.set(payload.exp + DAY);
    assert.deepEqual([(await page.client.current()).state, (await page.client.current()).reason], ["grace", "in_grace"]);
    page.set(payload.exp + 15 * DAY);
    const lapsed = await page.client.current();
    assert.deepEqual([lapsed.state, lapsed.entitlements], ["expired", []]);
    // A reload a day later records the moment (the mark), then the clock goes back.
    const reload = tab({ clock: page.clock, service: page.service, store: page.store });
    await reload.client.restore({ heartbeatEveryMs: 0 });
    page.set(payload.iat + DAY);
    const back = tab({ clock: page.clock, service: page.service, store: page.store });
    await back.client.restore({ heartbeatEveryMs: 0 });
    const held = await back.client.current();
    assert.equal(held.state, "expired");
    assert.equal(held.clock_rollback, true);
});

await check("the heartbeat is due after 7 days or inside 21 days of the end, never within 6 hours of a try, and a failure changes nothing", async () => {
    const page = await connected();
    const { client, service } = page;
    assert.equal(await client.heartbeat(), "not_due");
    page.set(page.t + 8 * DAY);
    service.down(true);
    assert.equal(await client.heartbeat(), "failed");
    assert.equal((await client.current()).state, "active");
    assert.equal(client.files().license.last_attempt, page.t);
    page.set(page.t + 3600);
    service.down(false);
    assert.equal(await client.heartbeat(), "not_due", "six hours after a failed try");
    page.set(page.t + 6 * 3600);
    assert.equal(await client.heartbeat(), "ok");
    assert.equal(client.files().license.last_validated, page.t);
    assert.equal(service.of("/refresh").length, 2);
    assert.equal(await client.heartbeat(), "not_due");
    page.set(page.t + 7 * 3600);
    const at = page.t;
    const base = { state: "active", clock_rollback: false };
    assert.equal(client.due({ ...base, last_validated: at - 2 * DAY, expires_at: at + 20 * DAY }), true);
    assert.equal(client.due({ ...base, last_validated: at - 2 * DAY, expires_at: at + 30 * DAY }), false);
    assert.equal(client.due({ ...base, last_validated: at - 12 * 3600, expires_at: at + 20 * DAY }), false);
    assert.equal(client.due({ ...base, last_validated: at - 2 * DAY, expires_at: at + 30 * DAY, clock_rollback: true }), true);
});

await check("renewed and revoked answers are applied, and a renewal that does not verify is rejected", async () => {
    const page = await connected();
    const { client, service } = page;
    const env = service.of("/activate/start")[0].body.environment;
    const before = client.files().license.certificate;
    service.script("/refresh", 200, { status: "renewed", server_time: page.t,
                                      certificate: await service.certificate(env, {}, ROGUE) });
    const rejected = await ask(client, "POST", "/settings/license/refresh");
    assert.deepEqual([rejected.status, rejected.body.outcome], [200, "rejected"]);
    assert.equal(client.files().license.certificate, before);
    const fresh = await service.certificate(env, { cid: "crt_web0002", exp: page.t + 120 * DAY });
    service.script("/refresh", 200, { status: "renewed", certificate: fresh, server_time: page.t });
    const renewed = await ask(client, "POST", "/settings/license/refresh");
    assert.equal(renewed.body.outcome, "renewed");
    assert.equal(client.files().license.certificate, fresh);
    service.script("/refresh", 403, { error: { code: "forged_certificate", message: "no" } });
    const revoked = await ask(client, "POST", "/settings/license/refresh");
    assert.equal(revoked.body.outcome, "revoked");
    const held = await client.current();
    assert.deepEqual([held.state, held.entitlements], ["revoked", []]);
    assert.equal(client.files().license.revoked.reason, "forged_certificate");
    assert.equal(await client.heartbeat({ force: true }), "skipped:revoked");
});

await check("two tabs share one licence: one refreshes under the lock, the other hears of a change and re-reads", async () => {
    let t = 1_800_000_000;
    const clock = () => t;
    const service = platform(clock);
    const store = L.memoryStore();
    const locks = fakeLocks();
    const join_ = hub();
    const a = tab({ clock, service, store, locks, channel: join_() });
    const b = tab({ clock, service, store, locks, channel: join_() });
    await a.client.restore({ heartbeatEveryMs: 0 });
    await b.client.restore({ heartbeatEveryMs: 0 });
    const heard = [];
    b.client.on((event) => heard.push(event.from));
    await ask(a.client, "POST", "/settings/license/activate", { credential: "BIOC-ABCD-EFGH-JKLM-NPQR" });
    await until(() => heard.includes("elsewhere") && b.client.state().state === "active");
    assert.equal(b.client.state().state, "active", "the other tab re-read the store");
    assert.ok(heard.includes("elsewhere"));
    assert.ok(b.adopted.some((files) => files.license && files.license.certificate), "and wrote its own worker's files");
    t += 8 * DAY;
    const outcomes = await Promise.all([a.client.heartbeat(), b.client.heartbeat()]);
    assert.deepEqual(outcomes.sort(), ["busy", "ok"]);
    assert.equal(service.of("/refresh").length, 1, "one tab asked the platform");
    // Two tabs creating the device secret at once make one.
    const fresh = L.memoryStore();
    const c = tab({ clock, service, store: fresh, locks });
    const d = tab({ clock, service, store: fresh, locks });
    await Promise.all([ask(c.client, "POST", "/settings/license/connect"), ask(d.client, "POST", "/settings/license/connect")]);
    const bindings = service.of("/activate/start").map((r) => r.body.environment.binding);
    assert.equal(bindings[bindings.length - 1], bindings[bindings.length - 2]);
});

await check("Forget releases the device, then clears the certificate, the secret and the install id; a release that fails asks first", async () => {
    const page = await connected();
    const { client, service, store } = page;
    await client.installId();
    assert.deepEqual(Object.keys(store.dump()).sort(), ["environment", "install", "licence"]);
    const binding = service.of("/activate/start")[0].body.environment.binding;
    const gone = await ask(client, "POST", "/settings/license/forget");
    assert.deepEqual([gone.status, gone.body.success, gone.body.license.plan], [200, true, "free"]);
    assert.equal(service.of("/deactivate")[0].body.binding, binding);
    assert.deepEqual(store.dump(), {});
    const files = page.adopted[page.adopted.length - 1];
    assert.deepEqual([files.license, files.environment], [null, null]);
    assert.equal(client.state().state, "free");

    const again = await connected();
    again.service.down(true);
    const refused = await ask(again.client, "POST", "/settings/license/forget");
    assert.deepEqual([refused.status, refused.body.release_failed, refused.body.code], [409, true, "unreachable"]);
    assert.ok(again.store.dump().licence, "nothing is cleared before the person says so");
    const forced = await ask(again.client, "POST", "/settings/license/forget", { force: true });
    assert.equal(forced.status, 200);
    assert.deepEqual(again.store.dump(), {});
    // A device the platform no longer knows is already released.
    const third = await connected();
    third.service.script("/deactivate", 404, { error: { code: "environment_unknown", message: "gone" } });
    assert.equal((await ask(third.client, "POST", "/settings/license/forget")).status, 200);
});

await check("the AI token is exchanged once, held in memory, renewed at two thirds of its life, and never kept", async () => {
    const page = await connected();
    const { client, service, store } = page;
    const first = await client.aiTokenForPython({});
    assert.match(first.token, /^BIOCAI1\./);
    assert.equal(first.gateway, "https://ai.biocognia.test");
    assert.equal((await client.aiTokenForPython({})).token, first.token);
    assert.equal(service.of("/ai/token").length, 1);
    assert.equal(service.of("/ai/token")[0].body.app_version, "0.0.36");
    page.set(page.t + 21 * 60);
    assert.notEqual((await client.aiTokenForPython({})).token, first.token);
    await client.aiTokenForPython({ force: true });
    assert.equal(service.of("/ai/token").length, 3);
    assert.ok(!JSON.stringify(store.dump()).includes("BIOCAI1"));
    service.script("/ai/token", 403, { error: { code: "ai_not_entitled", message: "no" } });
    const refused = await client.aiTokenForPython({ force: true });
    assert.equal(refused.error.code, "ai_not_entitled");
    const free = tab({ clock: page.clock, service: platform(page.clock) });
    await free.client.restore({ heartbeatEveryMs: 0 });
    assert.equal((await free.client.aiTokenForPython({})).error.code, "no_license");
});

await check("the routes answer in license_routes.py's shapes, and other paths are not the licence's", async () => {
    let t = 1_800_000_000;
    const clock = () => t;
    const service = platform(clock);
    const { client } = tab({ clock, service });
    await client.restore({ heartbeatEveryMs: 0 });
    assert.equal(await client.answer(request("GET", "/license/status")), null, "Python answers the status");
    assert.equal(await client.answer(request("POST", "/settings/license/connectx", {})), null);
    assert.equal((await ask(client, "GET", "/settings/license/account")).status, 409);
    assert.equal((await ask(client, "POST", "/settings/license/refresh")).status, 400);
    assert.equal((await ask(client, "POST", "/settings/license/deactivate")).status, 400);
    assert.equal((await ask(client, "POST", "/settings/license/install", { certificate: "nothing here" })).status, 400);
    await ask(client, "POST", "/settings/license/activate", { credential: "BIOC-ABCD-EFGH-JKLM-NPQR" });
    const who = await ask(client, "GET", "/settings/license/account");
    assert.deepEqual(who.body, { success: true, account: { email: "ada@lab.example.org", name: "Ada Lovelace",
                                                           org: { name: "Example Lab", kind: "institution" } } });
    service.script("/deactivate", 429, { error: { code: "cooldown_active", message: "later", next_allowed_at: t + DAY } });
    const later = await ask(client, "POST", "/settings/license/deactivate");
    assert.deepEqual([later.status, later.body.code], [429, "cooldown_active"]);
    const released = await ask(client, "POST", "/settings/license/deactivate");
    assert.equal(released.status, 200);
    assert.equal(client.state().state, "free");
    assert.ok(client.files().environment, "releasing keeps the device secret; Forget does not");
    const removed = await ask(client, "POST", "/settings/license/remove");
    assert.equal(removed.status, 200);
});

await check("the platform's address must be https, unless it is this machine", async () => {
    assert.equal(L.isSecureUrl("https://api.biocognia.com/v1"), true);
    assert.equal(L.isSecureUrl("http://127.0.0.1:8123/v1"), true);
    assert.equal(L.isSecureUrl("http://localhost:8123/v1"), true);
    assert.equal(L.isSecureUrl("http://api.biocognia.com/v1"), false);
    assert.equal(L.isSecureUrl("ftp://api.biocognia.com"), false);
    let t = 1_800_000_000;
    const service = platform(() => t);
    const { client } = tab({ clock: () => t, service, api: "http://api.example.org/v1" });
    await client.restore({ heartbeatEveryMs: 0 });
    const refused = await ask(client, "POST", "/settings/license/connect");
    assert.equal(refused.body.code, "insecure_server");
    assert.equal(service.requests.length, 0);
});
