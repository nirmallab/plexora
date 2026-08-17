/**
 * Does every method that calls a plugin route actually send its request, or
 * does it die on the way there?
 *
 * The bug this exists to catch, in full:
 *
 * `saveGatingList(channels, selections, lassos)` took `lassos` as a parameter.
 * When lasso drawing was removed the parameter went, but the body kept
 * `lassos: lassos` in its request payload. That turned a parameter read into a
 * read of an undeclared global, which throws ReferenceError. Every gate the
 * user set silently failed to persist, the UI showed no error, and the AnnData
 * "save" button reported success while flushing nothing.
 *
 * Nothing caught it: `node --check` only sees syntax, the Python suite never
 * executes client JS, and -- the reason it survived -- each method wraps itself
 * in `try { ... } catch (e) { console.log(...) }`, so the exception never
 * escapes. A probe that merely called these methods and watched for a throw saw
 * nothing wrong. That was tried; it passed with the bug reinstated.
 *
 * So the assertion is not "it did not throw" but "it reached the network".
 * Every method here exists to make one request. If invoking it produces no
 * request, it is broken -- whatever swallowed the reason. That holds for any
 * failure on the path to the call, not just one undeclared name.
 *
 * These methods used to live on core's DataLayer and now live on each plugin's
 * own client. SOURCES lists the files holding them; a companion Python test
 * fails if a file with plugin routes is missing from it.
 *
 * Run directly: `node tests/js/datalayer_globals_probe.mjs`
 * Exit 0 = every method reached the network. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Files that own plugin-route calls. Each must be a bare class declaration so
 *  it can be loaded in isolation. */
const SOURCES = [
  { file: "plexora/plugins/gating/static/gatingApi.js", className: "GatingApi" },
];

const outbound = [];   // every request the code under test managed to start
const swallowed = [];  // what its catch blocks hid, for the failure message

/** The globals a browser genuinely provides. Deliberately no wider than that:
 *  every name added here is a name this probe stops checking. */
function browserGlobals() {
  // A DOM stand-in that is callable as well as indexable, since the code both
  // reads properties off elements and invokes them (form.appendChild(...)).
  const node = () => {
    const known = { submit: () => outbound.push({ kind: "form-submit" }), value: "x", style: {} };
    return new Proxy(function () {}, {
      get: (t, k) => (k in known ? known[k] : k === "then" ? undefined : node()),
      set: (t, k, v) => ((known[k] = v), true),
      apply: () => node(),
    });
  };

  // Errors raised by the vm's own engine belong to the context's realm, so they
  // are NOT `instanceof` this realm's Error. Duck-type instead -- getting that
  // wrong is what made the first version of this probe report an empty reason.
  const looksLikeError = (x) =>
    x && typeof x === "object" && typeof x.message === "string" && "stack" in x;

  return {
    console: {
      ...console,
      // The catch blocks log rather than rethrow. Capture that so a failure can
      // say WHY the request never went out.
      log: (...a) => {
        const err = a.find(looksLikeError);
        if (err) swallowed.push(`${err.name || "Error"}: ${err.message}`);
      },
    },
    URLSearchParams, JSON, Math, Date, Promise, Object, Array, String, Number,
    Boolean, Error, Map, Set, parseInt, parseFloat, isNaN,
    encodeURIComponent, decodeURIComponent, setTimeout, clearTimeout,

    FormData: class FormData { append() {} },
    fetch: async (url) => {
      outbound.push({ kind: "fetch", url: String(url) });
      return { json: async () => ({ success: true, gates: {} }), ok: true, text: async () => "" };
    },
    document: { createElement: () => node(), getElementById: () => node(), querySelector: () => node(), body: node() },
    window: { location: { href: "http://localhost/" } },
    d3: node(),
    _: new Proxy({}, { get: () => () => "" }),
  };
}

/** What a plugin is handed at activation (see main.js). */
const fakeCtx = {
  url: (p) => "http://localhost/" + p,
  datasource: "probe_datasource",
  dataLayer: {},
  dataset: {},
  config: {},
  columns: [],
};

/** One argument shape satisfying every parameter these methods declare: usable
 *  as a dict, a list and a FormData. */
const anyArg = Object.assign([], { append() {} });

const report = [];
let failed = 0;

for (const { file, className } of SOURCES) {
  const ctx = createContext(browserGlobals());
  runInContext(
    readFileSync(join(REPO, file), "utf8") + `\n;globalThis.__cls = ${className};`,
    ctx
  );
  const instance = new ctx.__cls(fakeCtx);

  // Discovered from the prototype, not hardcoded, so a newly added
  // plugin-route method is covered without anyone editing this probe.
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(instance)).filter((name) => {
    const fn = instance[name];
    return typeof fn === "function" && name !== "constructor" && /plugins\//.test(fn.toString());
  });

  const failures = [];
  for (const name of methods) {
    outbound.length = 0;
    swallowed.length = 0;
    try {
      const out = instance[name](...Array.from({ length: instance[name].length }, () => anyArg));
      if (out && typeof out.then === "function") await out;
    } catch (e) {
      swallowed.push(String(e));
    }
    if (outbound.length === 0) {
      failures.push({ method: name, sent_no_request_because: swallowed.slice() });
    }
  }
  failed += failures.length;
  report.push({ file, className, checked: methods.sort(), failures });
}

console.error(JSON.stringify(report, null, 2));
process.exit(failed ? 1 : 0);
