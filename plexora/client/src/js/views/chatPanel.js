/**
 * chatPanel.js -- talking to Plexora AI from the viewer.
 *
 * A launcher chip in the viewer's lower-left corner ("Ask Plexora AI") opens a
 * NON-modal panel over the tissue: the conversation's transcript, a prompt
 * box that takes text and images (attached or pasted), and the controls. It
 * speaks the `/ai/v1/conversations` wire (server/routes/ai_chat_routes.py):
 *
 *   - POST /conversations starts one (with the viewer tools: this tab is the
 *     viewer); a Free licence answers 403 with a `license` block, which opens
 *     the paid-feature modal (PlexoraPaid.explain) instead of the panel;
 *   - POST /<id>/messages sends the user's turn;
 *   - GET /<id>/events streams the turn's events as server-sent events, and
 *     falls back to a held poll (?after=&wait=20) when the stream errors -- a
 *     proxy that buffers it, say -- the agent bridge's idiom;
 *   - POST /<id>/control (Stop), /<id>/approve (Approve / Deny), /<id>/undo
 *     (an Undo chip on a reversible write).
 *
 * What it shows, from the events: the AI disclosure line first ("AI-generated;
 * verify before relying on it"); the user's words with thumbnails of what they
 * attached; the answer as it streams (`text_delta`, then the whole `text`);
 * one chip per tool call (tool, ok or error, "cached" when the tool-result
 * cache answered, Undo when it was a reversible write with an operation id,
 * the images a tool returned); an approval card for a call that writes a
 * source file or cannot be undone; sub-agents' starts and summaries as muted
 * lines; a pause for credit as a notice; and a running credit meter.
 *
 * DOM built with createElement/textContent only: nothing the model says is
 * ever parsed as markup. Classic script, `window.PlexoraChatPanel`.
 */
window.PlexoraChatPanel = (function () {
    "use strict";

    const POLL_WAIT_S = 20;
    const MAX_IMAGES = 4;

    const state = {
        root: null, chip: null, log: null, input: null, send: null, stop: null, meter: null,
        thumbs: null, conversation: null, cursor: 0, source: null, polling: false,
        streamingNode: null, attached: [], busy: false, open: false, credits: 0,
    };

    function url(path) {
        return (typeof plexoraUrl === "function") ? plexoraUrl(path) : "/" + path;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function button(label, className, onClick) {
        const node = el("button", className, label);
        node.type = "button";
        node.addEventListener("click", onClick);
        return node;
    }

    async function post(path, body) {
        const response = await fetch(url(path), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        let payload = {};
        try { payload = await response.json(); } catch (_) { payload = {}; }
        return { status: response.status, body: payload };
    }

    function scroll() {
        if (state.log) state.log.scrollTop = state.log.scrollHeight;
    }

    function line(className, text) {
        const node = el("div", "plx-chat-line " + className, text);
        state.log.appendChild(node);
        scroll();
        return node;
    }

    function credits(micro) {
        return (Number(micro || 0) / 10000).toFixed(1);
    }

    function setBusy(busy) {
        state.busy = busy;
        if (state.send) state.send.disabled = busy;
        if (state.stop) state.stop.hidden = !busy;
        if (state.root) state.root.classList.toggle("is-busy", busy);
    }

    // -- the panel --------------------------------------------------------------------

    function mount() {
        if (state.chip) return;
        const host = document.getElementById("openseadragon_wrapper") || document.body;
        const chip = button("Ask Plexora AI", "plx-chat-chip", () => toggle());
        chip.setAttribute("aria-label", "Ask Plexora AI");
        host.appendChild(chip);
        state.chip = chip;

        const root = el("section", "plx-chat-panel");
        root.hidden = true;
        root.setAttribute("aria-label", "Plexora AI");
        const head = el("header", "plx-chat-head");
        head.appendChild(el("span", "plx-chat-title", "Plexora AI"));
        state.meter = el("span", "plx-chat-meter", "0.0 credits");
        state.meter.title = "Plexora AI credits this conversation has used";
        head.appendChild(state.meter);
        state.stop = button("Stop", "plx-chat-stop", () => control("stop"));
        state.stop.hidden = true;
        head.appendChild(state.stop);
        head.appendChild(button("Close", "plx-chat-close", () => toggle(false)));
        root.appendChild(head);

        state.log = el("div", "plx-chat-log");
        state.log.setAttribute("role", "log");
        state.log.setAttribute("aria-live", "polite");
        root.appendChild(state.log);

        state.thumbs = el("div", "plx-chat-thumbs");
        root.appendChild(state.thumbs);
        const form = el("div", "plx-chat-form");
        state.input = el("textarea", "plx-chat-input");
        state.input.placeholder = "Ask about this project...";
        state.input.rows = 2;
        state.input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
            }
        });
        state.input.addEventListener("paste", (event) => {
            const items = (event.clipboardData && event.clipboardData.files) || [];
            if (items.length) attach(items);
        });
        const picker = el("input", "plx-chat-file");
        picker.type = "file";
        picker.accept = "image/png,image/jpeg,image/webp";
        picker.multiple = true;
        picker.hidden = true;
        picker.addEventListener("change", () => attach(picker.files || []));
        form.appendChild(state.input);
        form.appendChild(button("Image", "plx-chat-attach", () => picker.click()));
        state.send = button("Send", "plx-chat-send", () => submit());
        form.appendChild(state.send);
        form.appendChild(picker);
        root.appendChild(form);
        host.appendChild(root);
        state.root = root;
    }

    async function toggle(open) {
        mount();
        state.open = open === undefined ? !state.open : Boolean(open);
        state.root.hidden = !state.open;
        state.chip.hidden = state.open;
        if (state.open && !state.conversation) {
            const started = await start();
            if (!started) {
                state.open = false;
                state.root.hidden = true;
                state.chip.hidden = false;
            }
        }
        if (state.open && state.input) state.input.focus();
    }

    async function start() {
        const title = (window.flaskVariables && window.flaskVariables.datasource) || "";
        const answer = await post("ai/v1/conversations", { title: String(title), viewer: true });
        if (answer.status === 403 && answer.body.license && window.PlexoraPaid) {
            window.PlexoraPaid.explain(answer.body.license);
            return false;
        }
        if (!answer.body.ok) {
            line("plx-chat-error", (answer.body.error && answer.body.error.message) || answer.body.error ||
                 "Plexora AI is not available.");
            return true;
        }
        state.conversation = answer.body.conversation_id;
        state.cursor = 0;
        listen();
        return true;
    }

    // -- sending ------------------------------------------------------------------------

    function attach(files) {
        for (const file of Array.from(files)) {
            if (state.attached.length >= MAX_IMAGES || !/^image\//.test(file.type || "")) continue;
            const reader = new FileReader();
            reader.onload = () => {
                state.attached.push({ data: String(reader.result), format: (file.type.split("/")[1] || "png") });
                drawThumbs();
            };
            reader.readAsDataURL(file);
        }
    }

    function drawThumbs() {
        state.thumbs.textContent = "";
        state.attached.forEach((image, index) => {
            const img = el("img", "plx-chat-thumb");
            img.src = image.data;
            img.alt = "attached image " + (index + 1);
            img.title = "Click to remove";
            img.addEventListener("click", () => {
                state.attached.splice(index, 1);
                drawThumbs();
            });
            state.thumbs.appendChild(img);
        });
    }

    async function submit() {
        if (state.busy || !state.conversation) return;
        const text = (state.input.value || "").trim();
        const images = state.attached.slice();
        if (!text && !images.length) return;
        state.input.value = "";
        state.attached = [];
        drawThumbs();
        const mine = line("plx-chat-user", text);
        for (const image of images) {
            const img = el("img", "plx-chat-thumb");
            img.src = image.data;
            img.alt = "your image";
            mine.appendChild(img);
        }
        setBusy(true);
        const answer = await post(`ai/v1/conversations/${state.conversation}/messages`, { text, images });
        if (!answer.body.ok) {
            setBusy(false);
            line("plx-chat-error", (answer.body.error && answer.body.error.message) || "The message was not sent.");
        }
    }

    function control(action) {
        if (!state.conversation) return Promise.resolve();
        return post(`ai/v1/conversations/${state.conversation}/control`, { action });
    }

    // -- events -------------------------------------------------------------------------

    function listen() {
        if (typeof EventSource === "function" && !state.polling) {
            const source = new EventSource(url(`ai/v1/conversations/${state.conversation}/events?after=${state.cursor}`));
            source.onmessage = (message) => receive(message);
            source.onerror = () => {
                source.close();
                state.source = null;
                state.polling = true;
                poll();
            };
            // Named events (`event: tool_result`) do not reach onmessage.
            for (const name of KNOWN) source.addEventListener(name, receive);
            state.source = source;
            return;
        }
        state.polling = true;
        poll();
    }

    async function poll() {
        while (state.polling && state.conversation) {
            try {
                const response = await fetch(url(
                    `ai/v1/conversations/${state.conversation}/events?after=${state.cursor}&wait=${POLL_WAIT_S}`));
                const body = await response.json();
                for (const event of body.events || []) handle(event);
                if (!response.ok) await new Promise((resolve) => setTimeout(resolve, 2000));
            } catch (_) {
                await new Promise((resolve) => setTimeout(resolve, 2000));
            }
        }
    }

    function receive(message) {
        let event;
        try { event = JSON.parse(message.data); } catch (_) { return; }
        handle(event);
    }

    const KNOWN = ["disclosure", "user_message", "turn_started", "text_delta", "text", "tool_call", "tool_result",
                   "approval_requested", "approval_decided", "usage", "done", "error", "paused", "stopped",
                   "compacted", "agent_started", "agent_finished", "control", "undone", "turn_finished"];

    function handle(event) {
        if (!event || typeof event.seq !== "number" || event.seq <= state.cursor) return;
        state.cursor = event.seq;
        const sub = event.agent ? "[" + event.agent + "] " : "";
        switch (event.event) {
        case "disclosure":
            line("plx-chat-disclosure", event.text);
            break;
        case "turn_started":
            setBusy(true);
            break;
        case "text_delta":
            if (event.agent) break;
            if (!state.streamingNode) state.streamingNode = line("plx-chat-assistant", "");
            state.streamingNode.textContent += event.text;
            scroll();
            break;
        case "text":
            if (event.agent) {
                line("plx-chat-sub", sub + event.text);
            } else {
                const node = state.streamingNode || line("plx-chat-assistant", "");
                node.textContent = event.text;
                state.streamingNode = null;
            }
            break;
        case "tool_result":
            toolChip(event, sub);
            break;
        case "approval_requested":
            approvalCard(event, sub);
            break;
        case "usage":
            if (typeof event.total_charged_micro === "number") {
                state.credits = event.total_charged_micro;
                state.meter.textContent = credits(state.credits) + " credits";
            }
            break;
        case "paused":
            line("plx-chat-notice", sub + "Paused: " + (event.message || event.reason) +
                 (event.resume ? " (" + event.resume + ")" : ""));
            break;
        case "error":
            line("plx-chat-error", sub + (event.message || event.code));
            break;
        case "stopped":
            line("plx-chat-notice", "Stopped.");
            break;
        case "agent_started":
            line("plx-chat-sub", sub + "started: " + (event.brief || event.role || ""));
            break;
        case "agent_finished":
            line("plx-chat-sub", sub + (event.state === "done" ? "done: " : event.state + ": ") + (event.summary || ""));
            break;
        case "undone":
            line("plx-chat-notice", "Undone: " + event.operation_id);
            break;
        case "turn_finished":
            state.streamingNode = null;
            setBusy(false);
            break;
        default:
            break;
        }
    }

    function toolChip(event, sub) {
        const chip = el("div", "plx-chat-tool" + (event.ok ? "" : " is-error"));
        chip.appendChild(el("span", "plx-chat-tool-name", sub + event.tool));
        const status = event.ok ? (event.source === "cache" ? "ok, cached" : "ok") :
            (event.source === "declined" ? "declined" : "error");
        chip.appendChild(el("span", "plx-chat-tool-status", status));
        if (event.summary && !event.ok) chip.title = event.summary;
        if (event.undo && event.operation_id) {
            const undo = button("Undo", "plx-chat-undo", async () => {
                undo.disabled = true;
                const answer = await post(`ai/v1/conversations/${state.conversation}/undo`,
                                          { operation_id: event.operation_id });
                undo.textContent = answer.body.ok ? "Undone" : "Undo failed";
            });
            undo.title = "Undo " + event.operation_id;
            chip.appendChild(undo);
        }
        for (const image of event.images || []) {
            const img = el("img", "plx-chat-thumb");
            img.src = "data:image/" + (image.format || "png") + ";base64," + image.data;
            img.alt = event.tool + " image";
            chip.appendChild(img);
        }
        state.log.appendChild(chip);
        scroll();
    }

    function approvalCard(event, sub) {
        const card = el("div", "plx-chat-approval");
        const what = event.permission === "destructive" ? "cannot be undone" : "writes into your source file";
        card.appendChild(el("div", "plx-chat-approval-title", sub + event.tool + " " + what));
        if (event.purpose) card.appendChild(el("div", "plx-chat-approval-purpose", event.purpose));
        card.appendChild(el("pre", "plx-chat-approval-args", JSON.stringify(event.arguments || {}, null, 1)));
        const row = el("div", "plx-chat-approval-actions");
        const decide = (decision) => async () => {
            approve.disabled = true;
            deny.disabled = true;
            const answer = await post(`ai/v1/conversations/${state.conversation}/approve`,
                                      { approval_id: event.approval_id, decision });
            row.textContent = answer.body.ok ? (decision === "approve" ? "Approved" : "Declined") :
                ((answer.body.error && answer.body.error.message) || "Not recorded");
        };
        const approve = button("Approve", "plx-chat-approve", decide("approve"));
        const deny = button("Deny", "plx-chat-deny", decide("deny"));
        row.appendChild(approve);
        row.appendChild(deny);
        card.appendChild(row);
        state.log.appendChild(card);
        scroll();
    }

    function boot() {
        if (!document.getElementById("openseadragon_wrapper")) return;
        mount();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }

    return { open: () => toggle(true), close: () => toggle(false), handle, mount, _state: state };
})();
