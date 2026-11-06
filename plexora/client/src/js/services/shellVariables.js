/**
 * The page's variables on the website, where base.html may not run inline
 * script (the site's Content-Security-Policy): the template writes them as a
 * JSON block (`#plexora-variables`) and this sets what the inline script sets
 * on a server's page -- `window.flaskVariables`, `window.PLEXORA_BASE_URL`
 * and `window.PLEXORA_SERVER_CONCURRENCY` (the worker answers one request at a
 * time, so one).
 */
(function (global) {
    "use strict";
    const block = global.document.getElementById("plexora-variables");
    const data = block ? JSON.parse(block.textContent) : {};
    global.flaskVariables = global.passVariablesToFrontend(data);
    global.PLEXORA_BASE_URL = global.flaskVariables.base_url || "";
    global.PLEXORA_SERVER_CONCURRENCY = 1;
})(window);
