/**
 * Which browsers the website asks to update before starting its Python
 * (client src/js/services/pythonClient.js `unsupported`): Chrome or Edge with
 * no JSPI -- Chrome 131 crashed on macOS and hung on Windows -- and nothing
 * else (Firefox and Safari without JSPI take the website's other path).
 * Prints JSON; tests/test_unsupported_browser.py asserts on it.
 *
 * Run:  node tests/js/unsupported_browser_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
new Function(readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "pythonClient.js"), "utf-8"))();
const { unsupported } = globalThis.PlexoraPython;

const MAC_CHROME_131 = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    + "Chrome/131.0.0.0 Safari/537.36";
const brands = (...pairs) => pairs.map(([brand, version]) => ({ brand, version }));
const cases = {
    macChrome131: { userAgent: MAC_CHROME_131,
                    userAgentData: { brands: brands(["Google Chrome", "131"], ["Chromium", "131"], ["Not_A Brand", "24"]) } },
    chrome131UaOnly: { userAgent: MAC_CHROME_131 },
    edge131: { userAgent: `${MAC_CHROME_131} Edg/131.0.0.0`,
               userAgentData: { brands: brands(["Microsoft Edge", "131"], ["Chromium", "131"]) } },
    firefox: { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:132.0) Gecko/20100101 Firefox/132.0" },
    safari: { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                         + "Version/18.1 Safari/605.1.15" },
    iosChrome: { userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
                            + "(KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1" },
};
const out = { withoutJspi: {}, withJspi: {} };
const had = WebAssembly.Suspending;
delete WebAssembly.Suspending;
for (const [name, nav] of Object.entries(cases)) out.withoutJspi[name] = unsupported(nav);
WebAssembly.Suspending = function Suspending() {};
for (const [name, nav] of Object.entries(cases)) out.withJspi[name] = unsupported(nav);
if (had) WebAssembly.Suspending = had;
else delete WebAssembly.Suspending;
console.log(JSON.stringify(out));
