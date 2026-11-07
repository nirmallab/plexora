/**
 * PlexoraPageRelay -- a page of the app answering its own route requests,
 * where the site's service worker does not see them.
 *
 * On the website every request a page makes for a route reaches the site's
 * service worker (workers/backendRelay.js), which hands it to the page's
 * backend (services/backend.js). Where the worker does not see a page's
 * requests -- a browser window without service workers (a private window), or
 * one that bypasses the worker for a frame (Playwright's Firefox, whatever it
 * says of `controller`) -- services/shell.js finds that out with one request
 * the worker answers itself (`/__plexora/relayed`) and installs this instead:
 * `fetch`, `XMLHttpRequest` and an image's `src` are answered by
 * `PlexoraBackend.answer`, as the worker would have had them answered. What
 * the worker would leave alone (the site's files, a "network" route, another
 * origin) goes on as it is.
 *
 * Not covered: a stylesheet's url() and an <img> written into the page's HTML
 * with a route's address (a project's thumbnail) -- the latter is re-pointed
 * once it is in the document.
 *
 *   install(win)   wraps win.fetch, win.XMLHttpRequest and HTMLImageElement src
 *
 * Classic script, `window.PlexoraPageRelay`.
 */
(function (global) {
    "use strict";

    //: The site's own files, whatever a route pattern says (as backendRelay.js).
    const FILES = ["/client/", "/py/", "/tests/"];

    function install(win) {
        if (win.__plexoraPageRelay) return;
        win.__plexoraPageRelay = true;
        const realFetch = win.fetch.bind(win);

        //: Whether the worker would have handed this request to the page.
        function relayed(url, method) {
            if (url.origin !== win.location.origin) return false;
            if (FILES.some((prefix) => url.pathname.startsWith(prefix))) return false;
            const backend = win.PlexoraBackend;
            if (!backend || !backend.classify) return false;
            const row = backend.classify(url.href, method);
            return Boolean(row && row.class !== "network");
        }

        const answer = (request) => win.PlexoraBackend.answer(request);

        win.fetch = function plexoraFetch(input, init) {
            const isRequest = input && typeof input === "object" && typeof input.url === "string";
            const url = new URL(isRequest ? input.url : String(input), win.location.href);
            const method = String((init && init.method) || (isRequest && input.method) || "GET").toUpperCase();
            if (!relayed(url, method)) return realFetch(input, init);
            return answer(new win.Request(isRequest ? input : url.href, init));
        };

        win.XMLHttpRequest = relayXHR(win, relayed, answer);
        relayImages(win, relayed, answer);
    }

    const EVENTS = ["readystatechange", "loadstart", "progress", "abort", "error", "load", "timeout", "loadend"];

    //: XMLHttpRequest, answered by the backend for a route; a real one otherwise.
    function relayXHR(win, relayed, answer) {
        const Real = win.XMLHttpRequest;

        class RelayXHR extends win.EventTarget {
            constructor() {
                super();
                this._real = new Real();
                this._relay = false;
                this._state = 0;
                this._status = 0;
                this._statusText = "";
                this._response = null;
                this._text = "";
                this._type = "";
                this._headers = [];
                this._answered = null;
                this._aborted = false;
                this._upload = new win.EventTarget();
                for (const name of EVENTS) {
                    this[`on${name}`] = null;
                    this._real.addEventListener(name, (event) => {
                        if (!this._relay) this._fire(name, event);
                    });
                }
            }

            _fire(name, from = null) {
                const event = from && "loaded" in from
                    ? new win.ProgressEvent(name, { lengthComputable: from.lengthComputable, loaded: from.loaded, total: from.total })
                    : new win.Event(name);
                const handler = this[`on${name}`];
                if (typeof handler === "function") {
                    try { handler.call(this, event); } catch (error) { win.console.error(error); }
                }
                this.dispatchEvent(event);
            }

            open(method, url, async = true, user, password) {
                this._method = String(method || "GET").toUpperCase();
                this._url = new URL(String(url), win.location.href);
                this._relay = async !== false && relayed(this._url, this._method);
                this._aborted = false;
                this._headers = [];
                if (this._relay) {
                    this._state = 1;
                    this._fire("readystatechange");
                } else {
                    this._real.open(method, url, async, user, password);
                }
            }

            setRequestHeader(name, value) {
                if (this._relay) this._headers.push([name, value]);
                else this._real.setRequestHeader(name, value);
            }

            overrideMimeType(type) {
                if (!this._relay) this._real.overrideMimeType(type);
            }

            send(body = null) {
                if (!this._relay) {
                    this._real.send(body);
                    return;
                }
                const bodyless = this._method === "GET" || this._method === "HEAD";
                const request = new win.Request(this._url.href, {
                    method: this._method, headers: this._headers, body: bodyless ? undefined : body,
                    credentials: this.withCredentials ? "include" : "same-origin" });
                this._fire("loadstart");
                let timer = null;
                if (this.timeout > 0) {
                    timer = win.setTimeout(() => this._fail("timeout"), this.timeout);
                }
                answer(request).then(async (response) => {
                    if (this._aborted || this._state === 4) return;
                    this._status = response.status;
                    this._statusText = response.statusText || "";
                    this._answered = response.headers;
                    this._state = 2;
                    this._fire("readystatechange");
                    const buffer = await response.arrayBuffer();
                    if (this._aborted || this._state === 4) return;
                    this._state = 3;
                    this._fire("readystatechange");
                    this._fire("progress", { lengthComputable: true, loaded: buffer.byteLength, total: buffer.byteLength });
                    this._settle(buffer, response.headers.get("Content-Type") || "");
                    if (timer) win.clearTimeout(timer);
                    this._state = 4;
                    this._fire("readystatechange");
                    this._fire("load", { lengthComputable: true, loaded: buffer.byteLength, total: buffer.byteLength });
                    this._fire("loadend");
                }, () => {
                    if (timer) win.clearTimeout(timer);
                    this._fail("error");
                });
            }

            _settle(buffer, type) {
                const text = () => new win.TextDecoder().decode(buffer);
                switch (this._type) {
                case "arraybuffer": this._response = buffer; break;
                case "blob": this._response = new win.Blob([buffer], { type }); break;
                case "json":
                    try { this._response = JSON.parse(text()); } catch (error) { this._response = null; }
                    break;
                case "document":
                    this._response = new win.DOMParser().parseFromString(text(), /xml/.test(type) ? "application/xml" : "text/html");
                    break;
                default:
                    this._text = text();
                    this._response = this._text;
                }
            }

            _fail(kind) {
                if (this._state === 4 || this._aborted) return;
                this._status = 0;
                this._state = 4;
                this._fire("readystatechange");
                this._fire(kind);
                this._fire("loadend");
            }

            abort() {
                if (!this._relay) {
                    this._real.abort();
                    return;
                }
                if (this._state === 0 || this._state === 4) return;
                this._aborted = true;
                this._status = 0;
                this._state = 4;
                this._fire("readystatechange");
                this._fire("abort");
                this._fire("loadend");
                this._state = 0;
            }

            getResponseHeader(name) {
                if (!this._relay) return this._real.getResponseHeader(name);
                return this._answered ? this._answered.get(name) : null;
            }

            getAllResponseHeaders() {
                if (!this._relay) return this._real.getAllResponseHeaders();
                if (!this._answered) return "";
                let out = "";
                this._answered.forEach((value, name) => { out += `${name}: ${value}\r\n`; });
                return out;
            }

            get readyState() { return this._relay ? this._state : this._real.readyState; }
            get status() { return this._relay ? this._status : this._real.status; }
            get statusText() { return this._relay ? this._statusText : this._real.statusText; }
            get response() { return this._relay ? this._response : this._real.response; }
            get responseText() {
                if (!this._relay) return this._real.responseText;
                if (this._type !== "" && this._type !== "text") {
                    throw new win.DOMException("responseText needs responseType '' or 'text'", "InvalidStateError");
                }
                return this._text;
            }
            get responseXML() { return this._relay ? (this._type === "document" ? this._response : null) : this._real.responseXML; }
            get responseURL() { return this._relay ? (this._state === 4 ? this._url.href : "") : this._real.responseURL; }
            get responseType() { return this._relay ? this._type : this._real.responseType; }
            set responseType(value) {
                this._type = value;
                try { this._real.responseType = value; } catch (error) { /* a sync request's: kept here */ }
            }
            get timeout() { return this._real.timeout; }
            set timeout(value) { this._real.timeout = value; }
            get withCredentials() { return this._real.withCredentials; }
            set withCredentials(value) { this._real.withCredentials = value; }
            get upload() { return this._relay ? this._upload : this._real.upload; }
        }
        Object.assign(RelayXHR, { UNSENT: 0, OPENED: 1, HEADERS_RECEIVED: 2, LOADING: 3, DONE: 4 });
        return RelayXHR;
    }

    //: An image whose address is a route: its bytes answered by the backend,
    //: shown through a blob: URL (the property, and an <img> put in the page).
    function relayImages(win, relayed, answer) {
        const proto = win.HTMLImageElement.prototype;
        const described = Object.getOwnPropertyDescriptor(proto, "src");
        if (!described || !described.set) return;
        //: The address each image was last pointed from (a failed one is set
        //: back as it was, and not answered again).
        const pointed = new WeakMap();
        const point = (image, value) => {
            const url = new URL(String(value), win.location.href);
            if (!relayed(url, "GET")) return false;
            if (pointed.get(image) === url.href && image.getAttribute("src") === String(value)) return true;
            pointed.set(image, url.href);
            answer(new win.Request(url.href)).then((response) => response.blob()).then((blob) => {
                described.set.call(image, win.URL.createObjectURL(blob));
            }, () => described.set.call(image, value));
            return true;
        };
        Object.defineProperty(proto, "src", {
            configurable: true,
            enumerable: described.enumerable,
            get() { return described.get.call(this); },
            set(value) { if (!point(this, value)) described.set.call(this, value); },
        });
        const visit = (node) => {
            if (node.nodeType !== 1) return;
            const images = node.tagName === "IMG" ? [node] : node.querySelectorAll ? node.querySelectorAll("img[src]") : [];
            for (const image of images) {
                const value = image.getAttribute("src");
                if (value && !value.startsWith("blob:") && !value.startsWith("data:")) point(image, value);
            }
        };
        new win.MutationObserver((records) => {
            for (const record of records) {
                if (record.type === "attributes") visit(record.target);
                else record.addedNodes.forEach(visit);
            }
        }).observe(win.document, { subtree: true, childList: true, attributes: true, attributeFilter: ["src"] });
    }

    global.PlexoraPageRelay = Object.freeze({ install });
})(window);
