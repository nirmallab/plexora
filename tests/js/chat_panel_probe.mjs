/**
 * Runs the real views/chatPanel.js in a `vm` context against a stand-in
 * viewer page: a small DOM (createElement, appendChild, classList, events),
 * a fetch spy that answers the /ai/v1 routes, no EventSource (so the panel
 * takes its held-poll fallback), a FileReader stub and a PlexoraPaid stub.
 *
 *     node tests/js/chat_panel_probe.mjs
 *
 * Prints one `PASS <name>` / `FAIL <name>` line per check;
 * tests/test_ai_chat_panel.py lists the names.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PANEL = join(REPO, "plexora/client/src/js/views/chatPanel.js");

const failures = [];
function check(name, condition, detail) {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}`);
    if (!condition) {
        failures.push(name);
        if (detail !== undefined) console.log("   ", JSON.stringify(detail));
    }
}
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function makeDom() {
    const byId = new Map();
    function node(tag) {
        const n = {
            tag, children: [], parentNode: null, style: {}, attributes: {}, listeners: {}, hidden: false,
            disabled: false, title: "", type: "", src: "", alt: "", value: "", placeholder: "", rows: 0,
            _text: "", className: "", scrollTop: 0, scrollHeight: 0,
            get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
            set textContent(value) { this._text = String(value); this.children = []; },
            get classList() {
                const self = this;
                const list = () => self.className.split(/\s+/).filter(Boolean);
                const add = (name) => { if (!list().includes(name)) self.className = [...list(), name].join(" "); };
                const remove = (name) => { self.className = list().filter((c) => c !== name).join(" "); };
                return {
                    contains: (name) => list().includes(name), add, remove,
                    toggle: (name, on) => {
                        const want = on === undefined ? !list().includes(name) : Boolean(on);
                        if (want) add(name); else remove(name);
                    },
                };
            },
            appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
            setAttribute(name, value) { this.attributes[name] = String(value); if (name === "id") byId.set(value, this); },
            addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
            dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, ...event }); },
            click() { this.dispatch("click"); },
            focus() {},
        };
        return n;
    }
    const body = node("body");
    const wrapper = node("div");
    wrapper.setAttribute("id", "openseadragon_wrapper");
    body.appendChild(wrapper);
    const document = {
        readyState: "complete", body,
        createElement: (tag) => node(tag),
        getElementById: (id) => byId.get(id) || null,
        addEventListener() {},
    };
    return { document, wrapper };
}

function all(root, predicate, out = []) {
    if (predicate(root)) out.push(root);
    for (const child of root.children) all(child, predicate, out);
    return out;
}
const byClass = (root, name) => all(root, (n) => n.classList.contains(name));

function makeWorld({ license = false, saved = null, gone = false } = {}) {
    const { document, wrapper } = makeDom();
    const posts = [];
    const explained = [];
    let pending = [];
    let release = null;
    const fetch = async (url, init) => {
        const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
        if (init && init.method === "POST") {
            const body = JSON.parse(init.body || "{}");
            posts.push({ url, body });
            if (url.endsWith("ai/v1/conversations")) {
                if (license) return answer(403, { success: false, license: { entitlement: "ai:chat", label: "Plexora AI chat" } });
                return answer(200, { ok: true, conversation_id: "cv_1" });
            }
            return answer(200, { ok: true });
        }
        // The resume check: does the server still have this conversation?
        if (!url.includes("/events")) return gone ? answer(404, { ok: false }) : answer(200, { ok: true });
        // A held poll: answers what is queued, or waits until something is.
        if (!pending.length) await new Promise((resolve) => { release = resolve; });
        const events = pending;
        pending = [];
        return answer(200, { events });
    };
    const store = new Map(saved ? [["plexora.chat.demo", JSON.stringify(saved)]] : []);
    const sessionStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
    };
    const context = {
        window: { flaskVariables: { datasource: "demo" }, sessionStorage,
                  PlexoraPaid: { explain: (detail) => { explained.push(detail); return Promise.resolve(null); } } },
        document, fetch, console, setTimeout, clearTimeout, JSON, Promise, Array, String, Number, Boolean,
        plexoraUrl: (path) => "/" + path,
        FileReader: class { readAsDataURL(file) { this.result = "data:" + file.type + ";base64,AAAA"; setTimeout(() => this.onload(), 0); } },
    };
    context.window.document = document;
    createContext(context);
    runInContext(readFileSync(PANEL, "utf8"), context);
    const panel = context.window.PlexoraChatPanel;
    let seq = 0;
    const push = async (...events) => {
        pending.push(...events.map((e) => ({ seq: ++seq, ...e })));
        if (release) { const r = release; release = null; r(); }
        await tick(5);
    };
    return { panel, wrapper, posts, explained, push, document, store };
}

const source = readFileSync(PANEL, "utf8");

// -- checks ------------------------------------------------------------------------

{
    const w = makeWorld();
    check("boot mounts the panel, hidden, under the viewer wrapper, and no corner chip (the sidebar AI button opens it)",
          byClass(w.wrapper, "plx-chat-chip").length === 0 && byClass(w.wrapper, "plx-chat-panel")[0].hidden === true);

    w.panel.open();
    await tick(5);
    const start = w.posts.find((p) => p.url === "/ai/v1/conversations");
    await w.push({ event: "disclosure", text: "AI-generated; verify before relying on it." });
    const log = byClass(w.wrapper, "plx-chat-log")[0];
    check("opening starts a conversation with the viewer tools, and the server's disclosure event draws no line",
          start && start.body.viewer === true && start.body.title === "demo" && !byClass(w.wrapper, "plx-chat-panel")[0].hidden
          && log.children.length === 0, start);

    const root = byClass(w.wrapper, "plx-chat-panel")[0];
    const draftInput = byClass(w.wrapper, "plx-chat-input")[0];
    draftInput.value = "half-typed";
    draftInput.dispatch("input");
    const drafted = byClass(w.wrapper, "plx-chat-dock")[0].classList.contains("has-draft");
    byClass(w.wrapper, "plx-chat-minimize")[0].click();
    const folded = root.classList.contains("is-minimized") && !root.hidden
        && byClass(root, "plx-chat-pill")[0].children.length === 2;
    byClass(w.wrapper, "plx-chat-restore")[0].click();
    check("Minimize folds the chat into a Restore / Close pill and Restore brings back the draft; Send is glass until there is one",
          drafted && folded && !root.classList.contains("is-minimized") && draftInput.value === "half-typed");
    draftInput.value = "";
    draftInput.dispatch("input");

    const input = byClass(w.wrapper, "plx-chat-input")[0];
    w.panel._state.attached.push({ data: "data:image/png;base64,AAAA", format: "png" });
    input.value = "what is in CD3?";
    byClass(w.wrapper, "plx-chat-send")[0].click();
    await tick(5);
    const sent = w.posts.find((p) => p.url.endsWith("/messages"));
    const userLine = byClass(log, "plx-chat-user")[0];
    check("Send posts the text and the attached images, and the user's line shows their thumbnails",
          sent && sent.url === "/ai/v1/conversations/cv_1/messages" && sent.body.text === "what is in CD3?"
          && sent.body.images.length === 1 && userLine && byClass(userLine, "plx-chat-thumb").length === 1
          && input.value === "", sent);

    await w.push({ event: "turn_started" });
    const stop = byClass(w.wrapper, "plx-chat-stop")[0];
    check("Stop shows while a turn runs and posts {action: stop}", stop.hidden === false);
    stop.click();
    await tick(5);
    check("...the control route got it", w.posts.some((p) => p.url.endsWith("/control") && p.body.action === "stop"));

    await w.push({ event: "text_delta", text: "<b>CD3" }, { event: "text_delta", text: " is bright" });
    const streaming = byClass(log, "plx-chat-assistant");
    check("text deltas stream into one assistant line, as text (never markup)",
          streaming.length === 1 && streaming[0].textContent === "<b>CD3 is bright" && streaming[0].children.length === 0);
    await w.push({ event: "text", text: "CD3 is bright in the tumour." });
    check("the whole text replaces the streamed line",
          byClass(log, "plx-chat-assistant").length === 1 && streaming[0].textContent === "CD3 is bright in the tumour.");

    await w.push({ event: "tool_result", tool: "get_gate", ok: true, source: "cache", undo: false },
                 { event: "tool_result", tool: "set_gate", ok: true, source: "live", undo: true, operation_id: "op_1",
                   images: [{ format: "webp", data: "QUJD" }] });
    const chips = byClass(log, "plx-chat-tool");
    const undo = byClass(chips[1], "plx-chat-undo")[0];
    check("tool chips name the tool and say cached; a reversible write carries Undo and its images",
          chips.length === 2 && chips[0].textContent.includes("ok, cached") && !byClass(chips[0], "plx-chat-undo").length
          && undo && byClass(chips[1], "plx-chat-thumb")[0].src === "data:image/webp;base64,QUJD");
    undo.click();
    await tick(5);
    check("Undo posts the operation id to the conversation's undo route",
          w.posts.some((p) => p.url === "/ai/v1/conversations/cv_1/undo" && p.body.operation_id === "op_1")
          && undo.textContent === "Undone");

    await w.push({ event: "approval_requested", approval_id: "apr_1", tool: "delete_roi", permission: "destructive",
                   purpose: "Delete a region.", arguments: { id: 3 } });
    const card = byClass(log, "plx-chat-approval")[0];
    check("an approval card says what the call is and that it cannot be undone",
          card && card.textContent.includes("delete_roi cannot be undone") && card.textContent.includes('"id": 3'));
    byClass(card, "plx-chat-approve")[0].click();
    await tick(5);
    check("Approve posts {approval_id, decision: approve}",
          w.posts.some((p) => p.url.endsWith("/approve") && p.body.approval_id === "apr_1" && p.body.decision === "approve"));
    await w.push({ event: "approval_requested", approval_id: "apr_2", tool: "write_gates_to_source",
                   permission: "source_file_write", arguments: {} });
    const second = byClass(log, "plx-chat-approval")[1];
    byClass(second, "plx-chat-deny")[0].click();
    await tick(5);
    check("Deny posts decision: deny, and a source-file write says so",
          second.textContent.includes("writes into your source file")
          && w.posts.some((p) => p.body.approval_id === "apr_2" && p.body.decision === "deny"));

    await w.push({ event: "usage", total_charged_micro: 15000 });
    check("the credit meter runs", byClass(w.wrapper, "plx-chat-meter")[0].textContent === "1.5 credits");

    await w.push({ event: "agent_started", agent: "left", brief: "Read the left half." },
                 { event: "paused", reason: "insufficient_credits", message: "out of credit", resume: "send again" },
                 { event: "turn_finished" });
    check("sub-agents and a credit pause read as muted lines, and the turn's end clears busy",
          byClass(log, "plx-chat-sub")[0].textContent === "[left] started: Read the left half."
          && byClass(log, "plx-chat-notice").some((n) => n.textContent.includes("out of credit"))
          && stop.hidden === true);

    const before = log.children.length;
    await w.push({ seq: 1, event: "text", text: "stale" });
    check("an event at or before the cursor is ignored", log.children.length === before);
}

{
    // A reload: this tab had cv_9 open and minimized.
    const w = makeWorld({ saved: { conversation: "cv_9", open: true, minimized: true } });
    await tick(10);
    const root = byClass(w.wrapper, "plx-chat-panel")[0];
    await w.push({ event: "user_message", text: "where is CD3?", images: 1 },
                 { event: "text", text: "In the margin." },
                 { event: "approval_requested", approval_id: "apr_9", tool: "delete_roi", permission: "destructive",
                   arguments: {} },
                 { event: "approval_decided", approval_id: "apr_9", status: "approved" });
    const log = byClass(w.wrapper, "plx-chat-log")[0];
    const card = byClass(log, "plx-chat-approval")[0];
    const started = w.posts.some((p) => p.url === "/ai/v1/conversations");
    check("a reload reopens the same conversation, minimized as it was, and the replay redraws the user's line and a decided approval",
          !started && !root.hidden && root.classList.contains("is-minimized")
          && byClass(log, "plx-chat-user")[0].textContent.startsWith("where is CD3?")
          && byClass(log, "plx-chat-assistant")[0].textContent === "In the margin."
          && byClass(card, "plx-chat-approve").length === 0 && card.textContent.includes("Approved")
          && JSON.parse(w.store.get("plexora.chat.demo")).conversation === "cv_9");

    byClass(w.wrapper, "plx-chat-restore")[0].click();
    const input = byClass(w.wrapper, "plx-chat-input")[0];
    input.value = "and CD8?";
    byClass(w.wrapper, "plx-chat-send")[0].click();
    await tick(5);
    await w.push({ event: "user_message", text: "and CD8?", images: 0 });
    check("a line sent from this page is not drawn twice when its user_message comes back",
          byClass(log, "plx-chat-user").length === 2);
}

{
    const w = makeWorld({ saved: { conversation: "cv_old", open: true }, gone: true });
    await tick(10);
    check("a conversation the server no longer has is not resumed: a new one starts in its place",
          w.posts.some((p) => p.url === "/ai/v1/conversations")
          && JSON.parse(w.store.get("plexora.chat.demo")).conversation === "cv_1");
}

{
    const w = makeWorld({ license: true });
    w.panel.open();
    await tick(5);
    check("on Free the paid-feature modal opens and the panel stays closed",
          w.explained.length === 1 && w.explained[0].entitlement === "ai:chat"
          && byClass(w.wrapper, "plx-chat-panel")[0].hidden === true);
}

check("the panel never parses markup", !/innerHTML|insertAdjacentHTML|outerHTML/.test(source));

if (failures.length) {
    console.log(`${failures.length} failed`);
    process.exit(1);
}
process.exit(0);
