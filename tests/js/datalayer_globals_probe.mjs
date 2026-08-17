/**
 * Does every DataLayer method that calls a plugin route actually send its
 * request, or does it die on the way there?
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
 * executes this file, and -- the reason it survived -- each method wraps itself
 * in `try { ... } catch (e) { console.log(...) }`, so the exception never
 * escapes. A probe that merely called these methods and watched for a throw
 * saw nothing wrong. That was tried; it passed with the bug reinstated.
 *
 * So the assertion is not "it did not throw" but "it reached the network".
 * Every method here exists to make one request. If invoking it produces no
 * request, it is broken -- whatever swallowed the reason. That holds for any
 * failure on the path to the call, not just this one undeclared name.
 *
 * Run directly: `node tests/js/datalayer_globals_probe.mjs`
 * Exit 0 = every method reached the network. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "client", "src", "js", "services", "dataLayer.js");

const outbound = [];   // every request the code under test managed to start
const swallowed = [];  // what its catch blocks hid, for the failure message

/** The globals the page genuinely provides. Deliberately no wider than that:
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
      // The catch blocks log rather than rethrow. Capture that so a failure
      // can say WHY the request never went out.
      log: (...a) => {
        const err = a.find(looksLikeError);
        if (err) swallowed.push(`${err.name || "Error"}: ${err.message}`);
      },
    },
    URLSearchParams, JSON, Math, Date, Promise, Object, Array, String, Number,
    Boolean, Error, Map, Set, parseInt, parseFloat, isNaN,
    encodeURIComponent, decodeURIComponent, setTimeout, clearTimeout,

    plexoraUrl: (p) => "http://localhost/" + p,
    datasource: "probe_datasource",
    FormData: class FormData { append() {} },
    fetch: async (url) => {
      outbound.push({ kind: "fetch", url: String(url) });
      return { json: async () => ({ success: true, gates: {} }), ok: true, text: async () => "" };
    },
    document: { createElement: () => node(), getElementById: () => node(), querySelector: () => node(), body: node() },
    window: { location: { href: "http://localhost/" } },
    d3: node(),
    _: new Proxy({ toString: (v) => String(v) }, { get: (t, k) => (k in t ? t[k] : () => undefined) }),
  };
}

const ctx = createContext(browserGlobals());
runInContext(readFileSync(SOURCE, "utf8") + "\n;globalThis.__DataLayer = DataLayer;", ctx);
const DataLayer = ctx.__DataLayer;

/** Methods that talk to a plugin's HTTP routes -- discovered, not hardcoded, so
 *  a newly added one is covered without anyone editing this list. */
const methods = Object.getOwnPropertyNames(DataLayer.prototype).filter((name) => {
  const fn = DataLayer.prototype[name];
  return typeof fn === "function" && name !== "constructor" && /plugins\//.test(fn.toString());
});

/** A permissive stand-in for instance state: any property read yields a stub. */
const fakeThis = new Proxy({}, { get: (t, k) => (k === "then" ? undefined : () => undefined), set: () => true });

/** One argument shape satisfying every parameter these methods declare: usable
 *  as a dict, a list and a FormData. */
const anyArg = Object.assign([], { append() {} });

const failures = [];
for (const name of methods) {
  const fn = DataLayer.prototype[name];
  outbound.length = 0;
  swallowed.length = 0;
  try {
    const out = fn.apply(fakeThis, Array.from({ length: fn.length }, () => anyArg));
    if (out && typeof out.then === "function") await out;
  } catch (e) {
    swallowed.push(String(e));
  }
  if (outbound.length === 0) {
    failures.push({ method: name, sent_no_request_because: swallowed.slice() });
  }
}

console.error(JSON.stringify({ checked: methods.sort(), failures }, null, 2));
process.exit(failures.length ? 1 : 0);
