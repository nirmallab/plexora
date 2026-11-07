/**
 * PlexoraLicense -- the website's licence, held by the page (plan D8).
 *
 * On the desktop a Plexora process holds its licence: the `biocognia`
 * package runs the device flow, keeps the certificate in a file and renews
 * it. On the website the browser is that process. This module does there what
 * biocognia/state.py does on a desktop, and Plexora's Python in the worker
 * reads the result exactly as it reads a desktop's:
 *
 * - **The device flow and the key**, against the platform's product API
 *   (`api`, default https://api.biocognia.com/v1): `/activate/start` and
 *   `/activate/poll`, or `/activate` with a licence key or activation code,
 *   for an environment of kind `browser`. A person signs in and approves at
 *   account.biocognia.com; the page never sees a password.
 * - **Verification** with the platform's own verifier (services/
 *   licenseVerify.js, vendored from packages/shared/src/verify.ts): the
 *   signature, the claims, and that `bind` names this browser.
 * - **The state machine** of biocognia/state.py: free, trial, active,
 *   offline_valid, grace, expired, revoked, invalid; expiry and grace; the
 *   clock's high-water mark; the heartbeat (a refresh when the last check is
 *   7 days old, or inside 21 days of the end, never sooner than 6 hours after
 *   a failed one), failing OPEN: a refresh that fails changes nothing.
 * - **Storage** in IndexedDB on the site's origin (`plexora-licence`): the
 *   certificate's record (biocognia's license.json), a random device secret
 *   (environment.json: the platform is told only its SHA-256) and an install
 *   id. Never the licence key, an activation code, an email or a token.
 * - **The worker's files**: every change writes license.json and
 *   environment.json into BIOCOGNIA_DIR (/bioc) through
 *   plexora/browser/licence.py `adopt`, so `licensing.peek()`, Jinja's
 *   `licensed()` and the capability guards run unchanged, and offline.
 * - **Tabs**: a change is made under one Web Lock (`plexora-licence`) and
 *   told to the other tabs on a BroadcastChannel of the same name; each
 *   re-reads the store and writes its own worker's files. One tab refreshes.
 * - **The AI token**: the 30-minute token exchanged against the certificate
 *   (`/ai/token`) is held in memory here and handed to Python when it asks
 *   (`ai_token`, plexora/browser/licence.py PageTokenSource).
 *
 * The routes Settings > Account calls (`/settings/license/*`, classed "js" in
 * plexora/browser/routes.py) are answered here, in the shapes
 * plexora/server/routes/license_routes.py answers them on a desktop, plus
 * `/settings/license/forget`: Forget this browser releases its device on the
 * seat, then clears everything above.
 *
 *   createLicenseClient(options) -> client (the logic; tests drive it with a
 *                                   memory store and a fake platform)
 *   startInPage({config, version, python}) -> the page's client, restored
 *   client.answer(request)  -> a Response for a licence route, or null
 *
 * An ES module (shell.js imports it in the site's host page; the app's frame
 * borrows the host's `PlexoraLicense`).
 */
import { CertificateError, PUBLIC_KEYS, bindingOf, bindsHere, looksLike, verify } from "./licenseVerify.js";

export const PRODUCT = "plexora";
export const PRODUCT_NAME = "Plexora";
export const DEFAULT_API = "https://api.biocognia.com/v1";
export const DEFAULT_PORTAL = "https://account.biocognia.com";
export const SCHEMA = 1;

//: biocognia/state.py's clock, in seconds.
export const REFRESH_STALE_SECONDS = 7 * 86400;
export const RENEW_WINDOW_SECONDS = 21 * 86400;
export const RETRY_SECONDS = 6 * 3600;
export const ROLLBACK_TOLERANCE = 86400;
//: biocognia/client.py: a person is waiting / nobody is.
export const INTERACTIVE_TIMEOUT_MS = 15000;
export const BACKGROUND_TIMEOUT_MS = 5000;
//: How often the page asks itself whether a refresh is due.
export const HEARTBEAT_CHECK_MS = 3600 * 1000;

export const LICENSED_STATES = Object.freeze(["trial", "active", "offline_valid", "grace"]);
//: The Web Lock and the BroadcastChannel every tab of the site shares.
export const LOCK = "plexora-licence";
export const CHANNEL = "plexora-licence";
//: Store keys.
const LICENCE = "licence";
const ENVIRONMENT = "environment";
const INSTALL = "install";

//: A throwaway key a test build of the site trusts (biocognia.testing's kid).
const TEST_KID = /^bct\d*$/;
const CODE = /^BIOC-[A-Z0-9]{4}(?:-[A-Z0-9]{4}){1,4}$/;
const CERTIFICATE_LINE = /BIOC1\.[A-Za-z0-9]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/;
export const MAX_CREDENTIAL = 128;
export const MAX_CERTIFICATE_TEXT = 64 * 1024;

//: biocognia/codes.py SENTENCES, for the codes a licence surface can meet
//: (tests/test_license_page.py holds them equal to the package's).
export const SENTENCES = Object.freeze({
    invalid_credential: "That licence key, activation code or token is not recognised.",
    credential_revoked: "That licence key, activation code or token has been revoked.",
    credential_expired: "That activation code or token has expired; make a new one in the portal.",
    scope_not_allowed: "That token is not allowed to do this.",
    client_too_old: "This version is too old to activate; upgrade it.",
    denied: "The activation was declined in the portal.",
    expired: "The activation code expired before it was approved; start again.",
    code_invalid: "That activation code is not recognised.",
    license_expired: "This licence has expired.",
    license_revoked: "This licence has been revoked.",
    license_suspended: "This licence is suspended; contact whoever manages it.",
    seat_required: "You need a seat for this product; ask an owner of your organisation.",
    no_seat_available: "Every seat on this licence is taken; ask an owner for one.",
    device_limit: "This seat already has as many devices as it allows. Disconnect one in the portal "
        + "(Devices), deactivate it where it is no longer needed, or add devices to your seat at "
        + "account.biocognia.com.",
    seat_env_limit: "This seat already has as many devices as it allows. Disconnect one in the portal "
        + "(Devices), deactivate it where it is no longer needed, or add devices to your seat at "
        + "account.biocognia.com.",
    cooldown_active: "Devices on this seat were changed recently; try again later.",
    domain_not_verified: "Your organisation has not verified this email domain yet.",
    payment_required: "This needs a purchase; an owner can buy it in the portal.",
    rate_limited: "Too many requests; try again shortly.",
    forged_certificate: "The platform did not recognise this certificate.",
    certificate_revoked: "This device was signed out in the portal.",
    environment_unknown: "The platform does not know this device.",
    environment_mismatch: "This certificate belongs to a different device.",
    offline_not_allowed: "This licence does not include offline certificates.",
    signing_unavailable: "The platform cannot issue certificates right now.",
    ai_not_entitled: "This licence does not include AI.",
    ai_disabled: "AI is turned off for your organisation.",
});

//: biocognia/state.py `_invalid_sentence`.
function invalidSentence(reason) {
    return {
        environment_mismatch: "This certificate was issued for a different device.",
        unknown_key: `This certificate was signed with a key this ${PRODUCT_NAME} does not trust.`,
        bad_signature: "This certificate has been altered and does not verify.",
        wrong_product: `This is not a ${PRODUCT_NAME} certificate.`,
        unsupported_version: `This certificate needs a newer ${PRODUCT_NAME}.`,
        future_dated: "This certificate is dated in the future; check the clock.",
    }[reason] || `This is not a valid ${PRODUCT_NAME} certificate.`;
}

/** A refusal or a failure to reach the platform (biocognia.errors.ServerError). */
export class PlatformError extends Error {
    constructor(message, { code = "unreachable", status = null, retryAfter = null, nextAllowedAt = null,
                           detail = null } = {}) {
        super(message);
        this.code = code;
        this.status = status;
        this.retryAfter = retryAfter;
        this.nextAllowedAt = nextAllowedAt;
        this.detail = detail;
    }
}

const integer = (value) => (typeof value === "number" && Number.isInteger(value) ? value : null);
const number = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** biocognia/store.py `is_secure_url`: https anywhere, http only to this machine. */
export function isSecureUrl(url) {
    let parts;
    try { parts = new URL(String(url || "").trim()); } catch { return false; }
    if (!parts.hostname) return false;
    if (parts.protocol === "https:") return true;
    if (parts.protocol !== "http:") return false;
    const host = parts.hostname.replace(/^\[|\]$/g, "").toLowerCase();
    return host === "localhost" || host.endsWith(".localhost") || /^127\./.test(host) || host === "::1";
}

/** biocognia/client.py `error_from`: a structured refusal as a PlatformError. */
export function errorFrom(status, payload, action) {
    const error = payload && typeof payload === "object" && payload.error && typeof payload.error === "object"
        ? payload.error : {};
    const code = typeof error.code === "string" ? error.code : "bad_response";
    let message = SENTENCES[code] || error.message || `HTTP ${status}`;
    const next = number(error.next_allowed_at);
    if (code === "cooldown_active" && next !== null) {
        message += ` Next change allowed ${new Date(next * 1000).toLocaleString()}.`;
    }
    return new PlatformError(`${action} was refused: ${message}`, {
        code, status, retryAfter: integer(error.retry_after), nextAllowedAt: next, detail: error.details ?? null,
    });
}

/** Keys a test build trusts beside the shipped ones: `bct*` kids only, never a production kid. */
export function testKeys(keys) {
    const out = {};
    for (const [kid, value] of Object.entries(keys || {})) {
        if (TEST_KID.test(kid) && typeof value === "string" && /^[A-Za-z0-9_-]{43}$/.test(value)
            && !Object.prototype.hasOwnProperty.call(PUBLIC_KEYS, kid)) {
            out[kid] = value;
        }
    }
    return out;
}

function b64url(bytes) {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function entitlementsOf(value) {
    if (!Array.isArray(value)) return [];
    return [...new Set(value.filter((item) => typeof item === "string" && item))].sort();
}

/** An in-memory store with IndexedDB's shape here (tests; a browser without storage). */
export function memoryStore(initial = {}) {
    const data = new Map(Object.entries(initial));
    const copy = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
    return {
        kind: "memory",
        get: async (key) => copy(data.get(key)),
        put: async (key, value) => { data.set(key, copy(value)); },
        delete: async (key) => { data.delete(key); },
        clear: async () => { data.clear(); },
        dump: () => Object.fromEntries([...data].map(([key, value]) => [key, copy(value)])),
    };
}

/** The site's IndexedDB database `plexora-licence`, one object store of records by key. */
export function indexedStore(name = "plexora-licence") {
    let opened = null;
    const open = () => {
        if (!opened) {
            opened = new Promise((resolve, reject) => {
                const request = globalThis.indexedDB.open(name, 1);
                request.onupgradeneeded = () => request.result.createObjectStore("kv");
                request.onsuccess = () => {
                    const db = request.result;
                    // Another tab deleting or upgrading it: let go, open again next time.
                    db.onversionchange = () => { db.close(); opened = null; };
                    resolve(db);
                };
                request.onerror = () => reject(request.error);
                request.onblocked = () => reject(new Error("the licence store is blocked by another tab"));
            });
            opened.catch(() => { opened = null; });
        }
        return opened;
    };
    const run = async (mode, fn) => {
        const db = await open();
        return new Promise((resolve, reject) => {
            const tx = db.transaction("kv", mode);
            const request = fn(tx.objectStore("kv"));
            tx.oncomplete = () => resolve(request ? request.result : undefined);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error || new Error("aborted"));
        });
    };
    return {
        kind: "indexeddb",
        get: (key) => run("readonly", (store) => store.get(key)),
        put: (key, value) => run("readwrite", (store) => store.put(value, key)).then(() => undefined),
        delete: (key) => run("readwrite", (store) => store.delete(key)).then(() => undefined),
        clear: () => run("readwrite", (store) => store.clear()).then(() => undefined),
    };
}

/** What a browser calls itself, coarsely: never a host or user name. */
export function browserLabel(navigatorLike = globalThis.navigator) {
    const nav = navigatorLike || {};
    const ua = String(nav.userAgent || "");
    const data = nav.userAgentData || null;
    const brands = (data && Array.isArray(data.brands) ? data.brands : []).map((b) => b.brand);
    const browser = brands.find((b) => /Edge/.test(b)) ? "Edge"
        : brands.find((b) => /Opera/.test(b)) ? "Opera"
            : brands.find((b) => /Chrome/.test(b)) ? "Chrome"
                : /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox"
                    : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
    const platform = String((data && data.platform) || nav.platform || ua);
    const os = /Win/i.test(platform) ? "Windows" : /Mac|iPhone|iPad/i.test(platform) ? "macOS"
        : /Android/i.test(platform) ? "Android" : /Linux|X11|CrOS/i.test(platform) ? "Linux" : "";
    return {
        name: os ? `${browser} on ${os}` : browser,
        platform: os ? `web-${os.toLowerCase()}` : "web",
    };
}

/**
 * The licence logic, with everything it touches handed in:
 *
 *   store       {get, put, delete, clear} (indexedStore / memoryStore)
 *   fetch       the browser's fetch
 *   api         the platform's product API, with its /v1 (https, or loopback)
 *   portal      where a person approves a code, when the platform names no page
 *   version     Plexora's version (`BioCognia-Client: plexora/<version>`)
 *   keys        extra trusted keys, `bct*` kids only (a test build's)
 *   adopt(files) -> Python's licence status, after writing /bioc (or null)
 *   clock       wall time in seconds
 *   locks       navigator.locks, or null
 *   channel     a BroadcastChannel, or null
 *   label       browserLabel()
 */
export function createLicenseClient({
    store = memoryStore(),
    fetch: fetchImpl = (...args) => globalThis.fetch(...args),
    api = DEFAULT_API,
    portal = DEFAULT_PORTAL,
    version = "0",
    keys = {},
    adopt = async () => null,
    clock = () => Date.now() / 1000,
    locks = null,
    channel = null,
    label = browserLabel(),
    random = (n) => globalThis.crypto.getRandomValues(new Uint8Array(n)),
} = {}) {
    const base = String(api || DEFAULT_API).replace(/\/+$/, "");
    const server = base.replace(/\/v1$/, "");
    const portalBase = String(isSecureUrl(portal) ? portal : DEFAULT_PORTAL).replace(/\/+$/, "");
    const trusted = { ...PUBLIC_KEYS, ...testKeys(keys) };
    const listeners = new Set();
    let record = null;        // license.json's record
    let environment = null;   // environment.json's record: {secret, created_at}
    let floor = 0;            // the clock's high-water mark
    let state = null;         // the last evaluation
    let status = null;        // Python's last status
    let token = null;         // the AI token, in memory only
    let timer = null;

    // -- small helpers ------------------------------------------------------------

    const wall = () => clock();
    /** Wall time, never earlier than the latest this browser has seen (state.py `now`). */
    const now = () => Math.max(wall(), floor);
    const advanceFloor = (moment) => {
        const value = number(moment);
        if (value !== null && value > floor) floor = value;
    };

    function withLock(fn, { ifAvailable = false } = {}) {
        if (!locks || typeof locks.request !== "function") return fn(true);
        return locks.request(LOCK, ifAvailable ? { ifAvailable: true } : {}, (lock) => fn(Boolean(lock)));
    }

    function emit(kind, detail) {
        for (const fn of listeners) {
            try { fn({ kind, ...detail }); } catch (error) { console.error(error); }
        }
    }

    // -- the state (biocognia/state.py) ----------------------------------------------

    function free(reason = "no_license", { source = "none", stateName = "free" } = {}) {
        return {
            product: PRODUCT, state: stateName, reason, licensed: false, entitlements: [], granted: [],
            source, certificate_type: null, environment: null, trial: false, plan: null,
            expires_at: null, license_expires_at: null, grace_until: null, offline_until: null,
            last_validated: null, clock_rollback: false,
        };
    }

    function withState(current, name, reason) {
        const licensed = LICENSED_STATES.includes(name);
        return { ...current, state: name, reason, licensed, entitlements: licensed ? current.granted : [] };
    }

    /** state.py `_state_for_payload`, before expiry. */
    function stateForPayload(payload, held) {
        let expires = number(payload.exp);
        const offlineUntil = number(payload.offline_until);
        if (offlineUntil !== null) expires = expires === null ? offlineUntil : Math.min(expires, offlineUntil);
        const graceDays = number(payload.grace_days);
        const grace = graceDays !== null && graceDays > 0 ? graceDays * 86400 : 0;
        const trial = payload.trial === true;
        const name = trial ? "trial" : offlineUntil !== null ? "offline_valid" : "active";
        const granted = entitlementsOf(payload.ent);
        const env = held && held.environment && typeof held.environment === "object" ? held.environment : {};
        return {
            product: PRODUCT, state: name, reason: "valid", licensed: true, entitlements: granted, granted,
            source: "cache", certificate_type: payload.typ || "env", trial,
            plan: typeof payload.plan === "string" ? payload.plan : null,
            environment: { id: payload.env ?? null, type: payload.envt ?? null, name: env.name ?? null },
            expires_at: expires, license_expires_at: number(payload.lic_exp),
            grace_until: expires !== null ? expires + grace : null, offline_until: offlineUntil,
            last_validated: held ? number(held.last_validated) : null,
            clock_rollback: wall() < floor - ROLLBACK_TOLERANCE,
        };
    }

    /** A certificate as a state (state.py `state_for_certificate`), or invalid with the reason. */
    async function stateForCertificate(certificate, held) {
        let payload;
        try {
            payload = await verify(certificate, { audience: PRODUCT, now: now(), keys: trusted });
        } catch (error) {
            const code = error instanceof CertificateError ? error.code : "malformed";
            return free(code, { source: "cache", stateName: "invalid" });
        }
        const typ = payload.typ === undefined ? "env" : payload.typ;
        if (typ !== "env" && !(held && held.credential_hint)) {
            // An unbound (job, ci) certificate is never installed in a browser.
            return free("environment_mismatch", { source: "cache", stateName: "invalid" });
        }
        if (typ === "env" && !(await bindsHere(payload, environment && environment.secret))) {
            return free("environment_mismatch", { source: "cache", stateName: "invalid" });
        }
        return stateForPayload(payload, held);
    }

    /** state.py `_apply_expiry`: grace, then expired, at the moment they come. */
    function applyExpiry(current) {
        if (!current.licensed || current.expires_at === null) return current;
        const moment = now();
        if (moment <= current.expires_at) return current;
        if (current.grace_until !== null && moment <= current.grace_until) {
            return current.state === "grace" ? current : withState(current, "grace", "in_grace");
        }
        return withState(current, "expired", "expired");
    }

    async function evaluate() {
        if (!record || !record.certificate) {
            state = free();
            return state;
        }
        let current = await stateForCertificate(record.certificate, record);
        if (record.revoked && current.state !== "invalid") current = withState(current, "revoked", "revoked");
        state = applyExpiry(current);
        return state;
    }

    /** The state now, with expiry applied at this moment. */
    async function current() {
        if (state === null) await evaluate();
        state = applyExpiry(state);
        return state;
    }

    // -- the store ---------------------------------------------------------------------

    async function load() {
        const [held, env] = await Promise.all([store.get(LICENCE), store.get(ENVIRONMENT)]);
        record = held && typeof held === "object" && held.certificate ? held : null;
        environment = env && typeof env === "object" && typeof env.secret === "string" && env.secret.length >= 32
            ? env : null;
        const hwm = record ? number(record.hwm) : null;
        if (hwm !== null) advanceFloor(hwm);
        return evaluate();
    }

    /** What Python is handed: the package's own files, and a test build's keys. */
    function files() {
        return { license: record, environment, keys: testKeys(keys) };
    }

    async function toPython() {
        try {
            status = await adopt(files());
        } catch (error) {
            console.warn("[licence] the worker did not take the licence:", error);
        }
        return status;
    }

    /** Write, tell the worker, tell the other tabs. Under the lock, by the caller. */
    async function commit(nextRecord, nextEnvironment, { why = "local" } = {}) {
        if (nextRecord) await store.put(LICENCE, nextRecord); else await store.delete(LICENCE);
        if (nextEnvironment !== undefined) {
            if (nextEnvironment) await store.put(ENVIRONMENT, nextEnvironment); else await store.delete(ENVIRONMENT);
            environment = nextEnvironment;
        }
        // The AI token was exchanged against this certificate: a new one, a
        // revocation or none ends it; a refresh's bookkeeping does not.
        if (!nextRecord || !record || nextRecord.certificate !== record.certificate || nextRecord.revoked) token = null;
        record = nextRecord;
        await evaluate();
        await toPython();
        if (channel) {
            try { channel.postMessage({ kind: "changed" }); } catch (error) { /* the channel closed */ }
        }
        emit("changed", { status, state, from: why });
        return status;
    }

    /** Update fields of the record (state.py `store.update`); nothing without a certificate. */
    async function update(fields, options) {
        const held = await store.get(LICENCE);
        if (!held || !held.certificate) return null;
        const next = { ...held, ...fields };
        await commit(next, undefined, options);
        return next;
    }

    /** This browser's secret, created on first need, once across tabs. */
    async function ensureEnvironment() {
        if (environment) return environment;
        return withLock(async () => {
            const held = await store.get(ENVIRONMENT);
            if (held && typeof held.secret === "string" && held.secret.length >= 32) {
                environment = held;
                return held;
            }
            const fresh = { schema: SCHEMA, secret: b64url(random(32)), created_at: Math.floor(wall()) };
            await store.put(ENVIRONMENT, fresh);
            environment = fresh;
            return fresh;
        });
    }

    async function binding() {
        return environment ? bindingOf(environment.secret) : null;
    }

    /** environment.py `environment_block`, for a browser: a hash, a family, a name. */
    async function environmentBlock(name) {
        const env = await ensureEnvironment();
        const chosen = String(name || "").trim().slice(0, 80);
        return {
            kind: "browser",
            display_name: chosen || label.name || "Browser",
            binding: await bindingOf(env.secret),
            delegation_pubkey: null,
            platform: label.platform || "web",
            scheduler_hint: "none",
            app_version: String(version),
        };
    }

    // -- the platform (biocognia/client.py) ---------------------------------------------

    async function post(path, body, { action, timeoutMs = INTERACTIVE_TIMEOUT_MS }) {
        if (!isSecureUrl(base)) {
            throw new PlatformError(`${action} was not sent: the BioCognia platform address ${base} is not https, `
                + "and certificates and device bindings are not sent in clear.", { code: "insecure_server" });
        }
        let response;
        try {
            const init = {
                method: "POST", mode: "cors", credentials: "omit", cache: "no-store",
                headers: { "Content-Type": "application/json", "BioCognia-Client": `${PRODUCT}/${version}` },
                body: JSON.stringify(body),
            };
            if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
                init.signal = AbortSignal.timeout(timeoutMs);
            }
            response = await fetchImpl(`${base}${path}`, init);
        } catch (error) {
            throw new PlatformError(`Could not reach the BioCognia platform (${new URL(base).host}). `
                + "A licence already in this browser keeps working.", { code: "unreachable" });
        }
        let payload = null;
        try { payload = await response.json(); } catch { payload = null; }
        if (!response.ok) throw errorFrom(response.status, payload, action);
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
            throw new PlatformError(`${action}: the platform answered with something that is not JSON.`,
                                    { code: "bad_response", status: response.status });
        }
        return payload;
    }

    /** state.py `save_certificates`, for this product: the browser keeps Plexora's only. */
    async function save(result, { source }) {
        const moment = Math.floor(wall());
        const serverTime = number(result.server_time);
        advanceFloor(serverTime);
        const certificate = result.certificates && result.certificates[PRODUCT];
        if (typeof certificate !== "string" || !looksLike(certificate)) return null;
        const next = {
            schema: SCHEMA, certificate, source, credential_hint: null,
            installed_at: moment, last_validated: moment, last_attempt: moment,
            hwm: Math.floor(Math.max(moment, serverTime || 0)), server,
            environment: result.environment && typeof result.environment === "object" ? result.environment : {},
            revoked: null,
        };
        await withLock(() => commit(next));
        return next;
    }

    // -- what a person asks for ------------------------------------------------------------

    /** Connect this browser: a short code the person approves in the portal. */
    async function connect({ name = null, trial = false } = {}) {
        const body = { product: PRODUCT, environment: await environmentBlock(name) };
        if (trial) body.trial = true;
        const started = await post("/activate/start", body, { action: "Activation" });
        if (typeof started.code !== "string") {
            throw new PlatformError("Activation: the platform returned no code.", { code: "bad_response" });
        }
        const verifyUrl = typeof started.verify_url === "string" && isSecureUrl(started.verify_url)
            ? started.verify_url : `${portalBase}/activate`;
        return {
            code: started.code, verify_url: verifyUrl,
            interval: Math.max(1, Math.floor(number(started.interval) || 5)),
            expires_in: Math.floor(number(started.expires_in) || 900),
        };
    }

    /** One poll of the device flow: pending, or the certificate saved. */
    async function poll(code) {
        let result;
        try {
            result = await post("/activate/poll", { code, binding: await binding() }, { action: "Activation" });
        } catch (error) {
            if (error.code === "activation_pending") return { pending: true, retry_after: error.retryAfter };
            throw error;
        }
        if (result.status === "pending") return { pending: true, retry_after: result.retry_after ?? null };
        if (!result.certificates || typeof result.certificates !== "object" || !Object.keys(result.certificates).length) {
            throw new PlatformError("Activation: the platform approved but sent no certificate.",
                                    { code: "bad_response" });
        }
        await save(result, { source: "activation" });
        return { pending: false };
    }

    /** A licence key or activation code from the portal, exchanged for this browser's certificate. */
    async function activate(credential, { name = null } = {}) {
        const body = { product: PRODUCT, credential: String(credential).trim(),
                       environment: await environmentBlock(name) };
        const result = await post("/activate", body, { action: "Activation" });
        if (!result.certificates || typeof result.certificates[PRODUCT] !== "string") {
            throw new PlatformError(`Activation: the platform returned no certificate for ${PRODUCT_NAME}.`,
                                    { code: "bad_response" });
        }
        await save(result, { source: "activation" });
    }

    /** An offline licence file's certificate, verified and kept (state.py `install_certificate`). */
    async function install(certificate) {
        const checked = await stateForCertificate(certificate.trim(), null);
        if (checked.state === "invalid") {
            throw new PlatformError(invalidSentence(checked.reason), { code: checked.reason });
        }
        const moment = Math.floor(wall());
        const next = {
            schema: SCHEMA, certificate: certificate.trim(), source: "install", credential_hint: null,
            installed_at: moment, last_validated: null, last_attempt: null,
            hwm: Math.floor(Math.max(moment, floor)), server, environment: {}, revoked: null,
        };
        await withLock(() => commit(next));
    }

    /** state.py `apply_refresh`: what the platform said of the certificate. */
    async function applyRefresh(result, why = "local") {
        const answer = result || {};
        const serverTime = number(answer.server_time);
        advanceFloor(serverTime);
        const moment = Math.floor(wall());
        const hwm = Math.floor(Math.max(moment, serverTime || 0));
        if (answer.status === "revoked") {
            await update({ revoked: { at: moment, reason: answer.reason ?? null }, last_validated: moment, hwm },
                         { why });
            return "revoked";
        }
        if (answer.status === "renewed" && typeof answer.certificate === "string") {
            const fresh = await stateForCertificate(answer.certificate, record);
            if (fresh.state === "invalid") return "rejected";
            await update({ certificate: answer.certificate, last_validated: moment, hwm, revoked: null },
                         { why });
            return "renewed";
        }
        if (answer.status === "ok") {
            await update({ last_validated: moment, hwm }, { why });
            return "ok";
        }
        return "ignored";
    }

    function refreshResult(error) {
        if (["license_revoked", "credential_revoked", "forged_certificate", "certificate_revoked"].includes(error.code)) {
            return { status: "revoked", reason: error.code };
        }
        throw error;
    }

    /** state.py `refreshable`: why `current` is never refreshed, or null. */
    function refreshable(held) {
        if (held.source !== "cache" || !record || !record.certificate) return "no_certificate";
        if (held.certificate_type !== null && held.certificate_type !== "env") return "unbound";
        if (held.state === "invalid" || held.state === "revoked") return held.state;
        if (held.offline_until !== null) return "offline_licence";
        if (!isSecureUrl(base)) return "no_server";
        return null;
    }

    /** state.py `_due`: whether the background refresh should ask now. */
    function due(held) {
        const moment = wall();
        const lastAttempt = record ? number(record.last_attempt) : null;
        if (lastAttempt !== null && moment - lastAttempt >= 0 && moment - lastAttempt < RETRY_SECONDS) return false;
        if (held.clock_rollback) return true;
        const last = held.last_validated;
        if (last === null) return true;
        const age = moment - last;
        if (age > REFRESH_STALE_SECONDS || age < -ROLLBACK_TOLERANCE) return true;
        if (held.expires_at !== null && held.expires_at - now() < RENEW_WINDOW_SECONDS && age > 86400) return true;
        return (held.state === "grace" || held.state === "expired") && age > 86400;
    }

    /** Ask the platform about the certificate now (Check Now): `ok`, `renewed`, `revoked`... */
    async function refresh() {
        const held = await current();
        const certificate = record.certificate;
        const body = { product: PRODUCT, certificate, binding: await binding() };
        if (held.clock_rollback) body.flags = ["clock_rollback"];
        let result;
        try {
            result = await post("/refresh", body, { action: "Licence refresh" });
        } catch (error) {
            result = refreshResult(error);
        }
        return withLock(() => applyRefresh(result));
    }

    /**
     * The background refresh, when it is due: in one tab at a time, silent
     * on every failure (the certificate in hand stands). What happened, for
     * tests: skipped (why) / not_due / busy / failed / ok / renewed / revoked.
     */
    async function heartbeat({ force = false } = {}) {
        return withLock(async (granted) => {
            if (!granted) return "busy";
            // Another tab may have refreshed a moment ago: its record, not ours.
            await load();
            const held = await current();
            const why = refreshable(held);
            if (why) return `skipped:${why}`;
            if (!force && !due(held)) return "not_due";
            await update({ last_attempt: Math.floor(wall()) }, { why: "heartbeat" });
            const body = { product: PRODUCT, certificate: record.certificate, binding: await binding() };
            if (held.clock_rollback) body.flags = ["clock_rollback"];
            let result;
            try {
                result = await post("/refresh", body, { action: "Licence refresh", timeoutMs: BACKGROUND_TIMEOUT_MS });
            } catch (error) {
                try { result = refreshResult(error); } catch { return "failed"; }
            }
            try {
                return await applyRefresh(result, "heartbeat");
            } catch {
                return "failed";
            }
        }, { ifAvailable: true });
    }

    /** Release this browser's device on the seat, then forget the certificate here (keep the secret). */
    async function deactivate() {
        const held = await current();
        if (!record || !record.certificate || held.source !== "cache") {
            throw new PlatformError(`${PRODUCT_NAME} is not activated in this browser.`, { code: "not_activated" });
        }
        const result = await post("/deactivate", { product: PRODUCT, certificate: record.certificate,
                                                    binding: await binding() }, { action: "Deactivation" });
        await withLock(() => commit(null));
        return result;
    }

    /** Forget the certificate here without telling the platform. */
    async function remove() {
        await withLock(() => commit(null));
    }

    /**
     * Forget this browser: release its device on the seat, then clear the
     * certificate, the device secret and the install id. A release that the
     * platform refuses or cannot be asked stops here unless `force`.
     */
    async function forget({ force = false } = {}) {
        let released = null;
        if (record && record.certificate) {
            try {
                released = await post("/deactivate", { product: PRODUCT, certificate: record.certificate,
                                                        binding: await binding() }, { action: "Deactivation" });
            } catch (error) {
                // A device the platform no longer knows is already released.
                const gone = ["environment_unknown", "environment_mismatch", "forged_certificate"].includes(error.code);
                if (!gone && !force) throw error;
            }
        }
        await withLock(async () => {
            await store.clear();
            await commit(null, null, { why: "forget" });
        });
        return released;
    }

    /** Who this browser is signed in as: asked of the platform now, never kept. */
    async function account() {
        const held = await current();
        if (!record || !record.certificate || held.source !== "cache"
            || (held.certificate_type !== null && held.certificate_type !== "env")) {
            throw new PlatformError(`${PRODUCT_NAME} is not connected in this browser.`, { code: "not_activated" });
        }
        return post("/account", { product: PRODUCT, certificate: record.certificate, binding: await binding() },
                    { action: "Account lookup" });
    }

    /**
     * The 30-minute AI token for this browser, from memory until two thirds
     * of its life has passed (biocognia/gateway.py TokenSource). Never stored.
     */
    async function aiToken({ force = false } = {}) {
        if (!force && token && wall() < token.refreshAt) return token.answer;
        const held = await current();
        if (!record || !record.certificate || !held.licensed
            || (held.certificate_type !== null && held.certificate_type !== "env") || held.offline_until !== null) {
            throw new PlatformError(`AI needs ${PRODUCT_NAME} connected online in this browser (Settings > Account).`,
                                    { code: "no_license" });
        }
        const result = await post("/ai/token", { product: PRODUCT, certificate: record.certificate,
                                                  binding: await binding(), app_version: String(version) },
                                  { action: "AI sign-in" });
        if (typeof result.token !== "string") {
            throw new PlatformError("AI sign-in: the platform answered without a token.", { code: "bad_response" });
        }
        const serverTime = number(result.server_time) ?? wall();
        const life = Math.max(60, (number(result.expires_at) ?? serverTime + 1800) - serverTime);
        const answer = {
            token: result.token, expires_at: number(result.expires_at), gateway: result.gateway ?? null,
            mode: result.mode ?? null, mods: Array.isArray(result.mods) ? result.mods : [],
            balance: result.balance && typeof result.balance === "object" ? result.balance : {},
            server_time: serverTime,
        };
        token = { answer, refreshAt: wall() + (life * 2) / 3 };
        return answer;
    }

    /** Python's question `ai_token` (plexora/browser/licence.py): the token, or why not. */
    async function aiTokenForPython(args) {
        try {
            return await aiToken({ force: Boolean(args && args.force) });
        } catch (error) {
            return { error: { code: error.code || "unreachable", message: String(error.message || error),
                              status: error.status ?? null, retry_after: error.retryAfter ?? null } };
        }
    }

    /** A random id for this browser's install, made on first ask (telemetry's, later). */
    async function installId() {
        return withLock(async () => {
            const held = await store.get(INSTALL);
            if (held && typeof held.id === "string") return held.id;
            const fresh = { id: Array.from(random(16), (b) => b.toString(16).padStart(2, "0")).join(""),
                            created_at: Math.floor(wall()) };
            await store.put(INSTALL, fresh);
            return fresh.id;
        });
    }

    // -- the routes (license_routes.py's shapes) ------------------------------------------

    async function statusNow() {
        if (status === null) await toPython();
        return status;
    }

    async function refusal(error, httpStatus = 400, extra = {}) {
        return [{ success: false, error: String(error.message || error), code: error.code || "license_error",
                  license: await statusNow(), ...extra }, httpStatus];
    }

    const ROUTES = {
        "POST /settings/license/connect": async (body) => {
            try {
                const started = await connect({ name: String(body.name || "").trim().slice(0, 80) || null,
                                                trial: Boolean(body.trial) });
                return [{ success: true, ...started }, 200];
            } catch (error) {
                return refusal(error);
            }
        },
        "POST /settings/license/connect/poll": async (body) => {
            const code = String(body.code || "").trim().toUpperCase();
            if (!CODE.test(code)) return [{ success: false, error: "That is not a connection code." }, 400];
            try {
                const answer = await poll(code);
                if (answer.pending) return [{ success: true, pending: true, retry_after: answer.retry_after }, 200];
            } catch (error) {
                return refusal(error, ["expired", "denied"].includes(error.code) ? 410 : 400);
            }
            return [{ success: true, pending: false, license: await statusNow() }, 200];
        },
        "POST /settings/license/activate": async (body) => {
            const credential = String(body.credential || "").trim();
            if (!credential || credential.length > MAX_CREDENTIAL) {
                return [{ success: false, error: "Enter your licence key from account.biocognia.com "
                    + "(BIOC-XXXX-XXXX-XXXX-XXXX) or an activation code." }, 400];
            }
            try {
                await activate(credential, { name: String(body.name || "").trim().slice(0, 80) || null });
            } catch (error) {
                return refusal(error, error.status === 409 ? 409 : 400);
            }
            return [{ success: true, license: await statusNow() }, 200];
        },
        "POST /settings/license/install": async (body) => {
            const text = String(body.certificate || "");
            if (!text.trim() || text.length > MAX_CERTIFICATE_TEXT) {
                return [{ success: false, error: "Paste the licence file's contents." }, 400];
            }
            const found = CERTIFICATE_LINE.exec(text);
            if (!found) return [{ success: false, error: "That does not contain a licence certificate." }, 400];
            try {
                await install(found[0]);
            } catch (error) {
                return refusal(error);
            }
            return [{ success: true, license: await statusNow() }, 200];
        },
        "POST /settings/license/refresh": async () => {
            const held = await current();
            if (!record || !record.certificate || held.source !== "cache") {
                return [{ success: false, error: "There is no activated licence here to check." }, 400];
            }
            try {
                const outcome = await refresh();
                return [{ success: true, outcome, license: await statusNow() }, 200];
            } catch (error) {
                return refusal(error);
            }
        },
        "POST /settings/license/deactivate": async () => {
            const held = await current();
            if (!record || !record.certificate || held.source !== "cache") {
                return [{ success: false, error: "There is no activated licence here to deactivate." }, 400];
            }
            try {
                const result = await deactivate();
                return [{ success: true, next_allowed_at: result.next_allowed_at ?? null,
                          license: await statusNow() }, 200];
            } catch (error) {
                return refusal(error, error.code === "cooldown_active" ? 429 : 400);
            }
        },
        "POST /settings/license/remove": async () => {
            await remove();
            return [{ success: true, license: await statusNow() }, 200];
        },
        "POST /settings/license/forget": async (body) => {
            try {
                await forget({ force: Boolean(body.force) });
            } catch (error) {
                // Not released: the page asks whether to forget it anyway.
                return refusal(error, 409, { release_failed: true });
            }
            return [{ success: true, license: await statusNow() }, 200];
        },
        "GET /settings/license/account": async () => {
            try {
                const answer = await account();
                const org = answer.org && typeof answer.org === "object" ? answer.org : {};
                return [{ success: true, account: {
                    email: String(answer.email || ""), name: String(answer.name || ""),
                    org: { name: String(org.name || ""), kind: String(org.kind || "") } } }, 200];
            } catch (error) {
                const code = error.code || "license_error";
                return [{ success: false, error: String(error.message || error), code },
                        code === "not_activated" ? 409 : 502];
            }
        },
    };

    /** The page's answer to a licence route (backend.js, class "js"), or null. */
    async function answer(request) {
        let path;
        try { path = new URL(request.url, globalThis.location ? globalThis.location.href : "http://x/").pathname; }
        catch { return null; }
        const method = String(request.method || "GET").toUpperCase();
        const route = ROUTES[`${method} ${path}`];
        if (!route) return null;
        let body = {};
        if (method !== "GET" && method !== "HEAD") {
            try {
                const text = await request.text();
                const parsed = text ? JSON.parse(text) : {};
                body = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
            } catch {
                body = {};
            }
        }
        let payload;
        let code;
        try {
            [payload, code] = await route(body);
        } catch (error) {
            [payload, code] = [{ success: false, error: String(error && error.message || error) }, 500];
        }
        return new Response(JSON.stringify(payload), {
            status: code, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
        });
    }

    // -- life ---------------------------------------------------------------------------------

    /** Read the store, hand the worker its files (when there is anything), start the heartbeat. */
    async function restore({ heartbeatEveryMs = HEARTBEAT_CHECK_MS, firstCheckMs = 15000 } = {}) {
        await load();
        if (record || environment) {
            await toPython();
            if (record && record.certificate) {
                // A licence file older than an hour moves the mark (state.py `_load_clock_floor`).
                const hwm = number(record.hwm) || 0;
                if (wall() > hwm + 3600) {
                    await withLock(() => update({ hwm: Math.floor(wall()) }, { why: "clock" })).catch(() => null);
                }
            }
        }
        if (channel) {
            channel.onmessage = async ({ data }) => {
                if (!data || data.kind !== "changed") return;
                await load();
                token = null;
                await toPython();
                emit("changed", { status, state, from: "elsewhere" });
            };
        }
        if (heartbeatEveryMs > 0 && typeof setInterval === "function") {
            const check = () => { heartbeat().catch(() => null); };
            if (firstCheckMs >= 0) setTimeout(check, firstCheckMs);
            timer = setInterval(check, heartbeatEveryMs);
            if (typeof globalThis.addEventListener === "function") globalThis.addEventListener("online", check);
        }
        return state;
    }

    function stop() {
        if (timer) clearInterval(timer);
        timer = null;
        if (channel) channel.onmessage = null;
    }

    function on(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
    }

    return {
        answer, restore, stop, on, load, current, heartbeat, refresh, connect, poll, activate, install,
        deactivate, remove, forget, account, aiToken, aiTokenForPython, installId, files,
        status: () => status,
        state: () => state,
        storeKind: store.kind || "unknown",
        due: (held) => due(held), refreshable: (held) => refreshable(held),
    };
}

/**
 * The page's client: IndexedDB (or memory, where the browser keeps none), the
 * site's fetch, Web Locks, a BroadcastChannel, and the worker's /bioc.
 * `config` is shell.json's `licence` ({api, portal, test_keys}).
 */
export async function startInPage({ config = {}, version = "0", python = null } = {}) {
    let store = indexedStore();
    try {
        await store.get(LICENCE);
    } catch (error) {
        console.warn("[licence] this browser keeps no storage here; a licence lasts this visit only:", error);
        store = memoryStore();
    }
    const client = createLicenseClient({
        store,
        api: config.api || DEFAULT_API,
        portal: config.portal || DEFAULT_PORTAL,
        version,
        keys: config.test_keys || {},
        adopt: (files) => (python && python.licence ? python.licence(files) : Promise.resolve(null)),
        locks: globalThis.navigator && globalThis.navigator.locks ? globalThis.navigator.locks : null,
        channel: typeof BroadcastChannel === "function" ? new BroadcastChannel(CHANNEL) : null,
        label: browserLabel(),
    });
    await client.restore();
    if (python && typeof python.answers === "function") {
        python.answers("ai_token", async (args) => ({ value: await client.aiTokenForPython(args || {}) }));
    }
    return client;
}
