function passVariablesToFrontend(vars) {
    return vars
}

function plexoraBaseUrl() {
    const base = window.PLEXORA_BASE_URL || "";
    if (!base || base === "/") {
        return "";
    }
    return "/" + String(base).replace(/^\/+|\/+$/g, "");
}

function plexoraUrl(path) {
    const normalizedPath = String(path || "").replace(/^\/+/, "");
    const base = plexoraBaseUrl();
    if (!normalizedPath) {
        return base || "/";
    }
    return (base ? base + "/" : "/") + normalizedPath;
}
